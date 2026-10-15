# -*- coding: utf-8 -*-
"""内网极速文件检索原型：元数据索引 + 本地排序 + 可选 AI 候选重排。"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager, contextmanager
import sqlite3
import threading
import time
import unicodedata
import math
import secrets
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Any, Literal
from io import BytesIO
from urllib.parse import quote

import pandas as pd
from pydantic import BaseModel, Field

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
_raw_db_path = Path(os.getenv("INDEX_DB_PATH", str(APP_DIR / "data" / "file_index.db"))).expanduser()
DB_PATH = _raw_db_path if _raw_db_path.is_absolute() else (APP_DIR / _raw_db_path).resolve()
FILE_ROOT = os.getenv("FILE_ROOT", "").strip()
APP_ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "").strip()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("file_finder")
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
    if not APP_ACCESS_TOKEN:
        logger.warning("APP_ACCESS_TOKEN is empty; API routes have no shared-token gate. Use localhost or configure enterprise SSO/reverse proxy before LAN exposure.")
    yield


app = FastAPI(title="企业文件检索与轻量 BI 分析中心", version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.middleware("http")
async def optional_shared_access_token(request: Request, call_next):
    """Optional shared token gate for internal deployments; not a replacement for user ACL/SSO."""
    if APP_ACCESS_TOKEN and request.url.path.startswith("/api/"):
        supplied = request.headers.get("x-app-token", "")
        if not supplied or not secrets.compare_digest(supplied, APP_ACCESS_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "系统启用了访问令牌，请输入管理员配置的 APP_ACCESS_TOKEN。"})
    return await call_next(request)

_scan_lock = threading.Lock()
_scan_state_lock = threading.RLock()
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
    """Open one connection. Callers should use db_session() to guarantee closure."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db_session():
    """Commit on success, roll back on error, and always close the connection."""
    conn = db_connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _scan_state_snapshot() -> dict[str, Any]:
    with _scan_state_lock:
        return dict(_scan_state)


def _scan_state_replace(value: dict[str, Any]) -> None:
    with _scan_state_lock:
        _scan_state.clear()
        _scan_state.update(value)


def _scan_state_update(**changes: Any) -> None:
    with _scan_state_lock:
        _scan_state.update(changes)


def _scan_state_increment(key: str, amount: int = 1) -> None:
    with _scan_state_lock:
        _scan_state[key] = int(_scan_state.get(key, 0)) + amount


_root_scope_cache_lock = threading.Lock()
_root_scope_cache_source: str | None = None
_root_scope_cache_value = ""


def root_scope_key() -> str:
    """Stable, filesystem-I/O-free scope key for the configured root.

    Do not call realpath/stat on every search: resolving a UNC path may touch the network share,
    which would defeat the purpose of an index-only query path. A symlink alias is treated as a
    different configured root and is rebuilt explicitly, which is safer than accidental mixing.
    """
    global _root_scope_cache_source, _root_scope_cache_value
    source = FILE_ROOT.strip()
    if not source:
        return ""
    with _root_scope_cache_lock:
        if source != _root_scope_cache_source:
            normalized = os.path.abspath(str(Path(source).expanduser()))
            _root_scope_cache_value = os.path.normcase(os.path.normpath(normalized))
            _root_scope_cache_source = source
        return _root_scope_cache_value


