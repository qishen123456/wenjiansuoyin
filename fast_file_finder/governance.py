"""Privacy-conscious product baselines and durable metadata scan reconciliation."""
from __future__ import annotations

import json
import hashlib
import logging
import math
import statistics
import time
from typing import Any

CLIENT_EVENTS = {"result_click", "preview_open", "path_copy"}
VALID_ACTIONS = {"preview_open", "path_copy", "download_request"}
EVENTS = CLIENT_EVENTS | {"search", "download_request", "download_review", "archive_submit", "archive_review", "rule_create", "rule_update", "rule_toggle", "rule_delete", "index_start"}
RETENTION_DAYS = 90
_last_pruned_at = 0.0
METRIC_DEFINITIONS = [
    {"id": "indexed_files", "label": "已索引文件", "definition": "当前 FILE_ROOT 作用域的索引记录数，含零字节占位文件。"},
    {"id": "updated_today", "label": "今日更新", "definition": "当前目录中 mtime 自服务端本地今日 00:00 起的文件数；不是新建或首次入库数。"},
    {"id": "active_domains", "label": "今日有更新的业务域", "definition": "一级目录中至少有一份今日更新文件的目录数。"},
    {"id": "current_rule_pass_rate", "label": "当前规则达标率", "definition": "最新非空文件在规则更新窗口内的规则数 ÷ 可核验的启用规则数；无匹配、只有空文件和无效路径另列为规则异常。无可核验规则时不计算。不是历史按时归档率。"},
    {"id": "scan_reconciliation", "label": "本次扫描对账", "definition": "已发现文件 = 成功处理 + 明确排除 + 文件元数据读取失败。被排除或无法遍历的目录内部数量未知，不代表整个网盘覆盖率。"},
    {"id": "search_latency", "label": "检索计算耗时", "definition": "服务端索引召回与排序耗时；不含 HTTP 传输、浏览器渲染或埋点写入。基线展示最近最多 10,000 次样本的 P50/P95。"},
    {"id": "zero_result_rate", "label": "零结果率", "definition": "结果数为 0 的搜索事件数 ÷ 搜索事件数；不保存原始查询词。"},
    {"id": "action_search_rate", "label": "有效操作搜索占比", "definition": "有合法关联搜索 ID、同一会话且 10 分钟内发生预览、复制或下载申请的搜索数 ÷ 搜索数；不等于报表正确率。"},
]


