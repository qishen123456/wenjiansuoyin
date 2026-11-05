"""Demo pacing changes actual response duration, keeping compute metrics separate."""
from types import SimpleNamespace

import pytest
from test_product_foundation import foundation
import app as application


@pytest.mark.parametrize(
    "enabled,target,compute,expected",
    [(False, 2, .1, 0), (True, 2, .1, 1.9), (True, 2, 4, 0), (True, 0, .1, 0)],
)
def test_demo_pacing_only_waits_for_remaining_target(monkeypatch, enabled, target, compute, expected):
    clock = [10 + compute]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(application, "DEMO_MODE", enabled)
    monkeypatch.setattr(application, "DEMO_SEARCH_MIN_SECONDS", target)
    monkeypatch.setattr(application, "DEMO_SEARCH_MAX_SECONDS", target)
    monkeypatch.setattr(application, "time", SimpleNamespace(perf_counter=lambda: clock[0], sleep=sleep))
    assert application._pace_demo_search(10) == pytest.approx(expected * 1000)
    assert sum(waits) == pytest.approx(expected)


def test_search_response_and_telemetry_separate_demo_wait(foundation, monkeypatch):
    client, root, add = foundation
    fid = add("业务域/销售.csv")
    recorded = []
    monkeypatch.setattr(application, "DEMO_MODE", True)
    monkeypatch.setattr(application, "_ensure_demo_data", lambda *args, **kwargs: {})
    monkeypatch.setattr(application, "DEMO_SEARCH_MIN_SECONDS", .03)
    monkeypatch.setattr(application, "DEMO_SEARCH_MAX_SECONDS", .03)
    monkeypatch.setattr(application, "_record_product_event", lambda *args, **kwargs: recorded.append(kwargs))
    data = client.get("/api/search", params={"q": "销售"}).json()
    assert data["results"][0]["id"] == fid
    assert data["elapsed_ms"] >= 29
    assert data["demo_delay_ms"] > 0
    assert recorded[-1]["elapsed_ms"] == data["query_elapsed_ms"]
    assert data["query_elapsed_ms"] < data["elapsed_ms"]


def test_empty_search_does_not_wait(foundation, monkeypatch):
    client, _, _ = foundation
    def unexpected_wait(*args):
        pytest.fail("Empty search must not be paced")
    monkeypatch.setattr(application, "_pace_demo_search", unexpected_wait)
    assert client.get("/api/search", params={"q": ""}).json()["results"] == []


@pytest.mark.parametrize('enabled,compute,stopped,expected', [
    (True, 20, False, 100),
    (True, 140, False, 0),
    (False, 20, False, 0),
    (True, 20, True, 0),
])
def test_scan_wait_is_demo_only_and_interruptible(monkeypatch, enabled, compute, stopped, expected):
    clock = [10 + compute]
    waits = []
    states = []

    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(application, 'DEMO_MODE', enabled)
    monkeypatch.setattr(application, 'DEMO_SCAN_MIN_SECONDS', 120)
    monkeypatch.setattr(application, 'DEMO_SCAN_MAX_SECONDS', 120)
    monkeypatch.setattr(application, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    monkeypatch.setattr(application, '_auto_scan_stop', SimpleNamespace(is_set=lambda: stopped, wait=wait))
    monkeypatch.setattr(application, '_scan_state_update', lambda **kwargs: states.append(kwargs))
    assert application._pace_demo_scan(10) == expected
    assert sum(waits) == expected
    if expected:
        assert states[-1]['phase'] == 'demo_wait'


def test_scan_pacing_runs_after_database_transaction_closes(foundation, monkeypatch):
    import sqlite3
    client, root, add = foundation
    add('业务域/销售.csv')
    checked = []

    def pace(started):
        with sqlite3.connect(str(application.DB_PATH), timeout=.1) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        checked.append(True)
        return 1.0

    monkeypatch.setattr(application, '_pace_demo_scan', pace)
    application._scan_worker()
    state = application._scan_state_snapshot()
    assert checked == [True]
    assert state['phase'] == 'complete'
    assert state['running'] is False
    assert state['demo_delay_seconds'] == 1
    assert '含演示节奏等待' in state['message']


def test_partial_scan_reports_error_without_demo_wait(foundation, monkeypatch):
    _, root, add = foundation
    fid = add('业务域/销售.csv')
    monkeypatch.setattr(application.scan_pipeline, 'walk_metadata',
                        lambda *args, **kwargs: iter([('directory_error', str(root), 'denied')]))

    def unexpected_wait(*args):
        pytest.fail('Partial scans must return their error without pacing')

    monkeypatch.setattr(application, '_pace_demo_scan', unexpected_wait)
    application._scan_worker()
    assert application._scan_state_snapshot()['phase'] == 'partial'
    with application.db_session() as conn:
        assert conn.execute('SELECT id FROM files WHERE id=?', (fid,)).fetchone()