def _delete_index_rows(conn: sqlite3.Connection, where_sql: str, params: tuple[Any, ...], batch_size: int = 400) -> int:
    """Delete file metadata and FTS rows in bounded keyset batches, without loading all paths into RAM."""
    total = 0
    last_id = 0
    while True:
        rows = conn.execute(
            f"SELECT id FROM files WHERE ({where_sql}) AND id > ? ORDER BY id LIMIT ?",
            (*params, last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        ids = [int(row[0]) for row in rows]
        last_id = ids[-1]
        slots = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM file_fts WHERE rowid IN ({slots})", ids)
        conn.execute(f"DELETE FROM files WHERE id IN ({slots})", ids)
        total += len(ids)
    return total


def init_db() -> None:
    with db_session() as conn:
        # WAL is a database-level setting. Do it once at startup, not on every request.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                parent TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL,
                root_key TEXT NOT NULL DEFAULT '',
                last_seen_scan TEXT NOT NULL DEFAULT ''
            )
        """)
        # In-place migration for databases created by earlier MVP versions.
        file_columns = {row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
        if "root_key" not in file_columns:
            conn.execute("ALTER TABLE files ADD COLUMN root_key TEXT NOT NULL DEFAULT ''")
        if "last_seen_scan" not in file_columns:
            conn.execute("ALTER TABLE files ADD COLUMN last_seen_scan TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root_scan ON files(root_key,last_seen_scan)")
        # 通过文件名/路径的二元字符片段建立 FTS 候选召回。查询时不读取 Excel 内容。
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                name, name_grams, path_grams,
                tokenize='unicode61'
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('schema_version', '3')")
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
    return root.resolve()


def update_index_file(
    conn: sqlite3.Connection,
    file_path: Path,
    stat: os.stat_result,
    *,
    root_key: str | None = None,
    scan_id: str | None = None,
) -> str:
    """Upsert one file and stamp it for the current scan generation."""
    path_str = str(file_path.resolve(strict=False))
    root_key = root_key if root_key is not None else root_scope_key()
    scan_id = scan_id or uuid.uuid4().hex
    row = conn.execute(
        "SELECT id, size, mtime, name, root_key FROM files WHERE path = ?", (path_str,)
    ).fetchone()
    name = file_path.name
    parent = str(file_path.parent)
    ext = file_path.suffix.lower()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    now = time.time()
    unchanged = row and row["size"] == size and abs(row["mtime"] - mtime) < 0.0001 and row["name"] == name
    if unchanged and row["root_key"] == root_key:
        conn.execute("UPDATE files SET last_seen_scan=? WHERE id=?", (scan_id, row["id"]))
        return "unchanged"
    if row:
        file_id = row["id"]
        conn.execute(
            "UPDATE files SET name=?, parent=?, ext=?, size=?, mtime=?, indexed_at=?, root_key=?, last_seen_scan=? WHERE id=?",
            (name, parent, ext, size, mtime, now, root_key, scan_id, file_id),
        )
        if unchanged:
            return "updated"
        conn.execute("DELETE FROM file_fts WHERE rowid=?", (file_id,))
        action = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO files(path,name,parent,ext,size,mtime,indexed_at,root_key,last_seen_scan) VALUES(?,?,?,?,?,?,?,?,?)",
            (path_str, name, parent, ext, size, mtime, now, root_key, scan_id),
        )
        file_id = cur.lastrowid
        action = "indexed"
    conn.execute("INSERT INTO file_fts(rowid,name,name_grams,path_grams) VALUES(?,?,?,?)",
                 (file_id, name, grams_text(name), grams_text(parent)))
    return action


def _scan_worker(lock_already_acquired: bool = False) -> None:
    """Build/update the index in the background; only prune entries after a clean full scan."""
    if not lock_already_acquired and not _scan_lock.acquire(blocking=False):
        return
    start = time.time()
    scan_id = uuid.uuid4().hex
    logger.info("index scan started; scan_id=%s", scan_id)
    _scan_state_replace({
        "running": True, "phase": "scanning", "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None, "seen": 0, "indexed": 0, "updated": 0, "deleted": 0, "errors": 0,
        "message": "正在扫描目录并增量更新索引；查询不需要等待扫描结束。",
    })
    walk_error_count = 0
    walk_error_samples: list[str] = []

    def on_walk_error(exc: OSError) -> None:
        nonlocal walk_error_count
        walk_error_count += 1
        if len(walk_error_samples) < 5:
            walk_error_samples.append(f"{type(exc).__name__}: {exc}")

    try:
        root = get_root()
        root_key = root_scope_key()
        with db_session() as conn:
            for current, dirs, filenames in os.walk(root, followlinks=False, onerror=on_walk_error):
                dirs[:] = [d for d in dirs if d not in {"$RECYCLE.BIN", "System Volume Information", ".snapshot"}]
                for filename in filenames:
                    if filename.startswith("~$"):
                        continue  # Excel editing lock/temp files are not real business workbooks.
                    ext = Path(filename).suffix.lower()
                    if ALLOWED_EXTENSIONS and ext not in ALLOWED_EXTENSIONS:
                        continue
                    fp = Path(current) / filename
                    try:
                        # Avoid indexing file symlinks that may point outside the approved root.
                        if fp.is_symlink():
                            continue
                        stat = fp.stat()
                        if not fp.is_file():
                            continue
                        _scan_state_increment("seen")
                        action = update_index_file(conn, fp, stat, root_key=root_key, scan_id=scan_id)
                        if action in ("indexed", "updated"):
                            _scan_state_increment(action)
                        seen = _scan_state_snapshot()["seen"]
                        if seen % 300 == 0:
                            conn.commit()
                            _scan_state_update(message=f"已检查 {seen:,} 个表格文件，新增 {_scan_state_snapshot()['indexed']:,} 个。")
                    except (PermissionError, FileNotFoundError, OSError):
                        _scan_state_increment("errors")
                conn.commit()

            state = _scan_state_snapshot()
            scan_complete = walk_error_count == 0 and state["errors"] == 0
            if scan_complete:
                # Generation markers mean we do not need to keep every path in memory or load all DB paths.
                stale_deleted = _delete_index_rows(
                    conn, "root_key = ? AND last_seen_scan <> ?", (root_key, scan_id)
                )
                # This service indexes one configured root at a time. When FILE_ROOT changes,
                # old-root entries are hidden from search during scanning and purged only after
                # the new root has completed successfully.
                old_root_deleted = _delete_index_rows(conn, "root_key <> ?", (root_key,))
                _scan_state_update(deleted=stale_deleted + old_root_deleted)
                conn.execute(
                    "DELETE FROM saved_analyses WHERE file_id NOT IN (SELECT id FROM files WHERE root_key = ?)",
                    (root_key,),
                )
                conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('indexed_root',?)", (root_key,))
                conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)",
                             (f"last_scan_at:{root_key}", datetime.now().isoformat(timespec="seconds")))
                # Keep the legacy key for older deployments that may still read it.
                conn.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('last_scan_at',?)",
                             (datetime.now().isoformat(timespec="seconds"),))
                conn.commit()

        elapsed = time.time() - start
        _scan_state_update(phase="complete" if scan_complete else "partial")
        if scan_complete:
            state = _scan_state_snapshot()
            _scan_state_update(message=f"索引更新完成，用时 {elapsed:.1f} 秒；新增 {state['indexed']:,}，更新 {state['updated']:,}，清理 {state['deleted']:,}。")
            logger.info("index scan completed; scan_id=%s seen=%s indexed=%s updated=%s deleted=%s elapsed=%.2f errors=%s",
                        scan_id, state["seen"], state["indexed"], state["updated"], state["deleted"], elapsed, state["errors"])
        else:
            sample = "；示例：" + " | ".join(walk_error_samples) if walk_error_samples else ""
            _scan_state_update(message=f"扫描结束，用时 {elapsed:.1f} 秒；发现 {walk_error_count + _scan_state_snapshot()['errors']} 个访问异常，已保留疑似失效索引，避免误删。建议检查共享盘权限后重试。{sample}")
            logger.warning("index scan partial; scan_id=%s walk_errors=%s file_errors=%s elapsed=%.2f",
                           scan_id, walk_error_count, _scan_state_snapshot()["errors"], elapsed)
    except Exception as exc:
        logger.exception("index scan failed; scan_id=%s", scan_id)
        _scan_state_update(phase="error", message=f"建索引失败：{type(exc).__name__}: {exc}")
    finally:
        _scan_state_update(running=False, finished_at=datetime.now().isoformat(timespec="seconds"))
        _scan_lock.release()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    active_root_key = root_scope_key()
    with db_session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM files WHERE root_key=?", (active_root_key,)).fetchone()[0] if active_root_key else 0
    return {"ok": True, "root_configured": bool(FILE_ROOT), "ai_configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL),
            "access_token_enabled": bool(APP_ACCESS_TOKEN), "indexed_files": count}


@app.get("/api/index/status")
def index_status() -> dict[str, Any]:
    active_root_key = root_scope_key()
    with db_session() as conn:
        if active_root_key:
            count = conn.execute("SELECT COUNT(*) FROM files WHERE root_key=?", (active_root_key,)).fetchone()[0]
            total_size = conn.execute("SELECT COALESCE(SUM(size),0) FROM files WHERE root_key=?", (active_root_key,)).fetchone()[0]
        else:
            count, total_size = 0, 0
        last = conn.execute("SELECT value FROM app_meta WHERE key=?", (f"last_scan_at:{active_root_key}",)).fetchone() if active_root_key else None
    return {**_scan_state_snapshot(), "indexed_files": count, "indexed_size_bytes": total_size,
            "last_scan_at": last[0] if last else None, "root_configured": bool(FILE_ROOT),
            "root_display": FILE_ROOT or "尚未配置", "ai_configured": bool(AI_BASE_URL and AI_API_KEY and AI_MODEL),
            "access_token_enabled": bool(APP_ACCESS_TOKEN)}


@app.post("/api/index/start")
def start_index() -> dict[str, Any]:
    get_root()  # Fast path validation before taking the scan lock.
    # Acquire in the request handler to prevent two near-simultaneous requests from both claiming success.
    if not _scan_lock.acquire(blocking=False):
        return {"started": False, "message": "索引任务已经在运行。", **_scan_state_snapshot()}
    t = threading.Thread(target=_scan_worker, kwargs={"lock_already_acquired": True}, daemon=True)
    try:
        t.start()
    except Exception:
        _scan_lock.release()
        raise
    return {"started": True, "message": "索引任务已启动，可继续搜索已建立的索引。"}


def candidate_search(
    conn: sqlite3.Connection, q: str, limit: int = 500, ext: str = "", root_key: str | None = None
) -> list[dict[str, Any]]:
    match = fts_query_for(q)
    if not match:
        return []
    root_key = root_key if root_key is not None else root_scope_key()
    if not root_key:
        return []
    try:
        if ext:
            rows = conn.execute("""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? AND f.ext = ? AND f.root_key = ? ORDER BY rank LIMIT ?
            """, (match, ext, root_key, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? AND f.root_key = ? ORDER BY rank LIMIT ?
            """, (match, root_key, limit)).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("local FTS search failed (%s); returning no candidates", type(exc).__name__)
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
    except Exception as exc:
        logger.warning("AI query expansion failed (%s); continuing with local index only", type(exc).__name__)
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
    except Exception as exc:
        logger.warning("AI candidate reranking failed (%s); retaining local ranking", type(exc).__name__)
        return candidates


