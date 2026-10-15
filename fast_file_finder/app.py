# -*- coding: utf-8 -*-
"""内网极速文件检索原型：元数据索引 + 本地排序 + 可选 AI 候选重排。"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
import sqlite3
import threading
import time
import unicodedata
import math
from datetime import datetime, date
from pathlib import Path
from typing import Any, Literal
from io import BytesIO
from urllib.parse import quote

import pandas as pd
from pydantic import BaseModel, Field

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
DB_PATH = Path(os.getenv("INDEX_DB_PATH", str(APP_DIR / "data" / "file_index.db")))
FILE_ROOT = os.getenv("FILE_ROOT", "").strip()
ALLOWED_EXTENSIONS = {
    x.strip().lower() if x.strip().startswith(".") else "." + x.strip().lower()
    for x in os.getenv("ALLOWED_EXTENSIONS", ".xlsx,.xls,.xlsm,.xlsb,.csv,.tsv,.ods").split(",")
    if x.strip()
}
AI_BASE_URL = os.getenv("AI_BASE_URL", "").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "12"))
MAX_ANALYSIS_FILE_MB = int(os.getenv("MAX_ANALYSIS_FILE_MB", "256"))
MAX_ANALYSIS_ROWS = int(os.getenv("MAX_ANALYSIS_ROWS", "200000"))
PREVIEW_ROWS = int(os.getenv("PREVIEW_ROWS", "100"))

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="企业文件检索与轻量 BI 分析中心", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

_scan_lock = threading.Lock()
_scan_state: dict[str, Any] = {
    "running": False,
    "phase": "idle",
    "started_at": None,
    "finished_at": None,
    "seen": 0,
    "indexed": 0,
    "updated": 0,
    "deleted": 0,
    "errors": 0,
    "message": "尚未建立索引",
}


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                parent TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
        # 通过文件名/路径的二元字符片段建立 FTS 候选召回。查询时不读取 Excel 内容。
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                name, name_grams, path_grams,
                tokenize='unicode61'
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO app_meta(key, value) VALUES ('schema_version', '1')")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                file_id INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_analyses_file ON saved_analyses(file_id)")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[\s\u3000]+", "", value)


def char_grams(value: str) -> list[str]:
    """生成适合中文文件名的字符 n-gram；去掉标点与空白，但保留数字和字母。"""
    s = normalize_text(value)
    chars = [c for c in s if c.isalnum()]
    if not chars:
        return []
    if len(chars) == 1:
        return chars
    grams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    # 对长查询保留独立字符有利于混合中英文、数字和简称的召回。
    if len(chars) <= 5:
        grams.extend(chars)
    return list(dict.fromkeys(grams))


def grams_text(value: str) -> str:
    return " ".join(char_grams(value))


def fts_query_for(q: str) -> str:
    grams = char_grams(q)
    # 只有一个字符时使用该字符；否则 OR 召回候选，再由应用层按命中覆盖度精排。
    safe = [g.replace('"', '""') for g in grams[:48]]
    if not safe:
        return ""
    return " OR ".join(f'"{g}"' for g in safe)


def get_root() -> Path:
    if not FILE_ROOT:
        raise HTTPException(status_code=400, detail="尚未配置 FILE_ROOT。请在 .env 中填写网盘挂载目录或 UNC 共享路径。")
    root = Path(FILE_ROOT).expanduser()
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=400, detail=f"文件根目录不可访问：{FILE_ROOT}。请检查路径、挂载状态和服务账号权限。")
    return root


def update_index_file(conn: sqlite3.Connection, file_path: Path, stat: os.stat_result) -> str:
    path_str = str(file_path)
    row = conn.execute("SELECT id, size, mtime, name FROM files WHERE path = ?", (path_str,)).fetchone()
    name = file_path.name
    parent = str(file_path.parent)
    ext = file_path.suffix.lower()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    now = time.time()
    if row and row["size"] == size and abs(row["mtime"] - mtime) < 0.0001 and row["name"] == name:
        return "unchanged"
    if row:
        file_id = row["id"]
        conn.execute("UPDATE files SET name=?, parent=?, ext=?, size=?, mtime=?, indexed_at=? WHERE id=?",
                     (name, parent, ext, size, mtime, now, file_id))
        conn.execute("DELETE FROM file_fts WHERE rowid=?", (file_id,))
        action = "updated"
    else:
        cur = conn.execute("INSERT INTO files(path,name,parent,ext,size,mtime,indexed_at) VALUES(?,?,?,?,?,?,?)",
                           (path_str, name, parent, ext, size, mtime, now))
        file_id = cur.lastrowid
        action = "indexed"
    conn.execute("INSERT INTO file_fts(rowid,name,name_grams,path_grams) VALUES(?,?,?,?)",
                 (file_id, name, grams_text(name), grams_text(parent)))
    return action


def _scan_worker() -> None:
    global _scan_state
    if not _scan_lock.acquire(blocking=False):
        return
    start = time.time()
    _scan_state = {
        "running": True, "phase": "scanning", "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None, "seen": 0, "indexed": 0, "updated": 0, "deleted": 0, "errors": 0,
        "message": "正在扫描目录并增量更新索引；查询不需要等待扫描结束。"
    }
    seen_paths: set[str] = set()
    walk_errors: list[OSError] = []
    try:
        root = get_root()
        with db_connect() as conn:
            for current, dirs, filenames in os.walk(root, followlinks=False, onerror=lambda exc: walk_errors.append(exc)):
                # 跳过常见临时/系统目录，避免无意义遍历。
                dirs[:] = [d for d in dirs if d not in {"$RECYCLE.BIN", "System Volume Information", ".snapshot", "~$"}]
                for filename in filenames:
                    if filename.startswith("~$"):
                        continue  # 排除 Excel 正在编辑时创建的临时锁文件
                    ext = Path(filename).suffix.lower()
                    if ALLOWED_EXTENSIONS and ext not in ALLOWED_EXTENSIONS:
                        continue
                    fp = Path(current) / filename
                    try:
                        stat = fp.stat()
                        if not fp.is_file():
                            continue
                        p = str(fp)
                        seen_paths.add(p)
                        _scan_state["seen"] += 1
                        action = update_index_file(conn, fp, stat)
                        if action in ("indexed", "updated"):
                            _scan_state[action] += 1
                        if _scan_state["seen"] % 300 == 0:
                            conn.commit()
                            _scan_state["message"] = f"已检查 {_scan_state['seen']:,} 个表格文件，新增 {_scan_state['indexed']:,} 个。"
                    except (PermissionError, FileNotFoundError, OSError):
                        _scan_state["errors"] += 1
                conn.commit()
            # 只有完整遍历且没有权限/IO 异常时才清理失效记录，避免误删未能访问目录中的索引。
            scan_complete = not walk_errors and _scan_state["errors"] == 0
            if scan_complete:
                db_paths = conn.execute("SELECT path FROM files").fetchall()
                stale = [r["path"] for r in db_paths if r["path"] not in seen_paths]
                for stale_path in stale:
                    row = conn.execute("SELECT id FROM files WHERE path=?", (stale_path,)).fetchone()
                    if row:
                        conn.execute("DELETE FROM file_fts WHERE rowid=?", (row["id"],))
                        conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                        _scan_state["deleted"] += 1
            conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('last_scan_at',?)",
                         (datetime.now().isoformat(timespec="seconds"),))
            conn.commit()
        _scan_state["phase"] = "complete"
        if scan_complete:
            _scan_state["message"] = f"索引更新完成，用时 {time.time() - start:.1f} 秒。"
        else:
            _scan_state["message"] = f"扫描结束，用时 {time.time() - start:.1f} 秒；发现 {len(walk_errors) + _scan_state['errors']} 个访问异常，已保留疑似失效索引，避免误删。建议检查共享盘权限后重试。"
    except Exception as exc:
        _scan_state["phase"] = "error"
        _scan_state["message"] = f"建索引失败：{type(exc).__name__}: {exc}"
    finally:
        _scan_state["running"] = False
        _scan_state["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _scan_lock.release()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    return {"ok": True, "root_configured": bool(FILE_ROOT), "ai_configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL), "indexed_files": count}


@app.get("/api/index/status")
def index_status() -> dict[str, Any]:
    with db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        total_size = conn.execute("SELECT COALESCE(SUM(size),0) FROM files").fetchone()[0]
        last = conn.execute("SELECT value FROM app_meta WHERE key='last_scan_at'").fetchone()
    return {**_scan_state, "indexed_files": count, "indexed_size_bytes": total_size,
            "last_scan_at": last[0] if last else None, "root_configured": bool(FILE_ROOT),
            "root_display": FILE_ROOT or "尚未配置", "ai_configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL)}


@app.post("/api/index/start")
def start_index() -> dict[str, Any]:
    get_root()  # 先做目录和权限的快速校验
    if _scan_state.get("running"):
        return {"started": False, "message": "索引任务已经在运行。", **_scan_state}
    t = threading.Thread(target=_scan_worker, daemon=True)
    t.start()
    return {"started": True, "message": "索引任务已启动，可继续搜索已建立的索引。"}


def candidate_search(conn: sqlite3.Connection, q: str, limit: int = 500, ext: str = "") -> list[dict[str, Any]]:
    match = fts_query_for(q)
    if not match:
        return []
    try:
        if ext:
            rows = conn.execute("""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? AND f.ext = ? ORDER BY rank LIMIT ?
            """, (match, ext, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? ORDER BY rank LIMIT ?
            """, (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    qgrams = set(char_grams(q))
    qcompact = normalize_text(q)
    results = []
    for r in rows:
        name_norm = normalize_text(r["name"])
        path_norm = normalize_text(r["parent"])
        name_grams = set(char_grams(r["name"]))
        path_grams = set(char_grams(r["parent"]))
        name_cov = len(qgrams & name_grams) / max(len(qgrams), 1)
        path_cov = len(qgrams & path_grams) / max(len(qgrams), 1)
        # 精确短语匹配给予最高优先级；路径匹配仅作为补充证据。
        score = 100.0 * (0.78 * name_cov + 0.22 * path_cov)
        if qcompact and qcompact in name_norm:
            score = max(score, 98.0)
        elif qcompact and qcompact in path_norm:
            score = max(score, 76.0)
        # 文件名自身与 query 的短片段命中也增加一点权重。
        if len(qcompact) >= 2 and any(qcompact[i:i+2] in name_norm for i in range(max(1, len(qcompact)-1))):
            score += 1.5
        results.append({
            "id": int(r["id"]), "name": r["name"], "path": r["path"], "parent": r["parent"],
            "ext": r["ext"], "size": int(r["size"]), "mtime": float(r["mtime"]),
            "score": round(min(score, 100.0), 1), "rank": float(r["rank"]), "match_reason": "文件名/路径索引匹配"
        })
    results.sort(key=lambda x: (-x["score"], x["rank"], -x["mtime"]))
    return results


def ai_expand_query(q: str) -> list[str]:
    """本地索引没有候选时，让模型只扩展查询词；随后仍由 FTS 索引执行检索。"""
    if not (AI_BASE_URL and AI_API_KEY and AI_MODEL):
        return []
    prompt = (
        "你是企业内部文件检索的查询改写助手。不要猜测或生成任何文件名，不需要访问文件。"
        "请将用户的自然语言描述改写为最多 5 个可用于文件名/目录名检索的短关键词或短语，"
        "包含必要的同义词、常见业务叫法、时间/地域/部门关键词。只输出 JSON："
        "{\"search_terms\":[\"关键词1\",\"关键词2\"]}。"
        "每个短语尽量简短，不要输出解释。\n用户查询：" + q
    )
    try:
        url = AI_BASE_URL if AI_BASE_URL.endswith("/chat/completions") else AI_BASE_URL + "/chat/completions"
        with httpx.Client(timeout=AI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                               json={"model": AI_MODEL, "temperature": 0, "messages": [
                                   {"role": "system", "content": "只输出合法 JSON。"},
                                   {"role": "user", "content": prompt}
                               ]})
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return []
        data = json.loads(match.group(0))
        terms = data.get("search_terms", [])
        if not isinstance(terms, list):
            return []
        cleaned = []
        for term in terms:
            if isinstance(term, str):
                term = term.strip()[:60]
                if term and term not in cleaned and term != q:
                    cleaned.append(term)
        return cleaned[:5]
    except Exception:
        return []


def ai_rerank(q: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """可选 OpenAI-compatible 接口；输入严格限制为索引筛出的候选，不扫描或上传全量文件名。"""
    if not (AI_BASE_URL and AI_API_KEY and AI_MODEL and candidates):
        return candidates
    short = candidates[:30]
    payload_candidates = [{"id": c["id"], "name": c["name"], "parent": c["parent"],
                           "extension": c["ext"], "modified": datetime.fromtimestamp(c["mtime"]).strftime("%Y-%m-%d")}
                          for c in short]
    prompt = (
        "你是企业文件检索的候选重排器。只能从给定候选中选择，绝不能编造文件或路径。"
        "用户描述可能包含业务含义、时间、地区、文件类型等信息。请输出 JSON："
        "{\"ranked_ids\":[整数ID...],\"reason\":\"简短中文说明\"}。"
        "最多返回 10 个 ID，最相关的排前面。\n"
        f"用户查询：{q}\n候选索引记录：{json.dumps(payload_candidates, ensure_ascii=False)}"
    )
    try:
        url = AI_BASE_URL if AI_BASE_URL.endswith("/chat/completions") else AI_BASE_URL + "/chat/completions"
        with httpx.Client(timeout=AI_TIMEOUT_SECONDS) as client:
            resp = client.post(url, headers={"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"},
                               json={"model": AI_MODEL, "temperature": 0, "messages": [
                                   {"role": "system", "content": "严格遵守输出 JSON 的要求，不得输出候选以外的文件。"},
                                   {"role": "user", "content": prompt}
                               ]})
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            return candidates
        data = json.loads(match.group(0))
        by_id = {c["id"]: c for c in candidates}
        ranked = []
        for file_id in data.get("ranked_ids", []):
            try:
                item = by_id.get(int(file_id))
            except (TypeError, ValueError):
                item = None
            if item and item not in ranked:
                copy = dict(item)
                copy["match_reason"] = str(data.get("reason") or "AI 对索引候选进行语义重排")[:160]
                copy["ai_ranked"] = True
                ranked.append(copy)
        # 将模型未选中的本地结果补在后面，避免模型遗漏导致结果消失。
        seen = {x["id"] for x in ranked}
        ranked.extend(c for c in candidates if c["id"] not in seen)
        return ranked
    except Exception:
        return candidates


@app.get("/api/search")
def search(q: str = Query(default="", max_length=300), limit: int = Query(default=30, ge=1, le=100),
           ext: str = Query(default=""), sort: str = Query(default="relevance")) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"query": q, "count": 0, "results": [], "ai_used": False, "message": "请输入文件名、业务关键词或自然语言描述。"}
    start = time.perf_counter()
    wanted_ext = (ext.lower() if ext.startswith(".") else "." + ext.lower()) if ext else ""
    with db_connect() as conn:
        candidates = candidate_search(conn, query, limit=500, ext=wanted_ext)
        ai_used = False
        ai_expanded = False
        # 第一步始终是本地索引。完全无候选时，AI 只改写查询词，不接收全量文件名；
        # 每个改写词再由 FTS 索引检索，候选集去重后才进入后续排序。
        if not candidates and AI_BASE_URL and AI_API_KEY and AI_MODEL:
            expanded_terms = ai_expand_query(query)
            merged: dict[int, dict[str, Any]] = {}
            for term in expanded_terms:
                for item in candidate_search(conn, term, limit=120, ext=wanted_ext):
                    current = merged.get(item["id"])
                    if current is None or item["score"] > current["score"]:
                        item["match_reason"] = f"AI 扩展关键词：{term}"
                        merged[item["id"]] = item
            candidates = sorted(merged.values(), key=lambda x: (-x["score"], x["rank"], -x["mtime"]))[:500]
            ai_expanded = bool(expanded_terms)
        if ext:
            wanted = ext.lower() if ext.startswith(".") else "." + ext.lower()
            candidates = [c for c in candidates if c["ext"] == wanted]
        # 候选存在但本地匹配度仍弱时，AI 只重排索引已召回的前 30 条。
        if AI_BASE_URL and AI_API_KEY and AI_MODEL and candidates and (ai_expanded or candidates[0]["score"] < 42):
            candidates = ai_rerank(query, candidates)
            ai_used = bool(candidates and any(c.get("ai_ranked") for c in candidates))
        if sort == "modified":
            candidates.sort(key=lambda x: -x["mtime"])
        elif sort == "name":
            candidates.sort(key=lambda x: x["name"].lower())
        results = candidates[:limit]
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    return {"query": query, "count": len(results), "total_candidates": len(candidates), "results": results,
            "elapsed_ms": elapsed_ms, "ai_used": ai_used, "ai_expanded": ai_expanded,
            "message": "已使用索引检索" + ("；AI 扩展关键词并对索引候选进行重排。" if ai_used and ai_expanded else ("；AI 对索引候选进行重排。" if ai_used else ("；已使用 AI 扩展关键词。" if ai_expanded else "。")))}


@app.get("/api/recent")
def recent(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    with db_connect() as conn:
        rows = conn.execute("SELECT id,path,name,parent,ext,size,mtime FROM files ORDER BY mtime DESC LIMIT ?", (limit,)).fetchall()
    return {"results": [{"id": r["id"], "path": r["path"], "name": r["name"], "parent": r["parent"],
                         "ext": r["ext"], "size": r["size"], "mtime": r["mtime"], "score": None,
                         "match_reason": "最近修改"} for r in rows]}


@app.get("/api/config-check")
def config_check() -> dict[str, Any]:
    root = FILE_ROOT
    path = Path(root).expanduser() if root else None
    return {"root_configured": bool(root), "root_exists": bool(path and path.exists() and path.is_dir()),
            "root_display": root or "尚未配置", "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
            "ai_configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL),
            "note": "服务进程必须能直接读取配置的共享目录。"}


# ---------------------------------------------------------------------------
# 轻量 BI 分析模块：只分析用户主动选择且已在索引中的文件
# ---------------------------------------------------------------------------
class AnalysisFilter(BaseModel):
    field: str
    operator: Literal["contains", "equals", "not_equals", "gt", "gte", "lt", "lte", "is_empty", "not_empty"] = "contains"
    value: str = ""


class AnalysisRequest(BaseModel):
    file_id: int
    sheet_name: str | None = None
    row_field: str | None = None
    column_field: str | None = None
    value_fields: list[str] = Field(default_factory=list)
    aggregation: Literal["sum", "count", "mean", "median", "min", "max", "nunique"] = "sum"
    filters: list[AnalysisFilter] = Field(default_factory=list)
    max_categories: int = Field(default=30, ge=5, le=100)


class SaveAnalysisRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    file_id: int
    config: dict[str, Any]


def _get_indexed_file(file_id: int) -> tuple[dict[str, Any], Path]:
    """Only allow files already present in the metadata index and still under FILE_ROOT."""
    with db_connect() as conn:
        row = conn.execute(
            "SELECT id,path,name,parent,ext,size,mtime FROM files WHERE id=?", (file_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="索引中不存在这个文件，请先更新索引后重试。")
    root = get_root()
    try:
        root_real = os.path.realpath(str(root))
        file_real = os.path.realpath(row["path"])
        if os.path.commonpath([os.path.normcase(root_real), os.path.normcase(file_real)]) != os.path.normcase(root_real):
            raise HTTPException(status_code=403, detail="该文件不在允许分析的文件根目录内。")
    except ValueError:
        raise HTTPException(status_code=403, detail="文件路径与配置目录不匹配。")
    path = Path(row["path"])
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="原始文件已不存在，请先更新索引。")
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"当前文件格式未开放分析：{ext}")
    max_bytes = MAX_ANALYSIS_FILE_MB * 1024 * 1024
    if path.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件为 {path.stat().st_size / 1024 / 1024:.1f} MB，超过当前单文件分析上限 {MAX_ANALYSIS_FILE_MB} MB。请先拆分文件或调高 MAX_ANALYSIS_FILE_MB。")
    return dict(row), path


def _engine_for(path: Path) -> str | None:
    ext = path.suffix.lower()
    return {".xlsx": "openpyxl", ".xlsm": "openpyxl", ".xls": "xlrd", ".xlsb": "pyxlsb", ".ods": "odf"}.get(ext)


def _read_table(path: Path, sheet_name: str | None = None, nrows: int | None = None) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext in {".csv", ".tsv"}:
        sep = "\t" if ext == ".tsv" else None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                if sep is None:
                    return pd.read_csv(path, encoding=encoding, sep=None, engine="python", nrows=nrows, on_bad_lines="skip")
                return pd.read_csv(path, encoding=encoding, sep=sep, nrows=nrows, on_bad_lines="skip")
            except UnicodeDecodeError as exc:
                last_error = exc
        raise HTTPException(status_code=422, detail=f"CSV 编码无法识别：{last_error}")
    if ext not in {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods"}:
        raise HTTPException(status_code=415, detail=f"暂不支持分析 {ext} 文件。")
    engine = _engine_for(path)
    try:
        with pd.ExcelFile(path, engine=engine) as workbook:
            target_sheet = sheet_name if sheet_name in workbook.sheet_names else workbook.sheet_names[0]
        return pd.read_excel(path, sheet_name=target_sheet, engine=engine, nrows=nrows)
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=f"缺少读取 {ext} 的依赖：{exc}。请按 requirements.txt 安装依赖。")
    except Exception as exc:
        # Excel reader exceptions vary by engine; expose a concise error to the UI.
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=422, detail=f"读取表格失败：{type(exc).__name__}: {str(exc)[:240]}")


