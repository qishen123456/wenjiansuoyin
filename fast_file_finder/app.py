# -*- coding: utf-8 -*-
"""文件检索原型：元数据索引 + 本地排序 + 可选 AI 候选重排。"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import posixpath
import random
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
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Literal
from io import BytesIO
from urllib.parse import quote

import pandas as pd
from pydantic import BaseModel, Field, field_validator

import base64
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response, File, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import governance
import directory_access
import demo_data
import archive_schedule
import demo_approvals
import scan_pipeline
import limited_preview
import file_management
import media_preview
import feishu_integration
import sys
from collections import OrderedDict
from starlette.concurrency import run_in_threadpool

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
_raw_db_path = Path(os.getenv("INDEX_DB_PATH", str(APP_DIR / "data" / "file_index.db"))).expanduser()
DB_PATH = _raw_db_path if _raw_db_path.is_absolute() else (APP_DIR / _raw_db_path).resolve()
FILE_ROOT = os.getenv("FILE_ROOT", "").strip()
# Keep local demo configuration portable regardless of the launch working directory.
if FILE_ROOT and not FILE_ROOT.startswith("\\\\"):
    _file_root_path = Path(FILE_ROOT).expanduser()
    if not _file_root_path.is_absolute():
        FILE_ROOT = str((APP_DIR / _file_root_path).resolve())
APP_ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN", "").strip()
# 自动定时增量扫描间隔（分钟），0 表示关闭；增量扫描只处理有变化的文件
AUTO_SCAN_INTERVAL_MINUTES = float(os.getenv("AUTO_SCAN_INTERVAL_MINUTES", "30"))
# 敏感目录黑名单：目录名包含任一关键词的子树整体不入库、不可搜索、不可下载（防账号/薪酬类数据外发）
SENSITIVE_DIRS = [s.strip() for s in re.split(r"[;,，；]", os.getenv("SENSITIVE_DIRS", "")) if s.strip()]
# 登录与下载审批：开启后所有 /api 接口需登录，普通成员下载需审批票据
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "true").strip().lower() not in ("0", "false", "no")
DIRECTORY_ACL_ENABLED = os.getenv("DIRECTORY_ACL_ENABLED", "true").strip().lower() not in ("0", "false", "no")
DEMO_MODE = os.getenv("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes")
# Demo pacing affects successful search responses only, after database work is done.
DEMO_SEARCH_MIN_SECONDS = float(os.getenv("DEMO_SEARCH_MIN_SECONDS", "2"))
DEMO_SEARCH_MAX_SECONDS = float(os.getenv("DEMO_SEARCH_MAX_SECONDS", "4"))
if not (math.isfinite(DEMO_SEARCH_MIN_SECONDS) and math.isfinite(DEMO_SEARCH_MAX_SECONDS)
        and 0 <= DEMO_SEARCH_MIN_SECONDS <= DEMO_SEARCH_MAX_SECONDS <= 10):
    raise ValueError("Demo search timing must satisfy 0 <= MIN <= MAX <= 10 seconds")


DEMO_AI_MIN_SECONDS = float(os.getenv("DEMO_AI_MIN_SECONDS", "5"))
DEMO_AI_MAX_SECONDS = float(os.getenv("DEMO_AI_MAX_SECONDS", "10"))
if not (math.isfinite(DEMO_AI_MIN_SECONDS) and math.isfinite(DEMO_AI_MAX_SECONDS)
        and 0 <= DEMO_AI_MIN_SECONDS <= DEMO_AI_MAX_SECONDS <= 60):
    raise ValueError("Demo AI timing must satisfy 0 <= MIN <= MAX <= 60 seconds")


def _pace_demo_response(started_at: float, minimum: float, maximum: float) -> float:
    """Reach a randomized minimum response duration after database work is done."""
    if not DEMO_MODE or maximum == 0:
        return 0.0
    remaining = random.uniform(minimum, maximum) - (time.perf_counter() - started_at)
    if remaining <= 0:
        return 0.0
    before = time.perf_counter()
    time.sleep(remaining)
    return round((time.perf_counter() - before) * 1000, 1)


def _pace_demo_search(started_at: float) -> float:
    return _pace_demo_response(started_at, DEMO_SEARCH_MIN_SECONDS, DEMO_SEARCH_MAX_SECONDS)


def _pace_demo_ai(started_at: float) -> float:
    return _pace_demo_response(started_at, DEMO_AI_MIN_SECONDS, DEMO_AI_MAX_SECONDS)


DEMO_SCAN_MIN_SECONDS = float(os.getenv("DEMO_SCAN_MIN_SECONDS", "120"))
DEMO_SCAN_MAX_SECONDS = float(os.getenv("DEMO_SCAN_MAX_SECONDS", "240"))
if not (math.isfinite(DEMO_SCAN_MIN_SECONDS) and math.isfinite(DEMO_SCAN_MAX_SECONDS)
        and 0 <= DEMO_SCAN_MIN_SECONDS <= DEMO_SCAN_MAX_SECONDS <= 300):
    raise ValueError("Demo scan timing must satisfy 0 <= MIN <= MAX <= 300 seconds")


def _pace_demo_scan(started_at: float) -> float:
    """Wait after index transactions close; shutdown can interrupt demo pacing."""
    if not DEMO_MODE or DEMO_SCAN_MAX_SECONDS == 0 or _auto_scan_stop.is_set():
        return 0.0
    target = random.uniform(DEMO_SCAN_MIN_SECONDS, DEMO_SCAN_MAX_SECONDS)
    remaining = target - (time.perf_counter() - started_at)
    if remaining <= 0:
        return 0.0
    _scan_state_update(
        phase="demo_wait",
        message="文件索引已更新，正在等待演示刷新结束（含演示节奏等待）。",
    )
    before = time.perf_counter()
    _auto_scan_stop.wait(remaining)
    return time.perf_counter() - before


def _scan_duration_label(seconds: float) -> str:
    rounded = max(1, math.ceil(seconds))
    minutes, seconds = divmod(rounded, 60)
    return f"{minutes} 分 {seconds} 秒" if minutes else f"{seconds} 秒"


SESSION_TTL_HOURS = float(os.getenv("SESSION_TTL_HOURS", "12"))
DOWNLOAD_TICKET_TTL_HOURS = float(os.getenv("DOWNLOAD_TICKET_TTL_HOURS", "2"))
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
logging.getLogger("uvicorn.access").addFilter(governance.RedactQueryStringFilter())
ALLOWED_EXTENSIONS = {
    x.strip().lower() if x.strip().startswith(".") else "." + x.strip().lower()
    for x in os.getenv("ALLOWED_EXTENSIONS", ".xlsx,.xls,.xlsm,.xlsb,.csv,.tsv,.ods,.ppt,.pptx,.png,.jpg,.jpeg,.webp").split(",")
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
_auto_scan_stop = threading.Event()
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
                governance.prune_events(conn)
                res = run_archive_compliance_check(conn)
                _archive_scheduler_info["last_run_at"] = time.time()
                _archive_scheduler_info["run_count"] += 1
                _archive_scheduler_info["auto_resolved_total"] += res.get("tasks_auto_resolved", 0)
        except Exception as exc:
            logger.warning("Background archive compliance check error: %s", exc)
        try:
            feishu_integration.deliver_notices(sys.modules[__name__])
        except Exception:
            logger.exception("Feishu notification delivery failed")
        _archive_scheduler_stop.wait(60)


def _auto_scan_scheduler_loop() -> None:
    """定时对网盘做增量索引扫描；只处理有变化的文件，手动扫描运行时自动跳过。"""
    logger.info("Auto incremental index scan scheduler started (every %s minutes).", AUTO_SCAN_INTERVAL_MINUTES)
    while not _auto_scan_stop.wait(AUTO_SCAN_INTERVAL_MINUTES * 60):
        if not FILE_ROOT:
            continue
        try:
            if _scan_state_snapshot()["running"]:
                continue
            _scan_worker()  # 内部非阻塞取锁，锁被占用时直接返回
        except Exception as exc:
            logger.warning("Auto incremental index scan error: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _archive_scheduler_stop.clear()
    _auto_scan_stop.clear()
    init_db()
    with db_session() as conn:
        file_management.initialize(conn)
        feishu_integration.initialize(conn)
    if DEMO_MODE:
        await run_in_threadpool(_ensure_demo_data)
    if SENSITIVE_DIRS:
        # 启动时把黑名单子树从索引（含 FTS 分词）中清除，网盘原文件不受影响
        try:
            with db_session() as conn:
                for seg in SENSITIVE_DIRS:
                    _delete_index_rows(conn, "parent LIKE ? OR name LIKE ?", (f"%{seg}%", f"%{seg}%"))
            logger.info("Sensitive directory blacklist purged from index: %s", ",".join(SENSITIVE_DIRS))
        except Exception as e:
            logger.warning("Sensitive directory purge failed: %s", e)
    try:
        with db_session() as conn:
            _migrate_legacy_archive_paths(conn)
            run_archive_compliance_check(conn)
    except Exception as e:
        logger.warning("Initial archive compliance check failed: %s", e)

    scheduler_thread = threading.Thread(target=_archive_scheduler_loop, daemon=True, name="ArchiveScheduler")
    scheduler_thread.start()
    threading.Thread(target=file_management.backup_loop, args=(sys.modules[__name__],),
                     daemon=True, name='DatabaseBackup').start()

    if AUTO_SCAN_INTERVAL_MINUTES > 0:
        auto_scan_thread = threading.Thread(target=_auto_scan_scheduler_loop, daemon=True, name="AutoIndexScan")
        auto_scan_thread.start()

    if not APP_ACCESS_TOKEN:
        logger.warning("APP_ACCESS_TOKEN is empty; API routes have no shared-token gate. Use localhost or configure enterprise SSO/reverse proxy before LAN exposure.")
    try:
        yield
    finally:
        _archive_scheduler_stop.set()
        _auto_scan_stop.set()


app = FastAPI(title="企业文件检索与轻量 BI 分析中心", version="0.3.0", lifespan=lifespan)
# Windows 注册表常把 .js 映射为 text/plain，会导致 Web Worker importScripts 拒绝加载，这里强制修正
mimetypes.add_type("application/javascript", ".js")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.middleware("http")
async def optional_shared_access_token(request: Request, call_next):
    """Optional shared token gate for internal deployments; not a replacement for user ACL/SSO."""
    path = request.url.path
    if APP_ACCESS_TOKEN and path.startswith("/api/"):
        supplied = request.headers.get("x-app-token", "")
        if not supplied or not secrets.compare_digest(supplied, APP_ACCESS_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "系统启用了访问令牌，请输入管理员配置的 APP_ACCESS_TOKEN。"})
    # 登录门禁：除白名单外，所有 /api 接口需要有效会话
    user = None
    if (AUTH_REQUIRED or DIRECTORY_ACL_ENABLED) and path.startswith("/api/"):
        if path not in _AUTH_OPEN_PATHS:
            user = _get_session_user(request)
            if not user:
                return JSONResponse(status_code=401, content={"detail": "请先登录后再使用本系统。"})
    if DEMO_MODE and path.startswith('/api/') and path not in _AUTH_OPEN_PATHS:
        await run_in_threadpool(_ensure_demo_data)
    token = None
    if DIRECTORY_ACL_ENABLED and path.startswith("/api/"):
        if user is None:
            user = _get_session_user(request)
        with db_session() as conn:
            policy = directory_access.load_policy(conn, user, root_scope_key(), _catalog_root_path())
        token = directory_access.current.set(policy)
    try:
        return await call_next(request)
    finally:
        if token is not None:
            directory_access.current.reset(token)

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
    conn = sqlite3.connect(str(DB_PATH), timeout=30, factory=directory_access.ScopedConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.install_scope(directory_access.current.get())
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
        snapshot = dict(_scan_state)
        snapshot["exclusion_reasons"] = dict(_scan_state.get("exclusion_reasons", {}))
        return snapshot


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


def _scan_exclude(reason: str) -> None:
    with _scan_state_lock:
        _scan_state["excluded"] = _scan_state.get("excluded", 0) + 1
        reasons = _scan_state.setdefault("exclusion_reasons", {})
        reasons[reason] = reasons.get(reason, 0) + 1


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
        governance.initialize(conn)
        directory_access.initialize(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                parent TEXT NOT NULL,
                ext TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                ctime REAL NOT NULL DEFAULT 0,
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
        if "ctime" not in file_columns:
            conn.execute("ALTER TABLE files ADD COLUMN ctime REAL NOT NULL DEFAULT 0")
            # 历史数据无创建时间，暂用修改时间兜底；下次全量扫描会覆盖为真实创建时间
            conn.execute("UPDATE files SET ctime = mtime WHERE ctime = 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_name ON files(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ctime ON files(ctime DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_root_scan ON files(root_key,last_seen_scan)")
        # Cover scoped aggregates without loading every long path/name row.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_scope_metadata ON files(root_key,parent,mtime,ext,size)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_scope_recent ON files(root_key,mtime DESC)")
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
        rule_columns = {row['name'] for row in conn.execute('PRAGMA table_info(archive_rules)')}
        for column, definition in [('file_keyword', "TEXT NOT NULL DEFAULT ''"), ('deadline_time', "TEXT NOT NULL DEFAULT '18:00'"), ('schedule_config', "TEXT NOT NULL DEFAULT '{}'")]:
            if column not in rule_columns:
                conn.execute(f'ALTER TABLE archive_rules ADD COLUMN {column} {definition}')
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

        # 登录会话表与下载审批审计表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS login_sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                department TEXT DEFAULT '',
                file_id INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                ticket TEXT DEFAULT '',
                created_at REAL NOT NULL,
                reviewed_by TEXT DEFAULT '',
                reviewed_at REAL DEFAULT 0,
                expires_at REAL DEFAULT 0,
                downloaded_at REAL DEFAULT 0
            )
        """)
        download_columns = {r['name'] for r in conn.execute('PRAGMA table_info(download_requests)')}
        for column,definition in [('demo_key',"TEXT NOT NULL DEFAULT ''"),('review_note',"TEXT NOT NULL DEFAULT ''"),('workflow_events',"TEXT NOT NULL DEFAULT '[]'")]:
            if column not in download_columns:conn.execute(f'ALTER TABLE download_requests ADD COLUMN {column} {definition}')
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_download_demo_key ON download_requests(demo_key) WHERE demo_key<>''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_requests_status ON download_requests(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_requests_user ON download_requests(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_requests_ticket ON download_requests(ticket)")

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
    known_rows: dict | None = None,
    force: bool = False,
) -> str:
    """Upsert one file and stamp it for the current scan generation."""
    path_str = str(file_path.resolve(strict=False)) if known_rows is None else os.path.normpath(str(file_path))
    root_key = root_key if root_key is not None else root_scope_key()
    scan_id = scan_id or uuid.uuid4().hex
    row = conn.execute(
        "SELECT id, size, mtime, ctime, name, root_key FROM files WHERE path = ?", (path_str,)
    ).fetchone()
    name = file_path.name
    parent = str(Path(path_str).parent)
    ext = file_path.suffix.lower()
    size = int(stat.st_size)
    mtime = float(stat.st_mtime)
    ctime = float(getattr(stat, "st_birthtime", (row["ctime"] if row and row["ctime"] else stat.st_ctime)))
    now = time.time()
    unchanged = (
        not force and row
        and row["size"] == size
        and abs(row["mtime"] - mtime) < 0.0001
        and abs(row["ctime"] - ctime) < 0.0001
        and row["name"] == name
    )
    if unchanged and row["root_key"] == root_key:
        if known_rows is None:
            conn.execute("UPDATE files SET last_seen_scan=? WHERE id=?", (scan_id, row["id"]))
        return "unchanged"
    if row:
        file_id = row["id"]
        conn.execute(
            "UPDATE files SET name=?, parent=?, ext=?, size=?, mtime=?, ctime=?, indexed_at=?, root_key=?, last_seen_scan=? WHERE id=?",
            (name, parent, ext, size, mtime, ctime, now, root_key, scan_id, file_id),
        )
        if unchanged:
            return "updated"
        conn.execute("DELETE FROM file_fts WHERE rowid=?", (file_id,))
        action = "updated"
    else:
        cur = conn.execute(
            "INSERT INTO files(path,name,parent,ext,size,mtime,ctime,indexed_at,root_key,last_seen_scan) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (path_str, name, parent, ext, size, mtime, ctime, now, root_key, scan_id),
        )
        file_id = cur.lastrowid
        action = "indexed"
    conn.execute("INSERT INTO file_fts(rowid,name,name_grams,path_grams) VALUES(?,?,?,?)",
                 (file_id, name, grams_text(name), grams_text(parent)))
    return action


_demo_refresh_lock = threading.Lock()
_demo_state: dict[str, Any] = {}


def _seed_demo_monitors(conn, root: Path) -> None:
    key = f'demo_monitors:{root_scope_key()}'
    previous = conn.execute('SELECT value FROM app_meta WHERE key=?',(key,)).fetchone()
    if previous and json.loads(previous[0]).get('version') == demo_data.VERSION:
        return
    managed = []
    domains = {path.name[:2]:path for path in root.iterdir() if path.is_dir() and not path.is_symlink()}
    for code,name,keyword,cutoff,owner in demo_data.MONITORS:
        domain = domains.get(code)
        if not domain or any(term in domain.name for term in SENSITIVE_DIRS):
            continue
        target = str(domain/demo_data.REPORT_DIRECTORY)
        if not (domain/demo_data.REPORT_DIRECTORY).is_dir():
            continue
        legacy = None
        if code == '09':
            candidates = conn.execute("SELECT * FROM archive_rules WHERE name='9-新品简报 - 日增合规监控' AND responsible_person='新品运营组'").fetchall()
            for candidate in candidates:
                try:
                    if _resolve_catalog_path(candidate['target_path']) == str(domain):
                        legacy = candidate
                        break
                except HTTPException:
                    continue
        if legacy:
            rule_id = legacy['id']
            conn.execute("UPDATE archive_rules SET name=?,target_path=?,period_type='daily',period_days=1,file_keyword=?,deadline_time=? WHERE id=?",
                         (name,target,keyword,cutoff,rule_id))
            conn.execute("UPDATE archive_tasks SET target_path=?,rule_name=?,period_desc=? WHERE rule_id=? AND status IN ('pending','submitted','rejected')",
                         (target,name,f'每天 {cutoff} 前',rule_id))
        else:
            cursor = conn.execute("""INSERT INTO archive_rules(name,target_path,period_type,period_days,responsible_person,
                is_active,created_at,file_keyword,deadline_time) VALUES(?,?,'daily',1,?,1,?,?,?)""",
                (name,target,owner,time.time(),keyword,cutoff))
            rule_id = cursor.lastrowid
        managed.append({'code':code,'id':rule_id})
    conn.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)',
                 (key,json.dumps({'version':demo_data.VERSION,'rules':managed})))


