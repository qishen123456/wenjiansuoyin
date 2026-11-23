import csv
from datetime import datetime, timedelta
import io
import secrets
from urllib.parse import urlencode

import app
import report_daily
from test_product_foundation import foundation, login


def saved_row(path, name, state):
    return {'rule_id': 1, 'rule_name': '归档核验', 'report_name': name, 'status': state,
            'status_label': report_daily.LABELS[state], 'update_count': 0, 'last_updated': '',
            'deadline': '2026-11-23 10:00', 'reason': '该更新未到', 'owner': '业务组',
            'directory': path.name, 'target_path': str(path)}


def test_daily_table_and_csv_use_the_same_scope_and_status(foundation, monkeypatch):
    client, root, add = foundation
    keep = root / '可见'
    private = root / '机密'
    add('可见/每日.csv')
    add('机密/每日.csv')
    monkeypatch.setattr(app, 'DIRECTORY_ACL_ENABLED', True)
    # Startup can write a snapshot for today before the fixture runs.
    now = (datetime.now(report_daily.ZONE) - timedelta(days=1)).replace(hour=18, minute=0)
    day = now.date().isoformat()
    rows = [saved_row(keep, '库存表', 'normal'), saved_row(keep, '=更新汇总', 'abnormal'),
            saved_row(private, '内部报表', 'abnormal'), saved_row(keep, '等候更新', 'pending')]
    with app.db_session() as conn:
        report_daily.save(conn, app.root_scope_key(), now.timestamp(), rows)
        report_daily.freeze(conn, app.root_scope_key(), day)
        # Pinned snapshots remain consistent with a card or Base published earlier.
        report_daily.save(conn, app.root_scope_key(), now.timestamp()+60, [])
    url = '/reports/daily/abnormal.csv?' + urlencode({'date':day})
    prompt = client.get(url, follow_redirects=False)
    assert prompt.status_code == 307
    assert 'tab=report-audit' in prompt.headers['location'] and 'download=abnormal' in prompt.headers['location']

    login(client)
    client.post('/api/system/directory-grants', json={
        'subject_type':'user', 'subject_key':'4', 'target_path':str(keep), 'permissions':{'read':'allow'},
    }).raise_for_status()
    login(client, admin=False)
    response = client.get('/api/reports/daily', params={'date':day,'status':'abnormal'}).json()
    assert response['counts'] == {'total':3,'normal':1,'abnormal':1,'pending':1}
    assert [r['report_name'] for r in response['rows']] == ['=更新汇总']
    assert 'target_path' not in response['rows'][0]
    assert client.get('/api/reports/daily', params={'date':day,'status':'normal'}).json()['rows'][0]['report_name'] == '库存表'
    downloaded = client.get(url)
    assert downloaded.status_code == 200 and downloaded.headers['content-disposition'].startswith('attachment;')
    data = list(csv.reader(io.StringIO(downloaded.content.decode('utf-8-sig'))))
    assert data[0][:5] == ['日期','报表名称','监控任务','更新数量','状态']
    assert data[1][:5] == [day, "'=更新汇总", '归档核验', '0', '异常']
    assert len(data) == 2


def test_shared_token_gate_also_protects_daily_csv(foundation, monkeypatch):
    client, _, _ = foundation
    login(client)
    gate = secrets.token_urlsafe()
    monkeypatch.setattr(app, 'APP_ACCESS_TOKEN', gate)
    day = '2026-11-23'
    card = client.get('/reports/daily/abnormal.csv', params={'date': day}, follow_redirects=False)
    assert card.status_code == 307
    assert card.headers['location'] == f'/?tab=report-audit&date={day}&download=abnormal'
    url = '/api/reports/daily/abnormal.csv'
    assert client.get(url).status_code == 401
    response = client.get(url, headers={'X-App-Token': gate})
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('attachment;')
    client.cookies.clear()
    assert client.get(url, headers={'X-App-Token': gate}).status_code == 401


def test_daily_status_does_not_reuse_yesterday_outcome_before_todays_deadline():
    now = datetime(2026, 11, 23, 9, tzinfo=report_daily.ZONE)
    yesterday = datetime(2026, 11, 22, 12, tzinfo=report_daily.ZONE).timestamp()
    rule = {'id':1,'name':'日报更新','file_keyword':'日报','target_path':'/root/业务',
            'period_type':'daily','deadline_time':'10:00','responsible_person':'业务组'}
    evidence = {'state':'healthy','reason':'上个周期已更新','path':'/root/业务','latest_time':yesterday,
                'expected_at':yesterday,'today_updated':0}
    row = report_daily.row_for_rule(rule, evidence, '/root', now)
    assert row['status'] == 'pending' and row['deadline'] == '2026-11-23 10:00'
    evidence.update(latest_time=now.timestamp(), today_updated=1)
    row = report_daily.row_for_rule(rule, evidence, '/root', now)
    assert row['status'] == 'normal' and row['update_count'] == 1