def _unique_columns(columns: list[Any]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    for i, value in enumerate(columns):
        name = str(value).strip() if value is not None else ""
        if not name or name.lower().startswith("unnamed:"):
            name = f"未命名列{i + 1}"
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}_{count}")
    return result


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    frame.columns = _unique_columns(list(frame.columns))
    # Strip invisible whitespace in string headers but preserve the cell values.
    return frame


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_records(frame: pd.DataFrame, limit: int = 100) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in frame.head(limit).to_dict(orient="records"):
        records.append({str(k): _json_value(v) for k, v in row.items()})
    return records


def _column_metadata(frame: pd.DataFrame) -> list[dict[str, Any]]:
    output = []
    for col in frame.columns:
        s = frame[col]
        sample = []
        for val in s.dropna().head(3).tolist():
            sample.append(_json_value(val))
        dtype = str(s.dtype)
        if pd.api.types.is_numeric_dtype(s):
            kind = "number"
        elif pd.api.types.is_datetime64_any_dtype(s):
            kind = "date"
        else:
            kind = "text"
        output.append({"name": str(col), "dtype": dtype, "kind": kind,
                       "non_empty": int(s.notna().sum()),
                       "unique_count": int(s.nunique(dropna=True)), "samples": sample})
    return output


def _workbook_sheets(path: Path) -> list[str]:
    if path.suffix.lower() in {".csv", ".tsv"}:
        return ["数据"]
    try:
        with pd.ExcelFile(path, engine=_engine_for(path)) as workbook:
            return list(workbook.sheet_names)
    except ImportError as exc:
        raise HTTPException(status_code=501, detail=f"读取此格式缺少依赖：{exc}")
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"无法读取工作表列表：{str(exc)[:200]}")