def _ensure_demo_data(today: date | None = None) -> dict[str, Any]:
    """Refresh actual demo reports before the first API read each local day."""
    if not DEMO_MODE:
        return {}
    today = today or date.today()
    key = f'{root_scope_key()}|{today.isoformat()}|{demo_data.VERSION}'
    if _demo_state.get('key') == key:
        return _demo_state
    with _demo_refresh_lock:
        if _demo_state.get('key') == key or not _scan_lock.acquire(blocking=False):
            return _demo_state
        try:
            root = get_root()
            manifest_key = f'demo_reports:{root_scope_key()}'
            with db_session() as conn:
                previous = conn.execute('SELECT value FROM app_meta WHERE key=?',(manifest_key,)).fetchone()
                previous = json.loads(previous[0]).get('paths',[]) if previous else []
                result = demo_data.refresh(root,today,previous,excluded_terms=SENSITIVE_DIRS,
                                           enabled=not ALLOWED_EXTENSIONS or '.csv' in ALLOWED_EXTENSIONS)
                for relative in result['paths']:
                    path = root / relative
                    update_index_file(conn,path,path.stat(),root_key=root_scope_key(),scan_id='demo:'+today.isoformat())
                for relative in result['removed']:
                    _delete_index_rows(conn,'root_key=? AND path=?',(root_scope_key(),str(root/relative)))
                # Explicitly unclassified fill-in placeholders are excluded in demo mode.
                purged = _delete_index_rows(conn,
                    "root_key=? AND instr('/' || replace(parent,char(92),'/') || '/',?)>0",
                    (root_scope_key(),'/' + demo_data.UNCLASSIFIED_DIRECTORY + '/'))
                conn.execute('INSERT OR REPLACE INTO app_meta(key,value) VALUES(?,?)',
                             (manifest_key,json.dumps(result,ensure_ascii=False)))
                _seed_demo_monitors(conn,root)
                demo_approvals.seed(conn,root_scope_key(),time.time(),DOWNLOAD_TICKET_TTL_HOURS)
                run_archive_compliance_check(conn)
            _demo_state.clear()
            _demo_state.update(key=key,day=today.isoformat(),reports=len(result['paths']),created=len(result['created']),purged=purged)
            _tree_cache.update(time=0,data=None,key=None,revision=_tree_cache.get('revision',0)+1)
        except Exception:
            logger.exception('Demo daily report refresh failed')
        finally:
            _scan_lock.release()
    return _demo_state


def _scan_worker(lock_already_acquired: bool = False, mode: str = 'full', target_path: str = '') -> None:
    """Build/update the index in the background; only prune entries after a clean full scan."""
    if not lock_already_acquired and not _scan_lock.acquire(blocking=False):
        return
    scan_started_at = time.perf_counter()
    scan_id = uuid.uuid4().hex
    scan_root_key = root_scope_key()
    logger.info("index scan started; scan_id=%s", scan_id)
    _scan_state_replace({
        "running": True, "phase": "scanning", "root_key": scan_root_key, "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None, "seen": 0, "indexed": 0, "updated": 0, "deleted": 0, "errors": 0,
        "discovered": 0, "unchanged": 0, "excluded": 0, "exclusion_reasons": {},
        "directory_errors": 0, "excluded_directories": 0,
        "message": "正在扫描目录并增量更新索引；查询不需要等待扫描结束。",
    })
    rebuilding = mode == "rebuild"
    scoped = mode == "directory"
    _scan_state_update(mode=mode, target_path=target_path or FILE_ROOT)
    walk_error_count = 0
    walk_error_samples: list[str] = []

    def on_walk_error(exc: OSError) -> None:
        nonlocal walk_error_count
        walk_error_count += 1
        _scan_state_increment("directory_errors")
        if len(walk_error_samples) < 5:
            walk_error_samples.append(f"{type(exc).__name__}: {exc}")

    try:
        root = file_management.checked_directory(sys.modules[__name__], target_path) if scoped else get_root()
        root_key = root_scope_key()
        with db_session() as conn:
            known = {r['path']:r for r in conn.execute('SELECT id,path,size,mtime,ctime,name,root_key FROM files')}
            unseen = {path for path,row in known.items() if row["root_key"]==root_key
                      and (not scoped or directory_access.within(directory_access.normalise(path), directory_access.normalise(root)))}
            if rebuilding:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM file_fts")
            workers = max(1,min(16,int(os.getenv('SCAN_WORKERS',str(min(8,max(2,os.cpu_count() or 4)))))))
            batch=max(1,int(os.getenv('SCAN_BATCH_SIZE','300')))
            interval=max(.1,float(os.getenv('SCAN_COMMIT_SECONDS','1')))
            last_commit=time.monotonic();processed=0
            def excluded_directory(name):
                return name in {'$RECYCLE.BIN','System Volume Information','.snapshot'} or (DEMO_MODE and name==demo_data.UNCLASSIFIED_DIRECTORY) or any(t in name for t in SENSITIVE_DIRS)
            def file_filter(name):
                if name.startswith(('~$', '.file-finder-upload-')):return 'temporary'
                if any(t in name for t in SENSITIVE_DIRS):return 'sensitive_name'
                if ALLOWED_EXTENSIONS and Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:return 'unsupported_format'
            for event in scan_pipeline.walk_metadata(root,workers=workers,file_filter=file_filter,capacity=max(16,int(os.getenv('SCAN_QUEUE_SIZE','1024'))),excluded_directory=excluded_directory):
                kind=event[0]
                if kind=='idle':
                    if not rebuilding and conn.in_transaction and time.monotonic()-last_commit>=interval:conn.commit();last_commit=time.monotonic()
                    continue
                if kind=='directory_error':on_walk_error(OSError(event[1]+': '+event[2]));continue
                if kind=='excluded_directory':_scan_state_increment('excluded_directories');continue
                _scan_state_increment('discovered')
                if kind=='excluded_file':_scan_exclude(event[1]);continue
                if kind=='file_error':_scan_state_increment('errors');continue
                if kind=='link':_scan_exclude('symbolic_link');continue
                if kind=='special':_scan_exclude('not_a_regular_file');continue
                fp=Path(event[1]);filename=fp.name
                if filename.startswith('~$'):_scan_exclude('temporary');continue
                if any(t in filename for t in SENSITIVE_DIRS):_scan_exclude('sensitive_name');continue
                if ALLOWED_EXTENSIONS and fp.suffix.lower() not in ALLOWED_EXTENSIONS:_scan_exclude('unsupported_format');continue
                action=update_index_file(conn,fp,event[2],root_key=root_key,scan_id=scan_id,known_rows=known,force=rebuilding)
                unseen.discard(os.path.normpath(str(fp)))
                _scan_state_increment('seen');_scan_state_increment(action)
                processed+=1
                if processed % batch==0 or time.monotonic()-last_commit>=interval:
                    if not rebuilding:
                        conn.commit()
                    last_commit=time.monotonic()
                    _scan_state_update(message=f'已检查 {processed:,} 个文件 · {workers} 个目录扫描线程')
            if not rebuilding:
                conn.commit()

            state = _scan_state_snapshot()
            scan_complete = walk_error_count == 0 and state["errors"] == 0
            if rebuilding and not scan_complete:
                conn.rollback()
            if scan_complete:
                # Only a complete enumeration can prove that an old indexed record is missing.
                stale_deleted=0
                stale_ids=[known[path]['id'] for path in unseen]
                for offset in range(0,len(stale_ids),400):
                    ids=stale_ids[offset:offset+400]
                    stale_deleted+=_delete_index_rows(conn,'root_key=? AND id IN ('+','.join('?' for _ in ids)+')',(root_key,*ids))
                # This service indexes one configured root at a time. When FILE_ROOT changes,
                # old-root entries are hidden from search during scanning and purged only after
                # the new root has completed successfully.
                _scan_state_update(deleted=stale_deleted)
                if not scoped:
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
                if not rebuilding:
                    conn.commit()
                _migrate_legacy_archive_paths(conn)
                run_archive_compliance_check(conn)

        scan_elapsed = time.perf_counter() - scan_started_at
        demo_delay = _pace_demo_scan(scan_started_at) if scan_complete else 0.0
        elapsed = time.perf_counter() - scan_started_at
        _scan_state_update(
            phase="complete" if scan_complete else "partial",
            scan_elapsed_seconds=scan_elapsed,
            demo_delay_seconds=demo_delay,
        )
        if scan_complete:
            state = _scan_state_snapshot()
            pace_note = "（含演示节奏等待）" if demo_delay > 0 else ""
            _scan_state_update(message=f"索引更新完成，用时 {_scan_duration_label(elapsed)}{pace_note}；新增 {state['indexed']:,}，更新 {state['updated']:,}，清理 {state['deleted']:,}。")
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
        try:
            with db_session() as conn:
                governance.persist_scan(conn, scan_id, scan_root_key, _scan_state_snapshot())
        except Exception:
            logger.exception("Could not persist scan reconciliation; scan_id=%s", scan_id)
        _tree_cache.update(time=0, data=None, key=None, revision=_tree_cache.get("revision", 0) + 1)
        _scan_lock.release()


def _read_static_html(filename: str) -> str:
    return (APP_DIR / "static" / filename).read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return _read_static_html("index.html")


@app.get("/workbench", response_class=HTMLResponse)
def workbench() -> str:
    return _read_static_html("bi_workbench.html")


@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
def home_html_alias() -> str:
    return _read_static_html("index.html")


@app.get("/bi_workbench.html", response_class=HTMLResponse, include_in_schema=False)
def workbench_html_alias() -> str:
    return _read_static_html("bi_workbench.html")


@app.get("/guide", response_class=HTMLResponse, include_in_schema=False)
@app.get("/guide.html", response_class=HTMLResponse, include_in_schema=False)
def guide_page() -> str:
    return _read_static_html("guide.html")


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
        scans = governance.scan_history(conn, active_root_key, limit=5)
    policy = directory_access.current.get()
    if policy and not policy.super_admin:
        return {"running": False, "phase": "scoped", "seen": count, "indexed_files": count,
                "indexed_size_bytes": total_size, "scan_history": [], "last_scan_at": None,
                "root_configured": bool(FILE_ROOT), "root_display": "已授权目录", "ai_configured": is_ai_configured(),
                "message": "仅统计当前账号可访问的文件", "auto_scan_interval_minutes": 0}
    state = _scan_state_snapshot()
    if state.get("root_key") not in (None, active_root_key):
        state = {"running": False, "phase": "idle", "seen": 0, "indexed": 0, "updated": 0, "deleted": 0, "errors": 0, "message": "当前目录尚未扫描"}
    if not state["running"] and state["phase"] == "idle" and scans:
        run = scans[0]
        state = {**state, **run["counts"], "phase": run["phase"], "message": run["message"],
                 "started_at": run["started_at"], "finished_at": run["finished_at"]}
    return {**state, "scan_history": scans, "indexed_files": count, "indexed_size_bytes": total_size,
            "last_scan_at": last[0] if last else None, "root_configured": bool(FILE_ROOT),
            "root_display": FILE_ROOT or "尚未配置", "ai_configured": is_ai_configured(),
            "auto_scan_interval_minutes": AUTO_SCAN_INTERVAL_MINUTES,
            "access_token_enabled": bool(APP_ACCESS_TOKEN)}


