"""Integration tests for the search + lightweight BI workflow.

Run from project root with: pytest -q
"""
from __future__ import annotations

import sys
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
    with application.db_connect() as conn:
        action = application.update_index_file(conn, path, path.stat())
        conn.commit()
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