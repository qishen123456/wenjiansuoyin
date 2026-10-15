# 企业文件检索与轻量 BI 分析中心

本项目以企业共享盘中的 Excel/CSV 文件为数据源，先建立本地元数据索引，再提供文件检索和按需轻量 BI。目标是让普通检索只查索引，不在每次输入时扫描整个网盘；AI 仅作为可选的关键词扩展/候选重排，BI 则对用户主动选中的单个工作簿执行字段筛选、聚合、交叉表、图表和 Excel 导出。

## 快速开始

推荐用 Docker，开发与生产跑的是同一个镜像，避免“本机好好的”：

```bash
cp .env.example .env      # 改 SHARE_HOST_PATH 指向共享盘挂载点
docker compose up -d --build
```

打开 http://127.0.0.1:8080，点“建立 / 更新索引”，然后搜索。生产服务器部署、跨架构构建、令牌与挂载权限见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

也可以不走 Docker，直接装 Python 依赖（**需要 Python 3.10+**，代码里 `str | None` 注解由 pydantic 在运行时求值，3.9 会启动失败）：

```bash
cd fast_file_finder
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：先用样例数据体验可设置 `FILE_ROOT=./demo_data`；连接真实共享盘时替换成服务运行机器可访问的 UNC 路径或已挂载目录。详细参数见 [`fast_file_finder/README.md`](fast_file_finder/README.md)。

本机体验默认只监听 localhost：

```bash
uvicorn app:app --host 127.0.0.1 --port 8080
```

打开 http://127.0.0.1:8080，点击“建立 / 更新索引”，搜索样例供应商结算表，进入“开始分析”尝试交叉表、筛选、图表和 Excel 导出。

需要给其他内网机器访问时，**先在 `.env` 配置随机长 ASCII 令牌 `APP_ACCESS_TOKEN`**，再使用 `--host 0.0.0.0`。当前共享令牌只是基础入口保护，不等于企业登录或逐用户文件 ACL；生产上线前仍需公司 SSO、权限过滤、审计与实际 SMB/Excel 环境验证。

## 代码目录

- `fast_file_finder/app.py`：FastAPI 服务、SQLite FTS5 索引、后台增量扫描、BI 分析逻辑
- `fast_file_finder/static/index.html`：检索页与轻量 BI 工作区
- `fast_file_finder/tests/`：搜索、索引、BI、权限门槛与边界情况的自动化回归测试
- `fast_file_finder/demo_data/`：虚构样例工作簿
- `fast_file_finder/Dockerfile`：多阶段镜像（`runtime` 生产 / `dev` 含测试）
- `docker-compose.yml` / `docker-compose.prod.yml`：开发编排与生产覆盖层
- `.github/workflows/tests.yml`：Python 3.10/3.11/3.12 自动化测试
- `docs/ENGINEERING_CHECKLIST.md`：架构不变量、试点验收指标和生产上线前检查清单
- `docs/DEPLOYMENT.md`：Docker 部署、跨架构构建、升级回滚、已知架构约束

## 状态

当前是可运行的内测 MVP，真实共享盘性能和大工作簿行为仍需要在授权测试目录验证。它尚未完成公司级 SSO、逐用户文件 ACL、生产审计和所有终端上的一键启动 Excel 验收，不应直接无保护地对全员开放。