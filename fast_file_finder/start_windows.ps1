$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '已创建 .env。请先编辑 FILE_ROOT，填入 demo_data 或实际共享目录，然后重新运行。' -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host '创建 Python 虚拟环境…'
    python -m venv .venv
}

$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
Write-Host '检查 / 安装依赖（首次运行需要联网或配置公司 Python 镜像）…' -ForegroundColor Cyan
& $py -m pip install -r requirements.txt
$bindAddress = if ($env:APP_HOST) { $env:APP_HOST } else { '127.0.0.1' }
if ($bindAddress -notin @('127.0.0.1', 'localhost', '::1')) {
    $tokenState = & $py -c "from dotenv import dotenv_values; print('1' if (dotenv_values('.env').get('APP_ACCESS_TOKEN') or '').strip() else '0')"
    if ($LASTEXITCODE -ne 0 -or $tokenState.Trim() -ne '1') {
        throw "拒绝启动：当前 APP_HOST=$bindAddress 会对其他机器开放，但 .env 未配置非空 APP_ACCESS_TOKEN。请先设置随机长 ASCII 令牌；共享令牌仍不能替代企业 SSO/文件 ACL。"
    }
}
Write-Host "启动服务：http://$bindAddress`:8080" -ForegroundColor Green
& $py -m uvicorn app:app --host $bindAddress --port 8080