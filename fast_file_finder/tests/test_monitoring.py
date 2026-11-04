"""Report-specific daily delivery checks and access-safe task drilldown."""
from datetime import datetime, timezone
import os

import pytest

import app as application
import demo_data
from test_product_foundation import foundation, login


def stamp(day=4,hour=12,minute=0,second=0):
    return datetime(2026,11,day,hour,minute,second,tzinfo=demo_data.ZONE).timestamp()


@pytest.fixture
def monitoring(foundation,monkeypatch):
    client,root,add=foundation
    monkeypatch.setattr(application.time,'time',lambda:stamp())
    def add_at(name,when,content='日期,金额\n2026-11-03,100\n'):
        fid=add('业务域/'+name,content=content)
        path=root/'业务域'/name
        os.utime(path,(when,when))
        with application.db_session() as conn:
            application.update_index_file(conn,path,path.stat(),root_key=application.root_scope_key(),scan_id='fixture')
        return fid
    return client,root,add_at


def payload(root,**overrides):
    return dict(name='销售日报更新监控',target_path=str(root/'业务域'),period_type='daily',period_days=1,
                responsible_person='销售运营组',file_keyword='销售日报',deadline_time='10:00',**overrides)


def assess(root,now=stamp(),**changes):
    with application.db_session() as conn:
        return application._archive_rule_evidence(conn,str(root/'业务域'),1,now,
            file_keyword='销售日报',period_type='daily',deadline_time='10:00',**changes)


def test_unrelated_updates_do_not_close_named_report_monitor(monitoring):
    c,root,add=monitoring
    add('销售日报_昨日.csv',stamp(3,23,50))
    add('其他费用明细_今天.csv',stamp(4,11,30))
    login(c)
    assert c.post('/api/archive/rules',json=payload(root)).status_code==200
    task=c.get('/api/archive/tasks?status=pending').json()['items'][0]
    assert task['assessment']['reason_title']=='今天应更新的数据尚未更新'
    assert task['assessment']['latest_name']=='销售日报_昨日.csv'
    assert task['assessment']['expected_at_display']=='2026-11-04 10:00'
    assert task['assessment']['overdue_seconds']==7200
    add('销售日报_今天.csv',stamp(4,9,45))
    c.post('/api/archive/check')
    detail=c.get(f"/api/archive/tasks/{task['id']}").json()['task']
    assert detail['status']=='auto_resolved'
    assert detail['assessment']['state']=='healthy'


def test_calendar_deadline_not_rolling_24_hour_window(monitoring):
    c,root,add=monitoring
    add('销售日报_昨日.csv',stamp(3,23,50))
    assert assess(root,stamp(4,9,59,59))['state']=='healthy'
    at_deadline=assess(root,stamp(4,10))
    assert at_deadline['state']=='overdue'
    assert at_deadline['expected_at_display']=='2026-11-04 10:00'
    assert at_deadline['window_start']==stamp(4,0)
    # Epochs represent the same deadline independent of the host timezone.
    utc_deadline=datetime(2026,11,4,2,tzinfo=timezone.utc).timestamp()
    assert assess(root,utc_deadline)['state']=='overdue'


def test_empty_delivery_has_specific_reason_and_preserves_last_valid_file(monitoring):
    c,root,add=monitoring
    add('销售日报_昨日.csv',stamp(3,16))
    add('销售日报_今天.csv',stamp(4,9,30),content='')
    result=assess(root)
    assert result['state']=='overdue' and result['reason_code']=='empty_delivery'
    assert result['empty_due_files']==1
    assert result['latest_name']=='销售日报_昨日.csv'
    assert '空' in result['reason_title']


def test_future_dated_report_cannot_satisfy_current_delivery(monitoring):
    c,root,add=monitoring
    add('销售日报_昨日.csv',stamp(3,16))
    add('销售日报_明天.csv',stamp(5,9))
    result=assess(root)
    assert result['state']=='overdue' and result['latest_time']==stamp(3,16)


