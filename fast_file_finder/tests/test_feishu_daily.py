from datetime import datetime, timedelta
import json

from fastapi import HTTPException
import pytest

from test_product_foundation import foundation, login
import app
import feishu_daily as daily
import feishu_integration as feishu


@pytest.fixture
def configured_daily(monkeypatch, foundation):
    for name, value in {
        'FEISHU_APP_ID': 'cli_fixture', 'FEISHU_APP_SECRET': 'not-a-real-secret',
        'FEISHU_DAILY_ENABLED': 'true', 'FEISHU_DAILY_CHAT_ID': 'oc_fixture',
        'FEISHU_DAILY_BTABLE_APP_TOKEN': 'bascn_fixture', 'FEISHU_DAILY_BTABLE_TABLE_ID': 'tbl_fixture',
        'FEISHU_DAILY_BTABLE_URL': 'https://fixture.feishu.cn/base/fixture',
        'FEISHU_DAILY_TIME': '18:00',
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(feishu, 'tenant_token', lambda: 'synthetic-tenant-token')
    return foundation


def card_text(payload):
    card = json.loads(payload['content'])
    return card['header']['subtitle']['content'] + '\n' + card['body']['elements'][0]['content'].replace('**', '')


def test_only_new_index_entries_after_initial_inventory(configured_daily):
    _, root, _ = configured_daily
    old = root / '旧报表.csv'
    old.write_text('a,b')
    app._scan_worker()
    old.write_text('a,b\n1,2')
    new = root / '新报表.csv'
    new.write_text('a,b')
    app._scan_worker()
    app._scan_worker(mode='rebuild')
    with app.db_session() as conn:
        names = [row[0] for row in conn.execute('SELECT name FROM feishu_daily_files')]
    assert names == ['新报表.csv']


def test_daily_sheet_is_retried_without_changing_batch_or_repeating_messages(configured_daily, monkeypatch):
    _, _, add = configured_daily
    day = datetime.now(daily.ZONE).date()
    before_cutoff = datetime.combine(day, daily.wall_time(17, 50), daily.ZONE)
    cutoff = before_cutoff + timedelta(minutes=10)
    tables, messages = [], []

    def api(method, path, **request):
        assert method == 'POST'
        if path.endswith('/batch_create'):
            batch = request['params']['client_token'], request['json']['records']
            tables.append(batch)
            if len(tables) == 1:
                raise HTTPException(502, '暂时不可用')
        elif path == '/im/v1/messages':
            messages.append(request)
        else:
            raise AssertionError(path)

    monkeypatch.setattr(feishu, 'call', api)
    add('本部/首个报表.csv')
    daily.publish(app, before_cutoff)
    assert not messages
    assert daily.status(app)['table_pending'] == 1
    assert daily.status(app)['table_error'] == '暂时不可用'
    add('本部/第二个报表.csv')
    daily.publish(app, cutoff)
    daily.publish(app, cutoff + timedelta(minutes=1))
    assert len(tables) == 3 and len(messages) == 1
    assert tables[0] == tables[1] and tables[2][0] != tables[1][0]
    fields = tables[1][1][0]['fields']
    assert fields['相对目录'] == '本部'
    assert fields['日期'] == str(day)
    assert fields['文件名'] == '首个报表.csv'
    assert messages[0]['json']['msg_type'] == 'interactive'
    assert messages[0]['params']['receive_id_type'] == 'chat_id'
    message = card_text(messages[0]['json'])
    assert '今日新入索引：2 份文件（其中表格 2 份）' in message
    assert '今日暂无扫描记录' in message
    assert '归档检测：' in message
    card = json.loads(messages[0]['json']['content'])
    assert any(element.get('behaviors', [{}])[0].get('default_url') == 'https://fixture.feishu.cn/base/fixture'
               for element in card['body']['elements'])
    assert '首个报表.csv' not in message
    assert daily.status(app)['table_pending'] == 0
    assert daily.status(app)['table_error'] == ''


def test_reporting_cutoff_archive_exceptions_and_role(configured_daily, monkeypatch):
    client, _, _ = configured_daily
    now = datetime.now(daily.ZONE)
    rule_states = ['paused', 'scheduled', 'no_files', 'no_content', 'calendar_unavailable']
    with app.db_session() as conn:
        daily.record_check(conn, app.root_scope_key(), now.timestamp(), 8, 2,
                           [{'state': state} for state in rule_states])
    login(client, admin=False)
    assert client.get('/api/system/feishu').status_code == 403
    login(client)
    result = client.get('/api/system/feishu').json()['daily']
    assert '逾期未交 2 条，无法核验 3 条' in result['preview']
    with app.db_session() as conn:
        daily.record_check_failure(conn, app.root_scope_key(), now.timestamp() + .01)
    assert '最近一次执行失败' in daily.status(app)['preview']

    monkeypatch.setenv('FEISHU_DAILY_ENABLED', 'false')
    monkeypatch.setattr(feishu, 'call', lambda *args, **kwargs: pytest.fail('Disabled feature used Feishu API'))
    daily.publish(app)


def test_next_day_report_has_independent_delivery_and_file_counts(configured_daily, monkeypatch):
    _, _, add = configured_daily
    first = datetime.combine(datetime.now(daily.ZONE).date(), daily.wall_time(20), daily.ZONE)
    next_day = first + timedelta(days=1)
    monkeypatch.setattr(daily.time, 'time', lambda: first.timestamp())
    add('数据/昨日.csv')
    report_text = []
    monkeypatch.setattr(feishu, 'call',
                        lambda method, path, **request: report_text.append(card_text(request['json']))
                        if path == '/im/v1/messages' else None)
    daily.publish(app, first)
    monkeypatch.setattr(daily.time, 'time', lambda: next_day.timestamp())
    add('数据/今日.csv')
    add('数据/次日补充.csv')
    daily.publish(app, next_day)
    daily.publish(app, next_day + timedelta(minutes=1))
    assert len(report_text) == 2
    assert f'{first:%Y-%m-%d}' in report_text[0] and '1 份文件' in report_text[0]
    assert f'{next_day:%Y-%m-%d}' in report_text[1] and '2 份文件' in report_text[1]


def test_report_reuses_send_key_when_bitable_is_unavailable(configured_daily, monkeypatch):
    _, _, add = configured_daily
    cutoff = datetime.combine(datetime.now(daily.ZONE).date(), daily.wall_time(18), daily.ZONE)
    attempts = []

    def api(method, path, **request):
        if path.endswith('/batch_create'):
            raise HTTPException(502, '写表暂时不可用')
        attempts.append(request['json'])
        if len(attempts) == 1:
            raise HTTPException(502, '发送结果未确认')

    monkeypatch.setattr(feishu, 'call', api)
    add('本部/待写入的报表.csv')
    daily.publish(app, cutoff)
    daily.publish(app, cutoff + timedelta(minutes=1))
    daily.publish(app, cutoff + timedelta(minutes=2))
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert '1 条待同步' in card_text(attempts[0])
    assert daily.status(app)['table_pending'] == 1
    assert daily.status(app)['report_sent_at'] > 0


def test_personal_card_links_to_pinned_date_and_is_sent_only_once(configured_daily, monkeypatch):
    client, _, add = configured_daily
    monkeypatch.delenv('FEISHU_DAILY_CHAT_ID')
    monkeypatch.setenv('FEISHU_DAILY_OPEN_ID', 'ou_fixture')
    monkeypatch.setenv('FEISHU_DAILY_SYSTEM_URL', 'https://assets.example.test')
    monkeypatch.setenv('FEISHU_DAILY_TIME', '23:59')
    day = datetime.now(daily.ZONE).date()
    add('业务/更新清单.csv')
    calls = []

    def api(method, path, **request):
        assert path == '/im/v1/messages'
        calls.append(request)

    monkeypatch.setattr(feishu, 'call', api)
    assert daily.send_now(app) == 'sent'
    assert daily.send_now(app) == 'already_sent'
    assert len(calls) == 1
    assert calls[0]['params']['receive_id_type'] == 'open_id'
    payload = calls[0]['json']
    assert payload['receive_id'] == 'ou_fixture'
    card = json.loads(payload['content'])
    link = card['body']['elements'][1]['behaviors'][0]['default_url']
    assert link == f'https://assets.example.test/?tab=report-audit&date={day}'
    assert card['body']['elements'][-1]['behaviors'][0]['default_url'] == (
        f'https://assets.example.test/reports/daily/abnormal.csv?date={day}')
    assert '更新清单.csv' not in card_text(payload)
    assert '今日更新：1 份文件' in card_text(payload)
    login(client)
    listing = client.get('/api/search', params={
        'date_from': str(day), 'date_to': str(day), 'date_field': 'mtime', 'date_zone': 'asia_shanghai',
    }).json()
    assert listing['total_candidates'] == 1
    assert listing['results'][0]['name'] == '更新清单.csv'


def test_daily_card_has_three_links_and_uses_one_snapshot(configured_daily, monkeypatch):
    import report_daily

    _, root, _ = configured_daily
    monkeypatch.delenv('FEISHU_DAILY_CHAT_ID')
    monkeypatch.setenv('FEISHU_DAILY_OPEN_ID', 'ou_fixture')
    monkeypatch.setenv('FEISHU_DAILY_SYSTEM_URL', 'https://assets.example.test')
    monkeypatch.setenv('FEISHU_REPORT_BTABLE_APP_TOKEN', 'bascn_fixture')
    monkeypatch.setenv('FEISHU_REPORT_BTABLE_TABLE_ID', 'tbl_fixture')
    base_url = 'https://fixture.feishu.cn/base/fixture?table=tbl_fixture'
    monkeypatch.setenv('FEISHU_REPORT_BTABLE_URL', base_url)
    now = datetime.now(daily.ZONE)
    day = now.date().isoformat()
    row = {
        'rule_id': 8, 'report_name': '业务日报', 'rule_name': '例行检测', 'status': 'abnormal',
        'status_label': '异常', 'update_count': 0, 'last_updated': '', 'deadline': '',
        'reason': '到期未更新', 'owner': '业务组', 'directory': '华东', 'target_path': str(root / '华东'),
    }
    with app.db_session() as conn:
        daily.report_daily.save(conn, app.root_scope_key(), now.timestamp(), [row])
    copies = []
    monkeypatch.setattr(daily.feishu_report_bitable, 'publish',
                        lambda db, config, date, rows, token, **kwargs: copies.extend(rows))
    cards = []
    monkeypatch.setattr(feishu, 'call',
                        lambda method, path, **request: cards.append(json.loads(request['json']['content'])))
    assert daily.send_now(app) == 'sent'
    contents = cards[0]['body']['elements']
    buttons = {item['text']['content']:item['behaviors'][0]['default_url'] for item in contents if item['tag'] == 'button'}
    assert buttons == {
        '查看系统明细': f'https://assets.example.test/?tab=report-audit&date={day}',
        '打开飞书多维表': base_url,
        '下载异常清单': f'https://assets.example.test/reports/daily/abnormal.csv?date={day}',
    }
    assert copies == [row]
    assert '1 条异常' in contents[0]['content']
    assert cards[0]['header']['template'] == 'orange'
