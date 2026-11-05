"""Evidence, actor identity, reconciliation and privacy boundaries for phase zero."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application
import governance


@pytest.fixture
def foundation(tmp_path, monkeypatch):
    root = tmp_path / "share"
    root.mkdir()
    monkeypatch.setattr(application, "FILE_ROOT", str(root))
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(application, "APP_ACCESS_TOKEN", "")
    monkeypatch.setattr(application, "AUTH_REQUIRED", False)
    monkeypatch.setattr(application, "DEMO_MODE", False)
    monkeypatch.setattr(application, "DIRECTORY_ACL_ENABLED", False)
    monkeypatch.setattr(application, "SENSITIVE_DIRS", [])
    monkeypatch.setattr(application, "ALLOWED_EXTENSIONS", {".csv"})
    application.init_db()
    with application.db_session() as conn:
        conn.execute("DELETE FROM archive_tasks")
        conn.execute("DELETE FROM archive_rules")

    def add(relative="业务域/报表.csv", content="月份,金额\n11月,100\n", age=0):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        if age:
            os.utime(path, (time.time() - age, time.time() - age))
        with application.db_session() as conn:
            application.update_index_file(conn, path, path.stat(), root_key=application.root_scope_key(), scan_id="fixture")
            return conn.execute("SELECT id FROM files WHERE path=?", (str(path),)).fetchone()[0]

    with TestClient(application.app) as client:
        with application.db_session() as conn:
            conn.execute("DELETE FROM archive_tasks")
            conn.execute("DELETE FROM archive_rules")
        yield client, root, add


def login(client, admin=True):
    result = client.post("/api/auth/login", json={"phone": "13800100001" if admin else "13800100004", "password": "admin123" if admin else "123456"})
    assert result.status_code == 200
    return result.json()["user"]


def rule_payload(root, directory="业务域", **overrides):
    return {"name": "测试规则", "target_path": str(root / directory), "period_type": "weekly", "period_days": 7, "responsible_person": "运营组", **overrides}


def create_overdue_task(client, root, add):
    add(age=86400 * 20)
    login(client)
    result = client.post("/api/archive/rules", json=rule_payload(root))
    assert result.status_code == 200
    task = client.get("/api/archive/tasks").json()["items"][0]
    return task["id"]


def test_admin_required_for_rules_and_baseline_even_in_development_mode(foundation):
    client, root, add = foundation
    add()
    assert client.post("/api/archive/rules", json=rule_payload(root)).status_code == 401
    assert client.get("/api/governance/summary").status_code == 401
    login(client, admin=False)
    assert client.post("/api/archive/rules", json=rule_payload(root)).status_code == 403
    assert client.post("/api/archive/check").status_code == 403
    assert client.get("/api/archive/rules/preflight", params={"path": str(root)}).status_code == 403
    assert client.get("/api/governance/summary").status_code == 403


def test_rule_preflight_blocks_missing_paths_and_blank_responsibility(foundation):
    client, root, add = foundation
    add()
    login(client)
    preflight = client.get("/api/archive/rules/preflight", params={"path": str(root / "不存在")}).json()
    assert not preflight["can_save"] and preflight["state"] == "no_files"
    assert client.post("/api/archive/rules", json=rule_payload(root, "不存在")).status_code == 422
    assert client.post("/api/archive/rules", json=rule_payload(root, responsible_person="  ")).status_code == 422
    assert client.post("/api/archive/rules", json=rule_payload(root, target_path=str(root.parent / "outside"))).status_code == 422
    accepted = client.post("/api/archive/rules", json=rule_payload(root, target_path="/data/share/业务域", period_type="daily", period_days=99))
    assert accepted.status_code == 200
    saved = client.get("/api/archive/rules").json()["rules"][0]
    assert saved["target_path"] == str(root / "业务域") and saved["period_days"] == 1


def test_zero_byte_files_neither_close_nor_create_overdue_tasks(foundation):
    client, root, add = foundation
    add(content="")
    login(client)
    result = client.post("/api/archive/rules", json=rule_payload(root))
    assert result.status_code == 200
    assert client.get("/api/archive/tasks").json()["total"] == 0
    overview = client.get("/api/archive/overview").json()
    assert overview["compliance_rate"] is None and overview["verifiable_rules"] == 0
    assert overview["rule_issues"][0]["state"] == "no_content"
    check = client.post("/api/archive/check").json()["result"]
    assert check["tasks_auto_resolved"] == check["tasks_created"] == 0


def test_empty_newer_file_does_not_hide_old_nonempty_report(foundation):
    client, root, add = foundation
    add("业务域/原报表.csv", age=86400 * 20)
    add("业务域/新占位.csv", content="")
    login(client)
    client.post("/api/archive/rules", json=rule_payload(root))
    task = client.get("/api/archive/tasks").json()["items"][0]
    assert task["last_file_name"] == "原报表.csv" and task["status"] == "pending"
    assert task["assessment"]["state"] == "overdue"
    add("业务域/最新报表.csv")
    client.post("/api/archive/check")
    task = client.get("/api/archive/tasks").json()["items"][0]
    assert task["status"] == "auto_resolved"


def test_current_rule_rate_counts_rules_not_task_history(foundation):
    client, root, add = foundation
    add("正常/报表.csv")
    add("逾期/报表.csv", age=86400 * 20)
    add("占位/报表.csv", content="")
    login(client)
    for directory in ["正常", "逾期", "占位"]:
        assert client.post("/api/archive/rules", json=rule_payload(root, directory)).status_code == 200
    result = client.get("/api/archive/overview").json()
    assert result["total_rules"] == 3
    assert result["verifiable_rules"] == 2 and result["healthy_rules"] == 1
    assert result["compliance_rate"] == 50
    assert len(result["rule_issues"]) == 1


def test_archive_actor_cannot_be_forged_and_review_requires_real_role(foundation):
    client, root, add = foundation
    task_id = create_overdue_task(client, root, add)
    worker = login(client, admin=False)
    submitted = client.post(f"/api/archive/tasks/{task_id}/submit", json={"note": "例外交付", "proof_image": "data:image/png;base64,AAAA", "submitted_by": "伪造管理员"})
    assert submitted.status_code == 200
    assert client.post(f"/api/archive/tasks/{task_id}/review", json={"action": "approve", "reviewed_by": "超级管理员"}).status_code == 403
    admin = login(client)
    assert client.post(f"/api/archive/tasks/{task_id}/review", json={"action": "approve", "reviewed_by": "伪造审核人"}).status_code == 200
    assert client.post(f"/api/archive/tasks/{task_id}/review", json={"action": "reject"}).status_code == 409
    assert client.post(f"/api/archive/tasks/{task_id}/submit", json={"note": "重复", "proof_image": "image"}).status_code == 409
    task = client.get("/api/archive/tasks").json()["items"][0]
    assert task["submitted_by"] == worker["name"] and task["reviewed_by"] == admin["name"]
    with application.db_session() as conn:
        actors = [dict(row) for row in conn.execute("SELECT event_type,actor_id FROM product_events WHERE event_type IN ('archive_submit','archive_review')")]
    assert actors == [{"event_type": "archive_submit", "actor_id": worker["id"]}, {"event_type": "archive_review", "actor_id": admin["id"]}]


def test_scan_reconciliation_is_persisted_and_explains_exclusions(foundation):
    client, root, add = foundation
    add()
    (root / "~$临时.csv").write_text("lock")
    (root / "说明.txt").write_text("unsupported")
    (root / "link.csv").symlink_to(root / "业务域/报表.csv")
    application._scan_worker()
    status = client.get("/api/index/status").json()
    run = status["scan_history"][0]
    counts = run["counts"]
    assert run["phase"] == "complete" and counts["reconciled"]
    assert counts["discovered"] == 4 and counts["seen"] == 1 and counts["excluded"] == 3
    assert counts["exclusion_reasons"] == {"unsupported_format": 1, "symbolic_link": 1, "temporary": 1}
    application._scan_state_update(phase="idle", seen=0)
    assert client.get("/api/index/status").json()["scan_history"][0]["counts"]["seen"] == 1


def test_partial_scan_keeps_old_index_and_records_unknown_directory_scope(foundation, monkeypatch):
    client, root, add = foundation
    file_id = add()
    def denied_scandir(_root):
        raise PermissionError("目录不可访问")
    monkeypatch.setattr(application.os, "scandir", denied_scandir)
    application._scan_worker()
    status = client.get("/api/index/status").json()
    assert status["indexed_files"] == 1
    run = status["scan_history"][0]
    assert run["phase"] == "partial" and run["counts"]["directory_errors"] == 1
    assert run["counts"]["discovered"] == 0


def test_product_baseline_links_only_real_results_in_same_session(foundation):
    client, root, add = foundation
    file_id = add("业务域/供应商结算.csv")
    other_file = add("另一个域/库存.csv")
    login(client)
    headers = {"X-Product-Session": "session_test_1234"}
    search = client.get("/api/search", params={"q": "供应商结算"}, headers=headers).json()
    search_id = search["search_event_id"]
    assert search_id and search["count"] == 1
    wrong = {"event_type": "preview_open", "file_id": other_file, "search_event_id": search_id}
    assert client.post("/api/telemetry/events", json=wrong, headers=headers).status_code == 200
    valid = {"event_type": "path_copy", "file_id": file_id, "search_event_id": search_id}
    client.post("/api/telemetry/events", json=valid, headers={"X-Product-Session": "different_session_5678"})
    summary = client.get("/api/governance/summary").json()["product"]
    assert summary["searches"] == 1 and summary["action_searches"] == 0
    client.post("/api/telemetry/events", json=valid, headers=headers)
    summary = client.get("/api/governance/summary").json()["product"]
    assert summary["action_searches"] == 1 and summary["action_search_rate"] == 100
    assert summary["first_action_p50_seconds"] >= 0
    with application.db_session() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_events)")}
        assert not columns & {"query", "path", "password", "content"}
        searches = conn.execute("SELECT query_length,result_count FROM product_events WHERE id=?", (search_id,)).fetchone()
        assert searches["query_length"] == 5 and searches["result_count"] == 1
        serialized = repr([tuple(row) for row in conn.execute("SELECT * FROM product_events")])
        assert "供应商结算" not in serialized and str(root) not in serialized


def test_expired_actor_and_no_data_metrics_are_honest(foundation):
    client, root, add = foundation
    user = login(client)
    summary = client.get("/api/governance/summary").json()["product"]
    assert summary["searches"] == 0 and summary["zero_result_rate"] is None
    assert summary["search_p95_ms"] is None and summary["action_search_rate"] is None
    with application.db_session() as conn:
        conn.execute("UPDATE login_sessions SET expires_at=? WHERE user_id=?", (time.time() - 1, user["id"]))
    assert client.get("/api/governance/summary").status_code == 401


def test_account_management_cannot_bypass_admin_role_in_development_mode(foundation):
    client, root, add = foundation
    assert client.get('/api/system/permissions').status_code == 401
    worker = login(client, admin=False)
    assert client.get('/api/system/permissions').status_code == 403
    assert client.put(f"/api/system/permissions/users/{worker['id']}/role", json={"role": "super_admin"}).status_code == 403
    assert client.put(f"/api/system/permissions/users/{worker['id']}/password", json={"password": "changed"}).status_code == 403
    login(client)
    users = client.get('/api/system/permissions').json()['users']
    assert users and all('password' not in user for user in users)


def test_excluded_directories_are_not_claimed_as_known_file_totals(foundation, monkeypatch):
    client, root, add = foundation
    add()
    add('敏感目录/内部.csv')
    monkeypatch.setattr(application, 'SENSITIVE_DIRS', ['敏感目录'])
    application._scan_worker()
    run = client.get('/api/index/status').json()['scan_history'][0]
    assert run['counts']['excluded_directories'] == 1
    assert run['counts']['discovered'] == 1
    assert run['counts']['reconciled']


def test_access_logs_redact_queries_and_download_tickets():
    import logging
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 0, '%s - "%s %s HTTP/%s" %s', ('127.0.0.1', 'GET', '/api/search?q=内部经营数据&ticket=secret', '1.1', 200), None)
    assert governance.RedactQueryStringFilter().filter(record)
    message = record.getMessage()
    assert '/api/search?[redacted]' in message
    assert '内部经营数据' not in message and 'secret' not in message


def test_event_retention_prunes_related_results_without_deleting_recent_events(foundation):
    client, root, add = foundation
    with application.db_session() as conn:
        old = governance.record_event(conn, root_key=application.root_scope_key(), event_type='search', actor_id=None, actor_role='anonymous', session_id='old_session', occurred_at=time.time()-86400*100)
        recent = governance.record_event(conn, root_key=application.root_scope_key(), event_type='search', actor_id=None, actor_role='anonymous', session_id='new_session')
        conn.execute('INSERT INTO search_event_files VALUES (?,?)', (old, 123))
        governance.prune_events(conn, force=True)
        assert [row['id'] for row in conn.execute('SELECT id FROM product_events')] == [recent]
        assert conn.execute('SELECT count(*) FROM search_event_files').fetchone()[0] == 0
