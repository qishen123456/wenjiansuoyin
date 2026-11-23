"""Feishu enterprise app. Bind imported open_ids explicitly to local accounts."""
import json
import os
import secrets
import time
import threading
import uuid
from collections import deque
from datetime import date
from typing import Literal
from urllib.parse import urlencode, quote
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
import directory_access
import feishu_identity

BASE = 'https://open.feishu.cn/open-apis'
_sync_lock = threading.Lock()


def app_credentials_configured():
    return all(os.getenv(k, '').strip() for k in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET'))


def configured():
    return app_credentials_configured() and bool(os.getenv('FEISHU_REDIRECT_URI', '').strip())


class FeishuAPIError(HTTPException):
    def __init__(self, code):
        super().__init__(502, '飞书拒绝了请求，请检查应用权限和授权范围。')
        self.code = code


def call(method, path, *, token=None, **kwargs):
    try:
        response = httpx.request(method, BASE + path, headers={'Authorization': 'Bearer ' + token} if token else {},
                                 timeout=12, **kwargs)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, '飞书接口暂不可用，请检查应用配置和网络。')
    if not isinstance(data, dict):
        raise HTTPException(502, '飞书接口返回了无效响应。')
    if response.is_error or data.get('code', 0) != 0 or data.get('error'):
        raise FeishuAPIError(data.get('code'))
    return data


def tenant_token():
    if not app_credentials_configured():
        raise HTTPException(503, '请先配置飞书企业应用。')
    data = call('POST', '/auth/v3/tenant_access_token/internal', json={
        'app_id': os.environ['FEISHU_APP_ID'], 'app_secret': os.environ['FEISHU_APP_SECRET']})
    if not data.get('tenant_access_token'):
        raise HTTPException(502, '未能取得企业应用访问凭据。')
    return data['tenant_access_token']


