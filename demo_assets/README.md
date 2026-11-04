# 项目模拟数据

`demo-data.tar.gz` 保存完整的模拟网盘目录，`manifest.json` 提供文件数量、大小和压缩包 SHA-256 校验值。仅包含演示文件，不包含本地环境变量、数据库、会话、预览缓存或密钥。

首次拉取仓库后，在项目根目录执行：

```bash
python scripts/restore_demo_data.py
```

脚本将数据还原到项目根目录的 `网盘模拟数据/`，已有目录不会被覆盖。完整目录由 Git 忽略，避免将约十二万个小文件逐个纳入版本控制；压缩包随仓库提交。

本机 Python 启动时，在 `fast_file_finder/.env` 设置：

```dotenv
FILE_ROOT=../网盘模拟数据
DEMO_MODE=true
```

相对路径以 `fast_file_finder` 应用目录为基准。Docker 则使用根目录 `.env` 中的 `SHARE_HOST_PATH=./网盘模拟数据`。

还原后启动服务，首次点击“建立 / 更新索引”。索引数据库在本机生成，不随仓库分发。启用演示模式后，每天自动补充滚动报表与审批流程案例。
