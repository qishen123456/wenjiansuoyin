"""Catalog paths must follow the configured root and preserve subtree boundaries."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as application


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    root = tmp_path / "企业网盘"
    root.mkdir()
    monkeypatch.setattr(application, "FILE_ROOT", str(root))
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(application, "APP_ACCESS_TOKEN", "")
    monkeypatch.setattr(application, "AUTH_REQUIRED", False)
    monkeypatch.setattr(application, "DEMO_MODE", False)
    monkeypatch.setattr(application, "DIRECTORY_ACL_ENABLED", False)
    application.init_db()

    def add(relative, *, scope=None, mtime=None):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("月份,金额\n11月,100\n")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        with application.db_session() as conn:
            application.update_index_file(conn, path, path.stat(), root_key=scope or application.root_scope_key(), scan_id="fixture")
        return path

    with TestClient(application.app) as client:
        yield client, root, add


def walk_nodes(node):
    yield node
    for child in node["children"]:
        yield from walk_nodes(child)


def test_tree_counts_match_every_directory_and_deep_descendants(catalog):
    client, root, add = catalog
    add("01-业务域/本层.csv")
    add("01-业务域/2026年/11月/明细.csv")
    add("01-业务域/" + "/".join(f"层{i}" for i in range(15)) + "/深层.csv")
    add("02-业务域/周报.csv")
    tree = client.get("/api/catalog/tree").json()["tree"]
    assert tree["path"] == root.as_posix()
    assert tree["file_count"] == 4
    assert max(node["depth"] for node in walk_nodes(tree)) == 16
    for node in walk_nodes(tree):
        result = client.get("/api/catalog/files", params={"path": node["path"]})
        assert result.status_code == 200
        assert result.json()["total"] == node["file_count"]
    assert client.get("/api/catalog/files").json()["total"] == 4


def test_tree_overview_dashboard_and_files_ignore_other_root(catalog):
    client, root, add = catalog
    add("01-业务域/本层.csv")
    add("99-历史目录/旧文件.csv", scope="/old/share")
    assert client.get("/api/catalog/tree").json()["tree"]["file_count"] == 1
    overview = client.get("/api/catalog/overview").json()
    dashboard = client.get("/api/dashboard/summary").json()
    assert overview["total_files"] == dashboard["total_files"] == 1
    assert [d["name"] for d in overview["departments"]] == ["01-业务域"]
    assert [d["name"] for d in dashboard["departments"]] == ["01-业务域"]
    assert client.get("/api/catalog/files").json()["total"] == 1


def test_directory_wildcards_are_literal_and_siblings_do_not_leak(catalog):
    client, root, add = catalog
    wanted = add("目录_100%/下级/报表_100%.csv")
    add("目录A100X/下级/不应匹配.csv")
    add("目录_100%备份/下级/也不应匹配.csv")
    result = client.get("/api/catalog/files", params={"path": str(wanted.parent.parent)})
    assert result.status_code == 200
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["name"] == wanted.name
    literal = client.get("/api/catalog/files", params={"path": str(root), "keyword": "_100%"}).json()
    assert literal["total"] == 1


def test_mixed_separators_and_legacy_share_prefix_resolve(catalog):
    client, root, add = catalog
    path = add("01-业务域/2026年/11月/报表.csv")
    for directory in [str(path.parent), str(path.parent).replace("/", "\\"), "/data/share/01-业务域/2026年/11月"]:
        result = client.get("/api/catalog/resolve", params={"path": directory})
        assert result.status_code == 200
        assert result.json()["path"] == path.parent.as_posix()
        assert client.get("/api/catalog/files", params={"path": directory}).json()["total"] == 1


@pytest.mark.parametrize("suffix", ["../other", "../企业网盘备份", "../../outside"])
def test_outside_directory_is_rejected(catalog, suffix):
    client, root, add = catalog
    add("01-业务域/表.csv")
    path = str(root / suffix)
    assert client.get("/api/catalog/files", params={"path": path}).status_code == 403
    assert client.get("/api/catalog/resolve", params={"path": path}).status_code == 403


def test_legacy_default_rules_map_to_existing_real_directories(catalog):
    client, root, add = catalog
    path = add("09-产品研发-新品上市简报库/2026年/报表.csv")
    with application.db_session() as conn:
        assert application._migrate_legacy_archive_paths(conn) == 1
        assert application._migrate_legacy_archive_paths(conn) == 0
        rule = conn.execute("SELECT target_path FROM archive_rules WHERE id=1").fetchone()
        assert rule["target_path"] == str(path.parent.parent)
        application.run_archive_compliance_check(conn)
        pending = conn.execute("SELECT count(*) FROM archive_tasks WHERE rule_id=1 AND status='pending'").fetchone()[0]
        assert pending == 0
    result = client.get("/api/catalog/resolve", params={"path": "/data/share/9-新品简报"})
    assert result.json()["path"] == str(path.parent.parent)
    assert client.get("/api/catalog/resolve", params={"path": "/data/share/不存在"}).status_code == 404


def test_today_branch_filter_preserves_full_subtree_counts(catalog):
    client, root, add = catalog
    add("01-业务域/有更新/报表.csv")
    add("01-业务域/历史/旧表.csv", mtime=time.time() - 86400 * 10)
    add("02-旧业务域/旧表.csv", mtime=time.time() - 86400 * 10)
    tree = client.get("/api/catalog/tree", params={"only_today_branch": True}).json()["tree"]
    assert tree["file_count"] == 3
    assert [child["name"] for child in tree["children"]] == ["01-业务域"]
    assert tree["children"][0]["file_count"] == 2
    assert len(tree["children"][0]["children"]) == 1


def test_tree_cache_refreshes_after_scan_and_root_change(catalog, monkeypatch):
    client, root, add = catalog
    add("01-业务域/报表.csv")
    assert client.get("/api/catalog/tree").json()["tree"]["file_count"] == 1
    new = root / "02-业务域/新增.csv"
    new.parent.mkdir()
    new.write_text("字段\n值\n")
    application._scan_worker()
    assert client.get("/api/catalog/tree").json()["tree"]["file_count"] == 2
    other = root.parent / "另一网盘"
    other.mkdir()
    monkeypatch.setattr(application, "FILE_ROOT", str(other))
    tree = client.get("/api/catalog/tree").json()["tree"]
    assert tree["path"] == str(other)
    assert tree["file_count"] == 0


def test_symlink_root_and_indexed_parent_use_same_canonical_path(catalog, monkeypatch):
    client, root, add = catalog
    alias = root.parent / "网盘快捷方式"
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(application, "FILE_ROOT", str(alias))
    add("01-业务域/报表.csv")
    tree = client.get("/api/catalog/tree").json()["tree"]
    assert tree["path"] == str(root)
    assert tree["file_count"] == 1
    result = client.get("/api/catalog/files", params={"path": str(alias / "01-业务域")})
    assert result.status_code == 200
    assert result.json()["total"] == 1


def test_dashboard_monthly_is_local_calendar_window_including_empty_months(catalog,monkeypatch):
    import os
    from datetime import datetime
    client, root, add = catalog
    now = datetime(2026,11,4,12).timestamp()
    monkeypatch.setattr(application.time,'time',lambda:now)
    for name, stamp in [('old',datetime(2025,11,30,23,59).timestamp()),('start',datetime(2025,12,1).timestamp()),('now',datetime(2026,11,1).timestamp()),('future',datetime(2026,12,1).timestamp())]:
        add(f'业务域/{name}.csv')
        path=root/'业务域'/f'{name}.csv'
        os.utime(path,(stamp,stamp))
        with application.db_session() as conn:
            application.update_index_file(conn,path,path.stat(),root_key=application.root_scope_key(),scan_id='fixture')
    monthly=client.get('/api/dashboard/summary').json()['monthly']
    assert len(monthly)==12 and monthly[0]['month']=='2025-12' and monthly[-1]['month']=='2026-11'
    assert monthly[0]['count']==monthly[-1]['count']==1
    assert sum(item['count'] for item in monthly)==2