def _load_analysis_frame(file_info: dict[str, Any], path: Path, sheet_name: str | None) -> tuple[pd.DataFrame, bool, list[str]]:
    sheets = _workbook_sheets(path)
    chosen = sheet_name if sheet_name in sheets else sheets[0]
    df = _read_table(path, None if chosen == "数据" and path.suffix.lower() in {".csv", ".tsv"} else chosen,
                     nrows=MAX_ANALYSIS_ROWS + 1)
    truncated = len(df) > MAX_ANALYSIS_ROWS
    if truncated:
        df = df.head(MAX_ANALYSIS_ROWS).copy()
    df = _normalise_df(df)
    return df, truncated, sheets


def _apply_filters(df: pd.DataFrame, filters: list[AnalysisFilter]) -> pd.DataFrame:
    frame = df
    for f in filters:
        if f.field not in frame.columns:
            raise HTTPException(status_code=422, detail=f"筛选字段不存在：{f.field}")
        series = frame[f.field]
        op = f.operator
        value = f.value or ""
        if op == "is_empty":
            mask = series.isna() | series.astype("string").str.strip().eq("")
        elif op == "not_empty":
            mask = series.notna() & series.astype("string").str.strip().ne("")
        elif op == "contains":
            mask = series.astype("string").str.contains(value, case=False, regex=False, na=False)
        elif op in {"equals", "not_equals"}:
            if pd.api.types.is_numeric_dtype(series) and value.strip():
                try:
                    val_num = float(value)
                    mask = pd.to_numeric(series, errors="coerce").eq(val_num)
                except ValueError:
                    mask = series.astype("string").str.casefold().eq(value.casefold())
            else:
                mask = series.astype("string").str.strip().str.casefold().eq(value.strip().casefold())
            if op == "not_equals":
                mask = ~mask
        else:
            if pd.api.types.is_datetime64_any_dtype(series):
                val = pd.to_datetime(value, errors="coerce")
                comp = pd.to_datetime(series, errors="coerce")
            else:
                val = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
                comp = pd.to_numeric(series, errors="coerce")
            if pd.isna(val):
                raise HTTPException(status_code=422, detail=f"字段“{f.field}”的比较筛选需要输入有效数字或日期。")
            mask = {"gt": comp.gt, "gte": comp.ge, "lt": comp.lt, "lte": comp.le}[op](val)
        frame = frame[mask.fillna(False)]
    return frame.copy()


