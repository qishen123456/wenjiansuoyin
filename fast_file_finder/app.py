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

import base64
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
_raw_db_path = Path(os.getenv("INDEX_DB_PATH", str(APP_DIR / "data" / "file_index.db"))).expanduser()
DB_PATH = _raw_db_path if _raw_db_path.is_absolute() else (APP_DIR / _raw_db_path).resolve()
FILE_ROOT = os.getenv("FILE_ROOT", "").strip()
APP_ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "").strip()
SHARE_UNC_PREFIX = os.getenv("SHARE_UNC_PREFIX", r"\\172.11.111.11\share").strip()

def to_unc_path(posix_path: str) -> str:
    """将容器或 POSIX 路径转换为便于 Windows 资源管理器打开的 UNC 路径。"""
    if not posix_path:
        return ""
    root = FILE_ROOT.rstrip("/")
    rel = posix_path
    if root and posix_path.startswith(root):
        rel = posix_path[len(root):].lstrip("/")
    elif posix_path.startswith("/data/share"):
        rel = posix_path[len("/data/share"):].lstrip("/")

    clean_prefix = SHARE_UNC_PREFIX.rstrip("\\/")
    win_rel = rel.replace("/", "\\")
    return f"{clean_prefix}\\{win_rel}" if clean_prefix else win_rel

BUSINESS_PINYIN_MAP: dict[str, str] = {
    "cwb": "财务部", "cw": "财务", "gyl": "供应链", "scb": "市场部",
    "ds": "电商", "tm": "天猫", "jd": "京东", "dy": "抖音",
    "pdd": "拼多多", "kc": "库存", "xs": "销售", "ml": "毛利",
    "dz": "对账", "js": "结算", "zb": "周报", "rb": "日报", "yb": "月报",
    "tz": "台账", "mx": "明细", "qc": "千川", "zy": "自营", "fx": "分销"
}
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
AI_BACKUP_BASE_URL = os.getenv("AI_BACKUP_BASE_URL", "").rstrip("/")
AI_BACKUP_API_KEY = os.getenv("AI_BACKUP_API_KEY", "")
AI_BACKUP_MODEL = os.getenv("AI_BACKUP_MODEL", "")

def is_ai_configured() -> bool:
    return bool((AI_BASE_URL and AI_API_KEY and AI_MODEL) or (AI_BACKUP_BASE_URL and AI_BACKUP_API_KEY and AI_BACKUP_MODEL))

MAX_ANALYSIS_FILE_MB = int(os.getenv("MAX_ANALYSIS_FILE_MB", "256"))
MAX_ANALYSIS_ROWS = int(os.getenv("MAX_ANALYSIS_ROWS", "200000"))
PREVIEW_ROWS = int(os.getenv("PREVIEW_ROWS", "100"))

_archive_scheduler_stop = threading.Event()
_archive_scheduler_info: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 60,
    "last_run_at": 0.0,
    "run_count": 0,
    "auto_resolved_total": 0,
    "status": "active"
}