@app.post("/api/index/start")
def start_index(request: Request = None, mode: Literal['full','directory','rebuild'] = 'full', path: str = '') -> dict[str, Any]:
    if DIRECTORY_ACL_ENABLED or AUTH_REQUIRED or mode != 'full':
        _require_super_admin(request)
    if mode == 'directory':
        if not path.strip():
            raise HTTPException(422, '请选择需要同步的文件夹。')
        path = str(file_management.checked_directory(sys.modules[__name__], path))
    get_root()  # Fast path validation before taking the scan lock.
    # Acquire in the request handler to prevent two near-simultaneous requests from both claiming success.
    if not _scan_lock.acquire(blocking=False):
        return {"started": False, "message": "索引任务已经在运行。", **_scan_state_snapshot()}
    t = threading.Thread(target=_scan_worker, kwargs={"lock_already_acquired": True, "mode": mode, "target_path": path}, daemon=True)
    try:
        t.start()
    except Exception:
        _scan_lock.release()
        raise
    _record_product_event("index_start", request)
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
    mtime_from: float | None = None,
    mtime_to: float | None = None,
    date_field: str = "mtime",
    directory: str = "", min_size: int | None = None, max_size: int | None = None, content_state: str = "all",
) -> list[dict[str, Any]]:
    match = fts_query_for(q, match_mode=match_mode, search_target=search_target)
    if not match:
        return []
    root_key = root_key if root_key is not None else root_scope_key()
    if not root_key:
        return []

    # 日期字段：支持按修改日期(mtime)或创建日期(ctime)过滤，两者均已建索引
    date_col = "ctime" if date_field == "ctime" else "mtime"
    # 日期范围过滤：拼接到 WHERE 子句，利用对应时间列索引加速
    date_clause = ""
    date_params: list[Any] = []
    if mtime_from is not None:
        date_clause += f" AND f.{date_col} >= ?"
        date_params.append(mtime_from)
    if mtime_to is not None:
        date_clause += f" AND f.{date_col} <= ?"
        date_params.append(mtime_to)

    if directory:
        condition, parameters = _catalog_directory_condition(directory)
        condition = re.sub(r'\bparent\b', 'f.parent', condition)
        date_clause += ' AND ' + condition
        date_params.extend(parameters)
    for operator,value in [('>=',min_size),('<=',max_size)]:
        if value is not None:
            date_clause += f' AND f.size {operator} ?'; date_params.append(value)
    if content_state == 'nonempty': date_clause += ' AND f.size>0'
    elif content_state == 'empty': date_clause += ' AND f.size=0'

    try:
        if ext:
            rows = conn.execute(f"""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, f.ctime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? AND f.ext = ? AND f.root_key = ?{date_clause} ORDER BY rank LIMIT ?
            """, (match, ext, root_key, *date_params, limit)).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT f.id, f.path, f.name, f.parent, f.ext, f.size, f.mtime, f.ctime, bm25(file_fts, 5.0, 2.0, 0.8) AS rank
                FROM file_fts JOIN files f ON f.id = file_fts.rowid
                WHERE file_fts MATCH ? AND f.root_key = ?{date_clause} ORDER BY rank LIMIT ?
            """, (match, root_key, *date_params, limit)).fetchall()
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
            "ext": r["ext"], "size": int(r["size"]), "mtime": float(r["mtime"]), "ctime": float(r["ctime"]),
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
    if not conn.execute("SELECT 1 FROM files WHERE root_key=? LIMIT 1", (active_root_key,)).fetchone():
        return {"reasoning": "当前账号没有可检索的已授权文件。请联系超级管理员配置目录访问权限，或同步已授权目录的索引。",
                "model_used": "目录权限校验", "suggested_terms": [], "matched_files": []}

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
            f"**找到了以下关键词相关文件，请核对名称和目录。**\n\n"
            f"📄 **报表名称**：`{best['name']}`\n"
            f"📁 **存放目录**：`{dir_clean or '根目录'}`\n"
            f"🔗 **Win 共享路径**：`{unc}`\n\n"
            f"可通过下方卡片定位目录、下载原件或复制路径。"
        )

        matched_output = []
        for c in candidates[:5]:
            matched_output.append({
                "id": c["id"],
                "name": c["name"],
                "path": c["path"],
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


def _event_context(request: Request | None) -> tuple[dict[str, Any] | None, str]:
    if request is None:
        return None, "anonymous"
    user = _get_session_user(request)
    session_id = request.headers.get("X-Product-Session", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", session_id):
        session_id = "anonymous"
    return user, session_id


def _record_product_event(event_type: str, request: Request | None, *, subject_id: int | None = None,
                          search_event_id: int | None = None, query_length: int | None = None,
                          result_count: int | None = None, elapsed_ms: float | None = None,
                          result_ids: list[int] | None = None) -> int | None:
    try:
        user, session_id = _event_context(request)
        with db_session() as conn:
            actor_id = user["id"] if user else None
            linked = governance.linked_search(conn, root_scope_key(), search_event_id, session_id, actor_id, subject_id)
            event_id = governance.record_event(conn, root_key=root_scope_key(), event_type=event_type,
                actor_id=actor_id, actor_role=user["role"] if user else "anonymous", session_id=session_id,
                subject_id=subject_id, search_event_id=linked, query_length=query_length,
                result_count=result_count, elapsed_ms=elapsed_ms)
            if event_type == "search" and result_ids:
                conn.executemany("INSERT OR IGNORE INTO search_event_files(search_event_id,file_id) VALUES (?,?)",
                                 [(event_id, file_id) for file_id in result_ids])
            return event_id
    except Exception:
        logger.warning("Product event persistence failed; event=%s", event_type, exc_info=True)
        return None


class ProductEventRequest(BaseModel):
    event_type: Literal["result_click", "preview_open", "path_copy"]
    file_id: int = Field(gt=0)
    search_event_id: int | None = Field(default=None, gt=0)


@app.post("/api/telemetry/events")
def record_client_event(req: ProductEventRequest, request: Request) -> dict[str, Any]:
    with db_session() as conn:
        exists = conn.execute("SELECT 1 FROM files WHERE id=? AND root_key=?", (req.file_id, root_scope_key())).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="当前索引中没有这个文件。")
    event_id = _record_product_event(req.event_type, request, subject_id=req.file_id, search_event_id=req.search_event_id)
    return {"ok": event_id is not None}


@app.get("/api/governance/summary")
def governance_summary(request: Request, days: int = Query(default=14, ge=1, le=90)) -> dict[str, Any]:
    _require_super_admin(request)
    with db_session() as conn:
        return {"ok": True, "metrics": governance.METRIC_DEFINITIONS,
                "product": governance.product_summary(conn, root_scope_key(), days),
                "scans": governance.scan_history(conn, root_scope_key(), limit=10)}


def browse_search(conn, *, root_key, ext, date_field, date_from, date_to,
                  directory, min_size, max_size, content_state, sort, page, page_size):
    clauses, params = ['root_key=?'], [root_key]
    if ext:
        clauses.append('ext=?')
        params.append(ext)
    for operator, value in [('>=', date_from), ('<=', date_to)]:
        if value is not None:
            clauses.append(f'{date_field} {operator} ?')
            params.append(value)
    if directory:
        condition, values = _catalog_directory_condition(directory)
        clauses.append(condition)
        params.extend(values)
    for operator, value in [('>=', min_size), ('<=', max_size)]:
        if value is not None:
            clauses.append(f'size {operator} ?')
            params.append(value)
    if content_state == 'nonempty':
        clauses.append('size>0')
    elif content_state == 'empty':
        clauses.append('size=0')
    where = ' AND '.join(clauses)
    orders = {'time_asc':'mtime ASC', 'modified_asc':'mtime ASC',
              'created_desc':'ctime DESC', 'ctime_desc':'ctime DESC',
              'created_asc':'ctime ASC', 'ctime_asc':'ctime ASC',
              'name':'name COLLATE NOCASE ASC', 'name_desc':'name COLLATE NOCASE DESC',
              'size_desc':'size DESC', 'size_asc':'size ASC'}
    order = orders.get(sort, 'mtime DESC')
    total = conn.execute(f'SELECT count(*) FROM files WHERE {where}', params).fetchone()[0]
    rows = conn.execute(f'''SELECT id,name,path,parent,ext,size,mtime,ctime FROM files
        WHERE {where} ORDER BY {order},id DESC LIMIT ? OFFSET ?''',
        [*params, page_size, (page-1)*page_size]).fetchall()
    return total, [{**dict(row), 'unc_path':to_unc_path(row['path']),
                    'score':None, 'match_reason':'按筛选条件展示'} for row in rows]


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
    date_from: str = Query(default=""),
    date_to: str = Query(default=""),
    date_field: Literal["mtime", "ctime"] = Query(default="mtime"),
    directory: str = Query(default="",max_length=4096),
    min_size: int | None = Query(default=None,ge=0),
    max_size: int | None = Query(default=None,ge=0),
    content_state: Literal["all","nonempty","empty"] = Query(default="all"),
    request: Request = None,
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

    if not FILE_ROOT:
        return {
            "query": query, "count": 0, "total_candidates": 0, "page": 1, "page_size": effective_page_size,
            "total_pages": 0, "results": [], "ai_used": False, "message": "尚未配置 FILE_ROOT。请在 .env 中设置文件根目录。"
        }
    if min_size is not None and max_size is not None and min_size>max_size:
        raise HTTPException(status_code=422,detail="最小文件大小不能大于最大文件大小。")
    directory_path = _resolve_catalog_path(directory) if directory else ''
    scoped_filters = dict(directory=directory_path,min_size=min_size,max_size=max_size,content_state=content_state)
    has_extra_filters = bool(directory or min_size is not None or max_size is not None or content_state!='all')
    start = time.perf_counter()
    wanted_ext = (ext.lower() if ext.startswith(".") else "." + ext.lower()) if ext else ""
    active_root_key = root_scope_key()

    # 日期范围 → Unix 时间戳（含当天全天）
    mtime_from_ts: float | None = None
    mtime_to_ts: float | None = None
    if date_from.strip():
        try:
            mtime_from_ts = datetime.strptime(date_from.strip(), "%Y-%m-%d").timestamp()
        except ValueError:
            mtime_from_ts = None
    if date_to.strip():
        try:
            # 截止日期取当天 23:59:59.999，保证包含当天所有文件
            mtime_to_ts = datetime.strptime(date_to.strip(), "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, microsecond=999999
            ).timestamp()
        except ValueError:
            mtime_to_ts = None

    if not query:
        with db_session() as conn:
            total, results = browse_search(conn, root_key=active_root_key, ext=wanted_ext,
                date_field=date_field, date_from=mtime_from_ts, date_to=mtime_to_ts,
                sort=sort, page=effective_page, page_size=effective_page_size, **scoped_filters)
        query_elapsed_ms = round((time.perf_counter()-start)*1000, 1)
        delay = _pace_demo_search(start) if results else 0
        event_id = _record_product_event('search', request, query_length=0, result_count=total,
            elapsed_ms=query_elapsed_ms, result_ids=[r['id'] for r in results])
        return {'query':'', 'count':len(results), 'total_candidates':total,
            'page':effective_page, 'page_size':effective_page_size,
            'total_pages':(total+effective_page_size-1)//effective_page_size,
            'results':results, 'ai_used':False, 'ai_expanded':False, 'ai_guess':None,
            'fallback_relaxed':False, 'match_mode':match_mode, 'target':target,
            'elapsed_ms':round((time.perf_counter()-start)*1000, 1),
            'query_elapsed_ms':query_elapsed_ms, 'demo_delay_ms':delay,
            'search_event_id':event_id, 'message':'已按当前筛选条件展示可访问文件。'}

    with db_session() as conn:
        search_scope = "title" if target in ("title", "content") else "path"
        candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode=match_mode, search_target=search_scope, mtime_from=mtime_from_ts, mtime_to=mtime_to_ts, date_field=date_field, **scoped_filters)
        ai_used = False
        ai_expanded = False
        fallback_relaxed = False

        # 如果严格全词模式下没有结果，自动放宽为宽松匹配，避免用户因单字错漏导致无结果
        if not candidates and match_mode == "all":
            candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode="any", search_target=search_scope, mtime_from=mtime_from_ts, mtime_to=mtime_to_ts, date_field=date_field, **scoped_filters)
            if candidates:
                fallback_relaxed = True

        # 如果文件名未能命中，自动扩展检索所属目录/路径，满足用户搜部门名直接找表格的自然诉求
        if not candidates and search_scope == "title":
            candidates = candidate_search(conn, query, limit=500, ext=wanted_ext, root_key=active_root_key, match_mode="any", search_target="path", mtime_from=mtime_from_ts, mtime_to=mtime_to_ts, date_field=date_field, **scoped_filters)
            if candidates:
                fallback_relaxed = True
                for c in candidates:
                    c["match_reason"] = "所属业务目录命中"

        ai_guess = None
        if not candidates:
            if enable_ai and is_ai_configured() and not has_extra_filters:
                # 显式开启 AI 选项时才调用配置的模型服务
                ai_guess = ai_guess_and_supplement(conn, query)
                ai_used = True
            else:
                tokens = extract_business_keywords(query)
                ai_guess = {
                    "reasoning": f"未搜到包含“{query}”的文件。建议尝试拆分关键词，或打开右下角 AI 找表，根据目录线索尝试缩小查找范围。",
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
        elif sort in ("created_desc", "ctime_desc"):
            candidates.sort(key=lambda x: (-x.get("ctime", 0), x["id"]))
        elif sort in ("created_asc", "ctime_asc"):
            candidates.sort(key=lambda x: (x.get("ctime", 0), x["id"]))
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

    query_elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    demo_delay_ms = _pace_demo_ai(start) if ai_used else _pace_demo_search(start)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    logger.info("search completed; query_length=%s results=%s candidates=%s match_mode=%s target=%s elapsed_ms=%s",
                len(query), len(results), total_count, match_mode, target, elapsed_ms)
    msg = "已按标题所有字段严格全包含检索。" if (match_mode == "all" and not fallback_relaxed) else (
        "全词严格匹配无候选，已自动为您放宽为相关度最优推荐。" if fallback_relaxed else "已使用相关度宽松索引检索。"
    )
    if not results and ai_guess:
        msg = "未搜到直接匹配文件，已由 AI 助手结合目录结构为您推测归属路径与搜索词。"
    event_id = _record_product_event("search", request, query_length=len(query), result_count=total_count, elapsed_ms=query_elapsed_ms, result_ids=[int(row["id"]) for row in results])
    return {
        "search_event_id": event_id,
        "query": query, "count": len(results), "total_candidates": total_count,
        "page": effective_page, "page_size": effective_page_size, "total_pages": total_pages,
        "match_mode": match_mode, "target": target, "fallback_relaxed": fallback_relaxed,
        "results": results, "elapsed_ms": elapsed_ms,
        "query_elapsed_ms": query_elapsed_ms, "demo_delay_ms": demo_delay_ms, "ai_used": ai_used, "ai_expanded": ai_expanded,
        "ai_guess": ai_guess,
        "message": msg
    }


class AIChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    query: str = Field(default="", max_length=300)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


def answer_dated_file_request(conn: sqlite3.Connection, message: str) -> dict[str, Any] | None:
    """Resolve temporal requests against authorized metadata before any generation."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ranges = [('今天', day, day + timedelta(days=1)),
              ('今日', day, day + timedelta(days=1)),
              ('昨天', day - timedelta(days=1), day),
              ('昨日', day - timedelta(days=1), day),
              ('近7天', day - timedelta(days=6), day + timedelta(days=1)),
              ('近30天', day - timedelta(days=29), day + timedelta(days=1)),
              ('本月', day.replace(day=1), day + timedelta(days=1)),
              ('本年', day.replace(month=1, day=1), day + timedelta(days=1))]
    selected = next((r for r in ranges if r[0] in message), None)
    if not selected and not any(w in message for w in ['最新', '最近更新', '最后更新']):
        return None
    cleaned = message
    for word in sorted([r[0] for r in ranges] + ['帮我找一下','帮我找下','帮我查找','帮我找','找一下','找下','查找','最新的','最新','最近更新的','最近更新','最后更新的','最后更新','文件','报表','数据','请','一下','的'], key=len, reverse=True):
        cleaned = cleaned.replace(word, ' ')
    terms = re.findall(r'[\u4e00-\u9fffA-Za-z0-9_]+', cleaned)
    terms = ['京东' if t.lower() == 'jd' else t for t in terms]
    clauses, params = ['root_key=?'], [root_scope_key()]
    for term in terms:
        clauses.append('(instr(lower(name),lower(?))>0 OR instr(lower(parent),lower(?))>0)')
        params.extend([term, term])
    base_where = ' AND '.join(clauses)
    label = selected[0] if selected else '不限日期'
    if selected:
        clauses += ['mtime>=?', 'mtime<?']
        params += [selected[1].timestamp(), selected[2].timestamp()]
    where = ' AND '.join(clauses)
    total = conn.execute(f'SELECT count(*) FROM files WHERE {where}', params).fetchone()[0]
    columns = 'id,path,name,parent,size,mtime'
    rows = conn.execute(f'SELECT {columns} FROM files WHERE {where} ORDER BY mtime DESC,id DESC LIMIT 5', params).fetchall()
    related = []
    if selected and not rows:
        related = conn.execute(f'SELECT {columns} FROM files WHERE {base_where} AND mtime<? ORDER BY mtime DESC,id DESC LIMIT 3', params[:-2] + [selected[1].timestamp()]).fetchall()
    def serialize(row):
        item = dict(row)
        item['unc_path'] = to_unc_path(item['path'])
        item['modified_at'] = datetime.fromtimestamp(item['mtime'], ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
        return item
    files, history = list(map(serialize, rows)), list(map(serialize, related))
    subject = '、'.join(terms) or '全部文件'
    scope = f'检索范围：{subject}；{label}，按修改时间从新到旧（北京时间）。'
    if selected:
        scope += f"\n日期：{selected[1]:%Y-%m-%d} 至 {selected[2] - timedelta(days=1):%Y-%m-%d}。"
    if files:
        facts = f'找到 {total} 份符合条件的文件，下面列出最新 {len(files)} 份。\n\n' + scope
        facts += f"\n\n最新一份：{files[0]['name']}\n修改时间：{files[0]['modified_at']}。"
        empty = sum(f['size'] == 0 for f in files)
        if empty: facts += f'\n其中 {empty} 份为空文件，卡片已标注。'
    else:
        facts = f'没有找到{label}修改的{subject}文件。\n\n' + scope
        if history:
            facts += f"\n\n历史最近更新：{history[0]['modified_at']}。下方是历史文件，不属于本次日期范围。"
        else: facts += '\n当前账号可访问的索引中也没有更早的匹配文件。'
    # Keep factual conclusions deterministic; model may only explain the retrieval scope.
    model, note = '', ''
    if is_ai_configured():
        note, model = call_llm([
            {'role':'system','content':'你是企业文件助手。仅根据给定的检索事实，用一句话解释匹配依据或无结果时的下一步。不能编造文件、时间、结果数量，不能说已找到今天文件，不能承诺分析功能。不要重复列表。'},
            {'role':'user','content':json.dumps({'request':message,'verified_facts':facts},ensure_ascii=False)}], max_tokens=600)
    return {'ok':True, 'reply':facts + ('\n\n' + note if note else ''),
            'model_used':model if note else '本地索引检索（模型未生成补充说明）',
            'suggested_terms':[], 'matched_files':files, 'related_files':history,
            'retrieval':{'date_label':label,'date_field':'mtime','sort':'mtime_desc','timezone':'Asia/Shanghai','total':total}}


@app.post("/api/ai/chat")
def ai_chat(req: AIChatRequest) -> dict[str, Any]:
    """AI 找表对话框专属接口：结合网盘目录与相近文件进行多轮深度解答与推荐。"""
    started_at = time.perf_counter()
    q = req.message.strip() or req.query.strip()
    greeting = re.sub(r"[\s?!？！。,.，~～]+", "", q).lower()
    if greeting in {"在吗", "在不在", "你好", "您好", "嗨", "hi", "hello"}:
        result = {
            "ok": True,
            "reply": "在的。告诉我想找的报表名称、业务主题或时间范围，我来帮你找。",
            "model_used": "本地对话引导",
            "suggested_terms": [],
            "matched_files": [],
        }
    else:
        with db_session() as conn:
            result = answer_dated_file_request(conn, q)
            if result is None:
                guess = ai_guess_and_supplement(conn, q)
                result = {
                    "ok": True,
                    "reply": guess["reasoning"],
                    "model_used": guess["model_used"],
                    "suggested_terms": guess["suggested_terms"],
                    "matched_files": guess["matched_files"],
                }
    result["demo_delay_ms"] = _pace_demo_ai(started_at)
    result["response_elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
    return result



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
def download_file(file_id: int, request: Request, ticket: str = Query(default="")) -> FileResponse:
    """浏览器直接下载，使用 RFC 5987 标准保证中文文件名不乱码。
    开启登录后：管理员直接下载；普通成员必须持审批通过的限时票据。"""
    file_info, path = _get_indexed_file(file_id)
    if AUTH_REQUIRED or DIRECTORY_ACL_ENABLED:
        user = _require_user(request)
        mode = _download_mode(file_info['parent'], user)
        if mode == 'deny':
            raise HTTPException(403, '你没有此文件的下载权限。')
        if mode != 'direct' and not _consume_download_ticket(file_id, ticket, user['id']):
            raise HTTPException(status_code=403, detail="下载需审批通过后的有效链接，请先在页面提交下载申请。")
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


_tree_cache: dict[str, Any] = {"time": 0, "data": None, "key": None}
_catalog_root_cache: dict[tuple[str, str], str] = {}
_LEGACY_DEMO_DIRECTORIES = {
    "9-新品简报": "09-产品研发-新品上市简报库",
    "12-推广ROI周报": "12-市场财务-全渠道ROI周报库",
    "2-阿里平台-分销": "02-华南大区-渠道经销拓展部",
    "11-小红书达人底层": "11-内容社区-KOL与红人矩阵部",
    "23-销售数据汇总": "23-销售管理-全域业绩统计大表",
}


def _normalise_catalog_path(value: str) -> str:
    return posixpath.normpath(str(value).replace("\\", "/"))


def _catalog_root_path() -> str:
    # Resolve aliases once per configuration, never walk the share on catalog queries.
    if not FILE_ROOT:
        return ""
    key = (FILE_ROOT, str(DB_PATH))
    if key not in _catalog_root_cache:
        _catalog_root_cache.clear()
        _catalog_root_cache[key] = _normalise_catalog_path(str(Path(FILE_ROOT).expanduser().resolve(strict=False)))
    return _catalog_root_cache[key]


def _relative_catalog_path(path: str, root: str) -> str | None:
    path, root = _normalise_catalog_path(path), _normalise_catalog_path(root)
    if path == root:
        return ""
    prefix = root.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _resolve_catalog_path(value: str = "") -> str:
    root = _catalog_root_path()
    if not root:
        raise HTTPException(status_code=400, detail="尚未配置文件根目录，请先配置 FILE_ROOT。")
    if not value.strip():
        return root
    path = _normalise_catalog_path(value.strip())
    aliases = [root, _normalise_catalog_path(str(Path(FILE_ROOT).expanduser().absolute())), "/data/share"]
    if SHARE_UNC_PREFIX:
        aliases.append(_normalise_catalog_path(SHARE_UNC_PREFIX))
    for alias in aliases:
        relative = _relative_catalog_path(path, alias)
        if relative is None:
            continue
        if alias == "/data/share" and relative:
            parts = relative.split("/")
            parts[0] = _LEGACY_DEMO_DIRECTORIES.get(parts[0], parts[0])
            relative = "/".join(parts)
        return root.rstrip("/") + ("/" + relative if relative else "") or "/"
    raise HTTPException(status_code=403, detail="该目录不在当前配置的文件根目录内。")


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _catalog_directory_condition(path: str) -> tuple[str, list[Any]]:
    # Include both separator forms for older Windows indices; SQL wildcards in
    # real directory names must remain literal, and siblings must never match.
    variants = {path, path.replace("/", "\\")}
    clauses, params = [], []
    for variant in sorted(variants):
        sep = "\\" if "\\" in variant else "/"
        clauses.append("(parent = ? OR parent LIKE ? ESCAPE '!')")
        params.extend([variant, _escape_like(variant.rstrip("/\\") + sep) + "%"])
    return "(" + " OR ".join(clauses) + ")", params


def _migrate_legacy_archive_paths(conn) -> int:
    migrated = 0
    for rule in conn.execute("SELECT id,target_path FROM archive_rules").fetchall():
        original = rule["target_path"]
        if _relative_catalog_path(original, "/data/share") is None:
            continue
        try:
            target = _resolve_catalog_path(original)
        except HTTPException:
            continue
        condition, params = _catalog_directory_condition(target)
        exists = conn.execute(f"SELECT 1 FROM files WHERE root_key=? AND {condition} LIMIT 1", [root_scope_key(), *params]).fetchone()
        if target != original and exists:
            conn.execute("UPDATE archive_rules SET target_path=? WHERE id=?", (target, rule["id"]))
            conn.execute("UPDATE archive_tasks SET target_path=? WHERE rule_id=? AND status IN ('pending','submitted','rejected')", (target, rule["id"]))
            migrated += 1
    return migrated


@app.get("/api/catalog/overview")
def get_catalog_overview() -> dict[str, Any]:
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    scope, root = root_scope_key(), _catalog_root_path()
    with db_session() as conn:
        stats = conn.execute("""SELECT count(*) AS total_files,
            coalesce(sum(CASE WHEN mtime>=? THEN 1 ELSE 0 END),0) AS today_added,
            count(DISTINCT parent) AS total_dirs, coalesce(sum(size),0) AS total_size
            FROM files WHERE root_key=?""", (today_start, scope)).fetchone()
        exts = conn.execute("SELECT ext,count(*) AS count FROM files WHERE root_key=? GROUP BY ext ORDER BY count DESC", (scope,)).fetchall()
        groups = conn.execute("""SELECT parent,count(*) AS total_files,
            sum(CASE WHEN mtime>=? THEN 1 ELSE 0 END) AS today_added
            FROM files WHERE root_key=? GROUP BY parent""", (today_start, scope)).fetchall()
    departments: dict[str, dict[str, Any]] = {}
    for row in groups:
        relative = _relative_catalog_path(row["parent"], root) if root else None
        if relative is None:
            continue
        name = relative.split("/")[0] if relative else "根目录"
        department = departments.setdefault(name, {"name": name, "path": root.rstrip("/") + ("/" + name if relative else ""), "total_files": 0, "today_added": 0})
        department["total_files"] += row["total_files"]
        department["today_added"] += int(row["today_added"] or 0)
    return {"ok": True, "root_path": root, "total_files": stats["total_files"],
            "today_added": stats["today_added"], "total_dirs": stats["total_dirs"],
            "total_size_mb": round(stats["total_size"] / 1024**2, 2),
            "total_size_gb": round(stats["total_size"] / 1024**3, 2),
            "today_date": now.strftime("%Y-%m-%d"),
            "ext_distribution": [{"ext": row["ext"] or "未知", "count": row["count"]} for row in exts],
            "departments": sorted(departments.values(), key=lambda item: (-item["total_files"], item["name"]))}


@app.get("/api/dashboard/summary")
def get_dashboard_summary() -> dict[str, Any]:
    """首页仪表盘：全部取自索引库的真实统计，不使用任何写死的展示数字。"""
    now = time.time()
    day = 86400
    root_prefix = _catalog_root_path()
    scope = root_scope_key()
    with db_session() as conn:
        total_files, total_size = conn.execute("SELECT count(*), coalesce(sum(size),0) FROM files WHERE root_key=?", (scope,)).fetchone()
        total_dirs = conn.execute("SELECT count(DISTINCT parent) FROM files WHERE root_key=?", (scope,)).fetchone()[0]
        real_count, real_size = conn.execute("SELECT count(*), coalesce(sum(size),0) FROM files WHERE root_key=? AND size > 0", (scope,)).fetchone()
        last_30 = conn.execute("SELECT count(*) FROM files WHERE root_key=? AND mtime >= ?", (scope, now - 30 * day,)).fetchone()[0]
        prev_30 = conn.execute("SELECT count(*) FROM files WHERE root_key=? AND mtime >= ? AND mtime < ?", (scope, now - 60 * day, now - 30 * day)).fetchone()[0]
        last_7 = conn.execute("SELECT count(*) FROM files WHERE root_key=? AND mtime >= ?", (scope, now - 7 * day,)).fetchone()[0]
        # Compute local calendar boundaries once. SQLite's per-row localtime
        # conversion is expensive on macOS and stalled the full-size dashboard.
        today = datetime.fromtimestamp(now)
        ordinal = today.year * 12 + today.month - 1
        bounds = []
        for offset in range(12):
            year, month = divmod(ordinal - offset, 12)
            next_year, next_month = divmod(ordinal - offset + 1, 12)
            bounds.append((f"{year:04d}-{month+1:02d}", datetime(year, month+1, 1).timestamp(),
                           datetime(next_year, next_month+1, 1).timestamp()))
        columns = [f"coalesce(sum(CASE WHEN mtime>=? AND mtime<? THEN 1 ELSE 0 END),0) AS m{i}" for i in range(12)]
        monthly_counts = conn.execute("SELECT " + ",".join(columns) + " FROM files WHERE root_key=? AND mtime>=? AND mtime<?",
                                      [value for _, start, end in bounds for value in (start,end)] + [scope,bounds[-1][1],bounds[0][2]]).fetchone()
        monthly = [{"m": item[0], "c": monthly_counts[f"m{i}"]} for i, item in enumerate(bounds)]
        ext_rows = conn.execute("SELECT lower(ext) AS e, count(*) AS c FROM files WHERE root_key=? GROUP BY e ORDER BY c DESC", (scope,)).fetchall()
        dept_groups = conn.execute("""SELECT parent,count(*) AS c,
            sum(CASE WHEN mtime>=? THEN 1 ELSE 0 END) AS r
            FROM files WHERE root_key=? GROUP BY parent""", (now - 30 * day, scope)).fetchall()
        department_map: dict[str, dict[str, Any]] = {}
        for row in dept_groups:
            relative = _relative_catalog_path(row["parent"], root_prefix) if root_prefix else None
            if relative is None:
                continue
            name = relative.split("/")[0] if relative else "根目录"
            department = department_map.setdefault(name, {"name": name, "total_files": 0, "updated_30d": 0})
            department["total_files"] += row["c"]
            department["updated_30d"] += int(row["r"] or 0)
        recent = conn.execute("SELECT id, name, parent, size, mtime, ext FROM files WHERE root_key=? ORDER BY mtime DESC LIMIT 8", (scope,)).fetchall()
        last_scan = conn.execute("SELECT value FROM app_meta WHERE key=?", (f"last_scan_at:{scope}",)).fetchone()

    def dept_of(parent: str) -> str:
        rel = _relative_catalog_path(parent, root_prefix) or ""
        return rel.split("/")[0] if rel else "根目录"

    return {
        "ok": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "last_scan_at": last_scan[0] if last_scan else None,
        "total_files": total_files,
        "total_size": total_size,
        "total_dirs": total_dirs,
        "real_count": real_count,
        "real_size": real_size,
        "updated_7d": last_7,
        "updated_30d": last_30,
        "updated_prev_30d": prev_30,
        "monthly": [{"month": r["m"], "count": r["c"]} for r in reversed(monthly)],
        "ext_distribution": [{"ext": r["e"] or "未知", "count": r["c"]} for r in ext_rows],
        "departments": sorted(department_map.values(), key=lambda item: -item["total_files"]),
        "recent": [
            {"id": r["id"], "name": r["name"], "dept": dept_of(r["parent"]), "size": r["size"], "mtime": r["mtime"], "ext": r["ext"]}
            for r in recent
        ],
    }

@app.get("/api/catalog/tree")
def get_catalog_tree(only_today_branch: bool = Query(default=False)) -> dict[str, Any]:
    """构建网盘组织架构树，并自动向上累加(Rollup)子目录的所有文件数与今日新增数。"""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    now_ts = time.time()

    policy = directory_access.current.get()
    cache_key = (root_scope_key(), str(DB_PATH), _catalog_root_path(), now.strftime("%Y-%m-%d"), _tree_cache.get("revision", 0), policy.cache_key if policy else "internal")
    if _tree_cache["data"] and _tree_cache.get("key") == cache_key and (now_ts - _tree_cache["time"] < 20) and not only_today_branch:
        return _tree_cache["data"]

    root_prefix = _catalog_root_path()

    with db_session() as conn:
        rows = conn.execute("""
            SELECT parent, count(*) as cnt, sum(CASE WHEN mtime >= ? THEN 1 ELSE 0 END) as today_cnt
            FROM files WHERE root_key=?
            GROUP BY parent
        """, (today_start, root_scope_key())).fetchall()

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

        rel = _relative_catalog_path(parent_path, root_prefix) if root_prefix else None
        if rel is None:
            continue
        parts = [p for p in re.split(r"[/\\]+", rel) if p] if rel else []

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
        # 先递归构建所有子树（不做排序），以便按「汇总文件数」排序时子节点已完成向上汇总
        c_list = []
        total_f = node.get("direct_files", 0)
        total_t = node.get("direct_today", 0)
        for _, v in node["children"].items():
            child_tree = to_list_tree(v, depth + 1)
            total_f += child_tree["file_count"]
            total_t += child_tree["today_added"]
            # 如果开启了“仅看今日新增分支”，且该子树今日没有新增，则跳过
            if only_today_branch and child_tree["today_added"] == 0:
                continue
            c_list.append(child_tree)

        # 按「今日新增数 → 汇总文件数」降序，让有层级的大事业部排前、扁平小目录排后
        c_list.sort(key=lambda x: (x["today_added"], x["file_count"]), reverse=True)

        # 向上汇总

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
        _tree_cache["key"] = cache_key
        _tree_cache["data"] = result

    return result


@app.get("/api/catalog/resolve")
def resolve_catalog_directory(path: str = Query(default="", max_length=4096)) -> dict[str, Any]:
    resolved = _resolve_catalog_path(path)
    condition, params = _catalog_directory_condition(resolved)
    with db_session() as conn:
        found = conn.execute(f"SELECT 1 FROM files WHERE root_key=? AND {condition} LIMIT 1", [root_scope_key(), *params]).fetchone()
        if not found:
            raise HTTPException(status_code=404, detail="当前索引中没有这个目录的文件，请确认目录路径并更新索引。")
    return {"ok": True, "path": resolved, "root_path": _catalog_root_path()}


@app.get("/api/catalog/files")
def get_catalog_files(
    path: str = Query(default=""),
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

    clean_path = _resolve_catalog_path(path)
    directory_sql, path_params = _catalog_directory_condition(clean_path)
    with db_session() as conn:
        where_clauses = ["root_key=?", directory_sql]
        params: list[Any] = [root_scope_key(), *path_params]

        if only_today:
            where_clauses.append("mtime >= ?")
            params.append(today_start)

        if keyword.strip():
            where_clauses.append("name LIKE ? ESCAPE '!'")
            params.append("%" + _escape_like(keyword.strip()) + "%")

        where_sql = " AND ".join(where_clauses)

        total = conn.execute(f"SELECT count(*) FROM files WHERE {where_sql}", params).fetchone()[0]

        # 排序支持：按时间(mtime)、文件名(name)、文件大小(size)，支持升序/降序
        col_map = {
            "time": "mtime",
            "mtime": "mtime",
            "ctime": "ctime",
            "name": "name COLLATE NOCASE",
            "size": "size",
        }
        order_col = col_map.get(str(sort_by).lower(), "mtime")
        order_dir = "ASC" if str(sort_order).lower() == "asc" else "DESC"

        offset = (page - 1) * page_size
        query_sql = f"""
            SELECT id, path, name, parent, ext, size, mtime, ctime, indexed_at
            FROM files
            WHERE {where_sql}
            ORDER BY {order_col} {order_dir}, id {order_dir}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [page_size, offset]).fetchall()

        items = []
        for r in rows:
            is_today = (r["mtime"] >= today_start)
            time_display = datetime.fromtimestamp(r["mtime"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S")
            created_display = datetime.fromtimestamp(r["ctime"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S") if r["ctime"] else "—"
            items.append({
                "id": r["id"],
                "name": r["name"],
                "path": r["path"],
                "parent": r["parent"],
                "unc_path": to_unc_path(r["path"]),
                "ext": r["ext"],
                "size": r["size"],
                "size_display": format_file_size(r["size"]),
                "mtime": r["mtime"], "ctime":r["ctime"],
                "mtime_display":time_display,"ctime_display":created_display,
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
            "SELECT id,path,name,parent,ext,size,mtime,ctime FROM files WHERE id=?", (file_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="索引中不存在这个文件，请先更新索引后重试。")
    root = get_root()
    try:
        root_real = os.path.realpath(str(root))
        file_real = os.path.realpath(row["path"])
        if os.path.commonpath([os.path.normcase(root_real), os.path.normcase(file_real)]) != os.path.normcase(root_real):
            raise HTTPException(status_code=403, detail="该文件不在允许分析的文件根目录内。")
        policy = directory_access.current.get()
        if policy is not None and not policy.visible(str(Path(file_real).parent)):
            raise HTTPException(403, '文件实际位置不在你的可阅读范围内。')
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
        raise HTTPException(status_code=422, detail="指定的工作表不存在，请重新选择。")
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
        "file": {"id": file_info["id"], "name": file_info["name"], "path": file_info["path"], "ext": file_info["ext"], "size": file_info["size"], "mtime": file_info["mtime"], "ctime":file_info["ctime"]},
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
        raise HTTPException(status_code=422, detail="指定的工作表不存在，请重新选择。")
    sheet = sheet_name or sheets[0]
    df = _normalise_df(_read_table(path, None if sheet == "数据" and path.suffix.lower() in {".csv", ".tsv"} else sheet, nrows=PREVIEW_ROWS))
    return {"file": {"id": file_info["id"], "name": file_info["name"], "path": file_info["path"], "ext": file_info["ext"], "size": file_info["size"], "mtime": file_info["mtime"], "ctime":file_info["ctime"]},
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
def export_analysis(req: AnalysisRequest, request: Request, ticket: str = Query(default='')) -> StreamingResponse:
    info, _ = _get_indexed_file(req.file_id)
    if AUTH_REQUIRED or DIRECTORY_ACL_ENABLED:
        user = _require_user(request)
        mode = _download_mode(info['parent'], user)
        if mode == 'deny' or (mode != 'direct' and not _consume_download_ticket(req.file_id, ticket, user['id'])):
            raise HTTPException(403, '导出包含文件明细，需要下载权限及有效审批授权。')
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
        if not conn.execute("SELECT id FROM saved_analyses WHERE id=?", (saved_id,)).fetchone():
            raise HTTPException(status_code=404, detail="未找到这条分析方案。")
        cur = conn.execute("DELETE FROM saved_analyses WHERE id=?", (saved_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="未找到这条分析方案。")
    return {"ok": True, "message": "已删除保存的分析方案。"}


# ============================================================
# 报表归档时效检测、待办告警与审核流转中心 (Archive Compliance & Task Center)
# ============================================================

class CreateArchiveRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=4096)
    period_type: Literal["daily", "weekly", "monthly", "custom"] = "weekly"
    period_days: int = Field(default=7, ge=1, le=365)
    responsible_person: str = Field(default="", max_length=50)
    file_keyword: str = Field(default="", max_length=200)
    deadline_time: str = Field(default="18:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    schedule: archive_schedule.Schedule = Field(default_factory=archive_schedule.Schedule)


class UpdateArchiveRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    target_path: str = Field(min_length=1, max_length=4096)
    period_type: Literal["daily", "weekly", "monthly", "custom"] = "weekly"
    period_days: int = Field(default=7, ge=1, le=365)
    responsible_person: str = Field(default="", max_length=50)
    file_keyword: str = Field(default="", max_length=200)
    deadline_time: str = Field(default="18:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    schedule: archive_schedule.Schedule = Field(default_factory=archive_schedule.Schedule)


class FeishuConfigRequest(BaseModel):
    app_id: str = Field(default="", max_length=100)
    app_secret: str = Field(default="", max_length=200)
    webhook_url: str = Field(default="", max_length=500)
    notify_enabled: bool = Field(default=True)
    sync_enabled: bool = Field(default=True)


class UpdateUserRoleRequest(BaseModel):
    role: Literal["super_admin", "dept_admin", "user"] = "user"


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=6, max_length=20)
    password: str = Field(default="123456", min_length=4, max_length=50)
    department: str = Field(default="业务运营部", max_length=100)
    role: Literal["super_admin", "dept_admin", "user"] = "user"
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


def _archive_rule_evidence(conn, target_path: str, period_days: int = 7, now: float | None = None,
                           *, file_keyword: str = "", period_type: str = "custom", deadline_time: str = "18:00",
                           schedule: dict | archive_schedule.Schedule | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    local_now = datetime.fromtimestamp(now,demo_data.ZONE)
    config = archive_schedule.Schedule.model_validate(schedule or {})
    calendar_mode = config.mode == 'calendar'
    calendar_info = archive_schedule.timeline(period_type,deadline_time,config,local_now) if calendar_mode else None
    daily = period_type == 'daily' and not calendar_mode
    if daily:
        hour,minute = map(int,deadline_time.split(':'))
        deadline = local_now.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if local_now < deadline:deadline -= timedelta(days=1)
        window_start = deadline.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    else:
        deadline = None
        window_start = now-period_days*86400
    if calendar_mode:
        due = calendar_info['latest_due']
        deadline = datetime.fromtimestamp(due['deadline'],demo_data.ZONE) if due else None
        window_start = due['window_start'] if due else now
    try:
        path = _resolve_catalog_path(target_path)
    except HTTPException:
        return {"state":"invalid_path","path":target_path,"matched_files":0,"nonempty_files":0,
                "latest_time":0,"latest_name":"","reason":"监控目录不在当前文件根目录内"}
    policy = directory_access.current.get()
    if policy and not policy.complete(path):
        raise HTTPException(status_code=403, detail="未获准管理该目录及其完整子目录。")
    condition,params = _catalog_directory_condition(path)
    keyword = file_keyword.strip()
    if keyword:
        condition += " AND instr(lower(name),lower(?))>0"
        params.append(keyword)
    args = [root_scope_key(),*params]
    row = conn.execute(f"""SELECT count(*) AS total,
        coalesce(sum(CASE WHEN size>0 THEN 1 ELSE 0 END),0) AS nonempty,
        coalesce(sum(CASE WHEN size=0 AND mtime>=? AND mtime<=? THEN 1 ELSE 0 END),0) AS empty_due
        FROM files WHERE root_key=? AND {condition}""", [window_start,now,*args]).fetchone()
    latest = conn.execute(f"""SELECT name,mtime FROM files WHERE root_key=? AND size>0
        AND {condition} AND mtime<=? ORDER BY mtime DESC LIMIT 1""", [*args,now]).fetchone()
    latest_time = latest['mtime'] if latest else 0
    if not row['total']:
        state,reason = 'no_files','当前索引没有匹配监控对象的文件，请核对目录、文件名关键词或同步索引'
    elif not row['nonempty']:
        state,reason = 'no_content','匹配到的报表都是空文件，尚无可用的交付数据'
    elif latest_time >= window_start:
        state,reason = 'healthy','已检测到要求时间窗口内的有效报表'
    else:
        state,reason = 'overdue','未检测到要求时间窗口内的有效报表'
    if not daily and not calendar_mode and latest_time:
        deadline = datetime.fromtimestamp(latest_time+period_days*86400,demo_data.ZONE)
    if calendar_mode:
        if calendar_info['calendar_error']:
            state,reason = 'calendar_unavailable',calendar_info['calendar_error']
        elif not calendar_info['latest_due']:
            state,reason = 'scheduled','尚未到达首个可计算的检测节点'
        elif config.holiday_policy == 'skip':
            try:
                if not archive_schedule.workday(local_now.date(),config):
                    state,reason = 'paused','今天为休息日，此规则跳过今天的检测；历史待办保留'
            except archive_schedule.CalendarUnavailable as exc:
                state,reason = 'calendar_unavailable',str(exc)
    expected_day = deadline.date() if deadline else local_now.date()
    day_label = '今天' if expected_day==local_now.date() else '昨天' if expected_day==local_now.date()-timedelta(days=1) else expected_day.strftime('%m月%d日')
    missing_title = f'{day_label}应更新的数据尚未更新' if daily else '已超过要求的更新周期'
    empty_failure = state in {'overdue','no_content'} and row['empty_due']>0
    title = '报表已提交，但文件为空' if empty_failure else missing_title if state=='overdue' else {
        'healthy':'已检测到有效更新','no_content':'监控报表暂无有效内容','no_files':'未找到匹配的监控报表'}.get(state,reason)
    if calendar_mode:
        missing_title = '本期报表未按检测节点更新'
        if state=='overdue': title = '报表已提交，但文件为空' if empty_failure else missing_title
        elif state in {'calendar_unavailable','scheduled','paused'}: title = reason
    if state=='overdue':
        reason = (f"{day_label}{deadline_time}前应完成更新，" if daily else f"要求每 {period_days} 天更新，") + (
            '当前时间窗口仅检测到空文件，最近有效数据仍停留在上一次更新。' if empty_failure else '截至本次检测，尚未发现本期的非空报表。')
    if calendar_mode and state=='overdue':
        reason = f"本期应在 {deadline:%Y-%m-%d %H:%M} 前完成交付；截至本次检测未发现交付窗口内的非空文件。"
    overdue_seconds = max(0,now-deadline.timestamp()) if state=='overdue' and deadline else 0
    return {"schedule":calendar_info,"state":state,"reason":reason,"reason_title":title,
            "reason_code":'empty_delivery' if empty_failure else 'missing_update' if state=='overdue' else state,
            "path":path,"directory_display":_relative_catalog_path(path,_catalog_root_path()) or '根目录',
            "matched_files":row['total'],"nonempty_files":row['nonempty'],"empty_due_files":row['empty_due'],
            "latest_time":latest_time,"latest_name":latest['name'] if latest else '',
            "window_start":window_start,"expected_at":deadline.timestamp() if deadline else None,
            "expected_at_display":deadline.strftime('%Y-%m-%d %H:%M') if deadline else '',
            "expected_label":f'{day_label} {deadline.strftime("%H:%M")}' if deadline else '',
            "report_name":keyword or '目录内报表',"file_keyword":keyword,"deadline_time":deadline_time,
            "frequency_label":calendar_info["summary"] if calendar_mode else f'每天 {deadline_time} 前' if daily else f'每 {period_days} 天',
            "overdue_seconds":overdue_seconds,"assessed_at":now,
            "assessed_at_display":local_now.strftime('%Y-%m-%d %H:%M')}


def _assess_archive_rule(conn, rule, now=None):
    rule = dict(rule)
    return _archive_rule_evidence(conn,rule['target_path'],rule['period_days'],now,
        file_keyword=rule.get('file_keyword',''),period_type=rule.get('period_type','custom'),
        deadline_time=rule.get('deadline_time','18:00'),schedule=json.loads(rule.get('schedule_config','{}')))


def _validate_archive_rule(conn, req) -> tuple[str, int]:
    if not req.name.strip() or not req.responsible_person.strip():
        raise HTTPException(status_code=422, detail="请填写规则名称和明确的责任人或责任组。")
    if req.period_type == "custom" and req.schedule.mode == "calendar":
        raise HTTPException(status_code=422, detail="自定义天数请使用滚动间隔检测模式")
    days = {"daily": 1, "weekly": 7, "monthly": 30}.get(req.period_type, req.period_days)
    evidence = _archive_rule_evidence(conn,req.target_path,days,file_keyword=req.file_keyword,period_type=req.period_type,deadline_time=req.deadline_time,schedule=req.schedule)
    if evidence["state"] in {"invalid_path", "no_files", "calendar_unavailable"}:
        raise HTTPException(status_code=422, detail=evidence["reason"])
    return evidence["path"], days


class ArchiveSchedulePreviewRequest(BaseModel):
    period_type: Literal['daily','weekly','monthly','custom'] = 'weekly'
    deadline_time: str = Field(default='18:00',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    schedule: archive_schedule.Schedule = Field(default_factory=archive_schedule.Schedule)


@app.post('/api/archive/rules/schedule-preview')
def preview_archive_schedule(req: ArchiveSchedulePreviewRequest, request: Request):
    _require_admin(request)
    if req.schedule.mode == 'rolling': return {'ok':True,'summary':'按上次有效更新时间加周期天数检测','upcoming':[],'warnings':[]}
    try:
        result = archive_schedule.timeline(req.period_type,req.deadline_time,req.schedule)
    except ValueError as exc:
        raise HTTPException(status_code=422,detail=str(exc))
    return {'ok':True,**result}


@app.get("/api/archive/rules/preflight")
def preflight_archive_rule(request: Request, path: str = Query(min_length=1, max_length=4096),
                           period_days: int = Query(default=7, ge=1, le=365),
                           file_keyword: str = Query(default="",max_length=200),
                           period_type: Literal["daily","weekly","monthly","custom"] = Query(default="custom"),
                           deadline_time: str = Query(default="18:00",pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"),
                           schedule_config: str = Query(default="{}",max_length=20000)) -> dict[str, Any]:
    _require_admin(request)
    try:
        schedule = archive_schedule.Schedule.model_validate(json.loads(schedule_config))
        if period_type == "custom" and schedule.mode == "calendar": raise ValueError("自定义天数请使用滚动间隔")
    except ValueError as exc:
        raise HTTPException(status_code=422,detail="检测日程配置无效："+str(exc))
    with db_session() as conn:
        days = {'daily':1,'weekly':7,'monthly':30}.get(period_type,period_days)
        evidence = _archive_rule_evidence(conn,path,days,file_keyword=file_keyword,period_type=period_type,deadline_time=deadline_time,schedule=schedule)
    return {"ok": True, "can_save": evidence["state"] not in {"invalid_path", "no_files", "calendar_unavailable"}, **evidence}


def run_archive_compliance_check(conn) -> dict[str, Any]:
    """执行全盘归档时效扫描：
    1. 针对每条启用的时效监控规则，检查对应目录在要求周期内是否有合格文件产出。
    2. 若已新增合格文件，自动将历史未闭环待办标记为【自动消结】(auto_resolved)。
    3. 若逾期未新增，自动生成或更新【逾期未归档待办任务】(pending)。
    """
    now = time.time()
    rules = conn.execute("""
        SELECT * FROM archive_rules 
        WHERE is_active=1
    """).fetchall()

    tasks_created = 0
    tasks_auto_resolved = 0
    tasks_updated = 0
    rule_issues = []

    for rule in rules:
        r_id = rule["id"]
        r_name = rule["name"]
        evidence = _assess_archive_rule(conn,rule,now)
        target_path = evidence["path"]
        if evidence["state"] not in {"healthy", "overdue"}:
            rule_issues.append({"rule_id": r_id, "state": evidence["state"], "reason": evidence["reason"]})
            conn.execute("UPDATE archive_rules SET last_checked_at=? WHERE id=?", (now, r_id))
            continue
        p_type = rule["period_type"]
        p_days = int(rule["period_days"])
        resp_person = rule["responsible_person"] or "数据专员"

        if p_type == "daily":
            period_desc = f"每天 {rule['deadline_time']} 前"
        elif p_type == "monthly" or p_days == 30:
            period_desc = "每 30 天更新"
        else:
            period_desc = f"每 {p_days} 天更新"

        if evidence.get("schedule"):
            period_desc = evidence["frequency_label"]
        latest_time = evidence["latest_time"]
        latest_name = evidence["latest_name"]
        is_compliant = evidence["state"] == "healthy"

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
            if p_type == 'daily' or evidence.get('schedule'):
                overdue_days = max(1, int(evidence['overdue_seconds']//86400)+1)
            elif latest_time > 0:
                diff_sec = now - latest_time
                overdue_days = max(1, int((diff_sec - (p_days * 86400)) / 86400) + 1)
            else:
                overdue_days = p_days

            if active_task:
                conn.execute("""
                    UPDATE archive_tasks
                    SET rule_name=?, period_desc=?, responsible_person=?, overdue_days = ?,
                        last_file_time = ?,
                        last_file_name = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (r_name,period_desc,resp_person,overdue_days,latest_time,latest_name,now,active_task["id"]))
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
        "checked_rules": len(rules),
        "rule_issues": rule_issues
    }


@app.get("/api/archive/overview")
def get_archive_overview() -> dict[str, Any]:
    with db_session() as conn:
        total_rules = conn.execute("SELECT count(*) FROM archive_rules WHERE is_active=1").fetchone()[0]
        pending_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status IN ('pending', 'rejected')").fetchone()[0]
        submitted_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status = 'submitted'").fetchone()[0]
        resolved_tasks = conn.execute("SELECT count(*) FROM archive_tasks WHERE status IN ('approved', 'auto_resolved')").fetchone()[0]

        rules = conn.execute("SELECT * FROM archive_rules WHERE is_active=1").fetchall()
        assessments = [{"rule_id": row["id"], **_assess_archive_rule(conn,row)} for row in rules]
        valid = [item for item in assessments if item["state"] in {"healthy", "overdue"}]
        healthy = sum(item["state"] == "healthy" for item in valid)
        compliance_rate = round(healthy / len(valid) * 100, 1) if valid else None
        rule_issues = [{"rule_id": item["rule_id"], "state": item["state"], "reason": item["reason"]} for item in assessments if item["state"] not in {"healthy", "overdue"}]

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
    policy = directory_access.current.get()
    if policy and not policy.super_admin:
        scheduler_data = {"enabled": True, "interval_seconds": 60, "status": "scoped",
                          "last_run_display": last_run_display, "run_count": None, "auto_resolved_total": None}

    return {
        "ok": True,
        "total_rules": total_rules,
        "pending_tasks": pending_tasks,
        "submitted_tasks": submitted_tasks,
        "resolved_tasks": resolved_tasks,
        "compliance_rate": compliance_rate,
        "metric_name": "当前规则达标率",
        "verifiable_rules": len(valid), "healthy_rules": healthy, "rule_issues": rule_issues,
        "scheduler": scheduler_data
    }


@app.get("/api/archive/rules")
def get_archive_rules() -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute("""
            SELECT * FROM archive_rules
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
                "file_keyword":r["file_keyword"], "deadline_time":r["deadline_time"],
                "schedule":json.loads(r["schedule_config"]),
                "is_active": bool(r["is_active"]),
                "last_checked_at": r["last_checked_at"],
                "last_checked_display": datetime.fromtimestamp(r["last_checked_at"]).strftime("%Y-%m-%d %H:%M") if r["last_checked_at"] > 0 else "尚未检测",
                "created_at": r["created_at"],
                "assessment": _assess_archive_rule(conn,r)
            })
    return {"ok": True, "rules": result}


@app.post("/api/archive/rules")
def create_archive_rule(req: CreateArchiveRuleRequest, request: Request) -> dict[str, Any]:
    _require_admin(request)
    now = time.time()
    with db_session() as conn:
        path, days = _validate_archive_rule(conn, req)
        cur = conn.execute("""
            INSERT INTO archive_rules (name,target_path,period_type,period_days,responsible_person,is_active,created_at,file_keyword,deadline_time,schedule_config)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """, (req.name.strip(),path,req.period_type,days,req.responsible_person.strip(),now,req.file_keyword.strip(),req.deadline_time,req.schedule.model_dump_json()))
        rule_id = cur.lastrowid
        # 立即执行一次时效检测
        run_archive_compliance_check(conn)
    _record_product_event("rule_create", request, subject_id=rule_id)
    return {"ok": True, "id": rule_id, "message": "规则已保存，已完成初次核验"}


@app.put("/api/archive/rules/{rule_id}")
def update_archive_rule(rule_id: int, req: UpdateArchiveRuleRequest, request: Request) -> dict[str, Any]:
    _require_admin(request)
    with db_session() as conn:
        if not conn.execute("SELECT id FROM archive_rules WHERE id=?", (rule_id,)).fetchone():
            raise HTTPException(status_code=404, detail="规则不存在")
        path, days = _validate_archive_rule(conn, req)
        cur = conn.execute("""
            UPDATE archive_rules 
            SET name=?,target_path=?,period_type=?,period_days=?,responsible_person=?,file_keyword=?,deadline_time=?,schedule_config=?
            WHERE id=?
        """, (req.name.strip(),path,req.period_type,days,req.responsible_person.strip(),req.file_keyword.strip(),req.deadline_time,req.schedule.model_dump_json(),rule_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
        # 立即执行一次时效检测
        run_archive_compliance_check(conn)
    _record_product_event("rule_update", request, subject_id=rule_id)
    return {"ok": True, "message": "监控规则已更新并重新校准时效"}


@app.put("/api/archive/rules/{rule_id}/toggle")
def toggle_archive_rule(rule_id: int, request: Request) -> dict[str, Any]:
    _require_admin(request)
    with db_session() as conn:
        rule = conn.execute("SELECT * FROM archive_rules WHERE id=?", (rule_id,)).fetchone()
        if not rule:
            raise HTTPException(status_code=404, detail="规则不存在")
        if rule and not rule["is_active"]:
            _validate_archive_rule(conn, CreateArchiveRuleRequest(**dict(rule),schedule=json.loads(rule["schedule_config"])))
        cur = conn.execute("UPDATE archive_rules SET is_active = (1 - is_active) WHERE id = ?", (rule_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="规则不存在")
    _record_product_event("rule_toggle", request, subject_id=rule_id)
    return {"ok": True, "message": "规则启停状态已更新"}


@app.delete("/api/archive/rules/{rule_id}")
def delete_archive_rule(rule_id: int, request: Request) -> dict[str, Any]:
    _require_admin(request)
    with db_session() as conn:
        if not conn.execute("SELECT id FROM archive_rules WHERE id=?", (rule_id,)).fetchone():
            raise HTTPException(status_code=404, detail="规则不存在")
        conn.execute("DELETE FROM archive_rules WHERE id = ?", (rule_id,))
        conn.execute("DELETE FROM archive_tasks WHERE rule_id = ?", (rule_id,))
    _record_product_event("rule_delete", request, subject_id=rule_id)
    return {"ok": True, "message": "规则及其历史待办已删除"}


@app.post("/api/archive/check")
def trigger_archive_check(request: Request) -> dict[str, Any]:
    _require_admin(request)
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
    page_size: int = Query(default=25, ge=1, le=100),
    task_id: int | None = Query(default=None,ge=1)
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

        if task_id is not None:
            where_clauses.append("id=?"); params.append(task_id)
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
        evidence_by_rule = {}
        for r in rows:
            if r["rule_id"] not in evidence_by_rule:
                rule = conn.execute("SELECT * FROM archive_rules WHERE id=?", (r["rule_id"],)).fetchone()
                evidence_by_rule[r["rule_id"]] = _assess_archive_rule(conn,rule) if rule else {"state": "invalid_path", "reason": "规则已不存在"}
            items.append({
                "assessment": evidence_by_rule[r["rule_id"]],
                "id": r["id"],
                "rule_id": r["rule_id"],
                "rule_name": r["rule_name"],
                "target_path": r["target_path"],
                "period_desc": r["period_desc"].replace("新增", "更新"),
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
                "created_at_display": datetime.fromtimestamp(r["created_at"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S")
            })

    return {
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items
    }


@app.get("/api/archive/tasks/{task_id}")
def get_archive_task_detail(task_id: int) -> dict[str, Any]:
    result = get_archive_tasks(status='all',page=1,page_size=1,task_id=task_id)
    if not result['items']:
        raise HTTPException(status_code=404,detail='监控任务不存在或当前账号无权访问。')
    return {'ok':True,'task':result['items'][0]}


@app.post("/api/archive/tasks/{task_id}/submit")
def submit_archive_task(task_id: int, req: SubmitArchiveTaskRequest, request: Request) -> dict[str, Any]:
    user = _require_user(request)
    now = time.time()
    with db_session() as conn:
        task = conn.execute("SELECT id, status FROM archive_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="待办任务不存在")

        if task["status"] not in {"pending", "rejected"}:
            raise HTTPException(status_code=409, detail="仅待处理或已驳回的任务可以提交。")
        if not req.note.strip() or not req.proof_image.strip():
            raise HTTPException(status_code=422, detail="请填写说明并附上交付凭证。")
        conn.execute("""
            UPDATE archive_tasks
            SET status = 'submitted',
                submit_note = ?,
                proof_image = ?,
                submitted_by = ?,
                submitted_at = ?,
                updated_at = ?
            WHERE id = ?
        """, (req.note.strip(), req.proof_image.strip(), user["name"], now, now, task_id))

    _record_product_event("archive_submit", request, subject_id=task_id)
    return {"ok": True, "message": "任务已提交证明，已流转至高级管理员待审核"}


@app.post("/api/archive/tasks/{task_id}/review")
def review_archive_task(task_id: int, req: ReviewArchiveTaskRequest, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    now = time.time()
    action = req.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="审核操作类型无效")

    with db_session() as conn:
        task = conn.execute("SELECT id, status FROM archive_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="待办任务不存在")

        if task["status"] != "submitted":
            raise HTTPException(status_code=409, detail="仅待审核的任务可以审核。")
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
            """, (new_status, admin["name"], now, req.comment.strip(), now, now, task_id))
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
            """, (new_status, admin["name"], now, req.comment.strip(), now, task_id))

    _record_product_event("archive_review", request, subject_id=task_id)
    return {"ok": True, "message": msg, "new_status": new_status}


@app.post("/api/archive/upload-proof")
async def upload_proof_image(request: Request) -> dict[str, Any]:
    """支持接收图片文件或 JSON base64 格式上传截图证明"""
    _require_user(request)
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
# 登录认证、下载审批与在线预览 (Auth, Download Approval & Preview)
# ============================================================

_AUTH_OPEN_PATHS = {"/api/auth/login", "/api/health"}
_ADMIN_ROLES = ("super_admin", "dept_admin")


def _get_session_user(request: Request) -> dict[str, Any] | None:
    """从 ff_session Cookie 解析当前登录用户；会话过期或账号停用一律视为未登录。"""
    token = request.cookies.get("ff_session", "")
    if not token:
        return None
    with db_session() as conn:
        row = conn.execute(
            """
            SELECT s.expires_at, u.id, u.name, u.phone, u.role, u.status, u.department
            FROM login_sessions s JOIN system_users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
    if not row or row["expires_at"] < time.time() or row["status"] != "active":
        return None
    return dict(row)


def _require_user(request: Request) -> dict[str, Any]:
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先登录后再使用本系统。")
    return user


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if user["role"] not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="该操作需要管理员权限。")
    return user


