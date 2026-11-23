from datetime import datetime, timedelta

from fastapi import HTTPException
import pytest

from test_feishu_daily import configured_daily
from test_product_foundation import foundation, login
import app
import feishu_daily as daily
import feishu_integration as feishu


TASKS = '/api/system/feishu/daily/tasks'


def task_body(name, target, schedule):
    return {'name': name, 'recipient_id': target, 'schedule': schedule, 'enabled': True,
            'recipient_type': 'open_id' if target.startswith('ou_') else 'chat_id'}


def task_list(client):
    return client.get(TASKS).raise_for_status().json()['tasks']


def create_task(client, body):
    client.post(TASKS, json=body).raise_for_status()
    return next(task for task in task_list(client) if task['name'] == body['name'])


def tick_today(hour, minute):
    return datetime.now(daily.ZONE).replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture
def task_client(configured_daily, monkeypatch):
    client, _, _ = configured_daily
    # No new files table is needed to test report delivery.
    for name in ('FEISHU_DAILY_BTABLE_APP_TOKEN', 'FEISHU_DAILY_BTABLE_TABLE_ID', 'FEISHU_DAILY_BTABLE_URL'):
        monkeypatch.delenv(name)
    monkeypatch.setattr(feishu, 'call', lambda *a, **kw: pytest.fail('Unexpected Feishu API call'))
    return client


def test_task_admin_can_manage_separate_people_and_group_schedules(task_client, monkeypatch):
    client = task_client
    with app.db_session() as conn:
        conn.execute("INSERT INTO feishu_members VALUES ('ou_owner','测试负责人','运营组',1)")
    login(client)
    initial = client.get(TASKS).raise_for_status().json()
    assert {task['id'] for task in initial['tasks']} == {'default'}
    assert any(person['open_id'] == 'ou_owner' for person in initial['members'])

    private_body = task_body('运营私聊', 'ou_owner', '08:00')
    group_body = task_body('运营工作群', 'oc_sales', '09:00')
    private = create_task(client, private_body)
    group = create_task(client, group_body)
    assert len({private['id'], group['id'], 'default'}) == 3
    assert private['recipient_label'] == '测试负责人'

    messages = []
    def send(method, path, *, params, json, **kwargs):
        assert (method, path) == ('POST', '/im/v1/messages')
        messages.append((params['receive_id_type'], json['receive_id']))
        return {'code': 0, 'data': {'message_id': 'mock-id'}}
    monkeypatch.setattr(feishu, 'call', send)
    daily.publish(app, tick_today(7, 59))
    daily.publish(app, tick_today(8, 0))
    assert messages == [('open_id', 'ou_owner')]

    paused = {**group_body, 'enabled': False, 'revision': group['revision']}
    client.put(f"{TASKS}/{group['id']}", json=paused).raise_for_status()
    assert client.put(f"{TASKS}/{group['id']}", json=paused).status_code == 409
    daily.publish(app, tick_today(9, 0))
    assert messages == [('open_id', 'ou_owner')]

    current = next(task for task in task_list(client) if task['id'] == group['id'])
    client.put(f"{TASKS}/{group['id']}", json={**group_body, 'revision': current['revision']}).raise_for_status()
    assert len(messages) == 1
    daily.publish(app, tick_today(9, 1))
    daily.publish(app, tick_today(9, 2))
    assert messages == [('open_id', 'ou_owner'), ('chat_id', 'oc_sales')]

    client.delete(f"{TASKS}/{private['id']}", params={'revision': private['revision']}).raise_for_status()
    assert {task['id'] for task in task_list(client)} == {'default', group['id']}
    daily.publish(app, tick_today(9, 0) + timedelta(days=1))
    assert messages[-1] == ('chat_id', 'oc_sales')
    assert len(messages) == 3


def test_failed_task_retries_its_own_message_without_blocking_other_tasks(task_client, monkeypatch):
    client = task_client
    login(client)
    failed = create_task(client, task_body('早班 A', 'oc_a', '07:00'))
    healthy = create_task(client, task_body('早班 B', 'oc_b', '07:00'))
    attempts = {'oc_a': [], 'oc_b': []}

    def flaky_send(method, path, *, json, **kwargs):
        assert (method, path) == ('POST', '/im/v1/messages')
        attempts[json['receive_id']].append(json['uuid'])
        if json['receive_id'] == 'oc_a' and len(attempts['oc_a']) == 1:
            raise HTTPException(502, '测试错误：群消息未确认')
        return {'code': 0, 'data': {'message_id': 'mock-id'}}

    monkeypatch.setattr(feishu, 'call', flaky_send)
    daily.publish(app, tick_today(7, 0))
    rows = {row['id']: row for row in task_list(client)}
    assert rows[failed['id']]['error'] and not rows[healthy['id']]['error']
    assert rows[healthy['id']]['last_sent_at'] and not rows[failed['id']]['last_sent_at']
    daily.publish(app, tick_today(7, 1))
    assert len(attempts['oc_b']) == 1
    assert len(attempts['oc_a']) == 2 and attempts['oc_a'][0] == attempts['oc_a'][1]
    assert attempts['oc_a'][0] != attempts['oc_b'][0]
    assert not next(task['error'] for task in task_list(client) if task['id'] == failed['id'])


