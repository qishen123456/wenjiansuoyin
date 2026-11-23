"""Publish daily index additions and a summary to configured Feishu resources."""
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
import hashlib
import json
import logging
import os
import re
import time
from threading import Lock
from types import SimpleNamespace
from urllib.parse import quote, urlencode, urlsplit
import uuid

from fastapi import HTTPException

from demo_data import ZONE
import feishu_integration as feishu
import feishu_report_bitable
import report_daily

logger = logging.getLogger(__name__)
SHEETS = {'.xlsx', '.xls', '.xlsm', '.xlsb', '.csv', '.tsv', '.ods'}
RULE_ERRORS = {'invalid_path', 'no_files', 'no_content', 'calendar_unavailable'}
SCAN_PHASE = {'complete': '已完成', 'partial': '部分完成', 'error': '失败'}
BATCH_SIZE = 100
MAX_BATCHES_PER_TICK = 5
_RECIPIENT_PREFIXES = {'open_id': 'ou_', 'chat_id': 'oc_'}
_recipient_lock = Lock()
_DEFAULT_TASK = 'default'
_DEFAULT_NAME = '飞书同步日报'
_TIME_PATTERN = r'(?:[01][0-9]|2[0-3]):[0-5][0-9]'


def enabled():
    return os.getenv('FEISHU_DAILY_ENABLED', 'false').strip().lower() == 'true'


@dataclass(frozen=True)
class Settings:
    active: bool
    report_time: str
    chat: str
    open_id: str
    app_token: str
    table: str
    table_url: str
    system_url: str
    report_base: str
    report_table: str
    report_url: str
    error: str
    recipient_source: str
    task_enabled: bool
    revision: float

    @property
    def recipient(self):
        return self.open_id or self.chat

    @property
    def recipient_type(self):
        return 'open_id' if self.open_id else 'chat_id'

    @property
    def table_key(self):
        if not (self.app_token and self.table):
            return ''
        return hashlib.sha256(f'{os.getenv("FEISHU_APP_ID", "")}:{self.app_token}:{self.table}'.encode()).hexdigest()


def _application_id():
    return os.getenv('FEISHU_APP_ID', '').strip()


def _stored_recipient(conn, root_key, task_id=_DEFAULT_TASK):
    return conn.execute("""SELECT * FROM feishu_daily_recipients
        WHERE root_key=? AND app_id=? AND task_id=?""", (root_key, _application_id(), task_id)).fetchone()


def _task_ids(conn, root_key):
    rows = conn.execute("""SELECT task_id, recipient_type FROM feishu_daily_recipients
        WHERE root_key=? AND app_id=? ORDER BY rowid""", (root_key, _application_id())).fetchall()
    ids = [row['task_id'] for row in rows if row['recipient_type'] != 'off']
    if not any(row['task_id'] == _DEFAULT_TASK for row in rows):
        if os.getenv('FEISHU_DAILY_CHAT_ID', '').strip() or os.getenv('FEISHU_DAILY_OPEN_ID', '').strip():
            ids.insert(0, _DEFAULT_TASK)
    return ids


def _member(conn, open_id):
    return conn.execute('SELECT name, active FROM feishu_members WHERE open_id=?', (open_id,)).fetchone()


