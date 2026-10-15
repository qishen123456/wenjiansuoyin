# Docker 部署说明

三个文件构成全部部署面：

| 文件 | 作用 |
|---|---|
| `fast_file_finder/Dockerfile` | 多阶段构建：`base`（依赖）→ `runtime`（生产镜像，非 root）/`dev`（含 pytest） |
| `docker-compose.yml` | 开发/单机编排，默认只监听 `127.0.0.1:8080` |
| `docker-compose.prod.yml` | 生产覆盖层，与主文件叠加使用 |

配置分两层，别混：`.env`（compose 变量，宿主机视角的路径/端口/令牌）与 `fast_file_finder/.env`（裸机跑 uvicorn 用的应用配置）。**容器里不要挂 `fast_file_finder/.env`**，一切通过 compose 的 `environment` 注入；`load_dotenv` 默认不覆盖已存在的环境变量，但两套配置并存迟早会互相误导。

## 开发机（macOS）

```bash
cp .env.example .env          # 默认已指向 ./网盘模拟数据
docker compose up -d --build
curl -s localhost:8080/api/health
curl -s -X POST localhost:8080/api/index/start    # 页面里点"更新索引"等价
docker compose --profile test run --rm test       # 容器内跑 pytest
docker compose down                               # 索引卷保留
docker compose down -v                            # 连索引卷一起删
```

实测本机 13 万文件：全量索引 35.5 秒（裸机 20 秒，慢 1.8 倍，Docker Desktop 跨文件系统挂载的固定代价），二次增量 25.4 秒（仍要 stat 全部条目，没有跳过机制），中文关键词检索 3–40ms，精确整名检索中位 194ms。

**生产服务器不受此影响**：Linux 上 CIFS 挂载 + 原生 overlay 没有这层翻译开销。别用本机数字推断生产性能，上线前在授权测试目录重跑。

## 生产服务器

镜像在本机是 `linux/arm64`（Apple Silicon 构建产物），**x86 服务器直接跑会 exec format error**。跨架构推送：

```bash
docker buildx create --use --name multiarch 2>/dev/null || true
docker buildx build --platform linux/amd64 \
  -t registry.intranet/file-finder:1.4.2 \
  --push fast_file_finder
```

服务器侧三步准备（容器不管挂载，必须宿主机先挂好）：

```bash
# 1. 挂共享盘，只读，uid 对齐镜像里的 appuser(10001)
mount -t cifs //fileserver/部门共享 /mnt/netapp/企业网盘 \
  -o ro,uid=10001,gid=10001,file_mode=440,dir_mode=750,vers=3.1.1,...
# 2. 索引库目录
install -d -o 10001 -g 10001 /srv/file-finder/data
# 3. 令牌
openssl rand -hex 32
```

启动（`IMAGE`/`SHARE_HOST_PATH`/`APP_ACCESS_TOKEN`/`CONTAINER_SHARE_PATH` 四个变量缺任一个，compose 会直接报错拒绝启动，这是有意的）：

```bash
IMAGE=registry.intranet/file-finder:1.4.2 \
SHARE_HOST_PATH=/mnt/netapp/企业网盘 \
CONTAINER_SHARE_PATH=/data/share \
APP_ACCESS_TOKEN='<上一步的随机串>' \
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

生产层已强制：`read_only` 根文件系统 + `cap_drop: ALL` + `no-new-privileges` + 只绑 `127.0.0.1` + 网盘 `:ro` + 3g 内存上限 + 日志滚动（20m×5）。对外访问一律经反向代理终结 TLS，SSO 和逐用户文件权限在网关层做——本服务自带的只是共享令牌入口，不是身份认证。

## 升级与回滚

元数据可重建，所以流程很轻：

```bash
docker compose ... pull && docker compose ... up -d      # 升级
docker compose ... exec app python -c "import app;print(app.DB_PATH)"  # 确认库位置
curl -s -X POST localhost:8080/api/index/start           # 新版本上线后重建一次
```

回滚 = 把 `IMAGE` 换回旧 tag 重新 `up -d`。索引库带 schema 版本（`app_meta.schema_version`），跨大版本回滚前先备份卷：`docker run --rm -v file-search_index_db:/from -v $PWD:/to alpine cp -a /from/. /to/backup-$(date +%F)/`。

## 已知约束（改代码前必读）

- **不能加 worker。** `--workers 1` 是硬约束：`_scan_lock`、`_scan_state` 都是进程内对象，多进程会各自全量扫共享盘并并发写同一个 SQLite。要横向扩展就一个 FILE_ROOT 一个容器，前面挂代理。
- **一个容器只服务一个根目录。** 扫描成功阶段会执行 `DELETE ... WHERE root_key <> ?`（`app.py:385`）清掉其他根目录的全部索引行。改 `FILE_ROOT` 等于换口径 = 下次扫描全量重建；这也是开发与生产不要共用同一个索引卷的原因。
- **`/api/analysis/*` 尚未注册。** `app.py` 只暴露 6 个路由，`tests/test_bi_workflow.py` 里 9 个用例因 404 失败，前端 BI 工作区不可用。这是待补功能，不是部署配置问题。
- **索引库体积大。** 13 万条目 ≈ 182MB，按 1.4KB/条目估算容量；百万级文件要预留 GB 级磁盘和相应的扫描时间。