def _archive_scheduler_loop() -> None:
    logger.info("Archive compliance background scheduler started (60s loop).")
    while not _archive_scheduler_stop.is_set():
        try:
            with db_session() as conn:
                res = run_archive_compliance_check(conn)
                _archive_scheduler_info["last_run_at"] = time.time()
                _archive_scheduler_info["run_count"] += 1
                _archive_scheduler_info["auto_resolved_total"] += res.get("tasks_auto_resolved", 0)
        except Exception as exc:
            logger.warning("Background archive compliance check error: %s", exc)
        _archive_scheduler_stop.wait(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    try:
        with db_session() as conn:
            run_archive_compliance_check(conn)
    except Exception as e:
        logger.warning("Initial archive compliance check failed: %s", e)

    scheduler_thread = threading.Thread(target=_archive_scheduler_loop, daemon=True, name="ArchiveScheduler")
    scheduler_thread.start()

    if not APP_ACCESS_TOKEN:
        logger.warning("APP_ACCESS_TOKEN is empty; API routes have no shared-token gate. Use localhost or configure enterprise SSO/reverse proxy before LAN exposure.")
    try:
        yield
    finally:
        _archive_scheduler_stop.set()


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

        # 归档时效检测规则与待办任务中心数据表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_path TEXT NOT NULL,
                period_type TEXT NOT NULL DEFAULT 'weekly',
                period_days INTEGER NOT NULL DEFAULT 7,
                responsible_person TEXT DEFAULT '数据专员',
                is_active INTEGER NOT NULL DEFAULT 1,
                last_checked_at REAL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS archive_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER NOT NULL,
                rule_name TEXT NOT NULL,
                target_path TEXT NOT NULL,
                period_desc TEXT NOT NULL,
                responsible_person TEXT DEFAULT '',
                last_file_time REAL DEFAULT 0,
                last_file_name TEXT DEFAULT '',
                overdue_days INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                submit_note TEXT DEFAULT '',
                proof_image TEXT DEFAULT '',
                submitted_by TEXT DEFAULT '',
                submitted_at REAL DEFAULT 0,
                reviewed_by TEXT DEFAULT '',
                reviewed_at REAL DEFAULT 0,
                review_comment TEXT DEFAULT '',
                resolved_at REAL DEFAULT 0,
                resolved_file TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_tasks_status ON archive_tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_tasks_rule ON archive_tasks(rule_id)")

        # 预置典型部门归档监控规则
        rule_count = conn.execute("SELECT count(*) FROM archive_rules").fetchone()[0]
        if rule_count == 0:
            now_ts = time.time()
            default_rules = [
                ("9-新品简报 - 日增合规监控", "/data/share/9-新品简报", "daily", 1, "新品运营组"),
                ("12-推广ROI周报 - 7天周增监控", "/data/share/12-推广ROI周报", "weekly", 7, "投放推广组"),
                ("2-阿里平台-分销 - 30天月增核算", "/data/share/2-阿里平台-分销", "monthly", 30, "渠道分销部"),
                ("11-小红书达人底层 - 7天更新监控", "/data/share/11-小红书达人底层", "weekly", 7, "达人媒介部"),
                ("23-销售数据汇总 - 7天归档核查", "/data/share/23-销售数据汇总", "weekly", 7, "销售运营组"),
            ]
            for r in default_rules:
                conn.execute("""
                    INSERT INTO archive_rules (name, target_path, period_type, period_days, responsible_person, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                """, (r[0], r[1], r[2], r[3], r[4], now_ts))

        # 人员权限与飞书打通数据表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT DEFAULT '',
                password TEXT DEFAULT '123456',
                feishu_id TEXT DEFAULT '',
                email TEXT DEFAULT '',
                department TEXT DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                avatar TEXT DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # 兼容性迁移检查
        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(system_users)").fetchall()}
        if "phone" not in user_cols:
            conn.execute("ALTER TABLE system_users ADD COLUMN phone TEXT DEFAULT ''")
        if "password" not in user_cols:
            conn.execute("ALTER TABLE system_users ADD COLUMN password TEXT DEFAULT '123456'")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_users_role ON system_users(role)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_system_users_phone ON system_users(phone)")

        # 默认预置内网企业成员（电话号码+密码登录体系）测试数据
        user_count = conn.execute("SELECT count(*) FROM system_users").fetchone()[0]
        if user_count == 0:
            now_ts = time.time()
            default_users = [
                ("张明", "13800100001", "admin123", "IT系统与数据治理部", "super_admin", "active", "ZM", "zhangming@company.com"),
                ("王雪", "13800100002", "mgr123", "电商运营部 / 投放推广组", "dept_admin", "active", "WX", "wangxue@company.com"),
                ("赵强", "13800100003", "mgr123", "经营管理部 / 渠道分销部", "dept_admin", "active", "ZQ", "zhaoqiang@company.com"),
                ("陈晨", "13800100004", "123456", "财务运营部 / 利润核算组", "user", "active", "CC", "chenchen@company.com"),
                ("刘洋", "13800100005", "123456", "新零售事业部 / 商品运营组", "user", "active", "LY", "liuyang@company.com"),
            ]
            for u in default_users:
                conn.execute("""
                    INSERT INTO system_users (name, phone, password, department, role, status, avatar, email, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (u[0], u[1], u[2], u[3], u[4], u[5], u[6], u[7], now_ts, now_ts))
        else:
            # 平滑升级已有老数据的空手机号
            conn.execute("UPDATE system_users SET phone = '13800100001', password = 'admin123' WHERE id = 1 AND (phone = '' OR phone IS NULL)")
            conn.execute("UPDATE system_users SET phone = '13800100002', password = 'mgr123' WHERE id = 2 AND (phone = '' OR phone IS NULL)")
            conn.execute("UPDATE system_users SET phone = '13800100003', password = 'mgr123' WHERE id = 3 AND (phone = '' OR phone IS NULL)")
            conn.execute("UPDATE system_users SET phone = '13800100004', password = '123456' WHERE id = 4 AND (phone = '' OR phone IS NULL)")
            conn.execute("UPDATE system_users SET phone = '13800100005', password = '123456' WHERE id = 5 AND (phone = '' OR phone IS NULL)")


        # 飞书打通配置预置（可选，默认空）
        feishu_meta = conn.execute("SELECT value FROM app_meta WHERE key = 'feishu_config'").fetchone()
        if not feishu_meta:
            default_cfg = json.dumps({
                "app_id": "",
                "app_secret": "",
                "webhook_url": "",
                "notify_enabled": False,
                "sync_enabled": False,
                "last_synced_at": 0,
                "synced_users_count": 0,
                "synced_depts_count": 0
            }, ensure_ascii=False)
            conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES ('feishu_config', ?)", (default_cfg,))



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


def fts_query_for(q: str, match_mode: str = "all", search_target: str = "title") -> str:
    clean = re.sub(r"\.(xlsx|xls|csv|xlsm)$", "", q.strip(), flags=re.IGNORECASE)
    terms = [t.strip() for t in re.split(r"\s+", clean) if t.strip()]
    if not terms:
        terms = [clean] if clean else []
    if not terms:
        return ""

    col_prefix = "name_grams:" if search_target == "title" else ""

    if len(terms) > 1:
        term_subqueries = []
        for term in terms:
            words = [term]
            if term.lower() in BUSINESS_PINYIN_MAP:
                words.append(BUSINESS_PINYIN_MAP[term.lower()])
            sub_grams: list[str] = []
            for w in words:
                sub_grams.extend(char_grams(w)[:16])
            safe = [g.replace('"', '""') for g in list(dict.fromkeys(sub_grams))]
            if safe:
                term_subqueries.append("(" + " OR ".join(f'{col_prefix}"{g}"' for g in safe) + ")")
        if term_subqueries:
            sep = " AND " if match_mode == "all" else " OR "
            return sep.join(term_subqueries)

    # 单个词或短语（检测是否为高频拼音缩写，如 cwb -> 财务部）
    raw_term = clean.lower()
    words = [clean]
    if raw_term in BUSINESS_PINYIN_MAP:
        words.append(BUSINESS_PINYIN_MAP[raw_term])
    grams: list[str] = []
    for w in words:
        grams.extend(char_grams(w)[:32])
    safe = [g.replace('"', '""') for g in list(dict.fromkeys(grams))]
    if not safe:
        return ""
    if match_mode == "all" and len(safe) <= 4:
        return " AND ".join(f'{col_prefix}"{g}"' for g in safe)
    return " OR ".join(f'{col_prefix}"{g}"' for g in safe)


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


@app.get("/workbench", response_class=HTMLResponse)
def workbench() -> str:
    return (APP_DIR / "static" / "bi_workbench.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    active_root_key = root_scope_key()
    with db_session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM files WHERE root_key=?", (active_root_key,)).fetchone()[0] if active_root_key else 0
    return {"ok": True, "root_configured": bool(FILE_ROOT), "ai_configured": is_ai_configured(),
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
            "root_display": FILE_ROOT or "尚未配置", "ai_configured": is_ai_configured(),
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


def extract_query_features(q: str) -> dict[str, Any]:
    """智能解析查询词的核心语义实体、业务专有词与权重。"""
    q_norm = normalize_text(q)
    ext_match = re.search(r"\.(xlsx|xls|csv|xlsm)\b", q.lower())
    target_ext = ("." + ext_match.group(1)) if ext_match else ""
    alnum_tokens = re.findall(r"[a-zA-Z0-9]+", q)
    clean_q = re.sub(r"[^\w\u4e00-\u9fa5]+", " ", q)

    tokens_weighted: dict[str, float] = {}
    for t in alnum_tokens:
        t_lower = t.lower()
        if t_lower in ("xlsx", "xls", "csv", "xlsm"):
            continue
        if t_lower in BUSINESS_PINYIN_MAP:
            mapped_chinese = BUSINESS_PINYIN_MAP[t_lower]
            tokens_weighted[mapped_chinese] = 12.0
            for i in range(len(mapped_chinese) - 1):
                tokens_weighted[mapped_chinese[i:i+2]] = 9.0
        if len(t) <= 1:
            tokens_weighted[t_lower] = 1.0
        elif t.isupper() or len(t) <= 4:
            tokens_weighted[t_lower] = 9.0  # 核心大写英文缩写 (如 ROI, GMV, RPA, DY)
        elif t.isdigit() and len(t) == 4:
            tokens_weighted[t_lower] = 5.0  # 年份 (如 2024, 2026)
        else:
            tokens_weighted[t_lower] = 4.0

    chinese_blocks = re.findall(r"[\u4e00-\u9fa5]+", clean_q)
    for block in chinese_blocks:
        if len(block) >= 2:
            # 常见业务高频关键词额外加权
            for i in range(len(block) - 1):
                gram2 = block[i:i+2]
                w = 3.5
                if gram2 in ("渠道", "推广", "投放", "销售", "毛利", "对账", "结算", "库存", "周转", "回款", "分析", "周报", "日报", "月报", "台账", "明细", "千川", "自营", "旗舰", "业绩"):
                    w = 6.5
                tokens_weighted[gram2] = max(tokens_weighted.get(gram2, 0), w)
            for i in range(len(block) - 2):
                gram3 = block[i:i+3]
                tokens_weighted[gram3] = max(tokens_weighted.get(gram3, 0), 5.0)
            if 2 <= len(block) <= 4:
                tokens_weighted[block] = max(tokens_weighted.get(block, 0), 6.0)

    return {"q_norm": q_norm, "target_ext": target_ext, "tokens": tokens_weighted}


def score_candidate(features: dict[str, Any], file_name: str, parent: str, fts_rank: float, size: int, mtime: float) -> tuple[float, int]:
    name_norm = normalize_text(file_name)
    path_norm = normalize_text(parent)
    name_lower = file_name.lower()
    path_lower = parent.lower()
    q_norm = features["q_norm"]
    tokens = features["tokens"]

    score = 0.0
    name_hits = 0
    name_no_ext = os.path.splitext(name_norm)[0]
    q_no_ext = re.sub(r"\.(xlsx|xls|csv|xlsm)$", "", q_norm)

    # 1. 精确/长子串包含 (最高统治力)
    if q_no_ext and q_no_ext == name_no_ext:
        score += 850.0  # 完全同名
    elif q_no_ext and q_no_ext in name_no_ext:
        score += 550.0  # 文件名包含整个查询词
    elif name_no_ext and len(name_no_ext) >= 4 and name_no_ext in q_no_ext:
        score += 380.0  # 查询词包含整个文件名
    elif q_no_ext and q_no_ext in path_norm:
        score += 160.0  # 路径包含整个查询词

    total_token_weight = sum(tokens.values()) if tokens else 1.0
    matched_weight_name = 0.0
    matched_weight_path = 0.0

    strong_abbrs = [t for t, w in tokens.items() if w >= 8.0]
    has_strong_abbr = bool(strong_abbrs)
    name_has_strong_abbr = any(abbr in name_lower for abbr in strong_abbrs) if has_strong_abbr else True

    for tok, weight in tokens.items():
        if tok in name_lower:
            matched_weight_name += weight
            name_hits += 1
        elif tok in path_lower:
            matched_weight_path += weight * 0.2

    name_token_ratio = matched_weight_name / total_token_weight
    path_token_ratio = matched_weight_path / total_token_weight

    score += name_token_ratio * 350.0
    score += path_token_ratio * 50.0

    # 强力缩写词惩罚：如果用户输入了大写专有名词 (如 ROI)，但候选文件名中完全没有，严厉打折
    if has_strong_abbr and not name_has_strong_abbr:
        score *= 0.2

    # 连续子串长度奖励
    for l in (6, 5, 4, 3):
        if any(tok in name_norm for tok in tokens if len(tok) == l):
            score += l * 8.0
            break

    # 格式后缀吻合
    if features["target_ext"] and file_name.lower().endswith(features["target_ext"]):
        score += 15.0

    # 业务数据文件优先 (真实非空表)
    if size > 0:
        score += 15.0
    if "_补位_" in file_name or "mock_" in file_name.lower():
        score -= 200.0

    # BM25 轻微平滑
    score += max(0.0, min(10.0, -fts_rank * 0.1))

    # 归一化为 0 ~ 99.9 分
    final_score = min(99.9, max(5.0, score / 5.0))
    if q_no_ext and q_no_ext == name_no_ext:
        final_score = 100.0
    return round(final_score, 1), name_hits


def candidate_search(
    conn: sqlite3.Connection,
    q: str,
    limit: int = 500,
    ext: str = "",
    root_key: str | None = None,
    match_mode: str = "all",
    search_target: str = "title",
) -> list[dict[str, Any]]:
    match = fts_query_for(q, match_mode=match_mode, search_target=search_target)
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

    features = extract_query_features(q)
    results = []

    # 提取用户必须包含的所有核心关键词 (用于严格 AND 过滤)
    clean_q = re.sub(r"\.(xlsx|xls|csv|xlsm)$", "", q.strip(), flags=re.IGNORECASE)
    space_tokens = [t.lower() for t in re.split(r"\s+", clean_q) if t.strip()]

    for r in rows:
        name_lower = r["name"].lower()
        parent_lower = r["parent"].lower()
        scope_text = name_lower if search_target == "title" else (name_lower + " " + parent_lower)

        # 严格全包含检查：必须所有空格拆分的关键词都包含在内
        if match_mode == "all" and space_tokens:
            if not all(st in scope_text for st in space_tokens):
                continue

        score, name_hits = score_candidate(features, r["name"], r["parent"], r["rank"], r["size"], r["mtime"])
        results.append({
            "id": int(r["id"]), "name": r["name"], "path": r["path"], "parent": r["parent"],
            "unc_path": to_unc_path(r["path"]),
            "ext": r["ext"], "size": int(r["size"]), "mtime": float(r["mtime"]),
            "score": score, "name_hits": name_hits, "rank": float(r["rank"]),
            "match_reason": "标题全词严格包含" if (match_mode == "all" and search_target == "title") else "文件名/路径索引匹配"
        })

    # 优先看相关度得分，其次看文件名直接命中的关键词数，再次看非空业务表，最后看修改时间
    results.sort(key=lambda x: (-x["score"], -x["name_hits"], -(x["size"] > 0), -x["mtime"]))
    return results


def call_llm(messages: list[dict[str, str]], max_tokens: int = 800, temperature: float = 0.2) -> tuple[str, str]:
    """统一模型调用层：优先请求白山云配置；若遇阻自动降级到备用引擎（如百炼阿里）。"""
    endpoints = []
    if AI_BASE_URL and AI_API_KEY and AI_MODEL:
        endpoints.append({"url": AI_BASE_URL, "key": AI_API_KEY, "model": AI_MODEL, "name": f"主模型-{AI_MODEL}"})
    if AI_BACKUP_BASE_URL and AI_BACKUP_API_KEY and AI_BACKUP_MODEL:
        endpoints.append({"url": AI_BACKUP_BASE_URL, "key": AI_BACKUP_API_KEY, "model": AI_BACKUP_MODEL, "name": f"备用模型-{AI_BACKUP_MODEL}"})

    if not endpoints:
        return "", ""

    last_error = ""
    for ep in endpoints:
        target_url = ep["url"] if ep["url"].endswith("/chat/completions") else ep["url"] + "/chat/completions"
        ep_timeout = AI_TIMEOUT_SECONDS
        try:
            with httpx.Client(timeout=ep_timeout) as client:
                resp = client.post(
                    target_url,
                    headers={"Authorization": f"Bearer {ep['key']}", "Content-Type": "application/json"},
                    json={
                        "model": ep["model"],
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens
                    }
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content.strip(), ep["name"]
        except Exception as exc:
            last_error = f"{ep['name']}: {type(exc).__name__}: {exc}"
            logger.warning("LLM endpoint call failed (%s); trying fallback if configured", last_error)

    logger.error("All configured LLM endpoints failed; last_error=%s", last_error)
    return "", ""


def ai_expand_query(q: str) -> list[str]:
    """本地索引没有候选时，让模型只扩展查询词；随后仍由 FTS 索引执行检索。"""
    if not is_ai_configured():
        return []
    prompt = (
        "你是企业内部文件检索的查询改写助手。不要猜测或生成任何文件名，不需要访问文件。"
        "请将用户的自然语言描述改写为最多 5 个可用于文件名/目录名检索的短关键词或短语，"
        "包含必要的同义词、常见业务叫法、时间/地域/部门关键词。只输出 JSON："
        "{\"search_terms\":[\"关键词1\",\"关键词2\"]}。"
        "每个短语尽量简短，不要输出解释。\n用户查询：" + q
    )
    content, _ = call_llm([
        {"role": "system", "content": "只输出合法 JSON。"},
        {"role": "user", "content": prompt}
    ], max_tokens=200, temperature=0.0)
    if not content:
        return []
    try:
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
        logger.warning("parse AI query expansion failed (%s)", type(exc).__name__)
        return []


def ai_rerank(q: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """可选大模型语义重排：输入严格限制为索引筛出的候选，不扫描或上传全量文件名。"""
    if not (is_ai_configured() and candidates):
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
    content, _ = call_llm([
        {"role": "system", "content": "严格遵守输出 JSON 的要求，不得输出候选以外的文件。"},
        {"role": "user", "content": prompt}
    ], max_tokens=300, temperature=0.0)
    if not content:
        return candidates
    try:
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
        seen = {x["id"] for x in ranked}
        ranked.extend(c for c in candidates if c["id"] not in seen)
        return ranked
    except Exception as exc:
        logger.warning("AI candidate reranking parse failed (%s)", type(exc).__name__)
        return candidates


def extract_business_keywords(nl_query: str) -> list[str]:
    """从自然语言长句、问句中剥离口语停用词，提取真正的高权重业务实体词。"""
    cleaned = nl_query.strip()
    stopwords = [
        "帮我推测一下", "帮我推测", "推测一下", "帮我找找", "帮我查找", "帮我查找下", "帮我找一下", "帮我找下", "帮我找",
        "我想查一下", "我想查下", "我想查看", "我想查", "我想找找", "我想找", "请问一下", "请问",
        "网盘里有没有", "网盘中有没有", "网盘里有", "网盘中有", "网盘里", "网盘中", "有没有关于", "有没有",
        "放在哪个目录下", "在哪个目录下", "在哪个目录", "放在哪里", "在哪里", "在什么地方",
        "建议搜什么词", "建议搜什么关键词", "建议搜什么", "怎么搜", "怎么查",
        "相关的报表", "相关报表", "相关的表格", "相关表格", "相关的", "综合分析表", "明细汇总",
        "的报表", "的表格", "报表", "表格", "数据", "文件", "台账", "明细", "汇总"
    ]
    for sw in stopwords:
        cleaned = cleaned.replace(sw, " ")

    parts = re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fa5]+", cleaned)
    tokens: list[str] = []
    for p in parts:
        p_strip = p.strip()
        if not p_strip:
            continue
        if len(p_strip) <= 4:
            tokens.append(p_strip)
        else:
            tokens.append(p_strip)
            for i in range(len(p_strip) - 1):
                gram = p_strip[i:i+2]
                if gram not in ("一个", "一下", "什么", "关于", "可以", "如何"):
                    tokens.append(gram)

    seen = set()
    result = []
    for t in tokens:
        if len(t) >= 2 and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def ai_guess_and_supplement(conn: sqlite3.Connection, q: str) -> dict[str, Any]:
    """结合真实网盘索引、目录结构与大模型，解决员工日常寻表、查目录和推断报表的全部需求。"""
    active_root_key = root_scope_key()
    if not active_root_key or not q.strip():
        return {"reasoning": "", "model_used": "", "suggested_terms": [], "matched_files": []}

    # 1. 深度提取自然语言问句中的业务实体词
    tokens = extract_business_keywords(q)
    clean_search_query = " ".join(tokens) if tokens else q

    # 2. 先在已有 13 万个本地索引中进行相关度检索
    candidates = candidate_search(conn, clean_search_query, limit=10, match_mode="any") if clean_search_query else []

    # 3. 如果本地直接命中了高度吻合的业务文件（得分高于阈值或文件名包含实体词）
    if candidates and (candidates[0]["score"] >= 25.0 or any(t in candidates[0]["name"] for t in tokens if len(t) >= 2)):
        best = candidates[0]
        dir_clean = best["parent"].replace("/data/share", "").replace("\\", "/").strip("/")
        unc = best.get("unc_path") or to_unc_path(best["parent"] + "/" + best["name"])
        
        reasoning = (
            f"✅ **已在网盘中为您精确找到目标报表！**\n\n"
            f"📄 **报表名称**：`{best['name']}`\n"
            f"📁 **存放目录**：`{dir_clean or '根目录'}`\n"
            f"🔗 **Win 共享路径**：`{unc}`\n\n"
            f"💡 **快捷操作**：该报表现已就绪。您可直接点击下方【直接分析】启动轻量 BI 交叉汇总与趋势分析，或点击【下载原文件】在本地 Excel 中打开。"
        )

        matched_output = []
        for c in candidates[:5]:
            matched_output.append({
                "id": c["id"],
                "name": c["name"],
                "parent": c["parent"],
                "unc_path": c.get("unc_path") or to_unc_path(c["parent"] + "/" + c["name"]),
                "size": c.get("size", 0),
                "mtime": c.get("mtime", 0)
            })

        suggested = [t for t in tokens if len(t) >= 2 and t in best["name"]][:3]
        if not suggested:
            suggested = [best["name"].replace(".xlsx", "").replace(".xls", "")]

        return {
            "reasoning": reasoning,
            "model_used": "网盘语义直达引擎",
            "suggested_terms": suggested,
            "matched_files": matched_output
        }

    # 4. 如果没有直接高分命中，提取目录列表由大模型进行深度推理猜测
    clean_dirs: list[str] = []
    try:
        raw_parents = [r[0] for r in conn.execute(
            "SELECT DISTINCT parent FROM files WHERE root_key=? LIMIT 40", (active_root_key,)
        ).fetchall()]
        clean_dirs = sorted(list({p.replace("/data/share", "").replace("\\", "/").strip("/") for p in raw_parents if p}))[:30]
    except Exception:
        pass

    prompt = (
        "你是企业内网文件智能寻访助手。\n"
        f"用户正在寻找报表或文件：『{q}』\n"
        "目前本地索引未直接命中完全同名的表格。\n\n"
        "【企业网盘真实的代表性目录结构】：\n" +
        ("\n".join(f"- {d}" for d in clean_dirs[:18]) if clean_dirs else "（顶级业务目录）") +
        "\n\n请按以下结构简要、明确地回答：\n"
        "1. 【可能归属目录与命名推测】：结合上面的网盘结构，推断该文件最可能在哪个业务目录下（如推广ROI、阿里自营、分销等），以及常见的命名规范；\n"
        "2. 【推荐替代搜索词】：给出 2~3 个最可能查到的短关键词（请用双引号标出，如“推广 ROI”）；\n"
        "3. 语言精炼务实，直接给出结论。"
    )

    reply, model_name = call_llm([{"role": "user", "content": prompt}], max_tokens=600)
    if not reply:
        reply = (
            f"根据网盘目录结构推测，涉及“{'、'.join(tokens[:3]) or q}”的相关文件，"
            f"通常归属于自营或分销平台的相关数据汇总目录下。建议尝试按短词（如“{'”、“'.join(tokens[:3])}”）重搜。"
        )
        model_name = "本地推理引擎"

    extracted_terms = re.findall(r"“([^”]+)”|'([^']+)'|「([^」]+)」", reply)
    suggested = []
    for match in extracted_terms:
        term = (match[0] or match[1] or match[2]).strip()
        if term and len(term) <= 20 and term != q and term not in suggested:
            suggested.append(term)

    return {
        "reasoning": reply,
        "model_used": model_name,
        "suggested_terms": (suggested or tokens)[:4],
        "matched_files": []
    }


@app.get("/api/search")
def search(
    q: str = Query(default="", max_length=300),
    page: str = Query(default="1"),
    page_size: int = Query(default=10, ge=1, le=100),
    limit: int | None = Query(default=None, ge=1, le=100),
    ext: str = Query(default=""),
    sort: str = Query(default="relevance"),
    match_mode: Literal["all", "any"] = Query(default="all"),
    target: Literal["title", "path", "content"] = Query(default="title"),
    enable_ai: bool = Query(default=False),
) -> dict[str, Any]:
    query = q.strip()
    try:
        clean_page = int(page) if str(page).strip().isdigit() else 1
    except (ValueError, TypeError):
        clean_page = 1
    if clean_page < 1:
        clean_page = 1

    effective_page_size = limit if limit is not None else page_size
    effective_page = 1 if limit is not None else clean_page

    if not query:
        return {
            "query": q, "count": 0, "total_candidates": 0, "page": 1, "page_size": effective_page_size,
            "total_pages": 0, "results": [], "ai_used": False, "message": "请输入文件名、业务关键词或自然语言描述。"
        }
    if not FILE_ROOT:
        return {
            "query": query, "count": 0, "total_candidates": 0, "page": 1, "page_size": effective_page_size,
            "total_pages": 0, "results": [], "ai_used": False, "message": "尚未配置 FILE_ROOT。请在 .env 中设置文件根目录。"
        }
    start = time.perf_counter()
    wanted_ext = (ext.lower() if ext.startswith(".") else "." + ext.lower()) if ext else ""
    active_root_key = root_scope_key()
    with db_session() as conn:
        search_scope = "title" if target in ("title", "content") else "path"
        candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode=match_mode, search_target=search_scope)
        ai_used = False
        ai_expanded = False
        fallback_relaxed = False

        # 如果严格全词模式下没有结果，自动放宽为宽松匹配，避免用户因单字错漏导致无结果
        if not candidates and match_mode == "all":
            candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode="any", search_target=search_scope)
            if candidates:
                fallback_relaxed = True

        # 如果文件名未能命中，自动扩展检索所属目录/路径，满足用户搜部门名直接找表格的自然诉求
        if not candidates and search_scope == "title":
            candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode="any", search_target="path")
            if candidates:
                fallback_relaxed = True
                for c in candidates:
                    c["match_reason"] = "所属业务目录命中"

        ai_guess = None
        if not candidates:
            if enable_ai and is_ai_configured():
                # 显式开启 AI 选项时才触发外部大模型，保障常规搜索极限毫秒级吞吐
                ai_guess = ai_guess_and_supplement(conn, query)
                ai_used = True
            else:
                tokens = extract_business_keywords(query)
                ai_guess = {
                    "reasoning": f"未搜到包含“{query}”的文件。建议尝试拆分关键词，或点击右下角【🤖 AI 找表助手】由 DeepSeek 大模型深度推测其可能存放的业务目录。",
                    "model_used": "本地实体推理引擎",
                    "suggested_terms": tokens[:4],
                    "matched_files": []
                }

        if ext:
            wanted = ext.lower() if ext.startswith(".") else "." + ext.lower()
            candidates = [c for c in candidates if c["ext"] == wanted]

        if sort in ("modified", "time_desc", "time"):
            candidates.sort(key=lambda x: -x["mtime"])
        elif sort in ("time_asc", "modified_asc"):
            candidates.sort(key=lambda x: x["mtime"])
        elif sort == "name":
            candidates.sort(key=lambda x: x["name"].lower())
        elif sort == "name_desc":
            candidates.sort(key=lambda x: x["name"].lower(), reverse=True)
        elif sort == "size_desc":
            candidates.sort(key=lambda x: -x.get("size", 0))
        elif sort == "size_asc":
            candidates.sort(key=lambda x: x.get("size", 0))

        total_count = len(candidates)
        total_pages = (total_count + effective_page_size - 1) // effective_page_size if total_count > 0 else 0
        start_idx = (effective_page - 1) * effective_page_size
        results = candidates[start_idx : start_idx + effective_page_size]

    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info("search completed; query_length=%s results=%s candidates=%s match_mode=%s target=%s elapsed_ms=%s",
                len(query), len(results), total_count, match_mode, target, elapsed_ms)
    msg = "已按标题所有字段严格全包含检索。" if (match_mode == "all" and not fallback_relaxed) else (
        "全词严格匹配无候选，已自动为您放宽为相关度最优推荐。" if fallback_relaxed else "已使用相关度宽松索引检索。"
    )
    if not results and ai_guess:
        msg = "未搜到直接匹配文件，已由 AI 助手结合目录结构为您推测归属路径与搜索词。"
    return {
        "query": query, "count": len(results), "total_candidates": total_count,
        "page": effective_page, "page_size": effective_page_size, "total_pages": total_pages,
        "match_mode": match_mode, "target": target, "fallback_relaxed": fallback_relaxed,
        "results": results, "elapsed_ms": elapsed_ms, "ai_used": ai_used, "ai_expanded": ai_expanded,
        "ai_guess": ai_guess,
        "message": msg
    }


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    query: str = Field(default="", max_length=300)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


@app.post("/api/ai/chat")
def ai_chat(req: AIChatRequest) -> dict[str, Any]:
    """AI 找表对话框专属接口：结合网盘目录与相近文件进行多轮深度解答与推荐。"""
    with db_session() as conn:
        q = req.message.strip() or req.query.strip()
        guess = ai_guess_and_supplement(conn, q)
    return {
        "ok": True,
        "reply": guess["reasoning"],
        "model_used": guess["model_used"],
        "suggested_terms": guess["suggested_terms"],
        "matched_files": guess["matched_files"]
    }



@app.get("/api/recent")
def recent(limit: int = Query(default=10, ge=1, le=100)) -> dict[str, Any]:
    active_root_key = root_scope_key()
    if not active_root_key:
        return {"results": []}
    with db_session() as conn:
        rows = conn.execute("SELECT id,path,name,parent,ext,size,mtime FROM files WHERE root_key=? ORDER BY mtime DESC LIMIT ?", (active_root_key, limit)).fetchall()
    return {"results": [{"id": r["id"], "path": r["path"], "unc_path": to_unc_path(r["path"]), "name": r["name"], "parent": r["parent"],
                         "ext": r["ext"], "size": r["size"], "mtime": r["mtime"], "score": None,
                         "match_reason": "最近修改"} for r in rows]}


@app.api_route("/api/file/{file_id}/download", methods=["GET", "HEAD"])
def download_file(file_id: int) -> FileResponse:
    """提供企业员工最需要的浏览器直接下载功能，使用 RFC 5987 标准保证中文文件名不乱码。"""
    file_info, path = _get_indexed_file(file_id)
    filename = file_info["name"]
    quoted_filename = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"
    }
    return FileResponse(
        path=path,
        filename=filename,
        headers=headers,
        media_type="application/octet-stream"
    )


@app.get("/api/config-check")
def config_check() -> dict[str, Any]:
    root = FILE_ROOT
    path = Path(root).expanduser() if root else None
    return {"root_configured": bool(root), "root_exists": bool(path and path.exists() and path.is_dir()),
            "root_display": root or "尚未配置", "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
            "ai_configured": is_ai_configured(),
            "access_token_enabled": bool(APP_ACCESS_TOKEN),
            "note": "服务进程必须能直接读取配置的共享目录；访问令牌是共享入口保护，不替代企业登录与逐用户文件权限。"}


# ---------------------------------------------------------------------------
# 全网盘组织架构与统筹看板接口 (替代旧 BI 分析模块)
# ---------------------------------------------------------------------------

def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


_tree_cache: dict[str, Any] = {"time": 0, "data": None}


@app.get("/api/catalog/overview")
def get_catalog_overview() -> dict[str, Any]:
    """获取网盘全盘组织架构统筹概览指标，包括总文件、总目录、存储容量以及今日新增数量。"""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()

    with db_session() as conn:
        total_files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        today_added = conn.execute("SELECT count(*) FROM files WHERE mtime >= ?", (today_start,)).fetchone()[0]
        total_dirs = conn.execute("SELECT count(DISTINCT parent) FROM files").fetchone()[0]
        total_size = conn.execute("SELECT coalesce(sum(size), 0) FROM files").fetchone()[0]

        # 格式分布
        ext_rows = conn.execute("""
            SELECT ext, count(*) as cnt 
            FROM files 
            GROUP BY ext 
            ORDER BY cnt DESC 
            LIMIT 6
        """).fetchall()
        ext_distribution = [{"ext": r["ext"] or "未知", "count": r["cnt"]} for r in ext_rows]

        # 一级业务域统计
        dep_rows = conn.execute("""
            SELECT 
                CASE 
                    WHEN parent = '/data/share' THEN '根目录'
                    ELSE substr(parent, 13, instr(substr(parent, 13) || '/', '/') - 1)
                END as dept_name,
                count(*) as total_count,
                sum(CASE WHEN mtime >= ? THEN 1 ELSE 0 END) as today_count
            FROM files
            GROUP BY dept_name
            ORDER BY total_count DESC
        """, (today_start,)).fetchall()

        departments = [
            {
                "name": r["dept_name"],
                "total_files": r["total_count"],
                "today_added": int(r["today_count"] or 0)
            }
            for r in dep_rows
        ]

    return {
        "ok": True,
        "total_files": total_files,
        "today_added": today_added,
        "total_dirs": total_dirs,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "total_size_gb": round(total_size / (1024 * 1024 * 1024), 2),
        "today_date": now.strftime("%Y-%m-%d"),
        "ext_distribution": ext_distribution,
        "departments": departments
    }


@app.get("/api/catalog/tree")
def get_catalog_tree(only_today_branch: bool = Query(default=False)) -> dict[str, Any]:
    """构建网盘组织架构树，并自动向上累加(Rollup)子目录的所有文件数与今日新增数。"""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    now_ts = time.time()

    # 简单缓存 20 秒
    if _tree_cache["data"] and (now_ts - _tree_cache["time"] < 20) and not only_today_branch:
        return _tree_cache["data"]

    root_prefix = FILE_ROOT.rstrip("/") if FILE_ROOT else "/data/share"

    with db_session() as conn:
        rows = conn.execute("""
            SELECT parent, count(*) as cnt, sum(CASE WHEN mtime >= ? THEN 1 ELSE 0 END) as today_cnt
            FROM files
            GROUP BY parent
        """, (today_start,)).fetchall()

    tree: dict[str, Any] = {
        "name": "企业网盘总目录",
        "path": root_prefix,
        "file_count": 0,
        "today_added": 0,
        "children": {}
    }
    node_map: dict[str, Any] = {root_prefix: tree}

    for r in rows:
        parent_path = r["parent"]
        cnt = r["cnt"]
        today_cnt = int(r["today_cnt"] or 0)

        rel = parent_path[len(root_prefix):].strip("/")
        parts = rel.split("/") if rel else []

        cur_node = tree
        cur_path = root_prefix
        for part in parts:
            cur_path = f"{cur_path}/{part}"
            if cur_path not in node_map:
                new_node = {
                    "name": part,
                    "path": cur_path,
                    "file_count": 0,
                    "today_added": 0,
                    "children": {}
                }
                node_map[cur_path] = new_node
                cur_node["children"][part] = new_node
            cur_node = node_map[cur_path]

        cur_node["direct_files"] = cnt
        cur_node["direct_today"] = today_cnt

    def to_list_tree(node: dict[str, Any], depth: int = 0) -> dict[str, Any]:
        c_list = []
        for _, v in sorted(node["children"].items(), key=lambda x: (x[1].get("direct_today", 0), x[1].get("direct_files", 0)), reverse=True):
            child_tree = to_list_tree(v, depth + 1)
            # 如果开启了“仅看今日新增分支”，且该子树今日没有新增，则跳过
            if only_today_branch and child_tree["today_added"] == 0:
                continue
            c_list.append(child_tree)

        # 向上汇总
        total_f = node.get("direct_files", 0) + sum(c["file_count"] for c in c_list)
        total_t = node.get("direct_today", 0) + sum(c["today_added"] for c in c_list)

        return {
            "name": node["name"],
            "path": node["path"],
            "depth": depth,
            "direct_files": node.get("direct_files", 0),
            "file_count": total_f,
            "today_added": total_t,
            "children": c_list
        }

    final_tree = to_list_tree(tree, depth=0)
    result = {
        "ok": True,
        "today_date": now.strftime("%Y-%m-%d"),
        "total_today_added": final_tree["today_added"],
        "tree": final_tree
    }

    if not only_today_branch:
        _tree_cache["time"] = now_ts
        _tree_cache["data"] = result

    return result


@app.get("/api/catalog/files")
def get_catalog_files(
    path: str = Query(default="/data/share"),
    only_today: bool = Query(default=False),
    keyword: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    sort_by: str = Query(default="time"),
    sort_order: str = Query(default="desc"),
) -> dict[str, Any]:
    """查询指定组织架构节点（或子目录）下的具体文件明细，支持按创建时间/文件名/大小排序、今日新增过滤与分页。"""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()

    clean_path = path.rstrip("/")
    if not clean_path:
        clean_path = "/data/share"

    with db_session() as conn:
        where_clauses = ["(parent = ? OR parent LIKE ?)"]
        params: list[Any] = [clean_path, clean_path + "/%"]

        if only_today:
            where_clauses.append("mtime >= ?")
            params.append(today_start)

        if keyword.strip():
            where_clauses.append("name LIKE ?")
            params.append(f"%{keyword.strip()}%")

        where_sql = " AND ".join(where_clauses)

        total = conn.execute(f"SELECT count(*) FROM files WHERE {where_sql}", params).fetchone()[0]

        # 排序支持：按时间(mtime)、文件名(name)、文件大小(size)，支持升序/降序
        col_map = {
            "time": "mtime",
            "mtime": "mtime",
            "name": "name COLLATE NOCASE",
            "size": "size",
        }
        order_col = col_map.get(str(sort_by).lower(), "mtime")
        order_dir = "ASC" if str(sort_order).lower() == "asc" else "DESC"

        offset = (page - 1) * page_size
        query_sql = f"""
            SELECT id, path, name, parent, ext, size, mtime, indexed_at
            FROM files
            WHERE {where_sql}
            ORDER BY {order_col} {order_dir}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [page_size, offset]).fetchall()

        items = []
        for r in rows:
            is_today = (r["mtime"] >= today_start)
            m_dt = datetime.fromtimestamp(r["mtime"])
            time_display = m_dt.strftime("今天 %H:%M") if is_today else m_dt.strftime("%Y-%m-%d %H:%M")
            items.append({
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "parent": r["parent"],
                "unc_path": to_unc_path(r["path"]),
                "ext": r["ext"],
                "size": r["size"],
                "size_display": format_file_size(r["size"]),
                "mtime": r["mtime"],
                "time_display": time_display,
                "is_today": is_today
            })

    return {
        "ok": True,
        "path": clean_path,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }


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
        sheet_name = sheets[0]
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
    # 为避免分类维度与数值指标重名或数值字段作为维度时覆盖原指标，
    # 使用独立的分类分组列 _grp_row_ 和 _grp_col_，杜绝 pandas 字段冲突
    work = filtered.copy()
    if req.row_field:
        work["_grp_row_"] = work[req.row_field].map(lambda v: "(空白)" if pd.isna(v) else str(v))
    if req.column_field:
        work["_grp_col_"] = work[req.column_field].map(lambda v: "(空白)" if pd.isna(v) else str(v))

    aggregation = req.aggregation
    row_field, column_field = req.row_field, req.column_field
    measure_fields = metrics if metrics else []
    pivot_headers: list[str] = []
    pivot_rows: list[list[Any]] = []
    chart_categories: list[str] = []
    chart_series: list[dict[str, Any]] = []
    omitted_categories = False

    if row_field and column_field:
        row_counts = work["_grp_row_"].value_counts(dropna=False)
        col_counts = work["_grp_col_"].value_counts(dropna=False)
        row_categories = row_counts.head(req.max_categories).index.astype(str).tolist()
        col_categories = col_counts.head(12).index.astype(str).tolist()
        omitted_categories = len(row_counts) > len(row_categories) or len(col_counts) > len(col_categories)
        work = work[work["_grp_row_"].isin(row_categories) & work["_grp_col_"].isin(col_categories)]
        pivot_headers = [row_field]
        chart_categories = row_categories[:15]
        chart_series = []

        if measure_fields:
            pivot_by_metric: dict[str, pd.DataFrame] = {}
            fill_value = 0 if aggregation in {"sum", "count", "nunique"} else None
            for metric in measure_fields:
                pivot = pd.pivot_table(
                    work, index="_grp_row_", columns="_grp_col_", values=metric,
                    aggfunc=aggregation, dropna=False, observed=True,
                )
                pivot = pivot.reindex(index=row_categories, columns=col_categories)
                if fill_value is not None:
                    pivot = pivot.fillna(fill_value)
                pivot_by_metric[metric] = pivot
                for category in col_categories:
                    header = f"{metric} / {category}" if len(measure_fields) > 1 else str(category)
                    pivot_headers.append(header)
            for category in row_categories:
                cells: list[Any] = [category]
                for metric in measure_fields:
                    pivot = pivot_by_metric[metric]
                    for cat in col_categories:
                        val = pivot.at[category, cat] if (category in pivot.index and cat in pivot.columns) else None
                        cells.append(_json_value(val))
                pivot_rows.append(cells)
            chart_categories = row_categories[:15]
            chart_series = []
            for metric in measure_fields:
                pivot = pivot_by_metric[metric]
                for cat in col_categories[:6]:
                    series_name = f"{metric} / {cat}" if len(measure_fields) > 1 else str(cat)
                    vals = [_json_value(pivot.at[c, cat]) if c in pivot.index else 0 for c in chart_categories]
                    chart_series.append({"name": series_name, "values": vals})
        else:
            p = pd.crosstab(work["_grp_row_"], work["_grp_col_"])
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
        work["_grp_dim_"] = work[dim].map(lambda v: "(空白)" if pd.isna(v) else str(v))
        counts = work["_grp_dim_"].value_counts(dropna=False)
        categories = counts.head(req.max_categories).index.astype(str).tolist()
        omitted_categories = len(counts) > len(categories)
        work = work[work["_grp_dim_"].isin(categories)]
        pivot_headers = [dim]
        if measure_fields:
            pivot_headers.extend(measure_fields)
        else:
            pivot_headers.append("记录数")
        out_rows = []
        for category in categories:
            part = work[work["_grp_dim_"] == category]
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
    if not sheets:
        raise HTTPException(status_code=422, detail="工作簿中没有可读取的工作表。")
    if sheet_name is not None and sheet_name not in sheets:
        sheet_name = sheets[0]
    sheet = sheet_name or sheets[0]
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
    with db_session() as conn:
        if file_id is None:
            rows = conn.execute("SELECT id,name,file_id,config_json,created_at,updated_at FROM saved_analyses ORDER BY updated_at DESC LIMIT 100").fetchall()
        else:
            rows = conn.execute("SELECT id,name,file_id,config_json,created_at,updated_at FROM saved_analyses WHERE file_id=? ORDER BY updated_at DESC LIMIT 100", (file_id,)).fetchall()
    return {"results": [{"id": r["id"], "name": r["name"], "file_id": r["file_id"],
                          "config": json.loads(r["config_json"]), "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]}


@app.post("/api/analysis/saved")
def save_analysis(req: SaveAnalysisRequest) -> dict[str, Any]:
    _get_indexed_file(req.file_id)  # Validate access before storing a config.
    payload = json.dumps(req.config, ensure_ascii=False)
    if len(payload.encode("utf-8")) > 32 * 1024:
        raise HTTPException(status_code=413, detail="分析配置过大，不能超过 32 KB。")
    now = datetime.now().isoformat(timespec="seconds")
    with db_session() as conn:
        cur = conn.execute("INSERT INTO saved_analyses(name,file_id,config_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (req.name.strip(), req.file_id, payload, now, now))
        saved_id = cur.lastrowid
    return {"ok": True, "id": saved_id, "name": req.name.strip(), "message": "分析方案已保存。"}


@app.delete("/api/analysis/saved/{saved_id}")
def delete_saved_analysis(saved_id: int) -> dict[str, Any]:
    with db_session() as conn:
        cur = conn.execute("DELETE FROM saved_analyses WHERE id=?", (saved_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="未找到这条分析方案。")
    return {"ok": True, "message": "已删除保存的分析方案。"}


# ============================================================
# 报表归档时效检测、待办告警与审核流转中心 (Archive Compliance & Task Center)
# ============================================================

class CreateArchiveRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=300)
    period_type: str = Field(default="weekly") # daily, weekly, monthly, custom
    period_days: int = Field(default=7, ge=1, le=365)
    responsible_person: str = Field(default="数据专员", max_length=50)


class UpdateArchiveRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=300)
    period_type: str = Field(default="weekly") # daily, weekly, monthly, custom
    period_days: int = Field(default=7, ge=1, le=365)
    responsible_person: str = Field(default="数据专员", max_length=50)


class FeishuConfigRequest(BaseModel):
    app_id: str = Field(default="", max_length=100)
    app_secret: str = Field(default="", max_length=200)
    webhook_url: str = Field(default="", max_length=500)
    notify_enabled: bool = Field(default=True)
    sync_enabled: bool = Field(default=True)


class UpdateUserRoleRequest(BaseModel):
    role: str = Field(default="user") # 'super_admin', 'dept_admin', 'user'


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(default="123456", min_length=4, max_length=50)
    department: str = Field(default="业务运营部", max_length=100)
    role: str = Field(default="user")
    email: str = Field(default="", max_length=100)
    feishu_id: str = Field(default="", max_length=100)


class ResetUserPasswordRequest(BaseModel):
    password: str = Field(min_length=4, max_length=50)


class SubmitArchiveTaskRequest(BaseModel):
    note: str = Field(default="", max_length=1000)
    proof_image: str = Field(default="", max_length=5000000) # 支持 base64 或 相对路径
    submitted_by: str = Field(default="经办人", max_length=50)


class ReviewArchiveTaskRequest(BaseModel):
    action: str = Field(default="approve") # approve, reject
    comment: str = Field(default="", max_length=1000)
    reviewed_by: str = Field(default="高级管理员", max_length=50)


def run_archive_compliance_check(conn) -> dict[str, Any]:
    """执行全盘归档时效扫描：
    1. 针对每条启用的时效监控规则，检查对应目录在要求周期内是否有合格文件产出。
    2. 若已新增合格文件，自动将历史未闭环待办标记为【自动消结】(auto_resolved)。
    3. 若逾期未新增，自动生成或更新【逾期未归档待办任务】(pending)。
    """
    now = time.time()
    rules = conn.execute("""
        SELECT id, name, target_path, period_type, period_days, responsible_person, is_active 
        FROM archive_rules 
        WHERE is_active=1
    """).fetchall()

    tasks_created = 0
    tasks_auto_resolved = 0
    tasks_updated = 0

    for rule in rules:
        r_id = rule["id"]
        r_name = rule["name"]
        target_path = rule["target_path"].rstrip("/")
        p_type = rule["period_type"]
        p_days = int(rule["period_days"])
        resp_person = rule["responsible_person"] or "数据专员"

        if p_type == "daily" or p_days == 1:
            period_desc = "每日新增"
        elif p_type == "monthly" or p_days == 30:
            period_desc = "每月新增"
        else:
            period_desc = f"每 {p_days} 天新增"

        # 查找该目录下最近的一个文件（以及其子目录）
        latest_row = conn.execute("""
            SELECT name, path, mtime FROM files
            WHERE (parent = ? OR parent LIKE ?)
            ORDER BY mtime DESC LIMIT 1
        """, (target_path, target_path + "/%")).fetchone()

        latest_time = latest_row["mtime"] if latest_row else 0
        latest_name = latest_row["name"] if latest_row else ""

        threshold_time = now - (p_days * 86400)
        is_compliant = (latest_time >= threshold_time) if latest_time > 0 else False

        # 查询该规则已有的未闭环任务
        active_task = conn.execute("""
            SELECT id, status, overdue_days, created_at, submit_note FROM archive_tasks
            WHERE rule_id = ? AND status IN ('pending', 'submitted', 'rejected')
            ORDER BY id DESC LIMIT 1
        """, (r_id,)).fetchone()

        if is_compliant:
            if active_task:
                # 规则已满足：定时扫描自动消掉任务！
                conn.execute("""
                    UPDATE archive_tasks
                    SET status = 'auto_resolved',
                        resolved_at = ?,
                        resolved_file = ?,
                        submit_note = CASE WHEN submit_note = '' THEN '定时扫描检测到该文件夹已新增合规报表文件，系统已自动消结待办。' ELSE submit_note END,
                        updated_at = ?
                    WHERE id = ?
                """, (now, latest_name, now, active_task["id"]))
                tasks_auto_resolved += 1
        else:
            if latest_time > 0:
                diff_sec = now - latest_time
                overdue_days = max(1, int((diff_sec - (p_days * 86400)) / 86400) + 1)
            else:
                overdue_days = p_days

            if active_task:
                conn.execute("""
                    UPDATE archive_tasks
                    SET overdue_days = ?,
                        last_file_time = ?,
                        last_file_name = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (overdue_days, latest_time, latest_name, now, active_task["id"]))
                tasks_updated += 1
            else:
                conn.execute("""
                    INSERT INTO archive_tasks (
                        rule_id, rule_name, target_path, period_desc, responsible_person,
                        last_file_time, last_file_name, overdue_days, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """, (r_id, r_name, target_path, period_desc, resp_person,
                      latest_time, latest_name, overdue_days, now, now))
                tasks_created += 1

        conn.execute("UPDATE archive_rules SET last_checked_at = ? WHERE id = ?", (now, r_id))

    return {
        "tasks_created": tasks_created,
        "tasks_auto_resolved": tasks_auto_resolved,
        "tasks_updated": tasks_updated,
        "checked_rules": len(rules)
    }


@app.get("/api/archive/overview")
def get_archive_overview() -> dict[str, Any]:
    with db_session() as conn:
        total_rules = conn.execute("SELECT count(*) FROM archive_rules WHERE is_active=1").fetchone()[0]
        pending_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status IN ('pending', 'rejected')").fetchone()[0]
        submitted_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status = 'submitted'").fetchone()[0]
        resolved_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status IN ('approved', 'auto_resolved')").fetchone()[0]

        compliance_rate = 100.0
        if total_rules > 0:
            unresolved = pending_tasks + submitted_tasks
            compliance_rate = round(max(0.0, (1 - unresolved / total_rules) * 100), 1)

    last_run = _archive_scheduler_info["last_run_at"]
    last_run_display = datetime.fromtimestamp(last_run).strftime("%H:%M:%S") if last_run > 0 else "初始化就绪"
    scheduler_data = {
        "enabled": _archive_scheduler_info["enabled"],
        "interval_seconds": _archive_scheduler_info["interval_seconds"],
        "last_run_display": last_run_display,
        "run_count": _archive_scheduler_info["run_count"],
        "auto_resolved_total": _archive_scheduler_info["auto_resolved_total"],
        "status": _archive_scheduler_info["status"]
    }

    return {
        "ok": True,
        "total_rules": total_rules,
        "pending_tasks": pending_tasks,
        "submitted_tasks": submitted_tasks,
        "resolved_tasks": resolved_tasks,
        "compliance_rate": compliance_rate,
        "scheduler": scheduler_data
    }


@app.get("/api/archive/rules")
def get_archive_rules() -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute("""
            SELECT id, name, target_path, period_type, period_days, responsible_person, is_active, last_checked_at, created_at
            FROM archive_rules
            ORDER BY id ASC
        """).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "name": r["name"],
                "target_path": r["target_path"],
                "period_type": r["period_type"],
                "period_days": r["period_days"],
                "responsible_person": r["responsible_person"],
                "is_active": bool(r["is_active"]),
                "last_checked_at": r["last_checked_at"],
                "last_checked_display": datetime.fromtimestamp(r["last_checked_at"]).strftime("%Y-%m-%d %H:%M") if r["last_checked_at"] > 0 else "尚未检测",
                "created_at": r["created_at"]
            })
    return {"ok": True, "rules": result}


@app.post("/api/archive/rules")
def create_archive_rule(req: CreateArchiveRuleRequest) -> dict[str, Any]:
    now = time.time()
    with db_session() as conn:
        cur = conn.execute("""
            INSERT INTO archive_rules (name, target_path, period_type, period_days, responsible_person, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (req.name.strip(), req.target_path.strip(), req.period_type, req.period_days, req.responsible_person.strip(), now))
        rule_id = cur.lastrowid
        # 立即执行一次时效检测
        run_archive_compliance_check(conn)
    return {"ok": True, "id": rule_id, "message": "监控规则已创建并立即开始检测"}


@app.put("/api/archive/rules/{rule_id}")
def update_archive_rule(rule_id: int, req: UpdateArchiveRuleRequest) -> dict[str, Any]:
    with db_session() as conn:
        cur = conn.execute("""
            UPDATE archive_rules 
            SET name = ?, target_path = ?, period_type = ?, period_days = ?, responsible_person = ?
            WHERE id = ?
        """, (req.name.strip(), req.target_path.strip(), req.period_type, req.period_days, req.responsible_person.strip(), rule_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
        # 立即执行一次时效检测
        run_archive_compliance_check(conn)
    return {"ok": True, "message": "监控规则已更新并重新校准时效"}


@app.put("/api/archive/rules/{rule_id}/toggle")
def toggle_archive_rule(rule_id: int) -> dict[str, Any]:
    with db_session() as conn:
        cur = conn.execute("UPDATE archive_rules SET is_active = (1 - is_active) WHERE id = ?", (rule_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True, "message": "规则启停状态已更新"}


@app.delete("/api/archive/rules/{rule_id}")
def delete_archive_rule(rule_id: int) -> dict[str, Any]:
    with db_session() as conn:
        conn.execute("DELETE FROM archive_rules WHERE id = ?", (rule_id,))
        conn.execute("DELETE FROM archive_tasks WHERE rule_id = ?", (rule_id,))
    return {"ok": True, "message": "规则及其历史待办已删除"}


@app.post("/api/archive/check")
def trigger_archive_check() -> dict[str, Any]:
    with db_session() as conn:
        res = run_archive_compliance_check(conn)
    return {
        "ok": True,
        "result": res,
        "message": f"时效检测完成：扫描 {res['checked_rules']} 条规则，新增待办 {res['tasks_created']} 项，自动消结 {res['tasks_auto_resolved']} 项"
    }


@app.get("/api/archive/tasks")
def get_archive_tasks(
    status: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100)
) -> dict[str, Any]:
    with db_session() as conn:
        where_clauses = []
        params = []
        if status == "pending":
            where_clauses.append("status IN ('pending', 'rejected')")
        elif status == "submitted":
            where_clauses.append("status = 'submitted'")
        elif status == "resolved":
            where_clauses.append("status IN ('approved', 'auto_resolved')")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        total = conn.execute(f"SELECT count(*) FROM archive_tasks {where_sql}", params).fetchone()[0]

        offset = (page - 1) * page_size
        rows = conn.execute(f"""
            SELECT id, rule_id, rule_name, target_path, period_desc, responsible_person,
                   last_file_time, last_file_name, overdue_days, status, submit_note,
                   proof_image, submitted_by, submitted_at, reviewed_by, reviewed_at,
                   review_comment, resolved_at, resolved_file, created_at, updated_at
            FROM archive_tasks
            {where_sql}
            ORDER BY 
                CASE status 
                    WHEN 'pending' THEN 1
                    WHEN 'rejected' THEN 2
                    WHEN 'submitted' THEN 3
                    WHEN 'auto_resolved' THEN 4
                    WHEN 'approved' THEN 5
                    ELSE 6
                END,
                overdue_days DESC,
                updated_at DESC
            LIMIT ? OFFSET ?
        """, params + [page_size, offset]).fetchall()

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "rule_id": r["rule_id"],
                "rule_name": r["rule_name"],
                "target_path": r["target_path"],
                "period_desc": r["period_desc"],
                "responsible_person": r["responsible_person"],
                "last_file_time": r["last_file_time"],
                "last_file_time_display": datetime.fromtimestamp(r["last_file_time"]).strftime("%Y-%m-%d %H:%M") if r["last_file_time"] > 0 else "无历史文件",
                "last_file_name": r["last_file_name"],
                "overdue_days": r["overdue_days"],
                "status": r["status"],
                "submit_note": r["submit_note"] or "",
                "proof_image": r["proof_image"] or "",
                "submitted_by": r["submitted_by"] or "",
                "submitted_at": r["submitted_at"],
                "submitted_at_display": datetime.fromtimestamp(r["submitted_at"]).strftime("%Y-%m-%d %H:%M") if r["submitted_at"] > 0 else "",
                "reviewed_by": r["reviewed_by"] or "",
                "reviewed_at": r["reviewed_at"],
                "reviewed_at_display": datetime.fromtimestamp(r["reviewed_at"]).strftime("%Y-%m-%d %H:%M") if r["reviewed_at"] > 0 else "",
                "review_comment": r["review_comment"] or "",
                "resolved_at": r["resolved_at"],
                "resolved_at_display": datetime.fromtimestamp(r["resolved_at"]).strftime("%Y-%m-%d %H:%M") if r["resolved_at"] > 0 else "",
                "resolved_file": r["resolved_file"] or "",
                "created_at": r["created_at"],
                "created_at_display": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
            })

    return {
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }


@app.post("/api/archive/tasks/{task_id}/submit")
def submit_archive_task(task_id: int, req: SubmitArchiveTaskRequest) -> dict[str, Any]:
    now = time.time()
    with db_session() as conn:
        task = conn.execute("SELECT id, status FROM archive_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="待办任务不存在")

        conn.execute("""
            UPDATE archive_tasks
            SET status = 'submitted',
                submit_note = ?,
                proof_image = ?,
                submitted_by = ?,
                submitted_at = ?,
                updated_at = ?
            WHERE id = ?
        """, (req.note.strip(), req.proof_image.strip(), req.submitted_by.strip() or "经办人", now, now, task_id))

    return {"ok": True, "message": "任务已提交证明，已流转至高级管理员待审核"}


@app.post("/api/archive/tasks/{task_id}/review")
def review_archive_task(task_id: int, req: ReviewArchiveTaskRequest) -> dict[str, Any]:
    now = time.time()
    action = req.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="审核操作类型无效")

    with db_session() as conn:
        task = conn.execute("SELECT id, status FROM archive_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="待办任务不存在")

        if action == "approve":
            new_status = "approved"
            msg = "审核通过，该待办任务已成功闭环消结。"
            conn.execute("""
                UPDATE archive_tasks
                SET status = ?,
                    reviewed_by = ?,
                    reviewed_at = ?,
                    review_comment = ?,
                    resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
            """, (new_status, req.reviewed_by.strip() or "高级管理员", now, req.comment.strip(), now, now, task_id))
        else:
            new_status = "rejected"
            msg = "已驳回整改，任务已重新退回待办池。"
            conn.execute("""
                UPDATE archive_tasks
                SET status = ?,
                    reviewed_by = ?,
                    reviewed_at = ?,
                    review_comment = ?,
                    updated_at = ?
                WHERE id = ?
            """, (new_status, req.reviewed_by.strip() or "高级管理员", now, req.comment.strip(), now, task_id))

    return {"ok": True, "message": msg, "new_status": new_status}


@app.post("/api/archive/upload-proof")
async def upload_proof_image(request: Request) -> dict[str, Any]:
    """支持接收图片文件或 JSON base64 格式上传截图证明"""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        form = await request.form()
        file = form.get("file")
        if not file:
            raise HTTPException(status_code=400, detail="未检测到上传的文件")
        contents = await file.read()
        ext = Path(getattr(file, "filename", "proof.png") or "proof.png").suffix.lower() or ".png"
        filename = f"proof_{uuid.uuid4().hex[:12]}{ext}"
        target_dir = APP_DIR / "static" / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / filename).write_bytes(contents)
        url = f"/static/uploads/{filename}"
        return {"ok": True, "url": url}
    else:
        body = await request.json()
        b64 = body.get("image", "")
        if not b64:
            raise HTTPException(status_code=400, detail="图片数据不能为空")
        return {"ok": True, "url": b64}


# ============================================================
# 人员权限与飞书集成管理中心 (User Roles & Feishu Integration)
# ============================================================

@app.get("/api/system/permissions")
def get_system_permissions() -> dict[str, Any]:
    with db_session() as conn:
        feishu_raw = conn.execute("SELECT value FROM app_meta WHERE key = 'feishu_config'").fetchone()
        feishu_config = json.loads(feishu_raw[0]) if feishu_raw else {
            "app_id": "",
            "app_secret": "",
            "webhook_url": "",
            "notify_enabled": True,
            "sync_enabled": False,
            "last_synced_at": 0,
            "synced_users_count": 0,
            "synced_depts_count": 0
        }
        
        users_rows = conn.execute("""
            SELECT id, name, phone, password, feishu_id, email, department, role, status, avatar, created_at, updated_at
            FROM system_users
            ORDER BY 
              CASE role 
                WHEN 'super_admin' THEN 1 
                WHEN 'dept_admin' THEN 2 
                ELSE 3 
              END ASC, id ASC
        """).fetchall()
        
        users = []
        for u in users_rows:
            users.append({
                "id": u["id"],
                "name": u["name"],
                "phone": u["phone"] or "--",
                "password": u["password"] or "123456",
                "feishu_id": u["feishu_id"],
                "email": u["email"],
                "department": u["department"],
                "role": u["role"],
                "status": u["status"],
                "avatar": u["avatar"] or u["name"][:2],
                "created_at_display": datetime.fromtimestamp(u["created_at"]).strftime("%Y-%m-%d %H:%M") if u["created_at"] > 0 else "--",
                "updated_at": u["updated_at"]
            })
            
        super_admins = sum(1 for u in users if u["role"] == "super_admin")
        dept_admins = sum(1 for u in users if u["role"] == "dept_admin")
        normal_users = sum(1 for u in users if u["role"] == "user")
        active_users = sum(1 for u in users if u["status"] == "active")

    return {
        "ok": True,
        "feishu_config": feishu_config,
        "users": users,
        "summary": {
            "total_users": len(users),
            "super_admins": super_admins,
            "dept_admins": dept_admins,
            "normal_users": normal_users,
            "active_users": active_users
        }
    }


@app.post("/api/system/permissions/users")
def create_system_user(req: CreateUserRequest) -> dict[str, Any]:
    now_ts = time.time()
    phone = req.phone.strip()
    with db_session() as conn:
        existing = conn.execute("SELECT id FROM system_users WHERE phone = ?", (phone,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="该电话号码成员已存在")
        avatar = req.name.strip()[:2]
        cur = conn.execute("""
            INSERT INTO system_users (name, phone, password, department, role, status, avatar, email, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        """, (req.name.strip(), phone, req.password.strip() or "123456", req.department.strip(), req.role, avatar, req.email.strip(), now_ts, now_ts))
    return {"ok": True, "id": cur.lastrowid, "message": f"成员 {req.name}（手机号：{phone}）已授权创建"}


@app.put("/api/system/permissions/users/{user_id}/password")
def reset_user_password(user_id: int, req: ResetUserPasswordRequest) -> dict[str, Any]:
    new_pwd = req.password.strip()
    if not new_pwd:
        raise HTTPException(status_code=400, detail="密码不能为空")
    with db_session() as conn:
        cur = conn.execute("UPDATE system_users SET password = ?, updated_at = ? WHERE id = ?", (new_pwd, time.time(), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员密码已成功重置"}


@app.put("/api/system/permissions/users/{user_id}/role")
def update_user_role(user_id: int, req: UpdateUserRoleRequest) -> dict[str, Any]:
    if req.role not in ("super_admin", "dept_admin", "user"):
        raise HTTPException(status_code=400, detail="无效角色")
    with db_session() as conn:
        cur = conn.execute("UPDATE system_users SET role = ?, updated_at = ? WHERE id = ?", (req.role, time.time(), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员系统角色已更新"}


@app.put("/api/system/permissions/users/{user_id}/status")
def toggle_user_status(user_id: int) -> dict[str, Any]:
    with db_session() as conn:
        cur = conn.execute("""
            UPDATE system_users 
            SET status = CASE WHEN status='active' THEN 'disabled' ELSE 'active' END, updated_at = ?
            WHERE id = ?
        """, (time.time(), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员状态已更新"}


@app.delete("/api/system/permissions/users/{user_id}")
def delete_system_user(user_id: int) -> dict[str, Any]:
    with db_session() as conn:
        cur = conn.execute("DELETE FROM system_users WHERE id = ?", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员授权已移除"}



async def not_found_handler(request: Request, exc: Exception):
    if request.url.path.startswith('/api/'):
        return JSONResponse(status_code=404, content={'detail': 'Not Found'})
    return RedirectResponse(url='/')