def settings(conn=None, root_key='', task_id=_DEFAULT_TASK):
    chat = os.getenv('FEISHU_DAILY_CHAT_ID', '').strip()
    open_id = os.getenv('FEISHU_DAILY_OPEN_ID', '').strip()
    if task_id != _DEFAULT_TASK:
        chat = open_id = ''
    saved = _stored_recipient(conn, root_key, task_id) if conn is not None and root_key else None
    if saved:
        chat = saved['recipient_id'] if saved['recipient_type'] == 'chat_id' else ''
        open_id = saved['recipient_id'] if saved['recipient_type'] == 'open_id' else ''
    token = os.getenv('FEISHU_DAILY_BTABLE_APP_TOKEN', '').strip()
    table = os.getenv('FEISHU_DAILY_BTABLE_TABLE_ID', '').strip()
    url = os.getenv('FEISHU_DAILY_BTABLE_URL', '').strip()
    report_time = os.getenv('FEISHU_DAILY_TIME', '18:00').strip()
    task_enabled = enabled() and task_id == _DEFAULT_TASK
    other_active = False
    if task_id == _DEFAULT_TASK and conn is not None and root_key:
        other_active = bool(conn.execute("""SELECT 1 FROM feishu_daily_recipients
            WHERE root_key=? AND app_id=? AND task_id!=? AND task_enabled=1 AND recipient_type!='off' LIMIT 1""",
            (root_key, _application_id(), _DEFAULT_TASK)).fetchone())
    if saved:
        report_time = saved['schedule'] or report_time
        if saved['task_enabled'] is not None:
            task_enabled = bool(saved['task_enabled'])
    report_base = os.getenv('FEISHU_REPORT_BTABLE_APP_TOKEN', '').strip()
    report_table = os.getenv('FEISHU_REPORT_BTABLE_TABLE_ID', '').strip()
    report_url = os.getenv('FEISHU_REPORT_BTABLE_URL', '').strip()
    error = ''
    if chat and open_id:
        error = '请在接收群和个人接收账号中选择一个。'
    elif open_id and not re.fullmatch(r'ou_[A-Za-z0-9_-]+', open_id):
        error = '个人接收账号应填写该应用的飞书 open_id。'
    elif chat and not re.fullmatch(r'oc_[A-Za-z0-9_-]+', chat):
        error = '请填写指定的飞书群 ID。'
    elif bool(token) != bool(table):
        error = '请同时配置多维表格的应用标识和数据表标识。'
    elif not (saved or chat or open_id or table or report_table or other_active):
        error = '请配置日报接收人、接收群或新增文件数据表。'
    elif not feishu.app_credentials_configured():
        error = '请先配置飞书企业应用的 ID 和密钥。'
    elif not re.fullmatch(_TIME_PATTERN, report_time):
        error = '日报发送时间应使用 HH:MM 格式。'
    if any((report_base, report_table, report_url)) and not all((report_base, report_table, report_url)):
        error = '请同时配置报表明细多维表格的应用标识、数据表标识和链接。'
    for table_link in (url, report_url):
        if not table_link:
            continue
        try:
            parsed = urlsplit(table_link)
            host = parsed.hostname or ''
            safe = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and (host == 'feishu.cn' or host.endswith('.feishu.cn')))
        except ValueError:
            safe = False
        if not safe:
            error = '多维表格链接应使用飞书的 HTTPS 地址。'
    system_url = os.getenv('FEISHU_DAILY_SYSTEM_URL', '').strip()
    if not system_url:
        system_url = os.getenv('FEISHU_REDIRECT_URI', '').strip().removesuffix('/integrations/feishu/callback')
    if system_url:
        try:
            link = urlsplit(system_url)
            if (link.scheme not in ('http', 'https') or not link.hostname or link.username or link.password
                    or link.query or link.fragment):
                raise ValueError
        except ValueError:
            error = '更新明细需配置系统的 HTTP 或 HTTPS 入口地址。'
    return Settings(enabled() or task_enabled or other_active, report_time, chat, open_id, token, table, url, system_url,
                    report_base, report_table, report_url, error, 'system' if saved else 'environment',
                    task_enabled, saved['updated_at'] if saved else 0.0)


def _report_error_key(root_key, task_id):
    job = 'report' if task_id == _DEFAULT_TASK else f'report:{task_id}'
    return f'feishu_daily_error:{job}:{root_key}'


def _write_recipient(conn, root_key, user_id, task_id, name, kind, recipient_id, schedule, task_enabled, current):
    if kind == 'off':
        if recipient_id:
            raise HTTPException(422, '暂停推送时无需指定接收对象。')
    elif kind not in _RECIPIENT_PREFIXES or not re.fullmatch(_RECIPIENT_PREFIXES[kind] + r'[A-Za-z0-9_-]+', recipient_id):
        raise HTTPException(422, '请选择飞书成员或填写指定飞书群的 ID。')
    if schedule is not None and not re.fullmatch(_TIME_PATTERN, schedule):
        raise HTTPException(422, '日报发送时间应使用 HH:MM 格式。')
    if kind == 'open_id' and not (task_enabled is False and recipient_id == current.recipient):
        member = _member(conn, recipient_id)
        legacy_owner = (task_id == _DEFAULT_TASK and member is None
                        and recipient_id == os.getenv('FEISHU_DAILY_OPEN_ID', '').strip())
        if not ((member and member['active']) or legacy_owner):
            raise HTTPException(422, '请选择已同步的在职飞书成员。')
    changed_at = max(time.time(), current.revision + 0.000001)
    conn.execute("""INSERT INTO feishu_daily_recipients
        (root_key,app_id,task_id,name,recipient_type,recipient_id,updated_at,updated_by,schedule,task_enabled)
        VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(root_key,app_id,task_id) DO UPDATE SET
        name=excluded.name, recipient_type=excluded.recipient_type, recipient_id=excluded.recipient_id,
        updated_at=excluded.updated_at, updated_by=excluded.updated_by,
        schedule=coalesce(excluded.schedule, feishu_daily_recipients.schedule),
        task_enabled=coalesce(excluded.task_enabled, feishu_daily_recipients.task_enabled)""",
        (root_key, _application_id(), task_id, name, kind, recipient_id, changed_at, user_id, schedule, task_enabled))
    conn.execute('DELETE FROM app_meta WHERE key=?', (_report_error_key(root_key, task_id),))