def _aggregate(series: pd.Series, aggregation: str) -> Any:
    if aggregation == "count":
        return int(series.count())
    if aggregation == "nunique":
        return int(series.nunique(dropna=True))
    if aggregation == "sum":
        return series.sum(min_count=1)
    if aggregation == "mean":
        return series.mean()
    if aggregation == "median":
        return series.median()
    if aggregation == "min":
        return series.min()
    if aggregation == "max":
        return series.max()
    raise HTTPException(status_code=422, detail=f"不支持的聚合方式：{aggregation}")


def _analysis_compute(req: AnalysisRequest) -> dict[str, Any]:
    file_info, path = _get_indexed_file(req.file_id)
    df, truncated, sheets = _load_analysis_frame(file_info, path, req.sheet_name)
    for field_name in (req.row_field, req.column_field):
        if field_name and field_name not in df.columns:
            raise HTTPException(status_code=422, detail=f"分析维度不存在：{field_name}")
    if req.row_field and req.column_field and req.row_field == req.column_field:
        raise HTTPException(status_code=422, detail="行维度和列维度不能选择同一个字段。")
    metrics = list(dict.fromkeys([x for x in req.value_fields if x in df.columns]))[:5]
    missing_metrics = [x for x in req.value_fields if x not in df.columns]
    if missing_metrics:
        raise HTTPException(status_code=422, detail=f"指标字段不存在：{', '.join(missing_metrics)}")
    if req.aggregation in {"sum", "mean", "median"}:
        for metric in metrics:
            if not pd.api.types.is_numeric_dtype(df[metric]):
                converted = pd.to_numeric(df[metric], errors="coerce")
                if int(converted.notna().sum()) == 0 and int(df[metric].notna().sum()) > 0:
                    raise HTTPException(status_code=422, detail=f"指标“{metric}”不是数值字段，无法执行{req.aggregation}。请换一个指标或改用计数。")
                df[metric] = converted
    filtered = _apply_filters(df, req.filters)
    # Prepare display-friendly group labels without mutating source data.
    work = filtered.copy()
    if req.row_field:
        work[req.row_field] = work[req.row_field].map(lambda v: "(空白)" if pd.isna(v) else str(v))
    if req.column_field:
        work[req.column_field] = work[req.column_field].map(lambda v: "(空白)" if pd.isna(v) else str(v))

    aggregation = req.aggregation
    row_field, column_field = req.row_field, req.column_field
    measure_fields = metrics if metrics else []
    metric_label = lambda f: f if aggregation in {"sum", "count", "mean", "median", "min", "max", "nunique"} else f
    pivot_headers: list[str] = []
    pivot_rows: list[list[Any]] = []
    chart_categories: list[str] = []
    chart_series: list[dict[str, Any]] = []
    omitted_categories = False

    if row_field and column_field:
        row_counts = work[row_field].value_counts(dropna=False)
        col_counts = work[column_field].value_counts(dropna=False)
        row_categories = row_counts.head(req.max_categories).index.astype(str).tolist()
        col_categories = col_counts.head(12).index.astype(str).tolist()
        omitted_categories = len(row_counts) > len(row_categories) or len(col_counts) > len(col_categories)
        work = work[work[row_field].isin(row_categories) & work[column_field].isin(col_categories)]
        pivot_headers = [row_field]
        pivot_frames: list[tuple[str, pd.DataFrame]] = []
        if measure_fields:
            for metric in measure_fields:
                p = pd.pivot_table(work, index=row_field, columns=column_field, values=metric,
                                   aggfunc=aggregation, fill_value=0 if aggregation in {"sum", "count", "nunique"} else None,
                                   dropna=False, observed=False)
                for cat in col_categories:
                    if cat in p.columns:
                        colname = f"{metric} / {cat}" if len(measure_fields) > 1 else str(cat)
                        pivot_headers.append(colname)
                        chart_series.append({"name": colname, "values": []})
                        pivot_frames.append((colname, p[[cat]]))
            # Build cells in the exact same column order as pivot_headers.
            row_index = row_categories
            for ridx, category in enumerate(row_index):
                cells: list[Any] = [category]
                for metric in measure_fields:
                    p = pd.pivot_table(work, index=row_field, columns=column_field, values=metric,
                                       aggfunc=aggregation, fill_value=0 if aggregation in {"sum", "count", "nunique"} else None,
                                       dropna=False, observed=False)
                    for cat in col_categories:
                        if cat in p.columns:
                            val = p.at[category, cat] if category in p.index else None
                            cells.append(_json_value(val))
                pivot_rows.append(cells)
            chart_categories = row_categories[:15]
            chart_series = []
            for metric in measure_fields:
                p = pd.pivot_table(work, index=row_field, columns=column_field, values=metric,
                                   aggfunc=aggregation, fill_value=0 if aggregation in {"sum", "count", "nunique"} else None,
                                   dropna=False, observed=False)
                for cat in col_categories[:6]:
                    if cat in p.columns:
                        series_name = f"{metric} / {cat}" if len(measure_fields) > 1 else str(cat)
                        vals = [_json_value(p.at[c, cat]) if c in p.index else 0 for c in chart_categories]
                        chart_series.append({"name": series_name, "values": vals})
        else:
            p = pd.crosstab(work[row_field], work[column_field])
            for cat in col_categories:
                if cat in p.columns:
                    pivot_headers.append(str(cat))
            for category in row_categories:
                vals = [int(p.at[category, cat]) if category in p.index and cat in p.columns else 0 for cat in col_categories if cat in p.columns]
                pivot_rows.append([category] + vals)
            chart_categories = row_categories[:15]
            chart_series = [{"name": str(cat), "values": [int(p.at[c, cat]) if c in p.index and cat in p.columns else 0 for c in chart_categories]} for cat in col_categories[:6] if cat in p.columns]
    elif row_field or column_field:
        dim = row_field or column_field
        work[dim] = work[dim].map(lambda v: "(空白)" if pd.isna(v) else str(v))
        counts = work[dim].value_counts(dropna=False)
        categories = counts.head(req.max_categories).index.astype(str).tolist()
        omitted_categories = len(counts) > len(categories)
        work = work[work[dim].isin(categories)]
        pivot_headers = [dim]
        if measure_fields:
            pivot_headers.extend(measure_fields)
        else:
            pivot_headers.append("记录数")
        out_rows = []
        for category in categories:
            part = work[work[dim] == category]
            cells: list[Any] = [category]
            if measure_fields:
                for metric in measure_fields:
                    cells.append(_json_value(_aggregate(part[metric], aggregation)))
            else:
                cells.append(int(len(part)))
            out_rows.append(cells)
        # Sort by first value if numerical; otherwise keep frequency order.
        if len(pivot_headers) > 1:
            try:
                out_rows.sort(key=lambda x: (float(x[1]) if x[1] is not None else float("-inf")), reverse=True)
            except (TypeError, ValueError):
                pass
        pivot_rows = out_rows
        chart_categories = [str(r[0]) for r in out_rows[:15]]
        chart_series = []
        val_headers = pivot_headers[1:]
        for j, header in enumerate(val_headers, start=1):
            chart_series.append({"name": header, "values": [r[j] for r in out_rows[:15]]})
    else:
        pivot_headers = ["指标", "结果"]
        pivot_rows = []
        if measure_fields:
            for metric in measure_fields:
                pivot_rows.append([metric, _json_value(_aggregate(work[metric], aggregation))])
        else:
            pivot_rows.append(["记录数", int(len(work))])
        chart_categories = [str(r[0]) for r in pivot_rows]
        chart_series = [{"name": "结果", "values": [r[1] for r in pivot_rows]}]

    # KPIs always refer to the full filtered data, not only the displayed top categories.
    kpis: list[dict[str, Any]] = [
        {"label": "读取记录数", "value": int(len(df)), "format": "integer"},
        {"label": "筛选后记录", "value": int(len(filtered)), "format": "integer"},
    ]
    if row_field:
        kpis.append({"label": f"{row_field}去重数", "value": int(filtered[row_field].nunique(dropna=True)), "format": "integer"})
    if measure_fields:
        primary = measure_fields[0]
        if pd.api.types.is_numeric_dtype(filtered[primary]):
            kpis.append({"label": f"{primary}合计", "value": _json_value(pd.to_numeric(filtered[primary], errors="coerce").sum(min_count=1)), "format": "number"})
            kpis.append({"label": f"{primary}平均值", "value": _json_value(pd.to_numeric(filtered[primary], errors="coerce").mean()), "format": "number"})
    return {
        "file": {"id": file_info["id"], "name": file_info["name"], "path": file_info["path"], "ext": file_info["ext"], "size": file_info["size"], "mtime": file_info["mtime"]},
        "sheet_names": sheets, "sheet_name": req.sheet_name if req.sheet_name in sheets else sheets[0],
        "columns": _column_metadata(df), "preview": _json_records(df, PREVIEW_ROWS),
        "preview_rows": min(len(df), PREVIEW_ROWS), "rows_read": int(len(df)), "rows_matched": int(len(filtered)),
        "is_truncated": truncated, "max_analysis_rows": MAX_ANALYSIS_ROWS,
        "filters_applied": len(req.filters), "aggregation": aggregation,
        "pivot": {"headers": pivot_headers, "rows": pivot_rows, "omitted_categories": omitted_categories,
                  "display_limit": req.max_categories},
        "chart": {"categories": chart_categories, "series": chart_series},
        "kpis": kpis,
    }


