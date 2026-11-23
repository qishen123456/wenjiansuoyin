from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
import uuid

from fastapi import HTTPException
import pytest

from demo_data import ZONE
import feishu_report_bitable as bitable


DAY = '2026-11-23'
TARGET = bitable.Destination('base_fixture', 'table_fixture', 'https://fixture.feishu.cn/base/base_fixture')


@pytest.fixture
def db_session(tmp_path):
    @contextmanager
    def session():
        conn = sqlite3.connect(tmp_path / 'bitable.db')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    return session


def row(rule_id, status='normal'):
    return {
        'rule_id': rule_id, 'report_name': f'日报 {rule_id}', 'rule_name': '交付监控',
        'status': status, 'update_count': int(status == 'normal'),
        'last_updated': '2026-11-23 10:00', 'deadline': '2026-11-23 18:00',
        'reason': '交付窗口内未检测到有效文件' if status == 'abnormal' else '',
        'owner': '运营组', 'directory': '销售/日报', 'target_path': '/private/share/sales',
    }


def test_publish_resumes_with_the_same_token_without_resending_completed_batches(db_session, monkeypatch):
    monkeypatch.setattr(bitable, 'BATCH_SIZE', 2)
    requests = []

    def api(method, path, *, token, params, json):
        assert method == 'POST'
        assert path == '/bitable/v1/apps/base_fixture/tables/table_fixture/records/batch_create'
        assert token == 'test_token'
        records = json['records']
        batch_key = params['client_token']
        assert uuid.UUID(batch_key).version == 4
        requests.append((batch_key, records))
        if len(requests) == 2:
            raise HTTPException(502, '模拟网络中断')
        return {'data': {'records': [{'record_id': 'rec_fixture'} for _ in records]}}

    monkeypatch.setattr(bitable.feishu, 'call', api)
    rows = [row(3, 'pending'), row(2, 'abnormal'), row(1)]
    with pytest.raises(HTTPException, match='模拟网络中断'):
        bitable.publish(db_session, TARGET, DAY, rows, 'test_token', root_key='private-root')
    assert bitable.publish(db_session, TARGET, DAY, rows[::-1], 'test_token', root_key='private-root') == 3
    assert bitable.publish(db_session, TARGET, DAY, rows, 'test_token', root_key='private-root') == 3
    assert len(requests) == 3
    assert requests[0][0] != requests[1][0]
    assert requests[1] == requests[2]

    fields = requests[0][1][1]['fields']
    assert fields['日期'] == int(datetime(2026, 11, 23, tzinfo=ZONE).timestamp() * 1000)
    assert fields['状态'] == '异常'
    assert fields['更新数量'] == 0
    assert fields['业务目录'] == '销售/日报'
    assert fields['异常原因或说明'] == '交付窗口内未检测到有效文件'
    assert len(fields['同步键']) == 64
    assert '/private/share' not in str(requests)
    assert 'private-root' not in str(requests)


def test_publish_does_not_accept_a_changed_daily_snapshot(db_session, monkeypatch):
    def api(method, path, **kwargs):
        return {'data': {'records': [{'record_id': 'rec_fixture'}]}}

    monkeypatch.setattr(bitable.feishu, 'call', api)
    assert bitable.publish(db_session, TARGET, DAY, [row(1)], 'test_token') == 1
    with pytest.raises(HTTPException) as caught:
        bitable.publish(db_session, TARGET, DAY, [row(1, 'abnormal')], 'test_token')
    assert caught.value.status_code == 409
    assert bitable.publish(db_session, TARGET, '2026-11-24', [row(1, 'abnormal')], 'test_token') == 1


def test_empty_creation_response_can_retry(db_session, monkeypatch):
    client_tokens = []

    def api(method, path, **kwargs):
        client_tokens.append(kwargs['params']['client_token'])
        return {} if len(client_tokens) == 1 else {'data': {'records': [{'record_id': 'rec_fixture'}]}}

    monkeypatch.setattr(bitable.feishu, 'call', api)
    with pytest.raises(HTTPException, match='未确认全部'):
        bitable.publish(db_session, TARGET, DAY, [row(1)], 'test_token')
    assert bitable.publish(db_session, TARGET, DAY, [row(1)], 'test_token') == 1
    assert client_tokens[0] == client_tokens[1]


def test_provision_creates_table_and_real_status_filters_then_reuses_them(monkeypatch):
    tables, fields, views, saved_filters = [], [], [], {}
    tables_path = '/bitable/v1/apps/base_fixture/tables'
    table_path = tables_path + '/table_fixture'

    def api(method, path, *, token, params=None, json=None):
        assert token == 'test_token'
        if method == 'GET':
            return {'data': {'items': {
                tables_path: tables, table_path + '/fields': fields, table_path + '/views': views,
            }[path]}}
        if method == 'POST' and path == tables_path:
            assert json['table']['default_view_name'] == '全部'
            tables.append({'name': json['table']['name'], 'table_id': 'table_fixture'})
            fields.extend({**field, 'field_id': f'fld_{number}'}
                          for number, field in enumerate(json['table']['fields']))
            views.append({'view_name': '全部', 'view_id': 'view_all'})
            return {'data': {'table_id': 'table_fixture', 'default_view_id': 'view_all'}}
        if method == 'POST' and path == table_path + '/views':
            assert json['view_type'] == 'grid'
            view = {**json, 'view_id': f'view_{len(views)}'}
            views.append(view)
            return {'data': {'view': view}}
        assert method == 'PATCH' and path.startswith(table_path + '/views/')
        saved_filters[path.rsplit('/', 1)[1]] = json['property']['filter_info']
        return {'data': {}}

    monkeypatch.setattr(bitable.feishu, 'call', api)
    for _ in range(2):
        assert bitable.provision('test_token', app_token=TARGET.app_token, url=TARGET.url) == bitable.Destination(
            'base_fixture', 'table_fixture', TARGET.url + '?table=table_fixture&view=view_all'
        )

    assert len(tables) == 1
    assert fields[0]['field_name'] == '报表名称'
    assert fields[1]['type'] == 5
    assert fields[2]['type'] == 2
    assert fields[3]['type'] == 3
    assert [option['name'] for option in fields[3]['property']['options']] == ['正常', '异常', '待检测']
    assert [view['view_name'] for view in views] == ['全部', '正常', '异常']
    assert set(saved_filters) == {'view_1', 'view_2'}
    for view in views[1:]:
        condition = saved_filters[view['view_id']]['conditions'][0]
        assert condition['field_id'] == fields[3]['field_id']
        assert condition['operator'] == 'is'
        assert json.loads(condition['value']) == [view['view_name']]