def page_data(path, token, params):
    cursor = ''
    visited = set()
    while True:
        query = {**params, 'page_size': 50}
        if cursor:
            query['page_token'] = cursor
        result = call('GET', path, token=token, params=query)['data']
        yield result
        if not result.get('has_more'):
            break
        cursor = result.get('page_token')
        if not cursor or cursor in visited:
            raise HTTPException(502, '飞书通讯录分页中断。')
        visited.add(cursor)


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS feishu_members(open_id TEXT PRIMARY KEY, name TEXT NOT NULL,
        department TEXT NOT NULL, active INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS feishu_departments(open_id TEXT PRIMARY KEY, name TEXT NOT NULL, parent TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS feishu_login_states(nonce TEXT PRIMARY KEY, expires REAL NOT NULL,
        report_date TEXT NOT NULL DEFAULT '', report_view TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS feishu_notices(event_key TEXT PRIMARY KEY, recipient TEXT NOT NULL, body TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0);
    """)

    columns = {row[1] for row in conn.execute('PRAGMA table_info(feishu_login_states)')}
    if 'report_date' not in columns:
        conn.execute("ALTER TABLE feishu_login_states ADD COLUMN report_date TEXT NOT NULL DEFAULT ''")
    if 'report_view' not in columns:
        conn.execute("ALTER TABLE feishu_login_states ADD COLUMN report_view TEXT NOT NULL DEFAULT ''")


def clean_report_date(value):
    try:
        parsed = date.fromisoformat(value).isoformat()
    except ValueError:
        return ''
    return parsed if parsed == value else ''


def pages(path, token, params):
    for page in page_data(path, token, params):
        yield from page.get('items', [])


def _walk_departments(token, departments, queue):
    members = {}
    while queue:
        dept_id, label = queue.popleft()
        path = '/contact/v3/departments/' + quote(dept_id, safe='') + '/children'
        for dept in pages(path, token, {'department_id_type': 'open_department_id'}):
            key = dept['open_department_id']
            if key not in departments and key != '0':
                name = dept.get('name') or f'飞书部门（{key[-6:]}）'
                full = f'{label} / {name}' if label else name
                departments[key] = (key, full, dept_id)
                queue.append((key, full))
        for member in pages('/contact/v3/users/find_by_department', token,
                            {'department_id': dept_id, 'department_id_type': 'open_department_id', 'user_id_type': 'open_id'}):
            key = member['open_id']
            profile = feishu_identity.member_profile(member, key, dept_id)
            previous = members.get(key)
            if previous:
                profile.departments.update(previous.departments)
                profile.active = profile.active and previous.active
                if profile.phone and previous.phone and profile.phone != previous.phone:
                    raise HTTPException(502, '飞书返回了不一致的手机号，请重新同步。')
                profile.phone = profile.phone or previous.phone
                if not profile.job_key:
                    profile.job_key, profile.job_title = previous.job_key, previous.job_title
            members[key] = profile
    return members


def _authorized_directory(token):
    department_ids, user_ids, group_ids = set(), set(), set()
    id_options = {'user_id_type': 'open_id', 'department_id_type': 'open_department_id'}
    for page in page_data('/contact/v3/scopes', token, id_options):
        department_ids.update(page.get('department_ids') or [])
        user_ids.update(page.get('user_ids') or [])
        group_ids.update(page.get('group_ids') or [])
    if '0' in department_ids:
        raise HTTPException(502, '飞书拒绝了根部门访问，请检查通讯录数据权限是否已发布。')
    if group_ids:
        raise HTTPException(502, '请将通讯录数据权限设置为指定成员或部门，再同步。当前授权包含用户组。')

    departments = {}
    queue = deque()
    for key in sorted(department_ids):
        data = call('GET', '/contact/v3/departments/' + quote(key, safe=''), token=token,
                    params={'department_id_type': 'open_department_id'})['data']['department']
        label = data.get('name') or f'飞书部门（{key[-6:]}）'
        departments[key] = (key, label, data.get('parent_department_id') or '0')
        queue.append((key, label))
    members = _walk_departments(token, departments, queue)
    for key in sorted(user_ids - members.keys()):
        member = call('GET', '/contact/v3/users/' + quote(key, safe=''), token=token,
                      params=id_options)['data']['user']
        if member.get('open_id') not in (None, key):
            raise HTTPException(502, '飞书返回的成员标识与授权范围不一致。')
        members[key] = feishu_identity.member_profile(member, key)
    return departments, members


def directory_snapshot(conn, *, include_stored=False):
    enabled = app_credentials_configured()
    members = []
    last_sync = 0.0
    coverage = ''
    departments = 0
    if enabled or include_stored:
        members = [dict(row) for row in conn.execute("""SELECT m.*, p.phone_tail, p.job_title, p.local_user_id,
            p.match_status, u.id AS bound_user_id, u.feishu_binding_source AS binding_source
            FROM feishu_members m LEFT JOIN feishu_member_profiles p ON p.open_id=m.open_id
            LEFT JOIN system_users u ON u.feishu_id=m.open_id AND u.status='active'
            ORDER BY m.department,m.name,m.open_id""")]
        stamp = conn.execute("SELECT value FROM app_meta WHERE key='feishu_last_sync'").fetchone()
        last_sync = float(stamp[0]) if stamp else 0.0
        scope = conn.execute("SELECT value FROM app_meta WHERE key='feishu_sync_scope'").fetchone()
        coverage = scope[0] if scope else ''
        departments = conn.execute('SELECT count(*) FROM feishu_departments').fetchone()[0]
    return {
        'source': 'feishu' if enabled else 'local',
        'last_sync': last_sync,
        'coverage': coverage,
        'members': members,
        'summary': {
            'total': len(members),
            'active': sum(bool(m['active']) for m in members),
            'matched': sum(m['match_status'] == 'matched' and m['bound_user_id'] is not None for m in members),
            'departments': departments,
        },
    }


def linked_identities(conn, *, demo, user_id=None):
    """Read presentation fields for active, eligible Feishu associations."""
    if not app_credentials_configured():
        return {}
    query = """SELECT u.id, f.name, f.department, coalesce(p.job_title,'') AS job_title
        FROM system_users u JOIN feishu_members f ON u.feishu_id=f.open_id AND f.active=1
        LEFT JOIN feishu_member_profiles p ON p.open_id=f.open_id
        WHERE u.status='active' AND (
            (p.local_user_id=u.id AND p.match_status='matched')
            OR (? AND u.feishu_binding_source IN ('','manual'))
        )"""
    params = [demo]
    if user_id is not None:
        query += ' AND u.id=?'
        params.append(user_id)
    return {row['id']: dict(row) for row in conn.execute(query, params)}


def _department_display(member, departments):
    names = [departments[key][1] for key in sorted(member.departments & departments.keys())]
    if names:
        return '、'.join(names)
    if '0' in member.departments:
        return '根组织'
    return ''


def sync_directory(app):
    if not _sync_lock.acquire(blocking=False):
        raise HTTPException(409, '通讯录正在同步，请稍后查看结果。')
    try:
        return _sync_directory(app)
    finally:
        _sync_lock.release()


def _sync_directory(app):
    token = tenant_token()
    departments = {}
    coverage = 'root'
    try:
        members = _walk_departments(token, departments, deque([('0', '')]))
    except FeishuAPIError as exc:
        if exc.code != 40004:
            raise
        departments, members = _authorized_directory(token)
        coverage = 'specified'
    with app.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        matched = feishu_identity.reconcile(conn, members, departments, demo=app.DEMO_MODE)
        conn.execute('DELETE FROM feishu_members')
        conn.execute('DELETE FROM feishu_departments')
        conn.executemany('INSERT INTO feishu_members VALUES (?,?,?,?)', (
            (m.open_id, m.name, _department_display(m, departments), int(m.active))
            for m in members.values()
        ))
        conn.executemany('INSERT INTO feishu_departments VALUES (?,?,?)', departments.values())
        conn.execute("""DELETE FROM login_sessions WHERE user_id IN (
            SELECT id FROM system_users WHERE feishu_id<>'' AND feishu_id NOT IN
            (SELECT open_id FROM feishu_members WHERE active=1))""")
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES('feishu_last_sync',?)",(str(time.time()),))
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES('feishu_sync_scope',?)", (coverage,))
    return {'users': len(members), 'departments': len(departments), 'matched': matched}


def deliver_notices(app):
    if os.getenv('FEISHU_NOTIFY_ENABLED','false').lower() != 'true' or not configured():
        return
    with app.db_session() as conn:
        users = conn.execute("""SELECT u.* FROM system_users u JOIN feishu_members f ON u.feishu_id=f.open_id
            LEFT JOIN feishu_member_profiles p ON p.open_id=f.open_id
            WHERE u.status='active' AND f.active=1
              AND ((p.local_user_id=u.id AND p.match_status='matched')
                   OR (? AND u.feishu_binding_source IN ('','manual')))""", (app.DEMO_MODE,)).fetchall()
        # Scope the initial delivery window to the current activation. Do not notify historical demo records.
        conn.execute("INSERT OR IGNORE INTO app_meta VALUES('feishu_notify_since',?)",(str(time.time()),))
        since = float(conn.execute("SELECT value FROM app_meta WHERE key='feishu_notify_since'").fetchone()[0])
        requests = conn.execute("SELECT * FROM download_requests WHERE demo_key='' AND (created_at>=? OR reviewed_at>=?)",(since,since)).fetchall()
        tasks = conn.execute("SELECT * FROM archive_tasks WHERE status='pending' AND created_at>=?",(since,)).fetchall()
        eligible = set()
        for row in users:
            user = dict(row)
            policy = directory_access.load_policy(conn,user,app.root_scope_key(),app._catalog_root_path())
            recipient = user['feishu_id']
            events = []
            for req in requests:
                parent = os.path.dirname(req['file_path'])
                if not policy.visible(parent):
                    continue
                if req['status']=='pending' and user['role'] in app._ADMIN_ROLES:
                    events.append((f"download:{req['id']}:pending", f"下载申请 #{req['id']} 待审核，请进入文件资产中心处理。"))
                elif req['status'] in ('approved','rejected') and req['user_id']==user['id']:
                    events.append((f"download:{req['id']}:{req['status']}",f"下载申请 #{req['id']} 已处理，请在文件资产中心查看结果。"))
            for task in tasks:
                if user['name']==task['responsible_person'] and policy.complete(task['target_path']):
                    events.append((f"archive:{task['id']}",f"归档任务 #{task['id']} 逾期待处理，请在文件资产中心查看要求。"))
            for key,body in events:
                eligible.add(recipient + ':' + key)
                conn.execute('INSERT OR IGNORE INTO feishu_notices(event_key,recipient,body) VALUES(?,?,?)',(recipient+':'+key,recipient,body))
        pending = conn.execute('SELECT * FROM feishu_notices WHERE sent=0 LIMIT 30').fetchall()
    if pending:
        token = tenant_token()
        for notice in pending:
            if notice['event_key'] not in eligible:
                with app.db_session() as conn:
                    conn.execute('UPDATE feishu_notices SET sent=-1 WHERE event_key=?',(notice['event_key'],))
                continue
            call('POST','/im/v1/messages',token=token,params={'receive_id_type':'open_id'},json={
                'receive_id':notice['recipient'],'msg_type':'text','content':json.dumps({'text':notice['body']},ensure_ascii=False),
                'uuid':str(uuid.uuid5(uuid.NAMESPACE_URL,notice['event_key']))})
            with app.db_session() as conn:
                conn.execute('UPDATE feishu_notices SET sent=1 WHERE event_key=?',(notice['event_key'],))


class Binding(BaseModel):
    user_id: int
    open_id: str


class DailyRecipient(BaseModel):
    type: Literal['open_id', 'chat_id', 'off']
    id: str = Field(default='', max_length=128)


def register(app):
    @app.app.get('/integrations/feishu/status')
    def public_status():
        return {'configured':configured()}

    @app.app.get('/integrations/feishu/login')
    def begin_login(report_date: str = "", report_view: str = ""):
        if not configured():
            raise HTTPException(503,'飞书登录未配置。')
        nonce = secrets.token_urlsafe(32)
        with app.db_session() as conn:
            conn.execute('DELETE FROM feishu_login_states WHERE expires<?',(time.time(),))
            view = report_view if report_view in {'audit', 'exceptions'} else ''
            conn.execute('INSERT INTO feishu_login_states(nonce,expires,report_date,report_view) VALUES(?,?,?,?)',
                         (nonce, time.time()+300, clean_report_date(report_date), view))
        redirect = os.environ['FEISHU_REDIRECT_URI']
        response = RedirectResponse('https://accounts.feishu.cn/open-apis/authen/v1/authorize?'+urlencode({
            'client_id':os.environ['FEISHU_APP_ID'],'redirect_uri':redirect,'response_type':'code','state':nonce}))
        response.set_cookie('ff_feishu_state',nonce,max_age=300,httponly=True,samesite='lax',secure=redirect.startswith('https:'))
        return response

    @app.app.get('/integrations/feishu/callback')
    def callback(request: Request,code: str,state: str):
        cookie=request.cookies.get('ff_feishu_state','')
        if not configured() or not cookie or not secrets.compare_digest(cookie,state):
            raise HTTPException(403,'登录请求无效，请重新发起。')
        with app.db_session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            login_state = conn.execute('SELECT report_date,report_view FROM feishu_login_states WHERE nonce=? AND expires>?',
                                       (state, time.time())).fetchone()
            if login_state is None:
                raise HTTPException(403, '登录请求已使用或过期。')
            conn.execute('DELETE FROM feishu_login_states WHERE nonce=?', (state,))
        data=call('POST','/authen/v2/oauth/token',json={'grant_type':'authorization_code','code':code,
            'client_id':os.environ['FEISHU_APP_ID'],'client_secret':os.environ['FEISHU_APP_SECRET'],
            'redirect_uri':os.environ['FEISHU_REDIRECT_URI']})
        user_token = data.get('access_token')
        if not user_token:
            raise HTTPException(502, '飞书登录未返回用户凭据，请重新登录。')
        identity = (call('GET', '/authen/v1/user_info', token=user_token).get('data') or {}).get('open_id')
        if not identity:
            raise HTTPException(502, '无法读取飞书身份，请检查应用授权。')
        with app.db_session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("""SELECT u.id FROM system_users u JOIN feishu_members m ON u.feishu_id=m.open_id
                LEFT JOIN feishu_member_profiles p ON p.open_id=m.open_id
                WHERE m.open_id=? AND m.active=1 AND u.status='active'
                  AND ((p.local_user_id=u.id AND p.match_status='matched')
                       OR (? AND u.feishu_binding_source IN ('','manual')))""", (identity, app.DEMO_MODE)).fetchone()
            if not row:
                denied = HTMLResponse(
                    '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>请核对平台账号</title><main style="font:16px system-ui;max-width:480px;margin:15vh auto;padding:24px">'
                    '<h1>飞书身份尚未匹配</h1><p>请联系平台管理员同步通讯录，并核对平台账号的手机号。'
                    '管理员账号匹配后还需要确认关联。</p><a href="/">返回平台</a></main></html>',
                    status_code=403, headers={'Cache-Control': 'no-store'})
                denied.delete_cookie('ff_feishu_state')
                return denied
            session=secrets.token_urlsafe(48)
            ttl=int(app.SESSION_TTL_HOURS*3600)
            conn.execute("INSERT INTO login_sessions(token,user_id,created_at,expires_at,auth_source) VALUES(?,?,?,?,'feishu')",
                         (session, row[0], time.time(), time.time()+ttl))
        report_date = clean_report_date(login_state['report_date'])
        destination = '/'
        if report_date:
            if login_state['report_view'] == 'exceptions':
                destination = '/reports/daily/abnormal.csv?' + urlencode({'date':report_date})
            else:
                tab = 'report-audit' if login_state['report_view'] == 'audit' else 'updates'
                destination = '/?' + urlencode({'tab':tab,'date':report_date})
        response = RedirectResponse(destination)
        response.delete_cookie('ff_feishu_state')
        response.set_cookie('ff_session',session,max_age=ttl,httponly=True,samesite='lax',secure=os.environ['FEISHU_REDIRECT_URI'].startswith('https:'))
        return response

    @app.app.get('/api/system/feishu')
    def status(request: Request):
        app._require_super_admin(request)
        with app.db_session() as conn:
            snapshot = directory_snapshot(conn, include_stored=True)
        return {**snapshot, 'configured': configured(), 'demo_mode': app.DEMO_MODE,
                'daily': app.feishu_daily.status(app)}

    @app.app.put('/api/system/feishu/daily/recipient')
    def update_daily_recipient(body: DailyRecipient, request: Request):
        actor = app._require_super_admin(request)
        return app.feishu_daily.set_recipient(app, actor['id'], body.type, body.id)

    @app.app.post('/api/system/feishu/sync')
    def sync(request: Request):
        app._require_super_admin(request)
        return sync_directory(app)

    @app.app.post('/api/system/feishu/bind')
    def bind(body: Binding,request: Request):
        app._require_super_admin(request)
        with app.db_session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            target = conn.execute('SELECT id,status FROM system_users WHERE id=?', (body.user_id,)).fetchone()
            if not target:
                raise HTTPException(404, '本地成员不存在。')
            if not body.open_id:
                feishu_identity.clear_user_link(conn, body.user_id, block=True)
                return {'ok': True}
            if target['status'] != 'active':
                raise HTTPException(422, '请选择已启用的平台账号。')
            member = conn.execute("""SELECT m.active, p.local_user_id, p.match_status
                FROM feishu_members m LEFT JOIN feishu_member_profiles p ON p.open_id=m.open_id
                WHERE m.open_id=?""", (body.open_id,)).fetchone()
            if not member or not member['active']:
                raise HTTPException(422, '请选择已同步的在职成员。')
            if conn.execute('SELECT 1 FROM system_users WHERE feishu_id=? AND id<>?', (body.open_id, body.user_id)).fetchone():
                raise HTTPException(409, '该身份已绑定其他账号。')
            verified = member['local_user_id'] == body.user_id and member['match_status'] in ('review', 'matched')
            if not verified:
                raise HTTPException(422, '手机号匹配通过后才能确认绑定，请检查账号资料并重新同步。')
            conn.execute("UPDATE system_users SET feishu_id=?, feishu_binding_source='manual' WHERE id=?",
                         (body.open_id, body.user_id))
            conn.execute("UPDATE feishu_member_profiles SET match_status='matched' WHERE open_id=?", (body.open_id,))
            conn.execute('DELETE FROM login_sessions WHERE user_id=?', (body.user_id,))
        return {'ok': True}
