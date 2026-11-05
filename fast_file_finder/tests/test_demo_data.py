"""Demo reports remain real, scoped and current without mutating user-owned files."""
import importlib.util
import json
from datetime import date, timedelta, datetime
from pathlib import Path
import pytest

import app as application
import demo_data
from test_product_foundation import foundation


@pytest.fixture
def demo(foundation,monkeypatch):
    client,root,add=foundation
    for domain in ['01-华东-自营','02-华南-经销','09-产品研发-新品简报']:
        (root/domain).mkdir()
    monkeypatch.setattr(application,'DEMO_MODE',True)
    # Data-generation tests exercise indexing, not presentation pacing.
    monkeypatch.setattr(application,'DEMO_SCAN_MAX_SECONDS',0)
    monkeypatch.setattr(application,'DEMO_SEARCH_MAX_SECONDS',0)
    monkeypatch.setattr(application,'_demo_state',{})
    return client,root,add


def count_for_day(day):
    start=datetime.combine(day,datetime.min.time()).timestamp()
    end=datetime.combine(day+timedelta(days=1),datetime.min.time()).timestamp()
    with application.db_session() as conn:
        return conn.execute('SELECT count(*) FROM files WHERE root_key=? AND mtime>=? AND mtime<?',(application.root_scope_key(),start,end)).fetchone()[0]


def test_daily_reports_are_real_nonempty_and_idempotent(demo):
    client,root,add=demo
    today=date.today()
    application._ensure_demo_data(today)
    reports=list(root.rglob('*.csv'))
    assert reports and count_for_day(today)>0
    assert all(path.stat().st_size>0 for path in reports)
    assert '模拟演示数据' in reports[0].read_text(encoding='utf-8-sig')
    timestamps={str(path):path.stat().st_mtime_ns for path in reports}
    application._demo_state.clear()  # Simulate restarting the service on the same day.
    application._ensure_demo_data(today)
    assert timestamps=={str(path):path.stat().st_mtime_ns for path in reports}
    assert application._demo_state['created']==0


def test_opening_api_after_midnight_refreshes_demo_and_prunes_old_reports(demo,monkeypatch):
    client,root,add=demo
    today=date.today()
    application._ensure_demo_data(today)
    oldest=[p for p in root.rglob('*.csv') if (today-timedelta(days=6)).isoformat() in p.name]
    assert oldest
    tomorrow=today+timedelta(days=1)
    class Tomorrow(date):
        @classmethod
        def today(cls): return tomorrow
    monkeypatch.setattr(application,'date',Tomorrow)
    assert client.get('/api/catalog/overview').status_code==200
    assert count_for_day(tomorrow)>0
    assert all(not p.exists() for p in oldest)
    with application.db_session() as conn:
        assert conn.execute('SELECT count(*) FROM files WHERE path=?',(str(oldest[0]),)).fetchone()[0]==0


def test_demo_scanner_excludes_only_named_unclassified_fill_in_folder(demo):
    client,root,add=demo
    add('01-华东-自营/'+demo_data.UNCLASSIFIED_DIRECTORY+'/空文件.csv',content='')
    keep=add('01-华东-自营/正常报表.csv')
    application._ensure_demo_data()
    application._scan_worker()
    with application.db_session() as conn:
        rows=conn.execute('SELECT id,parent FROM files').fetchall()
        assert keep in [r['id'] for r in rows]
        assert all(demo_data.UNCLASSIFIED_DIRECTORY not in r['parent'] for r in rows)


def test_refresh_does_not_overwrite_or_adopt_preexisting_reports(tmp_path):
    (tmp_path/'01-业务域').mkdir()
    today=date(2026,11,4)
    relative,_,_=next(demo_data.reports_for(tmp_path,today))
    original=tmp_path/relative
    original.parent.mkdir(parents=True)
    original.write_text('用户自有报表')
    result=demo_data.refresh(tmp_path,today,[])
    assert original.read_text()=='用户自有报表'
    assert relative not in result['paths']
    later=demo_data.refresh(tmp_path,today+timedelta(days=8),result['paths'])
    assert original.exists()


def test_demo_manifest_cannot_escape_root_or_follow_symlink(tmp_path):
    root=tmp_path/'share'; root.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    secret=outside/'渠道销售日报_2026-01-01.csv';secret.write_text('keep')
    domain=root/'01-业务域';domain.mkdir()
    (domain/demo_data.REPORT_DIRECTORY).symlink_to(outside,target_is_directory=True)
    result=demo_data.refresh(root,date(2026,11,4),['../outside/渠道销售日报_2026-01-01.csv','01-业务域/每日经营报表/渠道销售日报_2026-01-01.csv'])
    assert secret.read_text()=='keep'
    assert list(outside.iterdir())==[secret]
    assert result['paths']==[]


def load_cleanup():
    path=Path(__file__).resolve().parents[2]/'scripts'/'cleanup_demo_unclassified.py'
    spec=importlib.util.spec_from_file_location('demo_cleanup',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_cleanup_preserves_recovery_copy_and_manifest(tmp_path,monkeypatch):
    cleanup=load_cleanup()
    root=tmp_path/'share';root.mkdir()
    folder=root/'01-业务'/cleanup.NAME;folder.mkdir(parents=True)
    (folder/'占位.csv').touch()
    normal=root/'正常.csv';normal.write_text('keep')
    quarantine=tmp_path/'recovery'
    monkeypatch.setattr('sys.argv',['cleanup','--root',str(root),'--quarantine',str(quarantine),'--apply'])
    cleanup.main()
    assert not folder.exists() and normal.read_text()=='keep'
    assert (quarantine/'01-业务'/cleanup.NAME/'占位.csv').exists()
    assert json.loads((quarantine/'manifest.json').read_text())['moved']==['01-业务/'+cleanup.NAME]


def test_cleanup_refuses_nonempty_files(tmp_path):
    cleanup=load_cleanup()
    folder=tmp_path/cleanup.NAME;folder.mkdir()
    (folder/'业务.csv').write_text('do not delete')
    with pytest.raises(ValueError,match='非空文件'):
        cleanup.plan(tmp_path)
    assert (folder/'业务.csv').read_text()=='do not delete'


def test_demo_generation_respects_excluded_domains_and_disabled_csv(tmp_path):
    (tmp_path/'01-财务薪资').mkdir()
    (tmp_path/'02-销售').mkdir()
    result=demo_data.refresh(tmp_path,date(2026,11,4),[],excluded_terms=['薪资'])
    assert result['paths'] and all(path.startswith('02-销售/') for path in result['paths'])
    assert not list((tmp_path/'01-财务薪资').rglob('*.csv'))
    stopped=demo_data.refresh(tmp_path,date(2026,11,4),result['paths'],enabled=False)
    assert not stopped['paths'] and not list(tmp_path.rglob('*.csv'))
