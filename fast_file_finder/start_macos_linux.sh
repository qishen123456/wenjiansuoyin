#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已创建 .env。请先编辑 FILE_ROOT，填入 ./demo_data 或实际共享目录，然后重新运行。"
  exit 0
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install -r requirements.txt
APP_HOST="${APP_HOST:-127.0.0.1}"
if [[ "$APP_HOST" != "127.0.0.1" && "$APP_HOST" != "localhost" && "$APP_HOST" != "::1" ]]; then
  token_configured="$(.venv/bin/python -c "from dotenv import dotenv_values; print('1' if (dotenv_values('.env').get('APP_ACCESS_TOKEN') or '').strip() else '0')")"
  if [[ "$token_configured" != "1" ]]; then
    echo "拒绝启动：当前 APP_HOST=$APP_HOST 会对其他机器开放，但 .env 未配置非空 APP_ACCESS_TOKEN。请先设置随机长 ASCII 令牌；共享令牌仍不能替代企业 SSO/文件 ACL。" >&2
    exit 1
  fi
fi
echo "启动服务：http://$APP_HOST:8080"
exec .venv/bin/python -m uvicorn app:app --host "$APP_HOST" --port 8080