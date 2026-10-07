<#
音璃 MusicGlass —— 打包脚本（阶段5）

用法（在项目根目录或任意位置都行，脚本自己会定位项目根）：
    .\packaging\build.ps1               # 默认：跑测试 -> 生成图标 -> 打**文件夹版**（推荐）
    .\packaging\build.ps1 -Onefile       # 打成单个 exe（方便分发，但启动慢、且极易被杀软误报）
    .\packaging\build.ps1 -Console       # 带控制台（排查“打包后才出现”的问题）
    .\packaging\build.ps1 -SkipTests     # 跳过 pytest（赶时间时用）
    .\packaging\build.ps1 -Clean         # 先清掉上次的 build/dist

为什么默认是文件夹版（-Onedir）而不是单文件：
    * 单文件 exe 的原理是“自解压到 %TEMP% 再跑”，这个行为正是杀软启发式
      最讨厌的东西 —— 卡巴斯基实测直接把 dist\MusicGlass.exe 删了（误报）；
    * 文件夹版没有解压动作，启动快很多（实测单文件约 4 秒，文件夹版约 1 秒）。
    代价：分发时是一个文件夹（不能只拷一个文件）。

产物：
    dist\MusicGlass\MusicGlass.exe     文件夹版（默认）
    dist\MusicGlass.exe                 单文件版（-Onefile）
#>
[CmdletBinding()]
param(
    [switch]$Onefile,
    [switch]$Console,
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pkg = $PSScriptRoot
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
$pyinstaller = Join-Path $root '.venv\Scripts\pyinstaller.exe'
$distPath = Join-Path $root 'dist'
$workPath = Join-Path $root 'build\pyinstaller'

if (-not (Test-Path $python)) { throw "找不到 venv 解释器：$python" }
if (-not (Test-Path $pyinstaller)) {
    throw "没装 PyInstaller。先执行：`n  & `"$python`" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller"
}

Write-Host "=== 音璃 MusicGlass 打包 ===" -ForegroundColor Cyan
Write-Host "项目根：$root"

# ---- 1. 自检：测试不过就别打包 ----
Write-Host "`n[1/4] 自检：pytest" -ForegroundColor Cyan
if ($SkipTests) {
    Write-Host "  已跳过（-SkipTests）"
} else {
    & $python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw '测试没通过，先修了再打包' }
}

# ---- 2. 图标（和托盘图标共用同一份绘制代码，保证长得一样）----
Write-Host "`n[2/4] 生成图标 packaging\musicglass.ico" -ForegroundColor Cyan
& $python (Join-Path $pkg 'make_icon.py')

# ---- 3. 清理 ----
if ($Clean) {
    Write-Host "`n[3/4] 清理旧的 build\pyinstaller 与 dist" -ForegroundColor Cyan
    foreach ($path in @($workPath, (Join-Path $root 'dist'))) {
        if (Test-Path $path) { Remove-Item $path -Recurse -Force }
    }
} else {
    Write-Host "`n[3/4] 保留上次的中间产物（要清干净用 -Clean）"
}

# ---- 4. PyInstaller ----
Write-Host "`n[4/4] PyInstaller（$($(if ($Onefile) { '单文件' } else { '文件夹版' }))，控制台=$Console）" -ForegroundColor Cyan
$env:MG_ONEFILE = if ($Onefile) { '1' } else { '0' }
$env:MG_CONSOLE = if ($Console) { '1' } else { '0' }

& $pyinstaller (Join-Path $pkg 'musicglass.spec') --noconfirm `
    --distpath $distPath --workpath $workPath
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 打包失败' }

$target = if ($Onefile) { Join-Path $distPath 'MusicGlass.exe' }
          else { Join-Path $distPath 'MusicGlass\MusicGlass.exe' }
if (-not (Test-Path $target)) {
    Write-Host "`n[!] 没找到产物：$target" -ForegroundColor Red
    Write-Host "    如果刚打包完就消失了，多半是杀软把它删了/隔离了 —— 见 README「杀软误报怎么办」" -ForegroundColor Yellow
    throw '产物不见了'
}
$sizeMb = [math]::Round((Get-Item $target).Length / 1MB, 1)
$dirMb = if ($Onefile) { $sizeMb }
         else { [math]::Round(((Get-ChildItem (Split-Path $target) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1) }

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host "产物：$target"
Write-Host "      exe 本体 $sizeMb MB，整个文件夹 $dirMb MB"
Write-Host @"

先别急着双击，按顺序验一遍：
  1) 跑起来看一眼（不写配置、自动退出）：
     & "$target" --source mock --on-top --no-tray --screenshot build\exe_shot.png --exit-after 8
  2) 验真实数据源（开着 QQ音乐）：
     & "$target" --debug --exit-after 8
     （无控制台版本看不到 --debug 输出，想看就重新用 -Console 打一份）
  3) 开机自启：先把这个文件夹放到固定位置，再在托盘右键 -> 取消“开机自启”再勾上；
     注册表 HKCU\Software\Microsoft\Windows\CurrentVersion\Run 里的 MusicGlass 会改成 exe 路径。
"@
