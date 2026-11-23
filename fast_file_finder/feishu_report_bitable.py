"""Publish daily monitoring snapshots to their dedicated Feishu Base table."""
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time
import hashlib
import json
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import uuid

from fastapi import HTTPException

import feishu_integration as feishu
from demo_data import ZONE
from report_daily import LABELS


BATCH_SIZE = 100
TABLE_NAME = '报表更新明细'
FIELD_TYPES = {
    '报表名称': 1,
    '日期': 5,
    '更新数量': 2,
    '状态': 3,
    '最近更新': 1,
    '截止时间': 1,
    '异常原因或说明': 1,
    '负责人': 1,
    '监控任务': 1,
    '业务目录': 1,
    '同步键': 1,
}


@dataclass(frozen=True)
class Destination:
    app_token: str
    table_id: str
    url: str


def _digest(*parts):
    return hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()


def _app_path(app_token):
    return f'/bitable/v1/apps/{quote(app_token, safe="")}'


def _table_path(app_token, table_id):
    return f'{_app_path(app_token)}/tables/{quote(table_id, safe="")}'


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS feishu_report_bitable_batches (
        target_key TEXT NOT NULL, day TEXT NOT NULL, batch_number INTEGER NOT NULL,
        snapshot_hash TEXT NOT NULL, client_token TEXT NOT NULL, sent_at REAL,
        PRIMARY KEY(target_key, day, batch_number)
    )""")


def _fields(row, day, day_start, root_key, rule_id):
    return {
        '报表名称': row['report_name'],
        '日期': day_start,
        '更新数量': int(row['update_count']),
        '状态': LABELS[row['status']],
        '最近更新': row.get('last_updated') or '',
        '截止时间': row.get('deadline') or '',
        '异常原因或说明': row.get('reason') or '',
        '负责人': row.get('owner') or '',
        '监控任务': row['rule_name'],
        '业务目录': row.get('directory') or '',
        '同步键': _digest(root_key, day, rule_id),
    }


def publish(db_session, config, day, rows, token, *, root_key=''):
    """Append a frozen day's rows once, resuming failed batches with the same UUID."""
    report_date = date.fromisoformat(day)
    day = report_date.isoformat()
    day_start = int(datetime.combine(report_date, wall_time.min, ZONE).timestamp() * 1000)
    ids = [str(row['rule_id']) for row in rows]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, '同一天的报表明细包含重复监控任务。')
    records = [
        {'fields': _fields(row, day, day_start, root_key, rule_id)}
        for rule_id, row in sorted(zip(ids, rows), key=lambda item: item[0])
    ]
    if not records:
        return 0

    target_key = _digest(config.app_token, config.table_id, root_key)
    snapshot_hash = _digest(json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    batches = [records[start:start + BATCH_SIZE] for start in range(0, len(records), BATCH_SIZE)]

    with db_session() as conn:
        initialize(conn)
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany(
            """INSERT OR IGNORE INTO feishu_report_bitable_batches
                 (target_key, day, batch_number, snapshot_hash, client_token)
               VALUES (?, ?, ?, ?, ?)""",
            [(target_key, day, number, snapshot_hash, str(uuid.uuid4())) for number in range(len(batches))],
        )
        prepared = conn.execute(
            """SELECT snapshot_hash, client_token, sent_at FROM feishu_report_bitable_batches
               WHERE target_key=? AND day=? ORDER BY batch_number""",
            (target_key, day),
        ).fetchall()
        if len(prepared) != len(batches) or any(batch[0] != snapshot_hash for batch in prepared):
            raise HTTPException(409, '这一天的报表明细已开始同步，请使用原来的日报快照重试。')

    path = f'{_table_path(config.app_token, config.table_id)}/records/batch_create'
    for number, (batch, (_, client_token, sent_at)) in enumerate(zip(batches, prepared)):
        if sent_at is not None:
            continue
        result = feishu.call('POST', path, token=token, params={'client_token': client_token},
                             json={'records': batch})
        created = (result.get('data') or {}).get('records')
        if not isinstance(created, list) or len(created) != len(batch):
            raise HTTPException(502, '飞书未确认全部报表明细已写入，稍后将使用原批次重试。')
        with db_session() as conn:
            conn.execute(
                """UPDATE feishu_report_bitable_batches SET sent_at=?
                   WHERE target_key=? AND day=? AND batch_number=?""",
                (time.time(), target_key, day, number),
            )
    return len(records)


def _field_definitions():
    definitions = [{'field_name': name, 'type': kind} for name, kind in FIELD_TYPES.items()]
    status = next(field for field in definitions if field['field_name'] == '状态')
    status['property'] = {'options': [
        {'name': LABELS['normal'], 'color': 2},
        {'name': LABELS['abnormal'], 'color': 1},
        {'name': LABELS['pending'], 'color': 0},
    ]}
    return definitions


def _items(path, token):
    page_token = ''
    seen = set()
    while True:
        params = {'page_size': 50}
        if page_token:
            params['page_token'] = page_token
        data = feishu.call('GET', path, token=token, params=params).get('data') or {}
        yield from data.get('items') or ()
        if not data.get('has_more'):
            return
        page_token = data.get('page_token')
        if not page_token or page_token in seen:
            raise HTTPException(502, '飞书多维表格的列表分页异常。')
        seen.add(page_token)


def _destination_url(base_url, table_id, view_id=''):
    url = urlsplit(base_url)
    query = dict(parse_qsl(url.query))
    query['table'] = table_id
    if view_id:
        query['view'] = view_id
    else:
        query.pop('view', None)
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ''))


