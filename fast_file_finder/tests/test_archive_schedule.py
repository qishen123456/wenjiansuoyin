from datetime import date, datetime
import os
import pytest
from test_product_foundation import foundation, login, rule_payload
import app as application
import archive_schedule as schedule


def config(**kw): return schedule.Schedule(mode='calendar',**kw)
def due(period, kind, **kw): return schedule.occurrence(date.fromisoformat(period),kind,'10:30',config(**kw))
def test_daily_same_or_next_day_and_weekly_next_tuesday():
    assert due('2026-11-04','daily',due_period='same')['deadline_display']=='2026-11-04 10:30'
    assert due('2026-11-04','daily',due_period='next')['deadline_display']=='2026-11-05 10:30'
    row=due('2026-11-02','weekly',due_period='next',due_day=2)
    assert row['period_end']=='2026-11-08'
    assert row['deadline_display']=='2026-11-10 10:30'
    assert datetime.fromtimestamp(row['window_start'],schedule.ZONE).date()==date(2026,11,9)


def test_monthly_sixth_next_month_and_short_month_end():
    assert due('2026-10-01','monthly',due_day=6)['deadline_display']=='2026-11-06 10:30'
    assert due('2026-01-01','monthly',due_day=31)['deadline_display']=='2026-02-28 10:30'
    assert due('2026-12-01','monthly',due_day=6)['deadline_display']=='2027-01-06 10:30'


def test_holiday_rollforward_respects_makeup_workdays_and_nth_workday():
    row=due('2026-09-01','monthly',due_day=6,holiday_policy='next_workday')
    assert row['original_date']=='2026-10-06'
    assert row['deadline_display']=='2026-10-08 10:30' and row['adjusted']
    assert due('2026-09-01','monthly',due_day=6,day_basis='workday')['deadline_display']=='2026-10-14 10:30'
    assert schedule.workday(date(2026,10,10),config())
    assert not schedule.workday(date(2026,10,10),config(calendar='weekend'))


def test_keep_skip_company_overrides_and_workday_grace():
    assert due('2026-09-01','monthly',due_day=6,holiday_policy='keep')['deadline_display']=='2026-10-06 10:30'
    assert due('2026-09-01','monthly',due_day=6,holiday_policy='skip')['skipped']
    row=due('2026-11-06','daily',due_period='same',day_basis='workday',grace_days=2,
            extra_holidays=['2026-11-09'],extra_workdays=['2026-11-07'])
    assert row['deadline_display']=='2026-11-10 10:30'
    with pytest.raises(ValueError):config(extra_holidays=['2026-11-06'],extra_workdays=['2026-11-06'])


def test_unknown_holiday_year_is_not_silently_treated_as_weekdays():
    result=schedule.timeline('monthly','18:00',config(holiday_policy='next_workday'),datetime(2027,1,8,tzinfo=schedule.ZONE))
    assert result['calendar_error'] and result['warnings']
    assert not schedule.timeline('monthly','18:00',config(calendar='weekend',holiday_policy='next_workday'),datetime(2027,1,8,tzinfo=schedule.ZONE))['calendar_error']


def test_schedule_preview_crud_and_preflight(foundation):
    client,root,add=foundation
    add('业务域/报表.csv')
    login(client)
    setting=config(due_day=6,holiday_policy='next_workday',grace_days=1).model_dump(mode='json')
    response=client.post('/api/archive/rules/schedule-preview',json={'period_type':'monthly','schedule':setting})
    assert response.status_code==200 and response.json()['upcoming']
    payload=rule_payload(root,period_type='monthly',schedule=setting)
    result=client.post('/api/archive/rules',json=payload)
    assert result.status_code==200,result.text
    rule_id=result.json()['id']
    row=next(r for r in client.get('/api/archive/rules').json()['rules'] if r['id']==rule_id)
    assert row['schedule']==setting and row['assessment']['schedule']
    payload['schedule']['due_day']=5
    assert client.put(f'/api/archive/rules/{rule_id}',json=payload).status_code==200
    assert client.get('/api/archive/rules').json()['rules'][-1]['schedule']['due_day']==5
    assert client.get('/api/archive/rules/preflight',params={'path':str(root/'业务域'),'period_type':'monthly','schedule_config':'{"due_day":0}'}).status_code==422
    assert client.post('/api/archive/rules',json={**payload,'period_type':'custom'}).status_code==422


def test_adjusted_deadline_drives_scheduler_overdue_and_resolution(foundation,monkeypatch):
    client,root,add=foundation
    # September's report is due Oct 6, postponed through the National Day break to Oct 8.
    add('业务域/报表.csv')
    path=root/'业务域/报表.csv'
    def stamp(text):
        dt=datetime.fromisoformat(text).replace(tzinfo=schedule.ZONE)
        os.utime(path,(dt.timestamp(),dt.timestamp()))
        with application.db_session() as conn:
            application.update_index_file(conn,path,path.stat(),root_key=application.root_scope_key(),scan_id='schedule')
    stamp('2026-09-09T12:00:00') # delivered August's report, not September's.
    rule={'target_path':str(root/'业务域'),'period_days':30,'period_type':'monthly','deadline_time':'10:30',
          'schedule_config':config(due_day=6,holiday_policy='next_workday').model_dump_json()}
    with application.db_session() as conn:
        before=application._assess_archive_rule(conn,rule,datetime(2026,10,7,12,tzinfo=schedule.ZONE).timestamp())
        after=application._assess_archive_rule(conn,rule,datetime(2026,10,8,12,tzinfo=schedule.ZONE).timestamp())
    assert before['state']=='healthy'
    assert after['state']=='overdue'
    assert after['expected_at_display']=='2026-10-08 10:30'
    stamp('2026-10-08T13:00:00')
    with application.db_session() as conn:
        fixed=application._assess_archive_rule(conn,rule,datetime(2026,10,8,14,tzinfo=schedule.ZONE).timestamp())
    assert fixed['state']=='healthy'
