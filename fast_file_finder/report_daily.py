"""A shared daily snapshot of the platform's monitored report rules."""
import csv
import io
import json
from datetime import date, datetime
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

import directory_access
from demo_data import ZONE

StatusFilter = Literal['all', 'normal', 'abnormal', 'pending']
LABELS = {'normal': '正常', 'abnormal': '异常', 'pending': '待检测'}


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS daily_report_snapshots (
        root_key TEXT NOT NULL, day TEXT NOT NULL, assessed_at REAL NOT NULL,
        frozen INTEGER NOT NULL DEFAULT 0, rows_json TEXT NOT NULL,
        PRIMARY KEY (root_key, day)
    )""")


def current_day():
    return datetime.now(ZONE).date().isoformat()


def save(conn, root_key, assessed_at, rows):
    if not root_key:
        return
    day = datetime.fromtimestamp(assessed_at, ZONE).date().isoformat()
    conn.execute("""INSERT INTO daily_report_snapshots(root_key,day,assessed_at,rows_json)
                    VALUES (?,?,?,?)
                    ON CONFLICT(root_key,day) DO UPDATE SET
                       assessed_at=excluded.assessed_at,rows_json=excluded.rows_json
                    WHERE frozen=0 AND assessed_at<=excluded.assessed_at""",
                 (root_key, day, assessed_at, json.dumps(rows, ensure_ascii=False)))


def load(conn, root_key, day):
    record = conn.execute("SELECT assessed_at,rows_json FROM daily_report_snapshots WHERE root_key=? AND day=?",
                          (root_key, day)).fetchone()
    if record is None:
        return [], ''
    stamp = datetime.fromtimestamp(record['assessed_at'], ZONE).strftime('%Y-%m-%d %H:%M')
    return json.loads(record['rows_json']), stamp


def freeze(conn, root_key, day):
    conn.execute("UPDATE daily_report_snapshots SET frozen=1 WHERE root_key=? AND day=? AND frozen=0",
                 (root_key, day))


def counts(rows):
    data = {state: 0 for state in LABELS}
    for row in rows:
        data[row['status']] += 1
    return {'total': len(rows), **data}


def row_for_rule(rule, evidence, root, now):
    state = evidence['state']
    status = 'normal' if state == 'healthy' else 'pending' if state in {'scheduled', 'paused'} else 'abnormal'
    label = LABELS[status]
    reason = evidence['reason']
    deadline = evidence.get('expected_at')
    if rule['period_type'] == 'daily' and not evidence.get('schedule') and deadline is not None:
        hour, minute = map(int, rule['deadline_time'].split(':'))
        due_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        deadline = due_today.timestamp()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if now < due_today and (evidence.get('latest_time') or 0) < day_start and state != 'invalid_path':
            status, label, reason = 'pending', LABELS['pending'], '待当日截止时间后检测'
    last = evidence.get('latest_time') or None
    path = evidence.get('path') or rule['target_path']
    relative = path[len(root.rstrip('/') + '/'):] if path.startswith(root.rstrip('/') + '/') else ''
    return {
        'rule_id': rule['id'], 'report_name': rule['file_keyword'] or rule['name'], 'rule_name': rule['name'],
        'status': status, 'status_label': label, 'update_count': int(evidence.get('today_updated', 0)),
        'last_updated': _format_time(last), 'deadline': _format_time(deadline), 'reason': reason,
        'owner': rule['responsible_person'] or '', 'directory': relative or ('根目录' if path == root else ''),
        'target_path': path,
    }


def _format_time(value):
    return datetime.fromtimestamp(value, ZONE).strftime('%Y-%m-%d %H:%M') if value is not None else ''


def _allowed_rows(app, request, day):
    user = app._require_user(request)
    with app.db_session() as conn:
        rows, stamp = load(conn, app.root_scope_key(), day)
        if app.DIRECTORY_ACL_ENABLED and user['role'] != 'super_admin':
            policy = directory_access.load_policy(conn, user, app.root_scope_key(), app._catalog_root_path())
            rows = [row for row in rows if policy.complete(row['target_path'])]
    return [{key: val for key, val in row.items() if key != 'target_path'} for row in rows], stamp


def _cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def register(app):
    @app.app.get('/api/reports/daily')
    def get_daily(request: Request, date: date | None = None, status: StatusFilter = Query('all')):
        day = date.isoformat() if date else current_day()
        rows, stamp = _allowed_rows(app, request, day)
        return {'date': day, 'snapshot_at': stamp, 'counts': counts(rows),
                'rows': [row for row in rows if status == 'all' or row['status'] == status]}

    @app.app.get('/reports/daily/abnormal.csv')
    def download_abnormal(request: Request, date: date | None = None):
        day = date.isoformat() if date else current_day()
        if app._get_session_user(request) is None:
            return RedirectResponse('/?' + urlencode({'tab': 'report-audit', 'date': day, 'download': 'abnormal'}))
        rows, _ = _allowed_rows(app, request, day)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        fields = {
            'report_name': '报表名称', 'rule_name': '监控任务', 'update_count': '更新数量',
            'status_label': '状态', 'last_updated': '最近更新', 'deadline': '截止时间',
            'reason': '异常原因', 'owner': '责任人', 'directory': '目录',
        }
        writer.writerow(['日期', *fields.values()])
        for row in rows:
            if row['status'] == 'abnormal':
                writer.writerow([day, *(_cell(row[field]) for field in fields)])
        return Response('\ufeff' + buffer.getvalue(), media_type='text/csv',
                        headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(f'异常清单-{day}.csv'),
                                 'Cache-Control': 'no-store'})
