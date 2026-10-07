# 音璃 MusicGlass —— 开发模式启动脚本（带控制台，方便看日志和报错）
#
# 用法：
#   .\run.ps1                    普通启动
#   .\run.ps1 --debug            启动并打印后端/窗口状态
#   .\run.ps1 --backend qt       换用 Qt 模糊后端
#   .\run.ps1 --scale 1.5 --pos 100,300
#
# 想"静默启动"（完全看不到控制台窗口）请用 README 里那条 pythonw 命令。

param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $AppArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "找不到虚拟环境：$python" -ForegroundColor Red
    Write-Host "请先执行：" -ForegroundColor Yellow
    Write-Host "    python -m venv .venv"
    Write-Host "    .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    exit 1
}

$env:PYTHONIOENCODING = "utf-8"   # 让中文日志在控制台里正常显示
& $python (Join-Path $root "main.pyw") @AppArgs
exit $LASTEXITCODE