class RedactQueryStringFilter(logging.Filter):
    """Keep Uvicorn access logs from retaining searches and download tickets."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3 and isinstance(record.args[2], str):
            parts = list(record.args)
            if "?" in parts[2]:
                parts[2] = parts[2].split("?", 1)[0] + "?[redacted]"
                record.args = tuple(parts)
        return True


def initialize(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS product_events (
        id INTEGER PRIMARY KEY, root_key TEXT NOT NULL, event_type TEXT NOT NULL,
        actor_id INTEGER, actor_role TEXT NOT NULL, session_id TEXT NOT NULL,
        subject_id INTEGER, search_event_id INTEGER, query_length INTEGER,
        result_count INTEGER, elapsed_ms REAL, occurred_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_events_scope_time ON product_events(root_key,occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_events_search ON product_events(search_event_id,session_id,occurred_at)")
    conn.execute("""CREATE TABLE IF NOT EXISTS search_event_files (
        search_event_id INTEGER NOT NULL, file_id INTEGER NOT NULL,
        PRIMARY KEY(search_event_id,file_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS index_scan_runs (
        id TEXT PRIMARY KEY, root_key TEXT NOT NULL, started_at TEXT,
        finished_at TEXT, phase TEXT NOT NULL, counts_json TEXT NOT NULL,
        message TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_runs_scope_time ON index_scan_runs(root_key,finished_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_product_events_time ON product_events(occurred_at)")
    prune_events(conn, force=True)


def prune_events(conn, *, force: bool = False) -> None:
    global _last_pruned_at
    now = time.time()
    if not force and now - _last_pruned_at < 3600:
        return
    conn.execute("DELETE FROM product_events WHERE occurred_at < ?", (now - RETENTION_DAYS * 86400,))
    conn.execute("DELETE FROM search_event_files WHERE search_event_id NOT IN (SELECT id FROM product_events WHERE event_type='search')")
    _last_pruned_at = now


def _event_scope(root_key: str) -> str:
    return hashlib.sha256(root_key.encode("utf-8")).hexdigest()


def record_event(conn, *, root_key: str, event_type: str, actor_id: int | None,
                 actor_role: str, session_id: str, subject_id: int | None = None,
                 search_event_id: int | None = None, query_length: int | None = None,
                 result_count: int | None = None, elapsed_ms: float | None = None,
                 occurred_at: float | None = None) -> int:
    root_key = _event_scope(root_key)
    if event_type not in EVENTS:
        raise ValueError("Unsupported event")
    cur = conn.execute("""INSERT INTO product_events
        (root_key,event_type,actor_id,actor_role,session_id,subject_id,
         search_event_id,query_length,result_count,elapsed_ms,occurred_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (root_key, event_type, actor_id, actor_role,
        session_id, subject_id, search_event_id, query_length, result_count,
        elapsed_ms, occurred_at if occurred_at is not None else time.time()))
    return int(cur.lastrowid)


def linked_search(conn, root_key: str, search_id: int | None, session_id: str,
                  actor_id: int | None, subject_id: int | None) -> int | None:
    root_key = _event_scope(root_key)
    if not search_id or not session_id or session_id == "anonymous":
        return None
    row = conn.execute("""SELECT id,occurred_at FROM product_events
        WHERE id=? AND root_key=? AND event_type='search' AND session_id=?
        AND actor_id IS ?""", (search_id, root_key, session_id, actor_id)).fetchone()
    if not row or not 0 <= time.time() - row["occurred_at"] <= 600:
        return None
    if subject_id is not None and not conn.execute("SELECT 1 FROM search_event_files WHERE search_event_id=? AND file_id=?", (search_id, subject_id)).fetchone():
        return None
    return row["id"]


def persist_scan(conn, scan_id: str, root_key: str, state: dict[str, Any]) -> None:
    counts = {key: state.get(key, 0) for key in ("discovered", "seen", "indexed", "updated", "deleted", "unchanged", "excluded", "errors", "directory_errors", "excluded_directories")}
    counts["mode"] = state.get("mode","full")
    counts["target_path"] = state.get("target_path","")
    counts["exclusion_reasons"] = state.get("exclusion_reasons", {})
    counts["reconciled"] = counts["discovered"] == counts["seen"] + counts["excluded"] + counts["errors"]
    conn.execute("""INSERT OR REPLACE INTO index_scan_runs
        (id,root_key,started_at,finished_at,phase,counts_json,message)
        VALUES (?,?,?,?,?,?,?)""", (scan_id, root_key, state.get("started_at"),
        state.get("finished_at"), state.get("phase", "error"),
        json.dumps(counts, ensure_ascii=False), state.get("message", "")))


def scan_history(conn, root_key: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute("""SELECT * FROM index_scan_runs WHERE root_key=?
        ORDER BY finished_at DESC,id DESC LIMIT ?""", (root_key, limit)).fetchall()
    return [{"id": row["id"], "started_at": row["started_at"],
        "finished_at": row["finished_at"], "phase": row["phase"],
        "message": row["message"], "counts": json.loads(row["counts_json"])} for row in rows]


def product_summary(conn, root_key: str, days: int = 14) -> dict[str, Any]:
    root_key = _event_scope(root_key)
    since = time.time() - days * 86400
    counts = {row["event_type"]: row["n"] for row in conn.execute("""SELECT event_type,count(*) AS n
        FROM product_events WHERE root_key=? AND occurred_at>=? GROUP BY event_type""", (root_key, since))}
    searches = conn.execute("""SELECT id,result_count,elapsed_ms,occurred_at FROM product_events
        WHERE root_key=? AND event_type='search' AND occurred_at>=?
        ORDER BY occurred_at DESC LIMIT 10000""", (root_key, since)).fetchall()
    linked = conn.execute("""SELECT s.id,min(a.occurred_at)-s.occurred_at AS seconds
        FROM product_events s JOIN product_events a
        ON a.search_event_id=s.id AND a.root_key=s.root_key AND a.session_id=s.session_id
          AND a.actor_id IS s.actor_id AND a.event_type IN ('preview_open','path_copy','download_request')
          AND a.occurred_at BETWEEN s.occurred_at AND s.occurred_at+600
        WHERE s.root_key=? AND s.event_type='search' AND s.occurred_at>=?
        GROUP BY s.id""", (root_key, since)).fetchall()
    n = counts.get("search", 0)
    zero = conn.execute("""SELECT count(*) FROM product_events
        WHERE root_key=? AND event_type='search' AND occurred_at>=? AND result_count=0""", (root_key, since)).fetchone()[0]
    latency = sorted(row["elapsed_ms"] for row in searches if row["elapsed_ms"] is not None)
    started = conn.execute("SELECT min(occurred_at) FROM product_events WHERE root_key=?", (root_key,)).fetchone()[0]
    roles = [dict(row) for row in conn.execute("""SELECT actor_role,count(*) AS searches,
        sum(CASE WHEN result_count=0 THEN 1 ELSE 0 END) AS zero_results
        FROM product_events WHERE root_key=? AND event_type='search' AND occurred_at>=?
        GROUP BY actor_role""", (root_key, since))]
    return {"days": days, "collection_started_at": started, "retention_days": RETENTION_DAYS,
        "searches": n, "zero_results": zero, "zero_result_rate": round(zero / n * 100, 1) if n else None,
        "action_searches": len(linked), "action_search_rate": round(len(linked) / n * 100, 1) if n else None,
        "first_action_p50_seconds": round(statistics.median(row["seconds"] for row in linked), 2) if linked else None,
        "search_p50_ms": round(statistics.median(latency), 2) if latency else None,
        "search_p95_ms": round(latency[max(0, math.ceil(len(latency) * .95) - 1)], 2) if latency else None,
        "latency_sample_size": len(latency), "latency_sample_limit": 10000,
        "event_counts": counts, "by_role": roles,
        "privacy": "不保存原始查询词、文件路径、密码或单元格内容；行为数据保留 90 天。"}