def _require_super_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="该操作需要超级管理员权限。")
    return user


def _public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {"id": u["id"], "name": u["name"], "phone": u.get("phone") or "",
            "role": u["role"], "department": u.get("department") or "", "directory_acl_enabled": DIRECTORY_ACL_ENABLED,
            "demo_mode": DEMO_MODE}


class LoginRequest(BaseModel):
    phone: str = Field(min_length=1, max_length=20)
    password: str = Field(min_length=1, max_length=64)


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response) -> dict[str, Any]:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM system_users WHERE phone = ?", (req.phone.strip(),)).fetchone()
    if not row or (row["password"] or "123456") != req.password.strip():
        raise HTTPException(status_code=401, detail="手机号或密码错误。")
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="账号已被停用，请联系管理员。")
    now = time.time()
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with db_session() as conn:
        conn.execute("DELETE FROM login_sessions WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO login_sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, row["id"], now, now + SESSION_TTL_HOURS * 3600),
        )
    response.set_cookie("ff_session", token, max_age=int(SESSION_TTL_HOURS * 3600), httponly=True, samesite="lax")
    return {"ok": True, "user": _public_user(dict(row))}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    token = request.cookies.get("ff_session", "")
    if token:
        with db_session() as conn:
            conn.execute("DELETE FROM login_sessions WHERE token = ?", (token,))
    response.delete_cookie("ff_session")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return {"ok": True, "user": _public_user(user), "auth_required": AUTH_REQUIRED or DIRECTORY_ACL_ENABLED,
            "preview_enabled": True}