def test_rule_fields_roundtrip_and_preflight_use_same_report_filter(monitoring):
    c,root,add=monitoring
    add('销售日报.csv',stamp(3,16))
    add('库存日报.csv',stamp(4,11))
    login(c)
    checked=c.get('/api/archive/rules/preflight',params=dict(path=str(root/'业务域'),period_days=1,
                    period_type='daily',file_keyword='销售日报',deadline_time='10:00')).json()
    assert checked['state']=='overdue' and checked['matched_files']==1
    res=c.post('/api/archive/rules',json=payload(root));assert res.status_code==200
    rules=c.get('/api/archive/rules').json()['rules']
    assert rules[0]['file_keyword']=='销售日报' and rules[0]['deadline_time']=='10:00'
    changed=payload(root);changed.update(file_keyword='库存日报',deadline_time='11:30')
    assert c.put(f"/api/archive/rules/{res.json()['id']}",json=changed).status_code==200
    rule=c.get('/api/archive/rules').json()['rules'][0]
    assert rule['assessment']['state']=='healthy' and rule['deadline_time']=='11:30'
    changed['deadline_time']='25:30'
    assert c.put(f"/api/archive/rules/{res.json()['id']}",json=changed).status_code==422


def test_task_details_do_not_depend_on_current_list_page_and_respect_acl(monitoring,monkeypatch):
    c,root,add=monitoring
    add('销售日报.csv',stamp(2,16))
    login(c)
    for index in range(3):
        body=payload(root);body['name']=f'监控{index}'
        assert c.post('/api/archive/rules',json=body).status_code==200
    tasks=c.get('/api/archive/tasks?page_size=100').json()['items']
    target=tasks[-1]['id']
    response=c.get(f'/api/archive/tasks/{target}')
    assert response.status_code==200 and response.json()['task']['id']==target
    monkeypatch.setattr(application,'DIRECTORY_ACL_ENABLED',True)
    login(c,admin=False)
    response=c.get(f'/api/archive/tasks/{target}')
    assert response.status_code==404 and '销售日报' not in response.text


def test_varied_demo_domains_report_types_times_and_exception_scenarios(tmp_path):
    for code in demo_data.PROFILES:(tmp_path/f'{code}-业务域').mkdir()
    day=datetime(2026,11,4).date()
    records=list(demo_data.reports_for(tmp_path,day))
    counts={}
    for path,content,mtime in records:
        if day.isoformat() in path:counts[path[:2]]=counts.get(path[:2],0)+1
    assert len(set(counts.values()))>=6
    assert counts['02']>counts.get('21',0)
    headers={content.decode('utf-8-sig').splitlines()[0] for _,content,_ in records if content}
    assert len(headers)>=5
    assert len({mtime%86400 for _,_,mtime in records})>10
    assert not any(path.startswith('09-') and '新品上市简报_'+day.isoformat() in path for path,_,_ in records)
    assert not any(path.startswith('04-') and '经销回款台账_'+day.isoformat() in path for path,_,_ in records)
    assert any(path.startswith('12-') and '渠道投放日报_'+day.isoformat() in path and not content for path,content,_ in records)
    assert any(path.startswith('23-') and '全渠道销售日报_'+day.isoformat() in path and content for path,content,_ in records)


def test_preflight_normalizes_standard_frequency_like_rule_save(monitoring):
    c,root,add=monitoring
    add('销售日报_历史.csv',stamp()-20*86400)
    login(c)
    check=c.get('/api/archive/rules/preflight',params=dict(path=str(root/'业务域'),period_type='weekly',period_days=99,file_keyword='销售日报')).json()
    assert check['state']=='overdue'
    body=payload(root);body.update(period_type='weekly',period_days=99)
    assert c.post('/api/archive/rules',json=body).status_code==200
    rule=c.get('/api/archive/rules').json()['rules'][0]
    assert rule['period_days']==7 and rule['assessment']['state']==check['state']