@app.get("/api/search")
def search(q: str = Query(default="", max_length=300), limit: int = Query(default=30, ge=1, le=100),
           ext: str = Query(default=""), sort: str = Query(default="relevance")) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"query": q, "count": 0, "results": [], "ai_used": False, "message": "请输入文件名、业务关键词或自然语言描述。"}
    if not FILE_ROOT:
        return {"query": query, "count": 0, "results": [], "ai_used": False,
                "message": "尚未配置 FILE_ROOT。请在 .env 中设置文件根目录。"}
    start = time.perf_counter()
    wanted_ext = (ext.lower() if ext.startswith(".") else "." + ext.lower()) if ext else ""
    active_root_key = root_scope_key()
    with db_session() as conn:
        candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key)
        ai_used = False
        ai_expanded = False
        # 第一步始终是本地索引。完全无候选时，AI 只改写查询词，不接收全量文件名；
        # 每个改写词再由 FTS 索引检索，候选集去重后才进入后续排序。
        if not candidates and AI_BASE_URL and AI_API_KEY and AI_MODEL:
            expanded_terms = ai_expand_query(query)
            merged: dict[int, dict[str, Any]] = {}
            for term in expanded_terms:
                for item in candidate_search(conn, term, limit=120, ext=wanted_ext, root_key=active_root_key):
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
    logger.info("search completed; query_length=%s results=%s candidates=%s elapsed_ms=%s ai_used=%s ai_expanded=%s",
                len(query), len(results), len(candidates), elapsed_ms, ai_used, ai_expanded)
    return {"query": query, "count": len(results), "total_candidates": len(candidates), "results": results,
            "elapsed_ms": elapsed_ms, "ai_used": ai_used, "ai_expanded": ai_expanded,
            "message": "已使用索引检索" + ("；AI 扩展关键词并对索引候选进行重排。" if ai_used and ai_expanded else ("；AI 对索引候选进行重排。" if ai_used else ("；已使用 AI 扩展关键词。" if ai_expanded else "。")))}