def install_old_schedule(conn, root_key, target, report_rows):
    # Reconstruct the previous release's schema in this test's temporary database.
    conn.execute('DROP TABLE feishu_daily_recipients')
    conn.execute('DROP TABLE feishu_daily_reports')
    conn.execute("""CREATE TABLE feishu_daily_recipients (
        root_key TEXT NOT NULL, app_id TEXT NOT NULL,
        recipient_type TEXT NOT NULL CHECK(recipient_type IN ('open_id','chat_id','off')),
        recipient_id TEXT NOT NULL, updated_at REAL NOT NULL, updated_by INTEGER NOT NULL,
        schedule TEXT, task_enabled INTEGER, PRIMARY KEY(root_key, app_id))""")
    conn.execute("""CREATE TABLE feishu_daily_reports (
        root_key TEXT NOT NULL, day TEXT NOT NULL, chat TEXT NOT NULL, send_key TEXT NOT NULL,
        body TEXT NOT NULL, prepared_at REAL NOT NULL, sent_at REAL, PRIMARY KEY(root_key, day))""")
    conn.execute('INSERT INTO feishu_daily_recipients VALUES (?,?,?,?,?,?,?,?)',
                 (root_key, 'cli_fixture', 'chat_id', target, 1234.5, 1, '16:15', 1))
    conn.executemany('INSERT INTO feishu_daily_reports VALUES (?,?,?,?,?,?,?)', report_rows)


def test_previously_delivered_default_is_not_replayed_after_upgrading_or_deleting(task_client):
    client = task_client
    login(client)
    morning = tick_today(8, 0)
    root_key = app.root_scope_key()
    original = (root_key, morning.date().isoformat(), 'ou_previous_private_target',
                'original-uuid', '旧版私聊日报', morning.timestamp(), morning.timestamp())
    with app.db_session() as conn:
        install_old_schedule(conn, root_key, 'oc_saved_group', [original])
    app.init_db()
    with app.db_session() as conn:
        stored, = conn.execute("""SELECT root_key,day,chat,send_key,body,prepared_at,sent_at
            FROM feishu_daily_reports WHERE task_id='default'""").fetchall()
    assert tuple(stored) == original
    default, = task_list(client)
    assert (default['id'], default['recipient_id'], default['schedule']) == ('default', 'oc_saved_group', '16:15')
    assert default['enabled'] and default['last_sent_at'] == morning.timestamp()
    # The editor changed the destination after the old task had already delivered today's card.
    daily.publish(app, tick_today(18, 0))
    client.delete(f'{TASKS}/default', params={'revision': default['revision']}).raise_for_status()
    app.init_db()
    assert task_list(client) == []
    daily.publish(app, tick_today(18, 1) + timedelta(days=1))


def test_pending_message_retains_its_uuid_after_migration(task_client, monkeypatch):
    client = task_client
    login(client)
    prepared = tick_today(16, 14)
    root_key = app.root_scope_key()
    with app.db_session() as conn:
        install_old_schedule(conn, root_key, 'oc_saved_group', [
            (root_key, prepared.date().isoformat(), 'oc_saved_group', 'original-pending-uuid',
             '准备好的日报正文', prepared.timestamp(), None)])
    app.init_db()
    sends = []

    def receive(method, path, *, json, **kwargs):
        assert (method, path) == ('POST', '/im/v1/messages')
        sends.append((json['receive_id'], json['uuid'], json['content']))
        return {'code': 0, 'data': {'message_id': 'mock-id'}}

    monkeypatch.setattr(feishu, 'call', receive)
    daily.publish(app, tick_today(16, 15))
    app.init_db()
    daily.publish(app, tick_today(16, 16))
    assert len(sends) == 1 and sends[0][:2] == ('oc_saved_group', 'original-pending-uuid')
    assert '准备好的日报正文' in sends[0][2]


def test_task_administration_rejects_anonymous_and_member_access(task_client):
    client = task_client
    body = task_body('不可发', 'oc_specific', '16:00')
    for expected in (401, 403):
        assert client.get(TASKS).status_code == expected
        assert client.post(TASKS, json=body).status_code == expected
        assert client.put(f'{TASKS}/default', json={**body, 'revision': 0}).status_code == expected
        assert client.delete(f'{TASKS}/default', params={'revision': 0}).status_code == expected
        if expected == 401:
            login(client, admin=False)


def test_new_task_runs_without_environment_recipient_or_table(task_client, monkeypatch):
    client = task_client
    monkeypatch.delenv('FEISHU_DAILY_CHAT_ID')
    monkeypatch.setenv('FEISHU_DAILY_ENABLED', 'false')
    login(client)
    task = create_task(client, task_body('独立日报', 'oc_standalone', '11:45'))
    assert task['enabled']
    messages = []

    def receive(method, path, *, json, **kwargs):
        assert (method, path) == ('POST', '/im/v1/messages')
        messages.append(json['receive_id'])
        return {'code': 0, 'data': {'message_id': 'mock-id'}}

    monkeypatch.setattr(feishu, 'call', receive)
    cutoff = tick_today(11, 45)
    daily.publish(app, cutoff - timedelta(minutes=1))
    assert not messages
    daily.publish(app, cutoff)
    daily.publish(app, cutoff + timedelta(minutes=1))
    assert messages == ['oc_standalone']
