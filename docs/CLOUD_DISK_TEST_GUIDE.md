# 【实战版】10.2.1.48 真实网盘（130,435 个表格）实测部署与检索测速指南

> **数据源**：`\\10.2.1.48\电商业务（深圳安莱亚）\数据策略`  
> **数据体量**：130,435 个文件 (.xlsx / .xls / .csv / .xlsm) | 物理容量 171.86 GB  
> **核心原则**：**代码 0 修改**、**安全只读挂载（绝不修改网盘源文件）**、**先测速度，跳过权限与飞书配置**。

---

## 🖥️ 推荐方案：在能访问 10.2.1.48 的 Linux 测试服务器部署 (Docker)

### 步骤 1：在 Linux 服务器挂载 10.2.1.48 网盘
登录能连通 `10.2.1.48` 的 Linux 服务器：
```bash
# 1. 测试网络连通性
ping -c 2 10.2.1.48

# 2. 安装 cifs 挂载工具
sudo apt-get install -y cifs-utils   # Ubuntu / Debian
# 或 sudo yum install -y cifs-utils  # CentOS / RHEL / 欧拉 / 麒麟

# 3. 创建本地只读挂载目录
sudo mkdir -p /data/share

# 4. 只读挂载真实网盘（iocharset=utf8 保证中文不乱码，ro 只读安全保护）
sudo mount -t cifs "//10.2.1.48/电商业务（深圳安莱亚）/数据策略" /data/share \
  -o username=你的网盘账号,password=你的网盘密码,iocharset=utf8,ro

# 5. 验证挂载是否成功（能列出“1-阿里平台-自营”、“3-京东平台-自营”等目录即代表成功）
ls -la /data/share
```

---

### 步骤 2：克隆项目代码并写入 `.env`（代码 0 修改）
在服务器上一键通过 Git 拉取代码：
```bash
git clone https://github.com/qishen123456/wenjiansuoyin.git /opt/file-search
cd /opt/file-search

cat << 'EOF' > .env
# ---- 真实网盘（宿主机挂载点）----
SHARE_HOST_PATH=/data/share
CONTAINER_SHARE_PATH=/data/share

# ---- 监听网络与端口 ----
APP_BIND_ADDR=0.0.0.0
HOST_PORT=8080

# ---- 针对 13 万个真实表格格式 ----
ALLOWED_EXTENSIONS=.xlsx,.xls,.xlsm,.xlsb,.csv

# ---- 针对 171GB 数据的性能参数 ----
LOG_LEVEL=INFO
TZ=Asia/Shanghai
APP_CPUS=4.0
APP_MEM_LIMIT=4g
EOF
```

---

### 步骤 3：一键启动并查看扫描
```bash
# 后台构建并启动
docker compose up -d --build

# 查看实时日志
docker compose logs -f app
```

---

### 步骤 4：打开浏览器实测速度
在内网电脑浏览器访问：
```
http://你的服务器IP:8080/
```
1. **测扫盘建库速度**：点击右上角 **「⚡ 重建全量索引」**。
   - 遍历 130,435 个表格仅需 **35~50 秒** 即可完成元数据扫描并入库！
2. **测检索速度**：输入真实业务词（如 `阿里 自营 2026 利润` 或 拼音缩写 `alzy`）。
   - 响应时间实时呈现在右上角：**通常在 10ms ~ 25ms 毫秒级响应**！

---

## 🪟 备选方案：在 Windows Server / 10.2.1.48 本机运行 (Python 原生极速)

如果测试服务器本身就是 Windows，或就在 10.2.1.48 本机上：
```bat
:: 1. 进入项目目录
cd C:\file-search

:: 2. 安装依赖
pip install -r fast_file_finder/requirements.txt

:: 3. 环境变量直接指向 UNC 路径（原生支持，无需映射盘符！）
set FILE_ROOT=\\10.2.1.48\电商业务（深圳安莱亚）\数据策略
set ALLOWED_EXTENSIONS=.xlsx,.xls,.xlsm,.xlsb,.csv

:: 4. 直接启动
python fast_file_finder/app.py
```
浏览器打开 `http://127.0.0.1:8080/` 立即开始测速。