def _check_revision(current, revision):
    if revision is not None and current.revision != revision:
        raise HTTPException(409, '任务配置已更新，请刷新页面后重试。')


def _writable_root(app):
    root_key = app.root_scope_key()
    if not root_key or not feishu.app_credentials_configured():
        raise HTTPException(422, '请先配置文件目录和飞书应用。')
    return root_key


def set_recipient(app, user_id, kind, recipient_id, *, expected_id=None, schedule=None, task_enabled=None, revision=None):
    root_key = _writable_root(app)
    with _recipient_lock, app.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = settings(conn, root_key)
        _check_revision(current, revision)
        if expected_id is not None and current.recipient != expected_id:
            raise HTTPException(409, '接收人已被修改，请刷新页面后重试。')
        saved = _stored_recipient(conn, root_key)
        _write_recipient(conn, root_key, user_id, _DEFAULT_TASK, saved['name'] if saved else _DEFAULT_NAME,
                         kind, recipient_id.strip(), schedule, task_enabled, current)
    return status(app)


def save_task(app, actor_id, *, task_id=None, name, recipient_type, recipient_id, schedule, enabled, revision=None):
    root_key = _writable_root(app)
    name = name.strip()
    if not 1 <= len(name) <= 80 or recipient_type not in _RECIPIENT_PREFIXES:
        raise HTTPException(422, '请填写任务名称（最多 80 字）并选择接收方式。')
    if not schedule or not re.fullmatch(_TIME_PATTERN, schedule) or not isinstance(enabled, bool):
        raise HTTPException(422, '请选择启用状态并填写有效的发送时间。')
    with _recipient_lock, app.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if task_id is not None and task_id not in _task_ids(conn, root_key):
            raise HTTPException(404, '推送任务不存在。')
        task_id = task_id or str(uuid.uuid4())
        current = settings(conn, root_key, task_id)
        _check_revision(current, revision)
        _write_recipient(conn, root_key, actor_id, task_id, name, recipient_type, recipient_id.strip(), schedule, enabled, current)
        return _task_detail(conn, root_key, task_id, datetime.now(ZONE))


def delete_task(app, task_id, revision):
    root_key = app.root_scope_key()
    with _recipient_lock, app.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if task_id not in _task_ids(conn, root_key):
            raise HTTPException(404, '推送任务不存在。')
        current = settings(conn, root_key, task_id)
        _check_revision(current, revision)
        if task_id == _DEFAULT_TASK:
            # An explicit tombstone suppresses the environment destination on restart.
            old = _stored_recipient(conn, root_key)
            _write_recipient(conn, root_key, old['updated_by'] if old else 0, task_id, _DEFAULT_NAME,
                             'off', '', None, False, current)
        else:
            conn.execute('DELETE FROM feishu_daily_recipients WHERE root_key=? AND app_id=? AND task_id=?',
                         (root_key, _application_id(), task_id))
            conn.execute('DELETE FROM app_meta WHERE key=?', (_report_error_key(root_key, task_id),))
    return {'ok': True}


