"""Feishu enterprise app. Bind imported open_ids explicitly to local accounts."""
import json
import os
import secrets
import time
import uuid
from collections import deque
from urllib.parse import urlencode, quote
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import directory_access

BASE = 'https://open.feishu.cn/open-apis'


def configured():
    return all(os.getenv(k) for k in ('FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_REDIRECT_URI'))


def call(method, path, *, token=None, **kwargs):
    try:
        response = httpx.request(method, BASE + path, headers={'Authorization': 'Bearer ' + token} if token else {},
                                 timeout=12, **kwargs)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, '飞书接口暂不可用，请检查应用配置和网络。')
    if data.get('code', 0) != 0 or data.get('error'):
        raise HTTPException(502, '飞书拒绝了请求，请检查应用权限和授权范围。')
    return data


def tenant_token():
    if not configured():
        raise HTTPException(503, '请先配置飞书企业应用。')
    data = call('POST', '/auth/v3/tenant_access_token/internal', json={
        'app_id': os.environ['FEISHU_APP_ID'], 'app_secret': os.environ['FEISHU_APP_SECRET']})
    if not data.get('tenant_access_token'):
        raise HTTPException(502, '未能取得企业应用访问凭据。')
    return data['tenant_access_token']


def pages(path, token, params):
    cursor = ''
    visited = set()
    while True:
        query = {**params, 'page_size': 50}
        if cursor:
            query['page_token'] = cursor
        result = call('GET', path, token=token, params=query)['data']
        yield from result.get('items', [])
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
    CREATE TABLE IF NOT EXISTS feishu_login_states(nonce TEXT PRIMARY KEY, expires REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS feishu_notices(event_key TEXT PRIMARY KEY, recipient TEXT NOT NULL, body TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0);
    """)


def sync_directory(app):
    token = tenant_token()
    queue = deque([('0', '')])
    departments = {}
    members = {}
    while queue:
        dept_id, label = queue.popleft()
        for dept in pages('/contact/v3/departments/' + quote(dept_id,safe='') + '/children', token,
                          {'department_id_type':'open_department_id'}):
            key = dept['open_department_id']
            if key not in departments and key != '0':
                full = (label + ' / ' if label else '') + dept['name']
                departments[key] = (key, full, dept_id)
                queue.append((key, full))
        for member in pages('/contact/v3/users/find_by_department', token,
                            {'department_id':dept_id,'department_id_type':'open_department_id','user_id_type':'open_id'}):
            status = member.get('status', {})
            key = member['open_id']
            active = not any(status.get(flag) for flag in ('is_resigned','is_frozen','is_exited','is_unjoin'))
            members[key] = (key, member['name'], label, int(active))
    with app.db_session() as conn:
        conn.execute('DELETE FROM feishu_members')
        conn.execute('DELETE FROM feishu_departments')
        conn.executemany('INSERT INTO feishu_members VALUES (?,?,?,?)', members.values())
        conn.executemany('INSERT INTO feishu_departments VALUES (?,?,?)', departments.values())
        conn.execute("""DELETE FROM login_sessions WHERE user_id IN (
            SELECT id FROM system_users WHERE feishu_id<>'' AND feishu_id NOT IN
            (SELECT open_id FROM feishu_members WHERE active=1))""")
        conn.execute("INSERT OR REPLACE INTO app_meta VALUES('feishu_last_sync',?)",(str(time.time()),))
    return {'users':len(members),'departments':len(departments)}


def deliver_notices(app):
    if os.getenv('FEISHU_NOTIFY_ENABLED','false').lower() != 'true' or not configured():
        return
    with app.db_session() as conn:
        users = conn.execute("""SELECT u.* FROM system_users u JOIN feishu_members f ON u.feishu_id=f.open_id
            WHERE u.status='active' AND f.active=1""").fetchall()
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


def register(app):
    @app.app.get('/integrations/feishu/status')
    def public_status():
        return {'configured':configured()}

    @app.app.get('/integrations/feishu/login')
    def begin_login():
        if not configured():
            raise HTTPException(503,'飞书登录未配置。')
        nonce = secrets.token_urlsafe(32)
        with app.db_session() as conn:
            conn.execute('DELETE FROM feishu_login_states WHERE expires<?',(time.time(),))
            conn.execute('INSERT INTO feishu_login_states VALUES(?,?)',(nonce,time.time()+300))
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
            if not conn.execute('DELETE FROM feishu_login_states WHERE nonce=? AND expires>?',(state,time.time())).rowcount:
                raise HTTPException(403,'登录请求已使用或过期。')
        data=call('POST','/authen/v2/oauth/token',json={'grant_type':'authorization_code','code':code,
            'client_id':os.environ['FEISHU_APP_ID'],'client_secret':os.environ['FEISHU_APP_SECRET'],
            'redirect_uri':os.environ['FEISHU_REDIRECT_URI']})
        identity=call('GET','/authen/v1/user_info',token=data['access_token'])['data']['open_id']
        with app.db_session() as conn:
            row=conn.execute("""SELECT u.id FROM system_users u JOIN feishu_members m ON u.feishu_id=m.open_id
                WHERE m.open_id=? AND m.active=1 AND u.status='active'""",(identity,)).fetchone()
            if not row:
                raise HTTPException(403,'此飞书身份尚未绑定已启用的平台账号。')
            session=secrets.token_urlsafe(48)
            ttl=int(app.SESSION_TTL_HOURS*3600)
            conn.execute('INSERT INTO login_sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)',(session,row[0],time.time(),time.time()+ttl))
        response=RedirectResponse('/')
        response.delete_cookie('ff_feishu_state')
        response.set_cookie('ff_session',session,max_age=ttl,httponly=True,samesite='lax',secure=os.environ['FEISHU_REDIRECT_URI'].startswith('https:'))
        return response

    @app.app.get('/api/system/feishu')
    def status(request: Request):
        app._require_super_admin(request)
        with app.db_session() as conn:
            rows=conn.execute('SELECT * FROM feishu_members ORDER BY department,name').fetchall()
            stamp=conn.execute("SELECT value FROM app_meta WHERE key='feishu_last_sync'").fetchone()
        return {'configured':configured(),'members':[dict(r) for r in rows],'last_sync':float(stamp[0]) if stamp else 0}

    @app.app.post('/api/system/feishu/sync')
    def sync(request: Request):
        app._require_super_admin(request)
        return sync_directory(app)

    @app.app.post('/api/system/feishu/bind')
    def bind(body: Binding,request: Request):
        app._require_super_admin(request)
        with app.db_session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if body.open_id:
                if not conn.execute('SELECT 1 FROM feishu_members WHERE open_id=? AND active=1',(body.open_id,)).fetchone():
                    raise HTTPException(422,'请选择已同步的在职成员。')
                if conn.execute('SELECT 1 FROM system_users WHERE feishu_id=? AND id<>?',(body.open_id,body.user_id)).fetchone():
                    raise HTTPException(409,'该身份已绑定其他账号。')
            if not conn.execute('UPDATE system_users SET feishu_id=? WHERE id=?',(body.open_id,body.user_id)).rowcount:
                raise HTTPException(404,'本地成员不存在。')
            conn.execute('DELETE FROM login_sessions WHERE user_id=?',(body.user_id,))
        return {'ok':True}
