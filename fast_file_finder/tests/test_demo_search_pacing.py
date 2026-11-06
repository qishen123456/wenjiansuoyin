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


@pytest.mark.parametrize('enabled,compute,expected', [(True, 1, 6.5), (True, 12, 0), (False, 1, 0)])
def test_ai_demo_pacing_uses_its_own_range(monkeypatch, enabled, compute, expected):
    clock = [10 + compute]
    ranges = []
    waits = []

    def sample(low, high):
        ranges.append((low, high))
        return 7.5

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(application, 'DEMO_MODE', enabled)
    monkeypatch.setattr(application, 'DEMO_AI_MIN_SECONDS', 5)
    monkeypatch.setattr(application, 'DEMO_AI_MAX_SECONDS', 10)
    monkeypatch.setattr(application.random, 'uniform', sample)
    monkeypatch.setattr(application, 'time', SimpleNamespace(perf_counter=lambda: clock[0], sleep=sleep))
    assert application._pace_demo_ai(10) == expected * 1000
    assert sum(waits) == expected
    assert ranges == ([(5, 10)] if enabled else [])


@pytest.mark.parametrize('dated', [True, False])
def test_ai_chat_waits_outside_db_for_both_reply_paths(foundation, monkeypatch, dated):
    import sqlite3
    client, _, _ = foundation
    reply = {'ok': True, 'reply': '本地检索结果', 'model_used': '本地索引',
             'matched_files': [], 'suggested_terms': []}
    monkeypatch.setattr(application, 'answer_dated_file_request', lambda *args: dict(reply) if dated else None)
    monkeypatch.setattr(application, 'ai_guess_and_supplement', lambda *args: {
        'reasoning': reply['reply'], 'model_used': reply['model_used'],
        'matched_files': [], 'suggested_terms': []})
    calls = []

    def pace(started):
        with sqlite3.connect(str(application.DB_PATH), timeout=.1) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        calls.append(started)
        return 5000

    monkeypatch.setattr(application, '_pace_demo_ai', pace)
    data = client.post('/api/ai/chat', json={'message': '今天的新品简报'}).json()
    assert data['reply'] == reply['reply']
    assert data['demo_delay_ms'] == 5000
    assert data['response_elapsed_ms'] >= 0
    assert len(calls) == 1


@pytest.mark.parametrize('message', ['在吗', '你好！', ' Hello? '])
def test_greetings_reply_locally_and_still_use_demo_pacing(foundation, monkeypatch, message):
    client, _, _ = foundation
    def unexpected_model(*args, **kwargs):
        pytest.fail('Greeting must not invoke the model or search')
    monkeypatch.setattr(application, 'call_llm', unexpected_model)
    monkeypatch.setattr(application, 'ai_guess_and_supplement', unexpected_model)
    monkeypatch.setattr(application, 'answer_dated_file_request', unexpected_model)
    monkeypatch.setattr(application, '_pace_demo_ai', lambda started: 5000)
    result = client.post('/api/ai/chat', json={'message': message}).json()
    assert result['reply'].startswith('在的。')
    assert result['model_used'] == '本地对话引导'
    assert result['matched_files'] == []
    assert result['demo_delay_ms'] == 5000
