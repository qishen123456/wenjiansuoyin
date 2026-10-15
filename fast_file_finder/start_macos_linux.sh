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
echo "启动服务：http://127.0.0.1:8080"
exec .venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8080