# ---------- 下载审批 ----------

class DownloadRequestCreate(BaseModel):
    file_id: int
    reason: str = Field(default="", max_length=200)
    search_event_id: int | None = Field(default=None, gt=0)


class DownloadReviewAction(BaseModel):
    action: Literal["approve", "reject"]


def _download_mode(parent: str, user: dict) -> str:
    policy = directory_access.current.get()
    if policy is not None:
        return policy.decision(parent, 'download')
    return 'direct' if user['role'] in _ADMIN_ROLES else 'approval'


@app.get('/api/file/{file_id}/permissions')
def file_permissions(file_id: int, request: Request):
    user = _require_user(request)
    info, _ = _get_indexed_file(file_id)
    policy = directory_access.current.get()
    return {'read': True, 'download': _download_mode(info['parent'], user),
            'upload': bool(policy and policy.decision(info['parent'], 'upload') == 'allow'),
            'update': bool(policy and policy.decision(info['parent'], 'update') == 'allow')}


def _consume_download_ticket(file_id: int, ticket: str, user_id: int | None = None) -> bool:
    """校验审批票据：需与本文件匹配、已批准且未过期；首次成功下载记录审计时间。"""
    if not ticket:
        return False
    now = time.time()
    with db_session() as conn:
        row = conn.execute(
            "SELECT id, file_id, expires_at FROM download_requests WHERE ticket = ? AND user_id = ? AND status = 'approved' AND demo_key=''",
            (ticket, user_id),
        ).fetchone()
        if not row or row["file_id"] != file_id or row["expires_at"] < now:
            return False
        conn.execute("UPDATE download_requests SET downloaded_at = ? WHERE id = ? AND downloaded_at = 0", (now, row["id"]))
    return True


