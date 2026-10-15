# 企业文件检索与轻量 BI 分析中心

这是文件检索 + 轻量表格分析的可运行 MVP。完整源码位于 `fast_file_finder/`。

## 功能

- SQLite FTS5 文件元数据索引：普通搜索只查本地索引，不对每次查询全盘扫描。
- 后台建立/更新索引，支持文件名、目录关键词、扩展名筛选和相关度排序。
- 可选 AI 检索兜底：先扩展查询词或重排少量索引候选，不扫描所有文件，也不把整本工作簿发给模型。
- 轻量 BI：读取指定 Excel 工作表，查看字段和数据预览，多条件筛选、聚合计算、交叉表、柱形/折线图。
- 导出新 Excel，保存分析配置；不会覆盖原始工作簿。
- 附带虚构样例数据和 BI API 自动化测试。

## 快速启动

进入源码目录：

```bash
cd fast_file_finder
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell 请使用 .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`，先使用样例数据验证：

```dotenv
FILE_ROOT=./demo_data
INDEX_DB_PATH=./data/file_index.db
```

启动：

```bash
uvicorn app:app --host 0.0.0.0 --port 8080
```

访问 http://127.0.0.1:8080，点击“建立 / 更新索引”，搜索“供应商结算明细”，再点击“开始分析”试用透视表、筛选和导出。

接入内网共享盘时，把 `FILE_ROOT` 改成运行服务的机器实际可访问的 UNC 路径或挂载路径。SQLite 索引建议保存在服务机器本地磁盘。

## 项目结构

- `fast_file_finder/app.py`：FastAPI 接口、索引与 BI 处理逻辑
- `fast_file_finder/static/index.html`：检索及 BI 前端
- `fast_file_finder/tests/`：自动化测试
- `fast_file_finder/demo_data/`：虚构样例工作簿
- `fast_file_finder/README.md`：详细部署说明、配置项和常见问题

## 当前边界

该版本是内测 MVP，尚未完成公司级统一登录、逐用户文件 ACL 过滤、审计和终端一键启动 Excel 的全环境验证。不要直接面向全员生产开放；接真实共享盘前应先在授权目录进行小范围测试。