def _migrate_daily_tasks(conn):
    # Copy the original singleton and its frozen send UUIDs in one transaction.
    # A delivered day continues to belong to that task even if its recipient changes.
    conn.execute('SAVEPOINT daily_tasks_schema')
    try:
        rc = {row[1] for row in conn.execute('PRAGMA table_info(feishu_daily_recipients)')}
        old_recipients = bool(rc and 'task_id' not in rc)
        if old_recipients:
            conn.execute('ALTER TABLE feishu_daily_recipients RENAME TO feishu_recipients_legacy')
        conn.execute("""CREATE TABLE IF NOT EXISTS feishu_daily_recipients (
            root_key TEXT NOT NULL, app_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL DEFAULT '飞书同步日报',
            recipient_type TEXT NOT NULL CHECK (recipient_type IN ('open_id','chat_id','off')),
            recipient_id TEXT NOT NULL, updated_at REAL NOT NULL, updated_by INTEGER NOT NULL,
            schedule TEXT, task_enabled INTEGER, PRIMARY KEY(root_key,app_id,task_id))""")
        if old_recipients:
            schedule = 'schedule' if 'schedule' in rc else 'NULL'
            active = 'task_enabled' if 'task_enabled' in rc else 'NULL'
            conn.execute(f"""INSERT INTO feishu_daily_recipients
                SELECT root_key,app_id,?,?,recipient_type,recipient_id,updated_at,updated_by,{schedule},{active}
                FROM feishu_recipients_legacy""", (_DEFAULT_TASK, _DEFAULT_NAME))
            conn.execute('DROP TABLE feishu_recipients_legacy')

        cols = {row[1] for row in conn.execute('PRAGMA table_info(feishu_daily_reports)')}
        old_reports = bool(cols and 'task_id' not in cols)
        if old_reports:
            conn.execute('ALTER TABLE feishu_daily_reports RENAME TO feishu_reports_legacy')
        conn.execute("""CREATE TABLE IF NOT EXISTS feishu_daily_reports (
            root_key TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT 'default', day TEXT NOT NULL, chat TEXT NOT NULL,
            send_key TEXT NOT NULL, body TEXT NOT NULL, prepared_at REAL NOT NULL, sent_at REAL,
            PRIMARY KEY(root_key,task_id,day))""")
        if old_reports:
            conn.execute("""INSERT INTO feishu_daily_reports
                SELECT root_key,?,day,chat,send_key,body,prepared_at,sent_at FROM feishu_reports_legacy""",
                (_DEFAULT_TASK,))
            conn.execute('DROP TABLE feishu_reports_legacy')
    except Exception:
        conn.execute('ROLLBACK TO daily_tasks_schema')
        raise
    finally:
        conn.execute('RELEASE daily_tasks_schema')


