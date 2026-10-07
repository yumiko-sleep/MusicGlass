# -*- mode: python ; coding: utf-8 -*-
"""音璃 MusicGlass —— PyInstaller 打包配置（阶段5）。

怎么用（不要直接 `pyinstaller main.pyw`，用这个 spec 或 packaging/build.ps1）：

    .\\.venv\\Scripts\\pyinstaller.exe packaging\\musicglass.spec --noconfirm
    .\\packaging\\build.ps1              # 推荐的入口：会先跑测试、再生成图标、再打包

为什么用 spec 文件而不是一长串命令行参数：
1. 这种"Qt + 原生扩展 + WinRT"的项目，隐藏导入/排除项会越攒越多，写在文件里
   才能一条条注释清楚原因；
2. 单文件/单目录、图标、版本信息只改一个地方。

产物：
    dist\\MusicGlass.exe             单文件版（MG_ONEFILE=1；启动慢、易被杀软误报）
    dist\\MusicGlass\\MusicGlass.exe  文件夹版（默认，推荐）

为什么默认文件夹版（onedir）：单文件 exe 的运行方式是“自己解压到 %TEMP% 再启动”，
这个行为本身就很像打包型恶意软件，杀软启发式很容易直接删掉它 —— 实测卡巴斯基
21.26 就把单文件版删了（误报）。文件夹版没有解压动作，启动也快得多（4 秒 -> 1 秒）。

环境变量：
    MG_ONEFILE=1  打成单个 exe（默认是文件夹版）
    MG_CONSOLE=1  带控制台窗口（排查打包后才出现的问题时非常有用：
                  无控制台时 print 会被丢掉，看不到任何日志）
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# SPECPATH 是 PyInstaller 注入的变量 = 本 spec 所在目录（packaging/）
SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent

ONEFILE = os.environ.get("MG_ONEFILE", "0") == "1"
CONSOLE = os.environ.get("MG_CONSOLE", "0") == "1"
ICON = SPEC_DIR / "musicglass.ico"
VERSION_FILE = SPEC_DIR / "version_info.txt"

# ---------------------------------------------------------------------------
# 隐藏导入：动态导入的东西静态分析看不见，漏了就是"打包成功、一运行 ModuleNotFoundError"
# ---------------------------------------------------------------------------
hiddenimports = [
    # pyglass 的抓屏实现是在函数里延迟导入的（放大镜 / GDI 两条路都在这）
    "pyglass._magnifier",
    # pywinrt：winrt 是 PEP420 命名空间包（没有 __init__.py），
    # 生成出来的 winrt.windows.* 子包得显式收进来
    *collect_submodules("winrt"),
]

# WinRT 的原生扩展（_winrt*.pyd）和它依赖的 msvcp140.dll
binaries = collect_dynamic_libs("winrt")

# ---------------------------------------------------------------------------
# 排除：装不进去的白送体积，以及用不到的 Qt 模块
# ---------------------------------------------------------------------------
excludes = [
    # 开发期才用到的（渲染对比图用 PIL；pytest 相关）
    "PIL", "pytest", "_pytest", "pyinstaller",
    # 标准库里的大件
    "tkinter", "unittest", "doctest", "pydoc", "distutils", "setuptools",
    "test", "lib2to3", "xmlrpc", "sqlite3",
    # numpy 用不到的东西（省一大截）
    "numpy.f2py", "numpy.testing", "numpy.distutils",
    # PyQt6 里我们一个都没用的模块（Qt 的 DLL 很大，排掉最明显）
    "PyQt6.QtNetwork", "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuick3D",
    "PyQt6.QtSql", "PyQt6.QtTest", "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets",
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebChannel",
    "PyQt6.QtWebSockets", "PyQt6.QtBluetooth", "PyQt6.QtNfc", "PyQt6.QtDesigner",
    "PyQt6.QtHelp", "PyQt6.QtOpenGL", "PyQt6.QtOpenGLWidgets", "PyQt6.QtPdf",
    "PyQt6.QtPdfWidgets", "PyQt6.QtPositioning", "PyQt6.QtSensors",
    "PyQt6.QtSerialPort", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets", "PyQt6.QtXml",
    "PyQt6.QtDBus", "PyQt6.Qt3DCore", "PyQt6.Qt3DRender",
]

a = Analysis(
    [str(ROOT / "main.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=[],                      # 项目里没有任何图片/资源素材（图标都是代码画的）
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="MusicGlass",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,                 # 不用 UPX：压缩过的 exe 容易被杀软误报
        runtime_tmpdir=None,       # 默认解压到 %TEMP%
        console=CONSOLE,           # 桌面组件：默认不留控制台窗口
        disable_windowed_traceback=False,
        icon=str(ICON) if ICON.exists() else None,
        version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="MusicGlass",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=CONSOLE,
        disable_windowed_traceback=False,
        icon=str(ICON) if ICON.exists() else None,
        version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="MusicGlass",
    )