@app.get("/api/analysis/file/{file_id}")
def analysis_file_info(file_id: int, sheet_name: str | None = None) -> dict[str, Any]:
    file_info, path = _get_indexed_file(file_id)
    sheets = _workbook_sheets(path)
    sheet = sheet_name if sheet_name in sheets else (sheets[0] if sheets else None)
    df = _normalise_df(_read_table(path, None if sheet == "数据" and path.suffix.lower() in {".csv", ".tsv"} else sheet, nrows=PREVIEW_ROWS))
    return {"file": {"id": file_info["id"], "name": file_info["name"], "path": file_info["path"], "ext": file_info["ext"], "size": file_info["size"], "mtime": file_info["mtime"]},
            "sheet_names": sheets, "sheet_name": sheet, "columns": _column_metadata(df),
            "preview": _json_records(df, PREVIEW_ROWS), "preview_rows": len(df),
            "max_analysis_rows": MAX_ANALYSIS_ROWS, "max_analysis_file_mb": MAX_ANALYSIS_FILE_MB}


@app.post("/api/analysis/run")
def run_analysis(req: AnalysisRequest) -> dict[str, Any]:
    started = time.perf_counter()
    result = _analysis_compute(req)
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


@app.post("/api/analysis/export")
def export_analysis(req: AnalysisRequest) -> StreamingResponse:
    result = _analysis_compute(req)
    table = pd.DataFrame(result["pivot"]["rows"], columns=result["pivot"]["headers"])
    detail_info, file_path = _get_indexed_file(req.file_id)
    full_df, _, _ = _load_analysis_frame(detail_info, file_path, req.sheet_name)
    filtered = _apply_filters(full_df, req.filters)
    output = BytesIO()
    safe_name = Path(detail_info["name"]).stem[:60]
    try:
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            table.to_excel(writer, index=False, sheet_name="交叉汇总")
            filtered.head(200000).to_excel(writer, index=False, sheet_name="筛选后明细")
            pd.DataFrame([{"文件": detail_info["name"], "工作表": result["sheet_name"],
                           "聚合方式": req.aggregation, "行维度": req.row_field or "", "列维度": req.column_field or "",
                           "分析时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "读取记录数": result["rows_read"], "筛选后记录数": result["rows_matched"],
                           "是否达到读取上限": result["is_truncated"]}]).to_excel(writer, index=False, sheet_name="分析说明")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成 Excel 失败：{str(exc)[:180]}")
    output.seek(0)
    filename = quote(f"{safe_name}_分析结果.xlsx")
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


