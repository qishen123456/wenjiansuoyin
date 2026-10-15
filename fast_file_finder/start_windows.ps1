$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '已创建 .env。请先编辑 FILE_ROOT，填入 demo_data 或实际共享目录。' -ForegroundColor Yellow
}

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host '创建 Python 虚拟环境…'
    python -m venv .venv
}

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Write-Host '检查 / 安装依赖（首次运行需要联网或配置公司 Python 镜像）…' -ForegroundColor Cyan
& $py -m pip install -r requirements.txt
Write-Host '启动服务：http://127.0.0.1:8080' -ForegroundColor Green
& $py -m uvicorn app:app --host 0.0.0.0 --port 8080