def _format_download_request(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "created_at":r["created_at"],"reviewed_at":r["reviewed_at"],"expires_at":r["expires_at"],"downloaded_at":r["downloaded_at"],
        "user_name": r["user_name"],
        "department": r["department"] or "--",
        "file_id": r["file_id"],
        "file_name": r["file_name"],
        "file_path": r["file_path"],
        "directory_display":_relative_catalog_path(str(Path(r["file_path"]).parent),_catalog_root_path()) or "根目录",
        "reason": r["reason"],
        "is_demo":bool(r["demo_key"]),"review_note":r["review_note"],
        "workflow_events":json.loads(r["workflow_events"]),
        "reviewed_at_display":datetime.fromtimestamp(r["reviewed_at"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S") if r["reviewed_at"] else "—",
        "status": r["status"],
        "ticket": r["ticket"],
        "created_at_display": datetime.fromtimestamp(r["created_at"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S"),
        "reviewed_by": r["reviewed_by"] or "--",
        "expires_at_display": datetime.fromtimestamp(r["expires_at"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S") if r["expires_at"] else "--",
        "expired": bool(r["expires_at"] and r["expires_at"] < time.time()),
        "downloaded_at_display": datetime.fromtimestamp(r["downloaded_at"],demo_data.ZONE).strftime("%Y-%m-%d %H:%M:%S") if r["downloaded_at"] else "--",
    }


@app.post("/api/downloads/requests")
def create_download_request(req: DownloadRequestCreate, request: Request) -> dict[str, Any]:
    """普通成员提交下载申请（用途必填）；管理员自助直下。已有待审批/有效授权时复用不重复创建。"""
    user = _require_user(request)
    file_info, _path = _get_indexed_file(req.file_id)
    mode = _download_mode(file_info['parent'], user)
    if mode == 'deny':
        raise HTTPException(403, '你没有此文件的下载权限，不能发起下载申请。')
    now = time.time()
    with db_session() as conn:
        existing = conn.execute(
            """
            SELECT id, status, ticket FROM download_requests
            WHERE user_id = ? AND file_id = ? AND demo_key=''
              AND (status = 'pending' OR (status = 'approved' AND expires_at > ?))
            ORDER BY id DESC LIMIT 1
            """,
            (user["id"], req.file_id, now),
        ).fetchone()
        if existing:
            return {"ok": True, "id": existing["id"], "status": existing["status"], "ticket": existing["ticket"],
                    "message": "已有待审批的申请" if existing["status"] == "pending" else "已有有效下载授权"}
        reason = req.reason.strip()
        if not reason:
            raise HTTPException(status_code=400, detail="请填写下载用途后再提交申请。")
        is_admin = mode == 'direct'
        status = "approved" if is_admin else "pending"
        ticket = (uuid.uuid4().hex + uuid.uuid4().hex) if is_admin else ""
        expires = now + DOWNLOAD_TICKET_TTL_HOURS * 3600 if is_admin else 0
        cur = conn.execute(
            """
            INSERT INTO download_requests(user_id, user_name, department, file_id, file_name, file_path, reason,
                                          status, ticket, created_at, reviewed_by, reviewed_at, expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (user["id"], user["name"], user.get("department") or "", req.file_id, file_info["name"], file_info["path"],
             reason, status, ticket, now, "管理员自助" if is_admin else "", now if is_admin else 0, expires),
        )
    _record_product_event("download_request", request, subject_id=req.file_id, search_event_id=req.search_event_id)
    return {"ok": True, "id": cur.lastrowid, "status": status, "ticket": ticket}


@app.get("/api/downloads/requests")
def list_download_requests(request: Request, scope: str = Query(default="mine")) -> dict[str, Any]:
    user = _require_user(request)
    with db_session() as conn:
        if scope == "all":
            if user["role"] not in _ADMIN_ROLES:
                raise HTTPException(status_code=403, detail="需要管理员权限。")
            rows = conn.execute("SELECT * FROM download_requests ORDER BY created_at DESC,id DESC LIMIT 300").fetchall()
        else:
            rows = conn.execute("SELECT * FROM download_requests WHERE user_id = ? ORDER BY created_at DESC,id DESC LIMIT 100",
                                (user["id"],)).fetchall()
        pending_count = conn.execute("SELECT count(*) FROM download_requests WHERE status = 'pending'").fetchone()[0]
    return {"ok": True, "requests": [_format_download_request(r) for r in rows], "pending_count": pending_count}


@app.post("/api/downloads/requests/{req_id}/review")
def review_download_request(req_id: int, req: DownloadReviewAction, request: Request) -> dict[str, Any]:
    admin = _require_admin(request)
    now = time.time()
    with db_session() as conn:
        row = conn.execute("SELECT id, user_id, status, demo_key, workflow_events FROM download_requests WHERE id = ?", (req_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="申请不存在。")
        if row["status"] != "pending":
            raise HTTPException(status_code=400, detail="该申请已被处理过。")
        if req.action == "approve":
            if row['user_id'] == admin['id'] and admin['role'] != 'super_admin':
                raise HTTPException(403, '需要审批的下载不能自行批准，请由其他有权限的管理员处理。')
            if DIRECTORY_ACL_ENABLED:
                applicant = conn.execute('SELECT u.* FROM system_users u JOIN download_requests d ON d.user_id=u.id WHERE d.id=?', (req_id,)).fetchone()
                target = conn.execute('SELECT file_path FROM download_requests WHERE id=?', (req_id,)).fetchone()
                if not applicant or applicant['status'] != 'active':
                    raise HTTPException(403, '申请人已停用或不存在，不能批准。')
                applicant_policy = directory_access.load_policy(conn, dict(applicant), root_scope_key(), _catalog_root_path())
                if applicant_policy.decision(str(Path(target['file_path']).parent), 'download') == 'deny':
                    raise HTTPException(403, '申请人的阅读或下载权限已撤销，不能批准。')
            ticket = "" if row["demo_key"] else uuid.uuid4().hex + uuid.uuid4().hex
            conn.execute(
                "UPDATE download_requests SET status='approved', ticket=?, reviewed_by=?, reviewed_at=?, expires_at=? WHERE id=?",
                (ticket, admin["name"], now, now + DOWNLOAD_TICKET_TTL_HOURS * 3600, req_id),
            )
            message = f"已批准，下载链接 {DOWNLOAD_TICKET_TTL_HOURS:g} 小时内有效。"
        else:
            conn.execute(
                "UPDATE download_requests SET status='rejected', reviewed_by=?, reviewed_at=? WHERE id=?",
                (admin["name"], now, req_id),
            )
            message = "已驳回该下载申请。"
        events=json.loads(row['workflow_events'])
        events.append({'kind':'approved' if req.action=='approve' else 'rejected','at':now,'actor':admin['name'],
                       'note':'同意按申请用途使用' if req.action=='approve' else '请补充用途与范围后重新申请'})
        conn.execute('UPDATE download_requests SET workflow_events=?,review_note=? WHERE id=?',
                     (json.dumps(events,ensure_ascii=False),events[-1]['note'],req_id))
    _record_product_event("download_review", request, subject_id=req_id)
    return {"ok": True, "message": message}


# ---------- 在线预览（服务端限行解析） ----------

@app.get("/api/file/{file_id}/preview")
def preview_file(file_id: int, request: Request) -> dict[str, Any]:
    """返回服务端限行预览页地址；原件下载使用独立审批流程。"""
    if AUTH_REQUIRED or DIRECTORY_ACL_ENABLED:
        _require_user(request)
    _get_indexed_file(file_id)
    return {"ok": True, "url": f"/static/preview.html?id={file_id}"}


_limited_preview_gate=threading.BoundedSemaphore(1)
_limited_preview_cache=OrderedDict()
_limited_preview_cache_lock=threading.Lock()


@app.get('/api/file/{file_id}/preview-media')
def preview_media(file_id: int, request: Request, page: int = Query(default=1, ge=1, le=100)):
    if AUTH_REQUIRED or DIRECTORY_ACL_ENABLED:
        _require_user(request)
    info, path = _get_indexed_file(file_id)
    data, asset = media_preview.render(path, DB_PATH.parent, page)
    return FileResponse(asset, media_type='image/png', headers={'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff'})


@app.get('/api/file/{file_id}/preview-data')
def preview_data(file_id: int,request: Request,sheet: str=Query(default='',max_length=200),
                 page: int=Query(default=1,ge=1),page_size: int=Query(default=100,ge=1,le=200)):
    if AUTH_REQUIRED or DIRECTORY_ACL_ENABLED:_require_user(request)
    info,path=_get_indexed_file(file_id)
    if path.suffix.lower() in media_preview.EXTENSIONS:
        data, asset = media_preview.render(path, DB_PATH.parent, page)
        return JSONResponse({**data, 'ok':True, 'name':info['name'], 'page':page,
            'asset_url':f'/api/file/{file_id}/preview-media?page={page}'}, headers={'Cache-Control':'no-store'})
    start=(page-1)*page_size
    if start>=limited_preview.MAX_ROWS:raise HTTPException(422,'仅预览前 10,000 行，请使用已批准的下载获取完整文件')
    st=path.stat()
    if st.st_size==0:raise HTTPException(422,'空文件，暂无可预览内容')
    limit=int(os.getenv('MAX_PREVIEW_FILE_MB','32'))*1024*1024
    if path.suffix.lower() not in {'.csv','.tsv','.xlsx','.xlsm'}:limit=min(limit,16*1024*1024)
    if st.st_size>limit:raise HTTPException(413,'文件超过当前服务端预览大小上限，请拆分文件或申请下载')
    key=(str(DB_PATH),root_scope_key(),str(path),st.st_mtime_ns,st.st_size,sheet,page,page_size)
    with _limited_preview_cache_lock:
        cached=_limited_preview_cache.get(key)
        if cached and time.monotonic()-cached[0]<60:
            _limited_preview_cache.move_to_end(key)
            return JSONResponse(cached[1],headers={'Cache-Control':'no-store'})
    if not _limited_preview_gate.acquire(timeout=.2):raise HTTPException(429,'预览任务繁忙，请稍后重试',headers={'Retry-After':'2'})
    try:
        data=limited_preview.read(path,sheet,start,page_size)
        after=path.stat()
        if (st.st_mtime_ns,st.st_size)!=(after.st_mtime_ns,after.st_size):raise HTTPException(409,'文件正在更新，请重试')
        data.update(ok=True,file_id=file_id,name=info['name'],page=page,page_size=page_size)
        with _limited_preview_cache_lock:
            _limited_preview_cache[key]=(time.monotonic(),data)
            while len(_limited_preview_cache)>32 or sum(len(json.dumps(v[1],ensure_ascii=False).encode()) for v in _limited_preview_cache.values())>16*1024*1024:
                _limited_preview_cache.popitem(last=False)
        return JSONResponse(data,headers={'Cache-Control':'no-store'})
    except HTTPException:raise
    except Exception as exc:raise HTTPException(422,detail=f'无法预览：{str(exc)[:180]}')
    finally:_limited_preview_gate.release()


# ============================================================
# 人员权限与飞书集成管理中心 (User Roles & Feishu Integration)
# ============================================================

@app.get("/api/system/permissions")
def get_system_permissions(request: Request) -> dict[str, Any]:
    _require_super_admin(request)
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
            SELECT id, name, phone, feishu_id, email, department, role, status, avatar, created_at, updated_at
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
        "feishu_config": {"configured": feishu_integration.configured()},
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
def create_system_user(req: CreateUserRequest, request: Request) -> dict[str, Any]:
    _require_super_admin(request)
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
def reset_user_password(user_id: int, req: ResetUserPasswordRequest, request: Request) -> dict[str, Any]:
    _require_super_admin(request)
    new_pwd = req.password.strip()
    if not new_pwd:
        raise HTTPException(status_code=400, detail="密码不能为空")
    with db_session() as conn:
        cur = conn.execute("UPDATE system_users SET password = ?, updated_at = ? WHERE id = ?", (new_pwd, time.time(), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员密码已成功重置"}


@app.put("/api/system/permissions/users/{user_id}/role")
def update_user_role(user_id: int, req: UpdateUserRoleRequest, request: Request) -> dict[str, Any]:
    _require_super_admin(request)
    if req.role not in ("super_admin", "dept_admin", "user"):
        raise HTTPException(status_code=400, detail="无效角色")
    with db_session() as conn:
        cur = conn.execute("UPDATE system_users SET role = ?, updated_at = ? WHERE id = ?", (req.role, time.time(), user_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员系统角色已更新"}


@app.put("/api/system/permissions/users/{user_id}/status")
def toggle_user_status(user_id: int, request: Request) -> dict[str, Any]:
    _require_super_admin(request)
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
def delete_system_user(user_id: int, request: Request) -> dict[str, Any]:
    _require_super_admin(request)
    with db_session() as conn:
        cur = conn.execute("DELETE FROM system_users WHERE id = ?", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="成员不存在")
    return {"ok": True, "message": "成员授权已移除"}



class DirectoryGrantRequest(BaseModel):
    subject_type: Literal['user', 'department']
    subject_key: str = Field(min_length=1, max_length=200)
    target_path: str = Field(min_length=1, max_length=4096)
    effect: Literal['allow', 'deny'] = 'allow'
    permissions: dict[str, str] | None = None

    @field_validator('permissions')
    @classmethod
    def validate_permissions(cls, value):
        if value is None:
            return value
        if not value:
            raise ValueError('至少选择一项操作权限')
        for action, mode in value.items():
            choices = {'deny', 'approval', 'direct'} if action == 'download' else {'allow', 'deny'}
            if action not in directory_access.OPERATIONS or mode not in choices:
                raise ValueError('权限类型或规则无效')
        return value


class DirectoryGrantBatch(BaseModel):
    subject_type: Literal['user', 'department']
    subject_keys: list[str] = Field(min_length=1, max_length=100)
    target_paths: list[str] = Field(min_length=1, max_length=100)
    permissions: dict[str, str]
    mode: Literal['merge', 'replace', 'revoke'] = 'merge'
    preview: bool = True
    revision: str = ''

    @field_validator('permissions')
    @classmethod
    def validate_permissions(cls, value):
        return DirectoryGrantRequest.validate_permissions(value)


def _validate_grant_target(conn, subject_type, subject, value):
    if not subject or len(subject) > 200:
        raise HTTPException(422, '请选择有效成员或部门。')
    if subject_type == 'user':
        exists = conn.execute('SELECT id FROM system_users WHERE CAST(id AS TEXT)=?', (subject,)).fetchone()
    else:
        exists = conn.execute("SELECT id FROM system_users WHERE department=? AND department<>''", (subject,)).fetchone()
    if not exists:
        raise HTTPException(422, '请选择系统已有的成员或部门。')
    target = _resolve_catalog_path(value)
    path = Path(target)
    # Allow existing empty folders, but never authorize a symlink escape.
    if not path.is_dir():
        raise HTTPException(422, '目标文件夹不存在或无法访问。')
    if not directory_access.within(directory_access.normalise(str(path.resolve())), _catalog_root_path()):
        raise HTTPException(403, '目标文件夹不能指向文件根目录以外。')
    return target


def _grant_audit(conn, actor, action, data):
    conn.execute('INSERT INTO directory_grant_audit(actor_id,action,grant_json,created_at) VALUES(?,?,?,?)',
                 (actor, action, json.dumps(data, ensure_ascii=False), time.time()))


def _write_grant(conn, actor, subject_type, subject, target, permissions, effect='allow'):
    conn.execute('''INSERT INTO directory_grants
        (root_key,subject_type,subject_key,target_path,effect,created_by,created_at,permissions)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(root_key,subject_type,subject_key,target_path)
        DO UPDATE SET effect=excluded.effect,permissions=excluded.permissions,
        created_by=excluded.created_by,created_at=excluded.created_at''',
        (root_scope_key(), subject_type, subject, target, effect, actor, time.time(),
         json.dumps(permissions, ensure_ascii=False) if permissions is not None else None))


@app.get('/api/auth/scopes')
def get_my_directory_scopes(request: Request) -> dict[str, Any]:
    _require_user(request)
    policy = directory_access.current.get()
    return {'enabled': DIRECTORY_ACL_ENABLED, 'unrestricted': not policy or policy.super_admin,
            'grants': [{'path': path, 'effect': effect} for path, effect in (policy.grants if policy else ())]}


@app.get('/api/system/directory-grants')
def list_directory_grants(request: Request) -> dict[str, Any]:
    _require_super_admin(request)
    with db_session() as conn:
        rows = conn.execute('SELECT * FROM directory_grants WHERE root_key=? ORDER BY id DESC', (root_scope_key(),)).fetchall()
        audit = conn.execute('SELECT * FROM directory_grant_audit ORDER BY id DESC LIMIT 30').fetchall()
    return {'enabled': DIRECTORY_ACL_ENABLED,
            'grants': [{**dict(row), 'permissions': directory_access.grant_permissions(row),
                        'legacy': row['permissions'] is None} for row in rows],
            'audit': [dict(row) for row in audit]}


@app.post('/api/system/directory-grants')
def save_directory_grant(req: DirectoryGrantRequest, request: Request) -> dict[str, Any]:
    admin = _require_super_admin(request)
    subject = req.subject_key.strip()
    with db_session() as conn:
        target = _validate_grant_target(conn, req.subject_type, subject, req.target_path)
        _write_grant(conn, admin['id'], req.subject_type, subject, target, req.permissions, req.effect)
        _grant_audit(conn, admin['id'], 'save', {**req.model_dump(), 'target_path': target})
    return {'ok': True, 'message': '授权已生效；子目录继承，每项操作分别校验，拒绝优先。'}


@app.post('/api/system/directory-grants/batch')
def batch_directory_grants(req: DirectoryGrantBatch, request: Request):
    admin = _require_super_admin(request)
    subjects = sorted(set(s.strip() for s in req.subject_keys))
    paths = sorted(set(req.target_paths))
    if len(subjects) * len(paths) > 500:
        raise HTTPException(422, '单批最多处理 500 组人员或部门与目录组合，请分批设置。')
    changes = []
    with db_session() as conn:
        # Serialize the comparison and mutation so a reviewed batch cannot overwrite newer grants.
        conn.execute('BEGIN IMMEDIATE')
        for subject in subjects:
            for value in paths:
                target = _validate_grant_target(conn, req.subject_type, subject, value)
                row = conn.execute('''SELECT * FROM directory_grants WHERE root_key=? AND
                    subject_type=? AND subject_key=? AND target_path=?''',
                    (root_scope_key(), req.subject_type, subject, target)).fetchone()
                before = directory_access.grant_permissions(row) if row else {}
                after = {} if req.mode == 'replace' else dict(before)
                if req.mode == 'revoke':
                    for action in req.permissions:
                        after.pop(action, None)
                else:
                    after.update(req.permissions)
                changes.append({'subject_key': subject, 'target_path': target, 'before': before, 'after': after})
        import hashlib
        revision = hashlib.sha256(json.dumps([root_scope_key(), req.subject_type, changes], sort_keys=True).encode()).hexdigest()
        if req.preview:
            return {'ok': True, 'revision': revision, 'changes': changes, 'count': len(changes),
                    'message': '撤销本级配置后可能继续继承上级或部门权限；明确禁止请设置为拒绝。'}
        if req.revision != revision:
            raise HTTPException(409, '授权配置已变化或尚未预览，请重新检查批量变更。')
        batch_id = uuid.uuid4().hex
        for change in changes:
            if change['after']:
                _write_grant(conn, admin['id'], req.subject_type, change['subject_key'], change['target_path'], change['after'])
            else:
                conn.execute('DELETE FROM directory_grants WHERE root_key=? AND subject_type=? AND subject_key=? AND target_path=?',
                             (root_scope_key(), req.subject_type, change['subject_key'], change['target_path']))
            _grant_audit(conn, admin['id'], 'batch_' + req.mode, {**change, 'subject_type': req.subject_type, 'batch_id': batch_id})
    return {'ok': True, 'count': len(changes), 'message': f'已处理 {len(changes)} 组授权，后续请求按新权限校验。'}


@app.get('/api/system/directory-grants/effective')
def effective_directory_grants(request: Request, user_id: int, path: str):
    _require_super_admin(request)
    with db_session() as conn:
        user = conn.execute('SELECT * FROM system_users WHERE id=?', (user_id,)).fetchone()
        if not user:
            raise HTTPException(404, '成员不存在。')
        target = _validate_grant_target(conn, 'user', str(user_id), path)
        policy = directory_access.load_policy(conn, dict(user), root_scope_key(), _catalog_root_path())
        rows = conn.execute('''SELECT * FROM directory_grants WHERE root_key=? AND
            ((subject_type='user' AND subject_key=?) OR (subject_type='department' AND subject_key=? AND subject_key<>''))''',
            (root_scope_key(), str(user_id), user['department'] or '')).fetchall()
    permissions = {op: policy.decision(target, op) for op in directory_access.OPERATIONS}
    if user['status'] != 'active':
        permissions = dict.fromkeys(directory_access.OPERATIONS, 'deny')
    return {'user_name': user['name'], 'permissions': permissions, 'super_admin': policy.super_admin,
            'sources': [{**dict(r), 'permissions': directory_access.grant_permissions(r)} for r in rows
                        if directory_access.within(target, r['target_path'])]}


@app.delete('/api/system/directory-grants/{grant_id}')
def delete_directory_grant(grant_id: int, request: Request) -> dict[str, Any]:
    admin = _require_super_admin(request)
    with db_session() as conn:
        row = conn.execute('SELECT * FROM directory_grants WHERE id=? AND root_key=?', (grant_id, root_scope_key())).fetchone()
        if not row:
            raise HTTPException(404, '授权记录不存在。')
        conn.execute('DELETE FROM directory_grants WHERE id=?', (grant_id,))
        _grant_audit(conn, admin['id'], 'delete', dict(row))
    return {'ok': True, 'message': '本级授权已撤销；上级或部门授权仍可能生效。'}


file_management.register(sys.modules[__name__])
feishu_integration.register(sys.modules[__name__])


async def not_found_handler(request: Request, exc: Exception):
    if request.url.path.startswith('/api/'):
        return JSONResponse(status_code=404, content={'detail': 'Not Found'})
    return RedirectResponse(url='/')
