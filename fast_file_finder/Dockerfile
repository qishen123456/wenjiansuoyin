# syntax=docker/dockerfile:1
# 企业文件检索与轻量 BI 分析中心 —— 生产可用镜像
# 构建上下文固定为 fast_file_finder/：
#   docker build -t file-finder fast_file_finder

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# base：只装运行依赖，命中层缓存后改代码不会重新 pip install
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# sqlite 由 python 自带的标准库提供，已确认支持 FTS5；无需额外系统包。
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# ---------------------------------------------------------------------------
# runtime：最终交付镜像。不含 .env、源码测试、git 信息
# ---------------------------------------------------------------------------
FROM base AS runtime

# 固定 uid/gid，方便和宿主机共享目录（尤其是 Linux 上挂载的 SMB 盘）对齐权限。
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid ${APP_GID} appgrp \
    && useradd --uid ${APP_UID} --gid appgrp --no-create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appgrp /app

COPY --chown=appuser:appgrp app.py /app/app.py
COPY --chown=appuser:appgrp static /app/static

# 容器内默认路径；生产用 compose/环境变量覆盖。
# 注意：这里的 FILE_ROOT 是「容器内」的挂载点，不是宿主机上的共享盘路径。
ENV FILE_ROOT=/data/share \
    INDEX_DB_PATH=/app/data/file_index.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080 \
    LOG_LEVEL=INFO

EXPOSE 8080
USER appuser

# 索引库必须落在持久卷上；/data/share 是只读网盘挂载点，由编排层负责。
VOLUME ["/app/data"]

# 健康检查探首页而不是 /api/health：令牌网关会拦截所有 /api/*（含 health），
# 生产配了 APP_ACCESS_TOKEN 后 urllib 收到 401 会抛异常，容器会被误判 unhealthy。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/',timeout=4).status==200 else 1)"]

# 单进程是硬性约束：扫描锁 _scan_lock 和进度状态 _scan_state 都是进程内对象，
# 多 worker 会让每个进程各自全量扫一遍共享盘，还会并发写同一个 SQLite。
# 需要更高并发时请起多个容器（每个容器一个 FILE_ROOT），前面放反向代理。
CMD ["sh", "-c", "exec python -m uvicorn app:app --host \"${APP_HOST:-0.0.0.0}\" --port \"${APP_PORT:-8080}\" --workers 1 --no-proxy-headers"]

# ---------------------------------------------------------------------------
# dev：只为在容器里跑仓库自带 pytest，不用于生产
# ---------------------------------------------------------------------------
FROM base AS dev

COPY dev-requirements.txt ./dev-requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r dev-requirements.txt
COPY app.py /app/app.py
COPY static /app/static
COPY tests /app/tests
RUN mkdir -p /app/data
CMD ["python", "-m", "pytest", "-q", "tests"]