def create_base(token, *, name='文件同步日预警明细'):
    """Create the container. Keep its returned address before provisioning the table."""
    result = feishu.call('POST', '/bitable/v1/apps', token=token,
                         json={'name': name, 'time_zone': 'Asia/Shanghai'})
    base = (result.get('data') or {}).get('app') or {}
    if not base.get('app_token') or not base.get('url'):
        raise HTTPException(502, '飞书未返回创建后的多维表格地址。')
    return Destination(base['app_token'], '', base['url'])


def provision(token, *, app_token, url, table_id=''):
    """Create the schema and status views in the chosen Base, reusing existing resources."""
    tables = f'{_app_path(app_token)}/tables'
    default_view = ''
    if not table_id:
        table_id = next((item['table_id'] for item in _items(tables, token) if item.get('name') == TABLE_NAME), '')
    if not table_id:
        result = feishu.call('POST', tables, token=token, json={'table': {
            'name': TABLE_NAME, 'default_view_name': '全部', 'fields': _field_definitions(),
        }})
        data = result.get('data') or {}
        table_id = data.get('table_id')
        default_view = data.get('default_view_id', '')
        if not table_id:
            raise HTTPException(502, '飞书未返回报表明细数据表标识。')

    table = _table_path(app_token, table_id)
    fields = {field['field_name']: field for field in _items(f'{table}/fields', token)}
    for name, kind in FIELD_TYPES.items():
        if fields.get(name, {}).get('type') != kind:
            raise HTTPException(422, f'报表明细数据表缺少「{name}」列，或该列类型不匹配。')
    status = fields['状态']
    options = {option['name'] for option in (status.get('property') or {}).get('options') or ()}
    if not set(LABELS.values()) <= options:
        raise HTTPException(422, '「状态」列需要配置正常、异常和待检测三个选项。')

    views = {view['view_name']: view['view_id'] for view in _items(f'{table}/views', token)}
    for label in (LABELS['normal'], LABELS['abnormal']):
        view_id = views.get(label)
        if not view_id:
            result = feishu.call('POST', f'{table}/views', token=token,
                                 json={'view_name': label, 'view_type': 'grid'})
            view_id = ((result.get('data') or {}).get('view') or {}).get('view_id')
        if not view_id:
            raise HTTPException(502, '飞书未返回报表状态视图标识。')
        feishu.call('PATCH', f'{table}/views/{quote(view_id, safe="")}', token=token, json={
            'property': {'filter_info': {'conjunction': 'and', 'conditions': [{
                'field_id': status['field_id'], 'operator': 'is', 'value': json.dumps([label], ensure_ascii=False),
            }]}},
        })
    return Destination(app_token, table_id, _destination_url(url, table_id, default_view or views.get('全部', '')))
