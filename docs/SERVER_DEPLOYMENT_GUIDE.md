# 企业文件检索与轻量 BI 分析平台 —— 服务器 Docker 生产部署与云盘切换指引

本文档详细说明如何将本项目通过 Docker / Docker Compose 部署到正式服务器，以及**如何从当前的本地模拟数据平滑切换到真实内网云盘（如 172.11.111.11 / 128GB 表格）**。

---

## 目录
1. [核心架构与运行原理](#1-核心架构与运行原理)
2. [服务器部署前置准备](#2-服务器部署前置准备)
3. [快速部署步骤（Docker Compose 方式）](#3-快速部署步骤docker-compose-方式)
4. [如何从模拟路径切换到真实云盘地址](#4-如何从模拟路径切换到真实云盘地址)
5. [常见关键问题与答疑（FAQ）](#5-常见关键问题与答疑faq)
6. [日常运维与健康监控](#6-日常运维与健康监控)

---

## 1. 核心架构与运行原理

```
[ 公司内网员工浏览器 ]
         │ HTTP: 8080 (或反向代理 Nginx/企业SSO)
         ▼
┌──────────────────────────────────────────────────────────┐
│ Docker 容器 (file-search-app)                           │
│   • 运行用户: appuser (uid: 10001, 只读根文件系统)       │
│   • FastAPI + SQLite FTS5 本地索引 (纯本地磁盘快速检索)    │
│   • Pandas 轻量 BI 内存引擎 (按需读取单个表格)            │
└────────┬────────────────────────────────┬────────────────┘
         │                                │
         │ (只读挂载 :ro)                  │ (读写持久化挂载)
         ▼                                ▼
[ 宿主机挂载的真实企业共享盘 ]        [ 宿主机本地高性能 SSD 磁盘 ]
 路径示例: /mnt/share/office          路径: /srv/file-finder/data
 (来自 172.11.111.11 CIFS/SMB)        (file_index.db 索引数据库)
```

- **索引与文件完全解耦**：所有搜索请求只查宿主机本地的 SQLite 数据库，毫秒级响应，**绝不因为用户搜索而实时扫描或遍历共享网盘**。
- **原始文件只读**：容器内部以 `:ro`（Read-Only）权限挂载共享盘，服务账号没有写权限，BI 分析导出只会生成新文件，绝不破坏原文件。
- **单 Worker 约束**：由于扫描任务锁（`_scan_lock`）和进度（`_scan_state`）运行在应用进程内，容器内强制使用 1 个 Worker，避免多进程重复扫描造成网盘 IO 拥堵。

---

## 2. 服务器部署前置准备

### 2.1 基础软件
- Linux 服务器（推荐 Ubuntu 22.04+ / Debian 12+ / Rocky Linux 9+）
- Docker 24.0+ 与 Docker Compose v2.20+
- `cifs-utils`（如果通过 SMB/CIFS 挂载 Windows 云盘）

### 2.2 在宿主机挂载企业云盘 (以 172.11.111.11 为例)
建议由宿主机操作系统负责共享盘的挂载与断线重连，容器只负责消费宿主机本地目录。

```bash
# 1. 创建本地挂载点
sudo mkdir -p /mnt/share/enterprise_disk

# 2. 创建 CIFS 凭证文件（权限设为 600）
sudo bash -c 'cat <<EOF > /etc/cifs-credentials
username=your_share_user
password=your_share_password
domain=your_domain_if_any
EOF'
sudo chmod 600 /etc/cifs-credentials

# 3. 在 /etc/fstab 中配置开机自动挂载（只读挂载，uid=10001 对应容器内部运行用户）
sudo bash -c 'echo "//172.11.111.11/share /mnt/share/enterprise_disk cifs credentials=/etc/cifs-credentials,ro,uid=10001,gid=10001,iocharset=utf8,nofail 0 0" >> /etc/fstab'

# 4. 执行挂载并验证
sudo mount -a
ls -l /mnt/share/enterprise_disk
```

### 2.3 创建本地索引持久化目录
索引数据库必须保存在宿主机的本地磁盘（推荐 SSD），绝不能写回网盘！

```bash
sudo mkdir -p /srv/file-finder/data
# 将属主赋予容器内 appuser (uid 10001)
sudo chown -R 10001:10001 /srv/file-finder/data
```

---

## 3. 快速部署步骤（Docker Compose 方式）

### 步骤 1：上传工程代码到服务器
将项目目录上传至服务器（如 `/opt/file-search-platform`）：
```bash
cd /opt/file-search-platform
```

### 步骤 2：配置环境变量 `.env`
从 `.env.example` 复制一份 `.env`，并配置关键生产项：
```bash
cp .env.example .env
```

编辑 `.env`：
```dotenv
# ==========================================
# 1. 宿主机上的共享盘路径（模拟数据或真实挂载路径）
# ==========================================
# 开发测试阶段：
# SHARE_HOST_PATH=./网盘模拟数据
# 生产上线阶段（填宿主机挂载的真实共享盘路径）：
SHARE_HOST_PATH=/mnt/share/enterprise_disk

# 容器内固定挂载点（保持 /data/share 即可，无需改动）
CONTAINER_SHARE_PATH=/data/share

# ==========================================
# 2. 网络监听与安全访问
# ==========================================
# 仅本机监听（由上层 Nginx 反向代理接入）：
APP_BIND_ADDR=127.0.0.1
HOST_PORT=8080

# 生产必须设置安全访问令牌（生成方法: openssl rand -hex 32）
APP_ACCESS_TOKEN=9f3b8c42a1e67d5e8f001234abcd5678ef901234567890abcdef1234567890ab

# ==========================================
# 3. 索引与单文件保护
# ==========================================
ALLOWED_EXTENSIONS=.xlsx,.xls,.xlsm,.xlsb,.csv,.tsv,.ods
MAX_ANALYSIS_FILE_MB=256
MAX_ANALYSIS_ROWS=200000
LOG_LEVEL=INFO
TZ=Asia/Shanghai

# 容器资源上限（防止扫描和并发分析耗尽服务器内存）
APP_CPUS=2.0
APP_MEM_LIMIT=3g
```

### 步骤 3：构建并启动服务
执行标准生产启动命令：
```bash
docker compose up -d --build
```

查看服务状态与健康检查：
```bash
docker compose ps
docker compose logs -f app
```

当看到 `Application startup complete.` 且状态显示 `healthy` 即说明服务已正常运行。

---

## 4. 如何从模拟路径切换到真实云盘地址

当你完成了本地模拟测试，需要正式接入 `172.11.111.11` 真实网盘时，请按以下简明流程操作：

### 切换步骤一览表

| 步骤 | 操作内容 | 说明 |
| :--- | :--- | :--- |
| **第 1 步** | 确认宿主机已挂载真实共享盘 | `ls -la /mnt/share/enterprise_disk` 能看到真实目录 |
| **第 2 步** | 修改 `.env` 中的 `SHARE_HOST_PATH` | 将 `./网盘模拟数据` 改为 `/mnt/share/enterprise_disk` |
| **第 3 步** | 重启 Docker 容器以加载新挂载 | 执行 `docker compose up -d` |
| **第 4 步** | **触发全量索引创建** | 打开网页点击【建立 / 更新索引】或调用 API 触发 |

---

## 5. 常见关键问题与答疑（FAQ）

### Q1：切换到真实云盘后，是否需要手动运行额外的 Python 脚本来建索引？
**回答：完全不需要！**
- 仓库中的 `create_mock_files.py`、`fill_gap_files.py` 等脚本**仅用于在没有真实网盘的开发机上生成模拟文件**，生产环境绝不需要运行它们。
- 索引的扫描、抽取元数据、中文分词、SQLite 写入全部由容器内的后端服务（`app.py` 中的 `_scan_worker`）全自动完成。
- 切换目录后，只需在网页管理后台点击一次 **【建立 / 更新索引】** 按钮，或者在服务器执行一条 HTTP 命令即可：
  ```bash
  curl -X POST http://127.0.0.1:8080/api/index/start \
       -H "X-App-Token: 你的APP_ACCESS_TOKEN"
  ```

### Q2：切换云盘地址后，旧的模拟数据索引会不会和新网盘混在一起？
**回答：绝对不会，系统内置了基于 `root_key` 的自动版本代际隔离与清理机制：**
1. 系统为每个根目录生成唯一的 `root_key`（例如 `/data/share`）。
2. 在扫描期间，普通用户搜索**只能看到当前生效根目录**的检索结果。
3. 当针对新网盘的全量扫描**完整无误地结束**后，系统会自动执行清理事务：
   - 清理所有旧目录的残留记录（`DELETE FROM files WHERE root_key <> ?`）；
   - 清理在本次全量扫描中未见到的失效文件（删除/移动的文件）；
   - 清理旧目录关联的已保存分析方案。
4. **Fail-Safe 容错保护**：如果扫描过程中因网络中断或某个子目录权限不足报错，系统会标记为“部分完成（partial）”，**绝不会提前误删旧索引**，保障业务搜索不中断。

### Q3：128GB / 13 万个真实表格，首次全量建索引大概需要多久？
- **实测性能参考**：在本地 13 万个模拟文件的全量扫描中，耗时约 **15 ~ 20 秒**；
- **网络共享盘预估**：在真实的内网千兆网络 CIFS/SMB 协议下，由于存在网络 round-trip 延迟，遍历 13 万个目录节点的 `stat()` 耗时预计在 **3 ~ 8 分钟** 左右；
- **扫描对用户无感**：扫描过程在后台独立线程运行，**期间不影响已经建好的索引查询**，前端会有动态进度条显示当前扫描进度。

### Q4：以后网盘文件增加了、删除了、改名了，怎么更新索引？
- **增量更新**：再次点击【建立 / 更新索引】即可。系统通过对比文件的 `size`、`mtime`（修改时间）与 `last_seen_scan`（扫描批次代号），未变动的文件直接跳过 FTS 重算，因此增量更新非常快。
- **定时调度（可选）**：可以在宿主机上配置 crontab，在每天凌晨或业务低峰期自动触发一次同步：
  ```bash
  # 每天凌晨 2:00 自动触发增量同步
  0 2 * * * curl -s -X POST http://127.0.0.1:8080/api/index/start -H "X-App-Token: 你的APP_ACCESS_TOKEN" > /dev/null
  ```

---

## 6. 日常运维与健康监控

### 6.1 查看索引与服务健康
```bash
# 检查应用配置与共享盘挂载连通性
curl -s http://127.0.0.1:8080/api/config-check

# 检查当前索引库文件数量与大小
curl -s http://127.0.0.1:8080/api/index/status
```

返回示例：
```json
{
  "running": false,
  "phase": "complete",
  "indexed_files": 130436,
  "indexed_size_bytes": 25286,
  "last_scan_at": "2026-10-15T16:47:56",
  "root_configured": true,
  "root_display": "/data/share"
}
```

### 6.2 索引数据库备份与灾备
索引文件统一存放在宿主机 `/srv/file-finder/data/file_index.db`。
- **备份**：该文件支持 SQLite 在线热备：
  ```bash
  sqlite3 /srv/file-finder/data/file_index.db ".backup '/backup/file_index_$(date +%F).db'"
  ```
- **灾难重建**：即便数据库完全损坏或丢失，只需重启容器并触发一次全量建索引，几分钟内即可重新从网盘生成完整索引。
