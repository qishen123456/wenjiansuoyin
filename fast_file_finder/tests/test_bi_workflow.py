"""Integration tests for the search + lightweight BI workflow.

Run from project root with: pytest -q
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import app as application  # noqa: E402


@pytest.fixture()
def client_and_sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "share"
    root.mkdir()
    monkeypatch.setattr(application, "FILE_ROOT", str(root))
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(application, "ALLOWED_EXTENSIONS", {".xlsx", ".csv", ".tsv"})

    path = root / "供应商结算明细.xlsx"
    detail = pd.DataFrame(
        {
            "供应商": ["A", "A", "B", "B", "C", "A", "C", "B"],
            "月份": ["7月", "8月", "7月", "8月", "7月", "9月", "8月", "9月"],
            "结算金额": [100, 150, 200, 250, 80, 300, 120, 160],
            "区域": ["华东", "华东", "华南", "华南", "华东", "华东", "华东", "华南"],
        }
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        detail.to_excel(writer, index=False, sheet_name="结算明细")
        pd.DataFrame({"部门": ["采购", "销售"], "人数": [12, 23]}).to_excel(
            writer, index=False, sheet_name="部门信息"
        )

    application.init_db()
    with application.db_session() as conn:
        action = application.update_index_file(conn, path, path.stat(), root_key=application.root_scope_key(), scan_id="fixture-scan")
    assert action == "indexed"

    with TestClient(application.app) as client:
        yield client, path


def test_search_returns_indexed_file(client_and_sample):
    client, path = client_and_sample
    response = client.get("/api/search", params={"q": "供应商结算明细", "limit": 10})
    assert response.status_code == 200
    result = response.json()
    assert result["count"] >= 1
    assert result["results"][0]["path"] == str(path)
    assert result["elapsed_ms"] >= 0


def test_workbook_lists_sheets_and_switches_sheet(client_and_sample):
    client, _ = client_and_sample
    response = client.get("/api/analysis/file/1")
    assert response.status_code == 200
    assert response.json()["sheet_names"] == ["结算明细", "部门信息"]

    second = client.get("/api/analysis/file/1", params={"sheet_name": "部门信息"})
    assert second.status_code == 200
    assert second.json()["sheet_name"] == "部门信息"
    assert [c["name"] for c in second.json()["columns"]] == ["部门", "人数"]


def test_cross_tab_sum_and_chart(client_and_sample):
    client, _ = client_and_sample
    payload = {
        "file_id": 1,
        "sheet_name": "结算明细",
        "row_field": "供应商",
        "column_field": "月份",
        "value_fields": ["结算金额"],
        "aggregation": "sum",
        "filters": [],
        "max_categories": 30,
    }
    response = client.post("/api/analysis/run", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rows_read"] == 8
    assert result["rows_matched"] == 8
    assert result["pivot"]["headers"] == ["供应商", "7月", "8月", "9月"]
    rows = {row[0]: row[1:] for row in result["pivot"]["rows"]}
    assert rows["A"] == [100, 150, 300]
    assert rows["B"] == [200, 250, 160]
    assert rows["C"] == [80, 120, 0]
    assert result["chart"]["categories"] == ["A", "B", "C"]
    assert next(k["value"] for k in result["kpis"] if k["label"] == "结算金额合计") == 1360


def test_filter_aggregation_and_excel_export(client_and_sample):
    client, _ = client_and_sample
    payload = {
        "file_id": 1,
        "sheet_name": "结算明细",
        "row_field": "供应商",
        "value_fields": ["结算金额"],
        "aggregation": "sum",
        "filters": [{"field": "区域", "operator": "equals", "value": "华东"}],
        "max_categories": 30,
    }
    result = client.post("/api/analysis/run", json=payload)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["rows_matched"] == 5
    values = {row[0]: row[1] for row in data["pivot"]["rows"]}
    assert values == {"A": 550, "C": 200}

    exported = client.post("/api/analysis/export", json=payload)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert exported.content[:2] == b"PK"


def test_saved_analysis_round_trip(client_and_sample):
    client, _ = client_and_sample
    config = {"file_id": 1, "sheet_name": "结算明细", "row_field": "供应商"}
    saved = client.post(
        "/api/analysis/saved",
        json={"name": "供应商汇总", "file_id": 1, "config": config},
    )
    assert saved.status_code == 200
    saved_id = saved.json()["id"]
    listed = client.get("/api/analysis/saved", params={"file_id": 1})
    assert listed.status_code == 200
    assert listed.json()["results"][0]["config"] == config
    deleted = client.delete(f"/api/analysis/saved/{saved_id}")
    assert deleted.status_code == 200


def test_multiple_metrics_and_record_count_pivot(client_and_sample):
    client, _ = client_and_sample
    multi = client.post(
        "/api/analysis/run",
        json={
            "file_id": 1,
            "sheet_name": "结算明细",
            "row_field": "供应商",
            "column_field": "月份",
            "value_fields": ["结算金额", "区域"],
            "aggregation": "count",
            "filters": [],
            "max_categories": 30,
        },
    )
    assert multi.status_code == 200, multi.text
    result = multi.json()
    assert result["pivot"]["headers"][0] == "供应商"
    assert any(h.startswith("结算金额 / ") for h in result["pivot"]["headers"])
    assert any(h.startswith("区域 / ") for h in result["pivot"]["headers"])

    counts = client.post(
        "/api/analysis/run",
        json={
            "file_id": 1,
            "sheet_name": "结算明细",
            "row_field": "供应商",
            "column_field": "月份",
            "value_fields": [],
            "aggregation": "count",
            "filters": [],
            "max_categories": 30,
        },
    )
    assert counts.status_code == 200, counts.text
    count_result = counts.json()
    assert count_result["pivot"]["headers"] == ["供应商", "7月", "8月", "9月"]
    assert sum(sum(row[1:]) for row in count_result["pivot"]["rows"]) == 8



def test_unknown_sheet_is_rejected_instead_of_silently_using_first_sheet(client_and_sample):
    client, _ = client_and_sample
    response = client.get("/api/analysis/file/1", params={"sheet_name": "拼错的工作表"})
    assert response.status_code == 422
    assert "工作表不存在" in response.json()["detail"]


def test_search_is_scoped_to_current_file_root(client_and_sample, tmp_path, monkeypatch):
    client, _ = client_and_sample
    before = client.get("/api/search", params={"q": "供应商结算明细"}).json()
    assert before["count"] >= 1
    another_root = tmp_path / "other_share"
    another_root.mkdir()
    monkeypatch.setattr(application, "FILE_ROOT", str(another_root))
    after = client.get("/api/search", params={"q": "供应商结算明细"}).json()
    assert after["count"] == 0
    assert client.get("/api/recent").json()["results"] == []


def test_db_session_closes_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "close.db")
    with application.db_session() as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_save_analysis_rejects_oversized_config(client_and_sample):
    client, _ = client_and_sample
    response = client.post(
        "/api/analysis/saved",
        json={"name": "超大配置", "file_id": 1, "config": {"payload": "x" * 33000}},
    )
    assert response.status_code == 413


def test_scan_generation_prunes_removed_files_only_after_clean_scan(client_and_sample, tmp_path, monkeypatch):
    client, indexed_path = client_and_sample
    extra_path = indexed_path.parent / "临时文件.csv"
    extra_path.write_text("名称,金额\nA,1\n", encoding="utf-8")
    # First scan indexes a second file and creates a complete scan generation.
    application._scan_worker()
    indexed_count = client.get("/api/index/status").json()["indexed_files"]
    assert indexed_count == 2
    extra_path.unlink()
    application._scan_worker()
    status = client.get("/api/index/status").json()
    assert status["indexed_files"] == 1
    assert status["deleted"] == 1



def test_access_token_gate_is_optional_and_checks_header(client_and_sample, monkeypatch):
    client, _ = client_and_sample
    monkeypatch.setattr(application, "APP_ACCESS_TOKEN", "test-intranet-token")
    blocked = client.get("/api/index/status")
    assert blocked.status_code == 401
    allowed = client.get("/api/index/status", headers={"X-App-Token": "test-intranet-token"})
    assert allowed.status_code == 200


def test_symlink_outside_root_cannot_be_analyzed(client_and_sample, tmp_path):
    client, _ = client_and_sample
    outside = tmp_path / "outside.xlsx"
    pd.DataFrame({"secret": [123]}).to_excel(outside, index=False)
    root = Path(application.FILE_ROOT)
    link = root / "outside_link.xlsx"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available in this environment")
    with application.db_session() as conn:
        application.update_index_file(conn, link, link.stat(), root_key=application.root_scope_key(), scan_id="symlink-test")
    # The safety boundary resolves the link target, not just its lexical path.
    assert client.get("/api/analysis/file/2").status_code == 403


def test_empty_search_does_not_scan_or_crash(client_and_sample):
    client, _ = client_and_sample
    response = client.get("/api/search", params={"q": "   "})
    assert response.status_code == 200
    assert response.json()["count"] == 0



def test_date_range_filter_handles_csv_style_date_text(tmp_path, monkeypatch):
    frame = pd.DataFrame({"日期": ["2026-01-02", "2026-05-10", "2025-12-31"], "金额": [10, 20, 30]})
    result = application._apply_filters(
        frame,
        [application.AnalysisFilter(field="日期", operator="gte", value="2026-01-01")],
    )
    assert result["金额"].tolist() == [10, 20]



def test_legacy_database_schema_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(application, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL, parent TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, indexed_at REAL NOT NULL)")
    conn.commit()
    conn.close()
    application.init_db()
    with application.db_session() as migrated:
        columns = {row["name"] for row in migrated.execute("PRAGMA table_info(files)").fetchall()}
        version = migrated.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]
    assert {"root_key", "last_seen_scan"}.issubset(columns)
    assert version == "3"


def test_incomplete_scan_does_not_prune_missing_files(client_and_sample, monkeypatch):
    client, indexed_path = client_and_sample
    indexed_path.unlink()

    def incomplete_walk(root, followlinks=False, onerror=None):
        if onerror:
            onerror(PermissionError("simulated inaccessible directory"))
        yield str(root), [], []

    monkeypatch.setattr(application.os, "walk", incomplete_walk)
    application._scan_worker()
    status = client.get("/api/index/status").json()
    assert status["phase"] == "partial"
    assert status["indexed_files"] == 1  # The stale row is deliberately retained after an incomplete scan.
    assert "访问异常" in status["message"]



def test_numeric_aggregation_refuses_silent_partial_conversion(client_and_sample):
    client, path = client_and_sample
    df = pd.DataFrame({"项目": ["A", "B", "C"], "金额": [100, "待确认", 200]})
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="结算明细")
    with application.db_session() as conn:
        application.update_index_file(conn, path, path.stat(), root_key=application.root_scope_key(), scan_id="dirty-data")
    response = client.post(
        "/api/analysis/run",
        json={"file_id": 1, "sheet_name": "结算明细", "row_field": "项目", "value_fields": ["金额"], "aggregation": "sum"},
    )
    assert response.status_code == 422
    assert "无法转为数字" in response.json()["detail"]


def test_concurrent_scan_request_does_not_claim_a_second_scan(client_and_sample):
    client, _ = client_and_sample
    assert application._scan_lock.acquire(blocking=False)
    try:
        response = client.post("/api/index/start")
    finally:
        application._scan_lock.release()
    assert response.status_code == 200
    assert response.json()["started"] is False