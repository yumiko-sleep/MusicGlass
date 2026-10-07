# -*- coding: utf-8 -*-
"""把窗口"贴"到桌面层——像壁纸的一部分，而不是浮在所有窗口之上。

为什么需要这么绕：
    Windows 没有提供"桌面小组件"这种窗口类型。
    "永远显示在桌面上的程序"（桌面宠物、Rainmeter、Wallpaper Engine 的小部件）
    用的都是同一个技巧：把窗口 **SetParent 到桌面窗口（Progman / WorkerW）**，
    让它变成桌面的一部分。这样：
      * 别的程序盖住桌面时，它自然被盖住；
      * Win+D / 显示桌面 时，桌面被展示出来，它跟着一起出现；
      * 它不在任务栏、不在 Alt+Tab 里。

层级关系（从下到上）：
    壁纸(Progman) → WorkerW（图标背后那层，我们要贴这里） → 桌面图标(SHELLDLL_DefView) → 普通程序窗口

关于 WorkerW：
    Win8 以后桌面壁纸被挪到一个叫 WorkerW 的窗口里。为了让桌面图标还能显示，
    系统会再创建一层 WorkerW。我们要找的是 **紧跟在 SHELLDLL_DefView 后面
    的那一个 WorkerW**（图标层的兄弟），贴上去后组件就落在"图标下面、壁纸上面"。
    如果系统没有这一层（比如换了主题），就退回到直接贴 Progman。

所有 Win32 函数都显式声明了参数/返回类型：
不声明的话，64 位下句柄会被当成 32 位 int 处理而截断，函数静默失败、返回 0。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 原型声明
# ---------------------------------------------------------------------------
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND

_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = wintypes.HWND

_user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
_user32.SetParent.restype = wintypes.HWND

_user32.GetParent.argtypes = [wintypes.HWND]
_user32.GetParent.restype = wintypes.HWND

_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int

_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND

_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL

_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL

_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND

_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long

_user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long

_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL

_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND

_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

# ---- kernel32：按进程认外壳窗口要用（见 process_image_name）----
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_user32.WindowFromPoint.argtypes = [wintypes.POINT]
_user32.WindowFromPoint.restype = wintypes.HWND

_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL

_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL

# ---- 常量 ----
_SMTO_NORMAL = 0x0000
_WM_SPAWN_WORKERW = 0x052C      # 未公开消息：让系统把壁纸/图标分成两层
_HWND_BOTTOM = 1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SWP_NOZORDER = 0x0004

_GA_PARENT = 1
_GA_ROOT = 2
_GW_HWNDNEXT = 2
_GW_OWNER = 4
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CHILD = 0x40000000
_WS_POPUP = 0x80000000
_WS_EX_NOACTIVATE = 0x08000000
_SWP_FRAMECHANGED = 0x0020
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2

SHELL_CLASSES = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
# 属于"桌面层"的窗口类：Progman = 桌面，WorkerW = 壁纸/图标所在的层
DESKTOP_CLASSES = ("Progman", "WorkerW")
# 桌面层归 explorer.exe 所有；别的进程也可能造 WorkerW（第三方桌面组件），
# 拿它们的窗口当 SetWindowPos 的参照会被系统拒绝（实测错误码 5）。
SHELL_PROCESS_NAME = "explorer.exe"

# 外壳窗口分两级（见 shell_surface_kind）：
#   persistent = 常驻表面：桌面、任务栏本体 —— 它们永远在那儿，**遮不住组件**；
#   transient  = 外壳弹出的浮窗：预览、托盘溢出、操作中心…… —— 它们**确实会压住组件**，
#                所以对"抓屏"要算遮挡（否则玻璃里映的是别人的窗口），
#                但对"动画"不该算遮挡（否则鼠标一划过任务栏、点一下托盘动画就停）。
#
# 任务栏/外壳弹出的**独立顶层窗口**：它们的根窗口就是自己，
# 光靠"根窗口类名"认不出来，必须显式列出来。
# （任务栏的**子窗口**不用列：TrayNotifyWnd、TrayClockWClass、TrayButton 这些
#   的根窗口就是 Shell_TrayWnd，走根窗口回溯就能归到 persistent。）
SHELL_POPUP_CLASSES = (
    "TaskListThumbnailWnd",                    # 悬停任务栏按钮弹出的窗口预览
    "NotifyIconOverflowWindow",                # 托盘"显示隐藏的图标"浮窗
    "TopLevelWindowForOverflowXamlIsland",     # Win11 托盘溢出
    "XamlExplorerHostIslandWindow",            # Win11 任务栏浮窗宿主（快速设置/通知中心）
    "tooltips_class32",                        # 气泡提示（任务栏那几个的属主就是 Shell_TrayWnd）
    "Shell_InputSwitchTopLevelWindow",         # 输入法切换浮窗
    "TaskSwitcherWnd",                         # Alt+Tab
    "MultitaskingViewFrame",                   # 任务视图
)

# 只能**按进程**认的外壳窗口：Windows.UI.Core.CoreWindow 这个类名普通 UWP 应用也在用
# （实测本机就有一个整屏的 UWP CoreWindow），按类名放行会把"UWP 盖住组件"也当成不算遮挡。
SHELL_UX_CLASSES = ("Windows.UI.Core.CoreWindow",)
SHELL_PROCESS_NAMES = (
    "explorer.exe",
    "ShellExperienceHost.exe",        # 操作中心 / 各种任务栏浮窗
    "StartMenuExperienceHost.exe",    # 开始菜单
    "SearchHost.exe",                 # Win11 搜索
    "TextInputHost.exe",              # Win11 输入法候选窗
)

# shell_surface_kind 的返回值
SHELL_PERSISTENT = "persistent"
SHELL_TRANSIENT = "transient"




def class_name(hwnd: int) -> str:
    """取窗口类名（用来辨认 Progman / WorkerW / 其他程序）。"""
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _spawn_workerw(progman: int) -> None:
    """给 Progman 发那条"秘密消息"，触发系统生成承载图标的 WorkerW 层。

    0x052C 是未公开消息，好几个参数组合都试过才稳定（0xD/0x1、0xD/0x0、0/0）。
    发完消息后桌面结构才会变成 Progman + WorkerW 两层，我们才有得贴。
    """
    result = ctypes.c_size_t()
    for wparam, lparam in ((0x0D, 0x01), (0x0D, 0x00), (0x00, 0x00)):
        _user32.SendMessageTimeoutW(
            progman, _WM_SPAWN_WORKERW, wparam, lparam, _SMTO_NORMAL, 1000,
            ctypes.byref(result),
        )


def candidates() -> dict[str, int]:
    """列出所有可能可以贴的"桌面层"窗口。

    不同 Windows 版本/主题下这四个的可用性和可见效果都不一样，
    所以都列出来，按实际效果选。
    """
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    progman = int(_user32.FindWindowW("Progman", None) or 0)
    icons_host = 0      # 装着桌面图标(SHELLDLL_DefView)的那个窗口
    after_icons = 0     # 它的下一个兄弟 WorkerW（经典做法贴这个）
    empty_workerw = 0   # 任意一个不含图标层的顶层 WorkerW

    def callback(hwnd, _lparam):
        nonlocal icons_host, after_icons, empty_workerw
        if _user32.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
            icons_host = int(hwnd)
            sibling = _user32.FindWindowExW(None, hwnd, "WorkerW", None)
            if sibling:
                after_icons = int(sibling)
        elif class_name(int(hwnd)) == "WorkerW" and not empty_workerw:
            empty_workerw = int(hwnd)
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return {
        "after_icons": after_icons,
        "empty_workerw": empty_workerw,
        "icons_host": icons_host,
        "progman": progman,
    }


def find_desktop_window(prefer: str = "") -> int:
    """找到应该贴上去的桌面窗口句柄；找不到返回 0。

    prefer 可以指定优先用哪个候选（调试用）。
    """
    progman = int(_user32.FindWindowW("Progman", None) or 0)
    if not progman:
        return 0
    # 发消息让系统把壁纸和图标分成两层，这样才有得贴
    _spawn_workerw(progman)

    found = candidates()
    if prefer and found.get(prefer):
        return found[prefer]
    for name in ("after_icons", "empty_workerw", "progman"):
        if found.get(name):
            return found[name]
    return progman


def current_parent(hwnd: int) -> int:
    """取窗口的**真实**父窗口。

    用 GetAncestor 而不是 GetParent：GetParent 对"有 owner 的顶层窗口"返回 owner，
    语义不纯；GetAncestor(GA_PARENT) 永远返回真正的父窗口，更适合做核验。
    """
    if not hwnd:
        return 0
    ancestor = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_PARENT)
    if ancestor:
        return int(ancestor)
    parent = _user32.GetParent(wintypes.HWND(hwnd))
    return int(parent) if parent else 0


def is_child_window(hwnd: int) -> bool:
    """窗口是不是子窗口（有 WS_CHILD 样式）。贴桌面成功后应该为 True。"""
    if not hwnd:
        return False
    return bool(_user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE) & _WS_CHILD)


def is_top_level(hwnd: int) -> bool:
    """窗口是不是顶层窗口。贴到桌面后应该变成 False。"""
    if not hwnd:
        return False
    return int(_user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)) == int(hwnd)


def _switch_child_style(hwnd: int, child: bool) -> None:
    """切换 WS_CHILD / WS_POPUP 样式。

    【坑】SetParent 不会自动改这两个样式（MSDN 明确说：为了兼容性它不碰）。
    所以如果只调 SetParent，窗口会变成"被 WorkerW 拥有的弹出窗口"，
    而不是真正的子窗口 —— 它依旧是个顶层窗口，不会被正确裁剪和盖住。
    必须自己把 WS_POPUP 换成 WS_CHILD，并用 SWP_FRAMECHANGED 让样式生效。
    """
    style = _user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE)
    if child:
        style = (style & ~_WS_POPUP) | _WS_CHILD
    else:
        style = (style & ~_WS_CHILD) | _WS_POPUP
    _user32.SetWindowLongW(wintypes.HWND(hwnd), _GWL_STYLE, style)
    _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(0), 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
    )


def attach(hwnd: int, target: int = 0) -> tuple[int, str, str]:
    """把窗口贴到桌面层。

    返回 (父窗口句柄, 父窗口类名, 错误说明)；成功时错误说明为空。

    【踩坑记录】不要用 GetParent 来判定 SetParent 是否成功！
    实测：SetParent 明明成功了，紧接着调 GetParent 却返回 0
    （Qt 在窗口显示流程里会干预原生父子关系），于是把成功误判成失败。
    正确做法是看 GetLastError：SetParent 失败才会设置错误码。
    真正的核验放到隔一会儿后再做（见 verify()）。
    """
    if not hwnd:
        return 0, "", "窗口句柄无效"
    if not target:
        target = find_desktop_window()
    if not target:
        return 0, "", "找不到桌面窗口（Progman）"

    ctypes.set_last_error(0)
    _user32.SetParent(wintypes.HWND(hwnd), wintypes.HWND(target))
    error = ctypes.get_last_error()

    if error:
        return 0, "", f"SetParent 失败（Win32 错误码 {error}）"

    # 变成真正的子窗口（理由见 _switch_child_style 的注释）
    _switch_child_style(hwnd, child=True)

    # 沉到父窗口子级的最底下：这样桌面图标还能显示在我们上面
    _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(_HWND_BOTTOM), 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )
    return int(target), class_name(int(target)), ""


def verify(hwnd: int, expected_parent: int) -> bool:
    """核验贴桌面是否还成立（父子关系在 + 子窗口样式在）。"""
    if not hwnd or not expected_parent:
        return False
    return current_parent(hwnd) == int(expected_parent) and is_child_window(hwnd)


def detach(hwnd: int) -> bool:
    """把窗口从桌面层摘下来，恢复成普通顶层窗口（用于"置顶"模式）。

    注意：除了 SetParent(0)，还要把样式从 WS_CHILD 换回 WS_POPUP，
    否则窗口会是一个"没有父窗口的子窗口"，行为莫名其妙。
    """
    if not hwnd:
        return False
    _user32.SetParent(wintypes.HWND(hwnd), wintypes.HWND(0))
    _switch_child_style(hwnd, child=False)
    return True


def window_below(hwnd: int) -> int:
    """Z 序里紧跟在 hwnd 下面（再往下）的那个窗口。"""
    if not hwnd:
        return 0
    below = _user32.GetWindow(wintypes.HWND(hwnd), _GW_HWNDNEXT)
    return int(below) if below else 0


def desktop_layer_candidates() -> list[int]:
    """按 Z 序（顶 -> 底）列出**可见的**桌面层窗口（Progman / WorkerW）。

    为什么要列出全部而不是只取第一个：别的进程也会造 WorkerW（第三方桌面组件），
    拿它们的窗口当 SetWindowPos 的参照会被系统拒绝（实测错误码 5），
    所以要多准备几个候选，一个不行换下一个（见 insert_above_desktop）。
    """
    found: list[int] = []

    def callback(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        if class_name(int(hwnd)) in DESKTOP_CLASSES:
            found.append(int(hwnd))
        return True

    _user32.EnumWindows(_WNDENUMPROC(callback), 0)
    return found


def find_top_desktop_layer() -> int:
    """桌面层里**最上面**的那一个窗口（图标层），Z 序插入的参照物。

    EnumWindows 是从 Z 序顶向底枚举的，所以候选里第一个就是它。
    优先返回 **explorer.exe 自己的**窗口：别的进程的 WorkerW 当参照会被拒绝。
    """
    candidates = desktop_layer_candidates()
    if not candidates:
        return 0
    for handle in candidates:
        if process_image_name(handle) == SHELL_PROCESS_NAME:
            return handle
    return candidates[0]


def _try_insert_above(hwnd: int, target: int) -> int:
    """把 hwnd 插到 target 之上，返回 Win32 错误码（0 = 成功）。

    单独抽出来是为了能单测"换候选重试"的逻辑，不必真的调 SetWindowPos。
    """
    ctypes.set_last_error(0)
    # hWndInsertAfter = target：本窗口会被放到 target 之上
    _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(target), 0, 0, 0, 0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )
    return ctypes.get_last_error()


def insert_above_desktop(hwnd: int, target: int = 0) -> tuple[int, str, str]:
    """把窗口插到"桌面层之上、所有普通窗口之下"（不 SetParent）。

    这就是它“只贴在桌面上”的实现：
      * 压在壁纸和桌面图标之上 → 桌面看得见它；
      * 低于所有普通窗口 → 微信/浏览器一开就把它盖住；
      * 仍然是普通顶层窗口 → Qt 的透明渲染、鼠标交互全部正常。

    参照窗口按"explorer.exe 优先"排序后**逐个试**：某个候选被系统拒绝
    （实测第三方 WorkerW 会返回错误码 5）就换下一个，而不是直接放弃贴桌面。

    返回 (参照窗口句柄, 其类名, 错误说明)。
    """
    if not hwnd:
        return 0, "", "窗口句柄无效"
    if target:
        tried = [int(target)]
    else:
        tried = desktop_layer_candidates()
        # 稳定排序：explorer.exe 自己的排前面（组内保持 EnumWindows 的 Z 序）
        tried.sort(key=lambda h: 0 if process_image_name(h) == SHELL_PROCESS_NAME else 1)
    if not tried:
        return 0, "", "找不到桌面层窗口（Progman/WorkerW）"

    last_error = ""
    for candidate in tried:
        error = _try_insert_above(hwnd, candidate)
        if not error:
            return int(candidate), class_name(int(candidate)), ""
        last_error = f"SetWindowPos 失败（Win32 错误码 {error}）"
    return 0, "", last_error

def is_above_desktop_layer(hwnd: int) -> bool:
    """当前窗口是不是正好压在桌面层的最上面（Z 序合法）。"""
    if not hwnd:
        return False
    below = window_below(hwnd)
    return bool(below) and class_name(below) in DESKTOP_CLASSES


def set_no_activate(hwnd: int, on: bool = True) -> None:
    """给窗口加/去 WS_EX_NOACTIVATE。

    为什么必须要它：普通窗口被点击时会「激活」并被提到同类窗口的最前面，
    那就会出现"我点一下组件，它又跑到微信上面去了"。
    加上这个扩展样式后，点它不会把它抬起来（但鼠标事件照常收到）。
    副作用：它不会获得键盘焦点，所以那种模式下快捷键用不了，改用右键菜单。
    """
    if not hwnd:
        return
    style = _user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE)
    new_style = (style | _WS_EX_NOACTIVATE) if on else (style & ~_WS_EX_NOACTIVATE)
    if new_style != style:
        _user32.SetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE, new_style)


def set_topmost(hwnd: int, on: bool = True) -> None:
    """置顶 / 取消置顶。"""
    if not hwnd:
        return
    _user32.SetWindowPos(
        wintypes.HWND(hwnd), wintypes.HWND(_HWND_TOPMOST if on else _HWND_NOTOPMOST),
        0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )


def window_at(x: int, y: int) -> int:
    """屏幕坐标 (x, y) 上"用户实际能看到/点到"的最上层窗口。

    这是判断"组件到底露出来了没有"最可靠的判据：
    不靠截图（截图会被 SetWindowDisplayAffinity / 放大镜干扰），
    而是直接问系统这个像素点归属谁。
    WindowFromPoint 会自动跳过设置了 WS_EX_TRANSPARENT 的窗口，
    所以返回的就是用户眼睛里看到的那一层。
    """
    hwnd = _user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return int(hwnd) if hwnd else 0


def shell_root_class(hwnd: int) -> str:
    """窗口所属**顶层（根）窗口**的类名；句柄无效时返回 ""。

    为什么必须往上找根窗口：WindowFromPoint 返回的常常是子窗口句柄 ——
    任务栏上有开始按钮、时间区、托盘区等子窗口（TrayNotifyWnd、
    MSTaskSwWClass、Start……），桌面图标区返回的也是 SysListView32。
    只看句柄自己的类名认不出"这是系统外壳窗口"，必须看根窗口。
    """
    if not hwnd:
        return ""
    root = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    return class_name(int(root)) if root else ""


def process_image_name(hwnd: int) -> str:
    """窗口所属进程的映像名（如 "explorer.exe"）；取不到返回 ""。

    为什么需要它：Windows.UI.Core.CoreWindow 这个类名**外壳和普通 UWP 应用都在用**
    （实测本机就有一个整屏的 UWP CoreWindow），只按类名放行会误伤。
    用 PROCESS_QUERY_LIMITED_INFORMATION 就够（不需要更高权限，同用户进程都能开）。
    """
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value.rsplit("\\", 1)[-1]
    finally:
        _kernel32.CloseHandle(handle)


def _shell_chain(hwnd: int) -> list[int]:
    """认"是不是外壳窗口"时要看的一串句柄：自己 → 根窗口 → 属主链。

    为什么要三个都看：
      * **自己**：TaskListThumbnailWnd 这类顶层浮窗（根窗口就是它自己）；
      * **根窗口**：任务栏的子窗口（WindowFromPoint 返回 TrayClockWClass，
        根窗口才是 Shell_TrayWnd），桌面图标区返回的 SysListView32 同理；
      * **属主链**：一堆外壳浮窗是"被任务栏拥有"的工具窗（实测 tooltips_class32
        的属主就是 Shell_TrayWnd），它们既不是子窗口、根窗口也不是外壳。
    属主链带环保护，最多走 8 层。
    """
    chain: list[int] = []
    if not hwnd:
        return chain
    chain.append(int(hwnd))
    root = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    if root and int(root) not in chain:
        chain.append(int(root))
    owner = int(_user32.GetWindow(wintypes.HWND(hwnd), _GW_OWNER) or 0)
    guard = 0
    while owner and owner not in chain and guard < 8:
        chain.append(owner)
        owner = int(_user32.GetWindow(wintypes.HWND(owner), _GW_OWNER) or 0)
        guard += 1
    return chain


def shell_surface_kind(hwnd: int) -> str:
    """窗口属于哪种外壳表面："" / "persistent" / "transient"。

    * persistent（常驻表面）：桌面 Progman/WorkerW、任务栏本体 Shell_TrayWnd，
      以及**它们的子窗口**（TrayClockWClass、TrayButton、SysListView32…）。
      这些东西永远在屏幕上，遮不住组件 —— 动画和抓屏都不该把它们算成遮挡。
    * transient（外壳浮窗）：任务栏按钮的窗口预览、托盘溢出浮窗、操作中心、
      Alt+Tab、任务视图、气泡提示…… 它们**确实会压住组件**：
      对抓屏要算遮挡（不然玻璃里映的是别人的窗口），
      对动画不该算（不然鼠标一划过任务栏、点一下托盘动画就停）。

    判据（自己 / 根窗口 / 属主链）：
      1. 自己或**根窗口**是 SHELL_CLASSES -> persistent
         （用根窗口判"是不是常驻"是关键：任务栏的子窗口根就是 Shell_TrayWnd，
           而被任务栏拥有的顶层浮窗根是它自己，两者由此区分开）；
      2. 链上任一句柄命中外壳浮窗类名 / 按进程认的外壳 UX 窗口 -> transient；
      3. **属主链**上出现 SHELL_CLASSES（被任务栏/桌面拥有的顶层窗口）-> transient。
    """
    if not hwnd:
        return ""
    hwnd = int(hwnd)
    root = _user32.GetAncestor(wintypes.HWND(hwnd), _GA_ROOT)
    root = int(root) if root else hwnd
    if class_name(hwnd) in SHELL_CLASSES or class_name(root) in SHELL_CLASSES:
        return SHELL_PERSISTENT
    for handle in _shell_chain(hwnd):
        cls = class_name(handle)
        if cls in SHELL_POPUP_CLASSES:
            return SHELL_TRANSIENT
        # 这一类只能按进程认（普通 UWP 应用也在用同一个类名）
        if cls in SHELL_UX_CLASSES and process_image_name(handle) in SHELL_PROCESS_NAMES:
            return SHELL_TRANSIENT
        # 属主是常驻表面 -> 任务栏/桌面弹出的浮窗
        if handle not in (hwnd, root) and cls in SHELL_CLASSES:
            return SHELL_TRANSIENT
    return ""


def is_shell_window(hwnd: int) -> bool:
    """窗口是不是 Windows 外壳自己的窗口（常驻表面或外壳浮窗）。

    用途：判断"组件有没有被盖住"时要把外壳窗口和组件自己的重叠区分开，
    以及判断"前台还是不是桌面"（见 is_desktop_in_front）。
    细化到"常驻/浮窗"请用 shell_surface_kind。
    """
    return shell_surface_kind(hwnd) != ""


def parent_screen_origin(parent: int) -> tuple[int, int]:
    """父窗口客户区左上角在屏幕上的坐标。

    贴到桌面后，窗口位置变成"相对父窗口"的坐标；
    WorkerW/Progman 通常正好在屏幕 (0,0)，但不能假设，所以老老实实换算。
    """
    if not parent:
        return 0, 0
    point = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(wintypes.HWND(parent), ctypes.byref(point)):
        return 0, 0
    return int(point.x), int(point.y)



def is_desktop_in_front(self_hwnd: int = 0) -> bool:
    """当前"桌面"是不是可见的（没被别的程序盖住）。

    判据：前台窗口是 Progman / WorkerW / 任务栏，或者就是我们自己的组件。
    只要前台是别的程序，就说明桌面被盖住了 —— 这时组件谁也看不见，
    可以放心把抓屏和重绘都停掉（这是省 CPU 的关键）。
    """
    foreground = _user32.GetForegroundWindow()
    if not foreground:
        return True
    foreground = int(foreground)
    if self_hwnd and foreground == int(self_hwnd):
        return True
    # 【bug 修复】这里也必须用 is_shell_window（含浮窗名单 + 进程判据 + 属主链），
    # 不能只看 SHELL_CLASSES 那 4 个名字。否则点一下托盘区，弹出的是
    # NotifyIconOverflowWindow / 操作中心这类**外壳浮窗**，前台判据认不出它是外壳
    # -> _occluded=True -> 抓屏被停 -> 液态玻璃变成静态玻璃，要点桌面才恢复。
    # 采样判据（is_shell_window）和这里的判据必须共用同一套机械，不能再各写一份。
    return is_shell_window(foreground)


class DesktopVisibilityWatcher(QObject):
    """轮询"桌面是否可见"，状态变化时发信号。

    为什么用轮询而不是事件钩子：SetWinEventHook 需要消息循环和回调用，
    在 Qt 里很容易互相干扰；而 800ms 一次的 GetForegroundWindow 几乎不花钱。
    """

    visibility_changed = pyqtSignal(bool)

    def __init__(self, self_hwnd: int, interval_ms: int = 800, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._self_hwnd = self_hwnd
        self._last: bool | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(int(interval_ms))
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        self._poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _poll(self) -> None:
        current = is_desktop_in_front(self._self_hwnd)
        if current != self._last:
            self._last = current
            self.visibility_changed.emit(current)
