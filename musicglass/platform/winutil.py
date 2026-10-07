# -*- coding: utf-8 -*-
"""Windows 原生小工具（全部用 ctypes，不依赖 pywin32）。

集中放"必须碰 Win32 的小代码"，方便以后审查和替换。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
# SetWindowDisplayAffinity 的两种模式
WDA_NONE = 0x00000000                 # 正常，可被录屏/截图
WDA_MONITOR = 0x00000001              # 老模式：录屏里是黑块
WDA_EXCLUDEFROMCAPTURE = 0x00000011   # Win10 2004+：抓屏时"看不见"这个窗口

_user32 = ctypes.windll.user32  # type: ignore[attr-defined]
_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def system_idle_seconds() -> float:
    """系统有多久没收到用户输入（键盘/鼠标）了，单位秒。

    为什么要它（实测结论）：屏幕抓取有个 ~21ms 的**固定延迟**，跟抓多少像素无关；
    所以降低 CPU 的唯一有效手段是"少抓"。而"用户多久没动"是一个几乎零成本、
    又非常准的信号：用户不动的时候，桌面基本上不会变，不需要高频抓。
    """
    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # dwTime 是 32 位 tick，49 天会回绕；max(0) 兼容回绕后的负值
        return max(0.0, (_kernel32.GetTickCount() - info.dwTime) / 1000.0)
    except Exception:
        return 0.0


def set_capture_visible(hwnd: int, visible: bool) -> bool:
    """控制"本窗口是否出现在屏幕抓取里"。

    visible=True  -> WDA_NONE，用户截图/录屏能看到组件（推荐，默认行为）
    visible=False -> WDA_EXCLUDEFROMCAPTURE，抓屏里看不到它（避免玻璃抓到自己）

    返回是否设置成功。老系统（Win10 2004 以前）会失败，调用方要能容忍失败。
    """
    if not hwnd:
        return False
    affinity = WDA_NONE if visible else WDA_EXCLUDEFROMCAPTURE
    try:
        _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        return bool(_user32.SetWindowDisplayAffinity(wintypes.HWND(int(hwnd)), affinity))
    except Exception:
        return False
