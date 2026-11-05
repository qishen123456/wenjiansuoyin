# 企业文件检索与轻量 BI 分析中心

本项目以企业共享盘中的 Excel/CSV 文件为数据源，建立本地元数据索引，并提供文件检索、AI 找表、目录权限、服务端限行预览、下载审批、归档治理和按需轻量 BI。系统的核心原则是：**普通搜索不重新扫描网盘，AI 不遍历全部 Excel，预览不传输完整原件，授权检查前置到查询层**。

> 当前技术口径（2026-11-05）：已实现动态多线程扫描、有限队列与增量批量写库，SQLite FTS5 检索、应用层用户/部门目录 ACL、服务端限行预览和原件审批。**应用目录 ACL 不能替代 NAS/SMB 原生文件 ACL、企业 SSO；现有密码存储逻辑仍需安全迁移。**真实客户共享盘性能与安全边界必须现场验收。

## 客户技术说明与产品演示

- **[16 页客户展示手册（HTML）](docs/customer_technology_manual.html)**：保留业务场景，并讲清技术架构、扫描提速、索引检索、AI 找表、限行预览、目录权限、下载审批与分阶段实施周期。采用客户展示版文案，不包含源码索引、开发日期或讲解备注。下载 HTML 到本地浏览器打开，可用方向键翻页、F 全屏。
- **[客户技术详解与验收清单](docs/CUSTOMER_TECHNICAL_BRIEF.md)**：说明核心算法和数据流、接口、限制、示例指标及真实环境验收口径。
- **[扫描与服务端限行预览实施记录](docs/SCAN_AND_LIMITED_PREVIEW.md)**：含 119,668 个本地模拟文件单轮对比、内存成本与安全边界。
- **系统内使用指南**：服务启动后进入 `/guide`，已增加“技术原理与安全边界”章节。



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

完整模拟网盘随仓库以压缩包保存。先在项目根目录运行 `python scripts/restore_demo_data.py`，再将 `fast_file_finder/.env` 设置为 `FILE_ROOT=../网盘模拟数据`、`DEMO_MODE=true`。已有模拟目录不会被还原脚本覆盖，详情见 [`demo_assets/README.md`](demo_assets/README.md)。连接真实共享盘时替换成服务器可访问的 UNC 路径或已挂载目录。详细参数见 [`fast_file_finder/README.md`](fast_file_finder/README.md)。

本机体验默认只监听 localhost：

```bash
uvicorn app:app --host 127.0.0.1 --port 8080
```

打开 http://127.0.0.1:8080，点击“建立 / 更新索引”，搜索样例供应商结算表，进入“开始分析”尝试交叉表、筛选、图表和 Excel 导出。

需要给其他内网机器访问时，**先在 `.env` 配置随机长 ASCII 令牌 `APP_ACCESS_TOKEN`**，再使用 `--host 0.0.0.0`。共享令牌只是额外入口保护；当前另有登录会话和应用层目录 ACL。两者仍不能代替企业 SSO、NAS 原生文件 ACL、TLS、凭据哈希和真实 SMB/Excel 环境验收。

## 代码目录

- `fast_file_finder/app.py`：FastAPI 服务、SQLite FTS5 索引、后台增量扫描、BI 分析逻辑
- `fast_file_finder/static/index.html`：检索页与轻量 BI 工作区
- `fast_file_finder/scan_pipeline.py`：动态多线程目录遍历与有界任务队列
- `fast_file_finder/limited_preview.py`：服务端限行解析，返回分页数据而非原件字节
- `fast_file_finder/directory_access.py`：应用层用户/部门目录授权、拒绝优先与 SQL 可见视图
- `fast_file_finder/tests/`：扫描、限行预览、AI、目录 ACL、BI、审批等回归测试
- `fast_file_finder/demo_data/`：虚构样例工作簿
- `fast_file_finder/Dockerfile`：多阶段镜像（`runtime` 生产 / `dev` 含测试）
- `docker-compose.yml` / `docker-compose.prod.yml`：开发编排与生产覆盖层
- `.github/workflows/tests.yml`：Python 3.10/3.11/3.12 自动化测试
- `docs/ENGINEERING_CHECKLIST.md`：架构不变量、试点验收指标和生产上线前检查清单
- `docs/DEPLOYMENT.md`：Docker 部署、跨架构构建、升级回滚、已知架构约束

## 状态

当前是已具备应用层目录 ACL、限行预览、AI 找表与下载审批的可运行内测 MVP；真实共享盘性能、大工作簿和多用户并发仍需要授权测试。**尚未完成**公司级 SSO、NAS/SMB 原生文件 ACL 关联、密码安全迁移、全部生产审计与终端一键启动 Excel 验收，不应无保护地对全员开放。
### 本机每日 Demo

每日滚动报表、组织目录样式、占位文件清理与恢复方式见 [每日演示数据说明](docs/DEMO_DAILY_DATA.md)。演示模式默认关闭，生产编排固定关闭；仅本机模拟数据环境已开启。