def initialize(conn):
    _migrate_daily_tasks(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS feishu_daily_files (
        id INTEGER PRIMARY KEY, root_key TEXT NOT NULL, day TEXT NOT NULL, item_key TEXT NOT NULL,
        name TEXT NOT NULL, ext TEXT NOT NULL, directory TEXT NOT NULL, added_at REAL NOT NULL,
        table_key TEXT NOT NULL, batch_id TEXT, published_at REAL,
        UNIQUE(root_key, day, item_key)
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_feishu_daily_files_pending
        ON feishu_daily_files(root_key, table_key, published_at, id)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS feishu_daily_checks (
        root_key TEXT PRIMARY KEY, checked_at REAL NOT NULL, checked_rules INTEGER NOT NULL,
        overdue_rules INTEGER NOT NULL, rule_errors INTEGER NOT NULL
    )""")


def record_file(conn, *, root_key, relative, name, ext, added_at, config):
    if not root_key or not relative:
        return
    day = datetime.fromtimestamp(added_at, ZONE).date().isoformat()
    key = hashlib.sha256(f'{root_key}\0{day}\0{relative}'.encode('utf-8')).hexdigest()
    conn.execute("""INSERT OR IGNORE INTO feishu_daily_files
        (root_key,day,item_key,name,ext,directory,added_at,table_key) VALUES (?,?,?,?,?,?,?,?)""",
        (root_key, day, key, name, ext, relative.rpartition('/')[0], added_at, config.table_key))


def record_check(conn, root_key, now, checked_rules, overdue_rules, issues):
    if not root_key:
        return
    conn.execute("""INSERT INTO feishu_daily_checks VALUES (?,?,?,?,?)
        ON CONFLICT(root_key) DO UPDATE SET checked_at=excluded.checked_at,
        checked_rules=excluded.checked_rules, overdue_rules=excluded.overdue_rules,
        rule_errors=excluded.rule_errors WHERE excluded.checked_at>=feishu_daily_checks.checked_at""",
        (root_key, now, checked_rules, overdue_rules, sum(issue['state'] in RULE_ERRORS for issue in issues)))


def record_check_failure(conn, root_key, now):
    conn.execute("INSERT OR REPLACE INTO app_meta VALUES (?,?)", (f'feishu_daily_check_failed:{root_key}', str(now)))


def _table_batch(conn, root_key, table_key):
    """Persist the membership and idempotency token before handing the batch to Feishu."""
    conn.execute('BEGIN IMMEDIATE')
    pending = conn.execute("""SELECT batch_id FROM feishu_daily_files
        WHERE root_key=? AND table_key=? AND published_at IS NULL AND batch_id IS NOT NULL
        ORDER BY id LIMIT 1""", (root_key, table_key)).fetchone()
    if pending:
        batch_id = pending['batch_id']
        return batch_id, conn.execute("""SELECT * FROM feishu_daily_files
            WHERE root_key=? AND table_key=? AND batch_id=? ORDER BY id""",
            (root_key, table_key, batch_id)).fetchall()
    rows = conn.execute("""SELECT * FROM feishu_daily_files
        WHERE root_key=? AND table_key=? AND published_at IS NULL AND batch_id IS NULL
        ORDER BY id LIMIT ?""", (root_key, table_key, BATCH_SIZE)).fetchall()
    if not rows:
        return '', []
    batch_id = str(uuid.uuid4())
    conn.executemany('UPDATE feishu_daily_files SET batch_id=? WHERE id=?', [(batch_id, row['id']) for row in rows])
    return batch_id, rows


def _file_fields(row):
    return {'日期': row['day'], '文件名': row['name'], '文件格式': row['ext'].lstrip('.').upper(),
            '相对目录': row['directory'] or '根目录',
            '首次入库时间': datetime.fromtimestamp(row['added_at'], ZONE).strftime('%Y-%m-%d %H:%M:%S'),
            '条目标识': row['item_key']}


def _publish_files(app, config, root_key, get_token):
    endpoint = f'/bitable/v1/apps/{quote(config.app_token, safe="")}/tables/{quote(config.table, safe="")}/records/batch_create'
    for _ in range(MAX_BATCHES_PER_TICK):
        with app.db_session() as conn:
            batch_id, rows = _table_batch(conn, root_key, config.table_key)
        if not rows:
            return
        feishu.call('POST', endpoint, token=get_token(), params={'client_token': batch_id},
                    json={'records': [{'fields': _file_fields(row)} for row in rows]})
        with app.db_session() as conn:
            conn.execute('UPDATE feishu_daily_files SET published_at=? WHERE batch_id=?', (time.time(), batch_id))


def _summary(app, conn, root_key, today, config):
    rows = conn.execute("""SELECT ext, count(*) AS total FROM feishu_daily_files
        WHERE root_key=? AND day=? GROUP BY ext""", (root_key, today.isoformat())).fetchall()
    files = sum(row['total'] for row in rows)
    sheets = sum(row['total'] for row in rows if row['ext'] in SHEETS)
    # Scan history stores naive timestamps in the service's timezone. Convert
    # the Shanghai day boundaries to that timezone before asking SQLite.
    start = datetime.combine(today, wall_time.min, ZONE).timestamp()
    end = datetime.combine(today + timedelta(days=1), wall_time.min, ZONE).timestamp()
    updated = conn.execute(
        'SELECT count(*) FROM files WHERE root_key=? AND mtime>=? AND mtime<?', (root_key, start, end)
    ).fetchone()[0]
    start_local = datetime.fromtimestamp(start).isoformat(timespec='seconds')
    end_local = datetime.fromtimestamp(end).isoformat(timespec='seconds')
    latest = conn.execute("""SELECT phase, finished_at, counts_json FROM index_scan_runs
        WHERE root_key=? AND finished_at>=? AND finished_at<?
        ORDER BY finished_at DESC, id DESC LIMIT 1""", (root_key, start_local, end_local)).fetchone()
    running = app._scan_state_snapshot()
    if running['running'] and running.get('root_key') == root_key:
        progress = f'正在扫描，已检查 {running["seen"]} 个文件'
        counts = running
    elif latest:
        counts = json.loads(latest['counts_json'])
        clock = datetime.fromisoformat(latest['finished_at']).astimezone(ZONE).strftime('%H:%M')
        progress = f'{SCAN_PHASE.get(latest["phase"], "状态未知")}（{clock}）'
    else:
        counts = None
        progress = '今日暂无扫描记录'
    lines = [f'文件同步日预警 · {today:%Y-%m-%d}（北京时间）', f'今日更新：{updated} 份文件',
             f'今日新入索引：{files} 份文件（其中表格 {sheets} 份）', f'索引扫描：{progress}']
    attention = bool(latest and latest['phase'] in ('partial', 'error'))
    if counts is not None:
        attention |= bool(counts.get('errors') or counts.get('directory_errors'))
        if latest and not running['running']:
            lines.append(f'本次扫描检查 {counts.get("seen", 0)} 个文件，更新 {counts.get("updated", 0)} 份已有文件')
        lines.append(f'本次扫描访问异常：{counts.get("errors", 0)} 个文件 / {counts.get("directory_errors", 0)} 个目录')

    check = conn.execute('SELECT * FROM feishu_daily_checks WHERE root_key=? AND checked_at>=? AND checked_at<?',
                         (root_key, start, end)).fetchone()
    failed = conn.execute('SELECT value FROM app_meta WHERE key=?', (f'feishu_daily_check_failed:{root_key}',)).fetchone()
    failure_time = float(failed[0]) if failed else 0
    if start <= failure_time < end and (not check or failure_time > check['checked_at']):
        attention = True
        lines.append('归档检测：最近一次执行失败，请到系统查看')
    elif check:
        attention |= bool(check['overdue_rules'] or check['rule_errors'])
        lines.append(f'归档检测：已检查 {check["checked_rules"]} 条规则，'
                     f'逾期未交 {check["overdue_rules"]} 条，无法核验 {check["rule_errors"]} 条')
    else:
        lines.append('归档检测：今日暂无检测记录')

    status_rows, _ = report_daily.load(conn, root_key, today.isoformat())
    if status_rows:
        tally = report_daily.counts(status_rows)
        lines.append(f'监控报表：{tally["normal"]} 条正常，{tally["abnormal"]} 条异常，{tally["pending"]} 条待检测')
        attention |= bool(tally['abnormal'])
        if not config.report_url:
            lines.append('多维表明细：待配置')
    if config.report_url:
        failed_export = conn.execute("SELECT 1 FROM app_meta WHERE key=?",
                                     (f'feishu_daily_error:report_table:{root_key}',)).fetchone()
        if failed_export:
            lines.append('多维表同步：等待自动重试')
    if config.table_key:
        outstanding = conn.execute("""SELECT count(*) FROM feishu_daily_files
            WHERE root_key=? AND day=? AND table_key=? AND published_at IS NULL""",
            (root_key, today.isoformat(), config.table_key)).fetchone()[0]
        lines.append(f'新增清单：{outstanding} 条待同步，稍后自动补写' if outstanding else '新增清单：可在多维表按日期查看')
        if config.table_url:
            lines.append(config.table_url)
    return '\n'.join(lines), attention


def _report_card(body, attention, day, config):
    title, *lines = body.splitlines()
    content = []
    for line in lines:
        if line == config.table_url:
            continue
        label, separator, value = line.partition('：')
        content.append(f'**{label}**{separator}{value}' if separator else line)
    elements = [{'tag': 'markdown', 'content': '\n'.join(content)}]
    destinations = []
    if config.system_url:
        system = config.system_url.rstrip('/')
        url = system + '/?' + urlencode({'tab': 'report-audit', 'date': day})
        destinations.append(('查看系统明细', url, 'primary'))
    if config.report_url:
        destinations.append(('打开飞书多维表', config.report_url, 'default'))
    elif config.table_url:
        destinations.append(('查看飞书多维表 · 新增清单', config.table_url, 'default'))
    if config.system_url:
        download = system + '/reports/daily/abnormal.csv?' + urlencode({'date': day})
        destinations.append(('下载异常清单', download, 'default'))
    for label, url, style in destinations:
        elements.append({'tag': 'button', 'text': {'tag': 'plain_text', 'content': label}, 'type': style,
                         'behaviors': [{'type': 'open_url', 'default_url': url}]})
    return {'schema': '2.0', 'header': {'title': {'tag': 'plain_text', 'content': '文件同步日预警'},
                                      'subtitle': {'tag': 'plain_text', 'content': title.split(' · ', 1)[-1]},
                                      'template': 'orange' if attention else 'blue'},
            'body': {'elements': elements}}


def _send_report(app, root_key, now, get_token, *, task_id=_DEFAULT_TASK, immediate=False):
    with _recipient_lock:
        day = now.date().isoformat()
        with app.db_session() as conn:
            conn.execute('BEGIN IMMEDIATE')
            config = settings(conn, root_key, task_id)
            if not config.task_enabled or config.error or not config.recipient:
                return 'disabled'
            if not immediate and now.strftime('%H:%M') < config.report_time:
                return
            report = conn.execute('SELECT * FROM feishu_daily_reports WHERE root_key=? AND task_id=? AND day=?', (root_key, task_id, day)).fetchone()
            if report and report['sent_at']:
                return 'already_sent'
            if config.open_id:
                member = _member(conn, config.open_id)
                if member is not None and not member['active']:
                    raise HTTPException(422, '日报接收人已停用，请重新选择飞书成员。')
                if member is None and (task_id != _DEFAULT_TASK or config.open_id != os.getenv('FEISHU_DAILY_OPEN_ID', '').strip()):
                    raise HTTPException(422, '日报接收人已离开通讯录范围，请重新选择飞书成员。')
            if not report:
                report_daily.freeze(conn, root_key, day)
                summary, attention = _summary(app, conn, root_key, now.date(), config)
                body = json.dumps(_report_card(summary, attention, day, config), ensure_ascii=False)
                conn.execute('INSERT INTO feishu_daily_reports VALUES (?,?,?,?,?,?,?,NULL)',
                             (root_key, task_id, day, config.recipient, str(uuid.uuid4()), body, now.timestamp()))
                report = conn.execute('SELECT * FROM feishu_daily_reports WHERE root_key=? AND task_id=? AND day=?', (root_key, task_id, day)).fetchone()
        if report['chat'] != config.recipient:
            raise HTTPException(409, '今日已有待确认的日报，接收对象配置已变化。')
        # Feishu guarantees UUID deduplication for one hour.
        if now.timestamp() - report['prepared_at'] >= 55 * 60:
            raise HTTPException(502, '今日推送未确认，请先检查飞书会话。')
        body = report['body']
        interactive = body.startswith('{')  # Existing rows store a plain text message.
        try:
            feishu.call('POST', '/im/v1/messages', token=get_token(), params={'receive_id_type': config.recipient_type},
                        json={'receive_id': config.recipient, 'msg_type': 'interactive' if interactive else 'text',
                              'uuid': report['send_key'],
                              'content': body if interactive else json.dumps({'text': body}, ensure_ascii=False)})
        except feishu.FeishuAPIError as exc:
            if str(exc.code) == '230002' and config.chat:
                # Feishu explicitly rejected this attempt. It is safe to retry once the bot joins.
                with app.db_session() as conn:
                    conn.execute("""DELETE FROM feishu_daily_reports WHERE root_key=? AND task_id=? AND day=?
                        AND send_key=? AND sent_at IS NULL""", (root_key, task_id, day, report['send_key']))
                raise HTTPException(502, '机器人未加入目标群，请在群设置中添加本应用机器人。') from exc
            raise
        with app.db_session() as conn:
            conn.execute("""UPDATE feishu_daily_reports SET sent_at=? WHERE root_key=? AND task_id=? AND day=?
                AND send_key=? AND sent_at IS NULL""", (time.time(), root_key, task_id, day, report['send_key']))
        return 'sent'


def _publish_report_table(app, config, root_key, now, get_token):
    day = now.date().isoformat()
    with app.db_session() as conn:
        conn.execute('BEGIN IMMEDIATE')
        report_daily.freeze(conn, root_key, day)
        rows, _ = report_daily.load(conn, root_key, day)
    if not rows:
        return
    target = SimpleNamespace(app_token=config.report_base, table_id=config.report_table)
    feishu_report_bitable.publish(app.db_session, target, day, rows, get_token(), root_key=root_key)


def send_now(app):
    root_key = app.root_scope_key()
    with app.db_session() as conn:
        config = settings(conn, root_key)
    if not config.task_enabled or config.error or not config.recipient or not root_key:
        raise HTTPException(422, config.error or '请先启用日报并配置接收对象和文件根目录。')
    now = datetime.now(ZONE)
    if config.report_url:
        _publish_report_table(app, config, root_key, now, feishu.tenant_token)
    return _send_report(app, root_key, now, feishu.tenant_token, immediate=True)


def publish(app, now=None):
    root_key = app.root_scope_key()
    if not root_key:
        return
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    with app.db_session() as conn:
        default = settings(conn, root_key)
        configs = [(task_id, settings(conn, root_key, task_id)) for task_id in _task_ids(conn, root_key)]
    # Table work belongs to the root, even when several tasks send the same day's snapshot.
    shared = next((c for _, c in configs if c.task_enabled and not c.error), default)
    if not shared.active or shared.error:
        return
    token = None

    def get_token():
        nonlocal token
        if not token:
            token = feishu.tenant_token()
        return token

    due = [task_id for task_id, c in configs
           if c.task_enabled and not c.error and c.recipient and now.strftime('%H:%M') >= c.report_time]
    jobs = []
    if shared.table_key:
        jobs.append((f'feishu_daily_error:table:{root_key}', lambda: _publish_files(app, shared, root_key, get_token)))
    table_due = due or (default.task_enabled and not default.error and now.strftime('%H:%M') >= default.report_time)
    if shared.report_url and table_due:
        jobs.append((f'feishu_daily_error:report_table:{root_key}', lambda: _publish_report_table(app, shared, root_key, now, get_token)))
    for task_id in due:
        jobs.append((_report_error_key(root_key, task_id), lambda task_id=task_id:
                     _send_report(app, root_key, now, get_token, task_id=task_id)))
    for key, action in jobs:
        error = ''
        try:
            action()
        except HTTPException as exc:
            error = str(exc.detail)
        except Exception:
            logger.exception('Feishu daily job failed')
            error = '执行失败，请查看服务日志。'
        with app.db_session() as conn:
            if error:
                conn.execute('INSERT OR REPLACE INTO app_meta VALUES (?,?)', (key, error))
            else:
                conn.execute('DELETE FROM app_meta WHERE key=?', (key,))


def loop(app, stop):
    while not stop.is_set():
        try:
            publish(app)
        except Exception:
            logger.exception('Could not publish Feishu daily sync data')
        stop.wait(60)


def _recipient_label(conn, config):
    if config.open_id:
        member = _member(conn, config.open_id)
        return member['name'] if member else '已配置的个人账号'
    if config.chat:
        return '指定飞书群'
    return '暂停推送' if config.recipient_source == 'system' else '未设置接收人'


def _task_detail(conn, root_key, task_id, now):
    config = settings(conn, root_key, task_id)
    row = _stored_recipient(conn, root_key, task_id)
    last = conn.execute("""SELECT day, chat, sent_at FROM feishu_daily_reports
        WHERE root_key=? AND task_id=? AND sent_at IS NOT NULL ORDER BY day DESC LIMIT 1""",
        (root_key, task_id)).fetchone()
    day = now.date().isoformat()
    sent = last if last and last['day'] == day else None
    error = conn.execute('SELECT value FROM app_meta WHERE key=?', (_report_error_key(root_key, task_id),)).fetchone()
    next_run = ''
    if config.task_enabled and config.recipient and not config.error:
        hour, minute = map(int, config.report_time.split(':'))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if sent:
            due += timedelta(days=1)
        next_run = '等待调度' if due <= now else due.strftime('%Y-%m-%d %H:%M')
    return {'id': task_id, 'name': row['name'] if row else _DEFAULT_NAME,
            'recipient_type': config.recipient_type if config.recipient else 'off', 'recipient_id': config.recipient,
            'recipient_label': _recipient_label(conn, config), 'schedule': config.report_time,
            'enabled': config.task_enabled, 'revision': config.revision, 'next_run': next_run,
            'last_sent_at': last['sent_at'] if last else 0, 'last_recipient_id': last['chat'] if last else '',
            'last_sent_to_current': bool(last and last['chat'] == config.recipient),
            'report_sent_at': sent['sent_at'] if sent else 0, 'error': config.error or (error[0] if error else '')}


def list_tasks(app):
    root_key = app.root_scope_key()
    if not root_key:
        return []
    now = datetime.now(ZONE)
    with app.db_session() as conn:
        return [_task_detail(conn, root_key, task_id, now) for task_id in _task_ids(conn, root_key)]


def status(app):
    root_key = app.root_scope_key()
    now = datetime.now(ZONE)
    day = now.date()
    with app.db_session() as conn:
        config = settings(conn, root_key)
        info = _task_detail(conn, root_key, _DEFAULT_TASK, now)
        recipient = {'type': info['recipient_type'], 'id': config.recipient,
                     'source': config.recipient_source, 'label': info['recipient_label']}
        errors = [conn.execute('SELECT value FROM app_meta WHERE key=?', (f'feishu_daily_error:{job}:{root_key}',)).fetchone()
                  for job in ('table', 'report', 'report_table')]
        pending = conn.execute("""SELECT count(*) FROM feishu_daily_files
            WHERE root_key=? AND table_key=? AND published_at IS NULL""",
            (root_key, config.table_key)).fetchone()[0] if config.table_key else 0
        preview = _summary(app, conn, root_key, day, config)[0] if config.active and not config.error and root_key else ''
    return {'enabled': config.active, 'task_enabled': config.task_enabled,
            'revision': config.revision, 'next_run': info['next_run'], 'last_sent_at': info['last_sent_at'],
            'configuration_error': config.error if config.active else '',
            'schedule': config.report_time, 'chat_configured': bool(config.chat), 'recipient': recipient,
            'recipient_type': config.recipient_type if config.recipient else '',
            'report_table_configured': bool(config.report_url),
            'report_table_error': errors[2][0] if errors[2] else '',
            'table_configured': bool(config.table_key), 'table_pending': pending,
            'report_sent_at': info['report_sent_at'],
            'table_error': errors[0][0] if errors[0] else '', 'report_error': errors[1][0] if errors[1] else '',
            'preview': preview}