@app.get("/api/analysis/saved")
def list_saved_analyses(file_id: int | None = None) -> dict[str, Any]:
    with db_connect() as conn:
        if file_id is None:
            rows = conn.execute("SELECT id,name,file_id,config_json,created_at,updated_at FROM saved_analyses ORDER BY updated_at DESC LIMIT 100").fetchall()
        else:
            rows = conn.execute("SELECT id,name,file_id,config_json,created_at,updated_at FROM saved_analyses WHERE file_id=? ORDER BY updated_at DESC LIMIT 100", (file_id,)).fetchall()
    return {"results": [{"id": r["id"], "name": r["name"], "file_id": r["file_id"],
                          "config": json.loads(r["config_json"]), "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]}


@app.post("/api/analysis/saved")
def save_analysis(req: SaveAnalysisRequest) -> dict[str, Any]:
    _get_indexed_file(req.file_id)  # Validate access before storing a config.
    now = datetime.now().isoformat(timespec="seconds")
    with db_connect() as conn:
        cur = conn.execute("INSERT INTO saved_analyses(name,file_id,config_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (req.name.strip(), req.file_id, json.dumps(req.config, ensure_ascii=False), now, now))
        saved_id = cur.lastrowid
    return {"ok": True, "id": saved_id, "name": req.name.strip(), "message": "分析方案已保存。"}


@app.delete("/api/analysis/saved/{saved_id}")
def delete_saved_analysis(saved_id: int) -> dict[str, Any]:
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM saved_analyses WHERE id=?", (saved_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="未找到这条分析方案。")
    return {"ok": True, "message": "已删除保存的分析方案。"}