@app.get("/api/recent")
def recent(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    active_root_key = root_scope_key()
    if not active_root_key:
        return {"results": []}
    with db_session() as conn:
        rows = conn.execute("SELECT id,path,name,parent,ext,size,mtime FROM files WHERE root_key=? ORDER BY mtime DESC LIMIT ?", (active_root_key, limit)).fetchall()
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
            "access_token_enabled": bool(APP_ACCESS_TOKEN),
            "note": "服务进程必须能直接读取配置的共享目录；访问令牌是共享入口保护，不替代企业登录与逐用户文件权限。"}


# ---------------------------------------------------------------------------
# 轻量 BI 分析模块：只分析用户主动选择且已在索引中的文件
# ---------------------------------------------------------------------------
class AnalysisFilter(BaseModel):
    field: str = Field(min_length=1, max_length=200)
    operator: Literal["contains", "equals", "not_equals", "gt", "gte", "lt", "lte", "is_empty", "not_empty"] = "contains"
    value: str = Field(default="", max_length=500)


class AnalysisRequest(BaseModel):
    file_id: int
    sheet_name: str | None = None
    row_field: str | None = None
    column_field: str | None = None
    value_fields: list[str] = Field(default_factory=list, max_length=5)
    aggregation: Literal["sum", "count", "mean", "median", "min", "max", "nunique"] = "sum"
    filters: list[AnalysisFilter] = Field(default_factory=list, max_length=20)
    max_categories: int = Field(default=30, ge=5, le=100)


class SaveAnalysisRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    file_id: int
    config: dict[str, Any]


def _get_indexed_file(file_id: int) -> tuple[dict[str, Any], Path]:
    """Only allow files already present in the metadata index and still under FILE_ROOT."""
    with db_session() as conn:
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
        if sheet_name not in (None, "数据"):
            raise HTTPException(status_code=422, detail="CSV/TSV 文件只有一个工作表：数据。")
        sep = "\t" if ext == ".tsv" else None
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                # Do not silently discard malformed rows: a partial dataset can yield plausible but wrong BI figures.
                if sep is None:
                    return pd.read_csv(path, encoding=encoding, sep=None, engine="python", nrows=nrows, on_bad_lines="error")
                return pd.read_csv(path, encoding=encoding, sep=sep, nrows=nrows, on_bad_lines="error")
            except UnicodeDecodeError as exc:
                last_error = exc
        raise HTTPException(status_code=422, detail=f"CSV 编码无法识别：{last_error}")
    if ext not in {".xlsx", ".xlsm", ".xls", ".xlsb", ".ods"}:
        raise HTTPException(status_code=415, detail=f"暂不支持分析 {ext} 文件。")
    engine = _engine_for(path)
    try:
        with pd.ExcelFile(path, engine=engine) as workbook:
            if sheet_name is not None and sheet_name not in workbook.sheet_names:
                raise HTTPException(status_code=422, detail=f"工作表不存在：{sheet_name}。可用工作表：{', '.join(workbook.sheet_names[:20])}")
            if not workbook.sheet_names:
                raise HTTPException(status_code=422, detail="工作簿中没有可读取的工作表。")
            target_sheet = sheet_name or workbook.sheet_names[0]
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
    if not sheets:
        raise HTTPException(status_code=422, detail="文件中没有可分析的工作表。")
    if sheet_name is not None and sheet_name not in sheets:
        raise HTTPException(status_code=422, detail=f"工作表不存在：{sheet_name}。请刷新工作表列表后重试。")
    chosen = sheet_name or sheets[0]
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
            date_like_value = bool(re.match(r"^\s*\d{4}[-/.年]\d{1,2}([-/ .月年]\d{1,2})?", value))
            if pd.api.types.is_datetime64_any_dtype(series) or date_like_value:
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
                original = df[metric]
                converted = pd.to_numeric(original, errors="coerce")
                non_empty = original.notna() & original.astype("string").str.strip().ne("")
                invalid = non_empty & converted.isna()
                if invalid.any():
                    examples = original[invalid].astype("string").drop_duplicates().head(3).tolist()
                    raise HTTPException(
                        status_code=422,
                        detail=f"指标“{metric}”有 {int(invalid.sum())} 条非空值无法转为数字（示例：{'、'.join(examples)}），为避免静默漏算，请先清理数据或改用计数。",
                    )
                if int(converted.notna().sum()) == 0 and int(original.notna().sum()) > 0:
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
        chart_categories = row_categories[:15]
        chart_series = []

        if measure_fields:
            # Compute each metric pivot once. The previous implementation recalculated the same
            # groupby for each output cell and again for charts, which was very costly on 200k rows.
            pivot_by_metric: dict[str, pd.DataFrame] = {}
            fill_value = 0 if aggregation in {"sum", "count", "nunique"} else None
            for metric in measure_fields:
                pivot = pd.pivot_table(
                    work, index=row_field, columns=column_field, values=metric,
                    aggfunc=aggregation, dropna=False, observed=True,
                )
                pivot = pivot.reindex(index=row_categories, columns=col_categories)
                if fill_value is not None:
                    pivot = pivot.fillna(fill_value)
                pivot_by_metric[metric] = pivot
                for category in col_categories:
                    header = f"{metric} / {category}" if len(measure_fields) > 1 else str(category)