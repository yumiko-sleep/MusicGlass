# -*- coding: utf-8 -*-
"""开机自启（写 HKCU 注册表 Run 键）。

原理：往
    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
写一个值，Windows 登录后就会执行这个值的命令行。
这是**用户级**设置，不需要管理员权限，也不会影响别的用户。

为什么用 HKCU 的 Run 而不是"启动文件夹"或任务计划：
    * 启动文件夹要往用户的 Startup 目录丢 .lnk 文件（还要调 COM 去建快捷方式）；
    * 任务计划程序需要额外的 XML/COM，且更适合"定时"而不是"登录即启"；
    * Run 键就是为这个场景设计的：一条命令，可增可删可查，最干净。

两种运行形态：
    * 打包成 exe 之后：注册的是 exe 自己的路径；
    * 从源码运行（现在）：注册 pythonw.exe + main.pyw 的绝对路径。
      pythonw.exe 没有控制台窗口，所以开机启动时不会闪黑框。

命令行里的**引号很重要**：路径带空格时不引起来，Windows 会把它拆成两段，直接启动失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import winreg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 非 Windows
    winreg = None  # type: ignore[assignment]

# HKCU 下的自启键（不要改，这是系统约定）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 值的名字：任务管理器的"启动"页里显示的就是它
VALUE_NAME = "MusicGlass"


def is_supported() -> bool:
    return winreg is not None


def project_root() -> Path:
    """项目根目录（这个文件在 musicglass/platform/ 下，往上两级）。"""
    return Path(__file__).resolve().parents[2]


def launch_command(executable: str | None = None, script: str | None = None) -> str:
    """拼出要写进注册表的命令行。

    打包后（sys.frozen）直接用 exe 自己；从源码运行则用 pythonw.exe 跑 main.pyw。
    纯计算，方便单测。
    """
    if getattr(sys, "frozen", False):
        target = executable or sys.executable
        return f'"{target}" --silent'

    python = executable
    if python is None:
        # 优先用同目录下的 pythonw.exe：它没有控制台窗口，开机启动不会闪黑框
        candidate = Path(sys.executable).with_name("pythonw.exe")
        python = str(candidate) if candidate.exists() else sys.executable
    entry = script or str((project_root() / "main.pyw").resolve())
    # 工作目录可能不是项目目录，所以 main.pyw 用绝对路径
    return f'"{python}" "{entry}" --silent'


def is_enabled() -> bool:
    """当前有没有开机自启。读不到（异常）一律当"没有"，不让 UI 崩。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def current_value() -> str:
    """读出注册表里实际存的命令行（调试用，能看到路径对不对）。"""
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return ""


def enable() -> bool:
    """开启自启。返回是否成功（失败时 UI 要提示，而不是默默取消勾选）。"""
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            # REG_SZ：普通字符串。别用 REG_EXPAND_SZ，那要求命令里不能有 % 之类
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
        return True
    except OSError:
        return False


def disable() -> bool:
    """关闭自启（删掉那个值）。值本来就不存在也算成功。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True          # 本来就没有 -> 目标状态已达成
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    return enable() if enabled else disable()


def status_text() -> str:
    """给 --debug / 托盘菜单用的一行描述。"""
    if not is_supported():
        return "不支持（非 Windows）"
    if not is_enabled():
        return "未开启"
    return f"已开启 → {current_value()}"
