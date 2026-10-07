# -*- coding: utf-8 -*-
"""desktop_layer 里"识别系统外壳窗口"的单元测试。

背景（修的 bug）：组件的动画被任务栏/它弹出的浮窗误判成"组件被盖住"而停摆。
后来在真机上枚举出来的关键事实：
  * 任务栏的**子窗口**（TrayNotifyWnd、TrayClockWClass、TrayButton…）根窗口就是
    Shell_TrayWnd —— 走根窗口回溯能认出来，不用一个个列名字；
  * 真正漏掉的是**独立顶层浮窗**：悬停预览 TaskListThumbnailWnd、
    托盘溢出 NotifyIconOverflowWindow、气泡 tooltips_class32（属主是 Shell_TrayWnd）；
  * Windows.UI.Core.CoreWindow 这个类名外壳和普通 UWP 应用都在用，
    只能按**进程**认。

这些用例把 Win32 调用换成查表（_user32 / class_name / process_image_name），
不碰真窗口，也不依赖屏幕状态。
"""

from __future__ import annotations

import pytest

from musicglass.platform import desktop_layer

TRAY = 0x1000          # Shell_TrayWnd
PROGMAN = 0x2000       # Progman


def _hval(hwnd) -> int:
    """句柄取整：真 Win32 传进来的是 c_void_p 包装，测试里也可能直接是 int。"""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


class _FakeUser32:
    """只实现被测代码用到的几个查询：根窗口、属主、进程号、前台窗口。"""

    def __init__(self, roots=None, owners=None, pids=None, foreground=0) -> None:
        self.roots = roots or {}
        self.owners = owners or {}
        self.pids = pids or {}
        self.foreground = foreground

    def GetAncestor(self, hwnd, _flag):  # noqa: N802
        return self.roots.get(_hval(hwnd), 0)

    def GetWindow(self, hwnd, _flag):  # noqa: N802
        return self.owners.get(_hval(hwnd), 0)

    def GetWindowThreadProcessId(self, hwnd, out):  # noqa: N802
        out._obj.value = self.pids.get(_hval(hwnd), 0)
        return 1

    def GetForegroundWindow(self):  # noqa: N802
        return self.foreground


@pytest.fixture
def win32(monkeypatch):
    """把类名/进程名/窗口关系全换成查表。"""

    def install(names=None, roots=None, owners=None, pids=None, processes=None,
                foreground=0):
        names = names or {}
        monkeypatch.setattr(desktop_layer, "class_name",
                            lambda hwnd: names.get(int(hwnd or 0), ""))
        monkeypatch.setattr(desktop_layer, "process_image_name",
                            lambda hwnd: (processes or {}).get(int(hwnd or 0), ""))
        monkeypatch.setattr(desktop_layer, "_user32",
                            _FakeUser32(roots, owners, pids, foreground))

    return install


# ---------------------------------------------------------------------------
# 类名名单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", [
    "Shell_TrayWnd",            # 主任务栏
    "Shell_SecondaryTrayWnd",   # 副屏任务栏
    "Progman",                  # 桌面
    "WorkerW",                  # 壁纸/图标所在的层
])
def test_桌面层与任务栏本体(win32, cls):
    win32({100: cls})
    assert desktop_layer.is_shell_window(100) is True


@pytest.mark.parametrize("cls", [
    "TaskListThumbnailWnd",                 # 悬停任务栏按钮的窗口预览
    "NotifyIconOverflowWindow",             # 托盘"显示隐藏的图标"
    "TopLevelWindowForOverflowXamlIsland",  # Win11 托盘溢出
    "XamlExplorerHostIslandWindow",         # Win11 任务栏浮窗宿主
    "tooltips_class32",                     # 气泡提示
    "Shell_InputSwitchTopLevelWindow",
    "TaskSwitcherWnd",
    "MultitaskingViewFrame",
])
def test_外壳浮窗类名在名单里(win32, cls):
    win32({200: cls})
    assert desktop_layer.is_shell_window(200) is True


def test_任务栏子窗口靠根窗口认出来(win32):
    """回归：WindowFromPoint 落在时间区/托盘时返回的是子窗口，不是 Shell_TrayWnd。"""
    win32({200: "TrayClockWClass", TRAY: "Shell_TrayWnd"}, roots={200: TRAY})
    assert desktop_layer.is_shell_window(200) is True


def test_桌面图标列表靠根窗口认出来(win32):
    win32({300: "SysListView32", PROGMAN: "Progman"}, roots={300: PROGMAN})
    assert desktop_layer.is_shell_window(300) is True


def test_属主链也走一遍(win32):
    """实测 tooltips_class32 的属主就是 Shell_TrayWnd；浮窗常常既不是子窗口也不是外壳类。"""
    win32({400: "SomeTaskbarFlyoutClass", TRAY: "Shell_TrayWnd"}, owners={400: TRAY})
    assert desktop_layer.is_shell_window(400) is True


def test_属主链带环也不会死循环(win32):
    win32({500: "A", 501: "B"}, owners={500: 501, 501: 500})
    assert desktop_layer.is_shell_window(500) is False


# ---------------------------------------------------------------------------
# 只能按进程认的那一类
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exe", [
    "explorer.exe", "ShellExperienceHost.exe", "StartMenuExperienceHost.exe",
    "SearchHost.exe", "TextInputHost.exe",
])
def test_外壳UX窗口按进程认(win32, exe):
    win32({600: "Windows.UI.Core.CoreWindow"}, processes={600: exe})
    assert desktop_layer.is_shell_window(600) is True


def test_普通UWP的CoreWindow不算外壳窗口(win32):
    """实测本机就有一个整屏的普通 UWP CoreWindow —— 按类名放行会误伤。"""
    win32({600: "Windows.UI.Core.CoreWindow"}, processes={600: "SomeUwpApp.exe"})
    assert desktop_layer.is_shell_window(600) is False


def test_取不到进程名时保守判不是外壳窗口(win32):
    win32({600: "Windows.UI.Core.CoreWindow"}, processes={})
    assert desktop_layer.is_shell_window(600) is False


# ---------------------------------------------------------------------------
# 普通程序 / 边界
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls", [
    "Chrome_WidgetWin_1", "Qt652QWindowIcon", "Notepad", "CatimeWindowClass",
])
def test_普通程序窗口不算外壳窗口(win32, cls):
    win32({400: cls})
    assert desktop_layer.is_shell_window(400) is False


def test_普通窗口的子窗口也不算(win32):
    win32({500: "Chrome_RenderWidgetHostHWND", 700: "Chrome_WidgetWin_1"},
          roots={500: 700})
    assert desktop_layer.is_shell_window(500) is False


def test_无效句柄返回False(win32):
    win32({})
    assert desktop_layer.is_shell_window(0) is False
    assert desktop_layer.shell_root_class(0) == ""
    assert desktop_layer._shell_chain(0) == []


# ---------------------------------------------------------------------------
# 不 mock 的冒烟：无效句柄走真实 Win32 也不能抛异常
# ---------------------------------------------------------------------------
def test_无效句柄走真实win32不抛异常():
    assert desktop_layer.is_shell_window(0) is False
    assert desktop_layer.process_image_name(0) == ""
    bogus = 0x0000DEAD
    assert isinstance(desktop_layer.shell_root_class(bogus), str)
    assert isinstance(desktop_layer.is_shell_window(bogus), bool)
    assert isinstance(desktop_layer._shell_chain(bogus), list)


def test_名单内容是有意为之():
    """is_shell_window 依赖这三份名单，改动必须是有意的。"""
    for name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"):
        assert name in desktop_layer.SHELL_CLASSES
    for name in ("TaskListThumbnailWnd", "NotifyIconOverflowWindow", "tooltips_class32"):
        assert name in desktop_layer.SHELL_POPUP_CLASSES
    assert "Windows.UI.Core.CoreWindow" in desktop_layer.SHELL_UX_CLASSES
    assert "explorer.exe" in desktop_layer.SHELL_PROCESS_NAMES


# ---------------------------------------------------------------------------
# 真机冒烟：把"任务栏本体 + 它的子窗口"真跑一遍（这是这次 bug 的现场）
# ---------------------------------------------------------------------------
def test_真机上任务栏和它的子窗口都算外壳窗口():
    tray = int(desktop_layer._user32.FindWindowW("Shell_TrayWnd", None) or 0)
    if not tray:
        pytest.skip("这台机器上没有任务栏窗口")
    assert desktop_layer.is_shell_window(tray) is True
    assert desktop_layer.class_name(tray) == "Shell_TrayWnd"
    # 时间区是子窗口：WindowFromPoint 在那一带返回的就是它
    clock = int(desktop_layer._user32.FindWindowExW(tray, None, "TrayClockWClass", None) or 0)
    if clock:
        assert desktop_layer.shell_root_class(clock) == "Shell_TrayWnd"
        assert desktop_layer.is_shell_window(clock) is True, "任务栏子窗口必须算露出"
    # 进程查询在这台机器上要能真的取到名字
    assert desktop_layer.process_image_name(tray).lower() == "explorer.exe"
    # 分类也要对：任务栏本体和它的子窗口都是"常驻表面"
    assert desktop_layer.shell_surface_kind(tray) == desktop_layer.SHELL_PERSISTENT
    if clock:
        assert desktop_layer.shell_surface_kind(clock) == desktop_layer.SHELL_PERSISTENT


# ---------------------------------------------------------------------------
# 外壳表面分级：常驻 vs 浮窗（浮窗只对抓屏算遮挡）
# ---------------------------------------------------------------------------
def test_常驻表面分类(win32):
    win32({TRAY: "Shell_TrayWnd", PROGMAN: "Progman",
           300: "TrayClockWClass", 400: "SysListView32"},
          roots={300: TRAY, 400: PROGMAN})
    for handle in (TRAY, PROGMAN, 300, 400):
        assert desktop_layer.shell_surface_kind(handle) == desktop_layer.SHELL_PERSISTENT


def test_任务栏子窗口是常驻而不是浮窗(win32):
    """关键区分：子窗口（根=Shell_TrayWnd）常驻；被任务栏拥有的顶层浮窗是瞬时的。"""
    win32({300: "TrayClockWClass", 700: "SomeFlyoutClass", TRAY: "Shell_TrayWnd"},
          roots={300: TRAY}, owners={700: TRAY})
    assert desktop_layer.shell_surface_kind(300) == desktop_layer.SHELL_PERSISTENT
    assert desktop_layer.shell_surface_kind(700) == desktop_layer.SHELL_TRANSIENT


@pytest.mark.parametrize("cls", [
    "TaskListThumbnailWnd", "NotifyIconOverflowWindow", "tooltips_class32",
    "TopLevelWindowForOverflowXamlIsland", "XamlExplorerHostIslandWindow",
    "Shell_InputSwitchTopLevelWindow", "TaskSwitcherWnd", "MultitaskingViewFrame",
])
def test_外壳浮窗分类(win32, cls):
    win32({500: cls})
    assert desktop_layer.shell_surface_kind(500) == desktop_layer.SHELL_TRANSIENT


def test_外壳UX窗口按进程分成浮窗(win32):
    win32({600: "Windows.UI.Core.CoreWindow"},
          processes={600: "ShellExperienceHost.exe"})
    assert desktop_layer.shell_surface_kind(600) == desktop_layer.SHELL_TRANSIENT

    win32({600: "Windows.UI.Core.CoreWindow"}, processes={600: "SomeUwpApp.exe"})
    assert desktop_layer.shell_surface_kind(600) == ""


def test_普通窗口分类为空(win32):
    win32({800: "Chrome_WidgetWin_1"})
    assert desktop_layer.shell_surface_kind(800) == ""
    assert desktop_layer.is_shell_window(800) is False


def test_两类外壳窗口is_shell_window都为真(win32):
    win32({TRAY: "Shell_TrayWnd", 500: "TaskListThumbnailWnd"})
    assert desktop_layer.is_shell_window(TRAY) is True
    assert desktop_layer.is_shell_window(500) is True


# ---------------------------------------------------------------------------
# 前台判据：必须和采样判据共用同一套机械（回归：以前只看 4 个类名）
# ---------------------------------------------------------------------------
def test_前台是外壳浮窗也算桌面在前(win32):
    """回归：点托盘区弹出的浮窗抢了前台，以前会被当成"别的程序" -> 停抓屏 -> 静态玻璃。"""
    win32({500: "NotifyIconOverflowWindow"}, foreground=500)
    assert desktop_layer.is_desktop_in_front(0) is True


def test_前台是任务栏本体也算桌面在前(win32):
    win32({TRAY: "Shell_TrayWnd"}, foreground=TRAY)
    assert desktop_layer.is_desktop_in_front(0) is True


def test_前台是普通程序才算别的程序(win32):
    win32({900: "Chrome_WidgetWin_1"}, foreground=900)
    assert desktop_layer.is_desktop_in_front(0) is False


def test_前台是组件自己或没有前台(win32):
    win32({}, foreground=0x9001)
    assert desktop_layer.is_desktop_in_front(0x9001) is True
    win32({}, foreground=0)
    assert desktop_layer.is_desktop_in_front(0) is True


# ---------------------------------------------------------------------------
# 参照窗口：explorer 优先 + 失败换候选重试（回归：错误码 5 直接放弃贴桌面）
# ---------------------------------------------------------------------------
def _fake_candidates(monkeypatch, handles):
    monkeypatch.setattr(desktop_layer, "desktop_layer_candidates", lambda: list(handles))


def test_参照窗口优先选explorer自己的(monkeypatch):
    _fake_candidates(monkeypatch, [100, 200])
    monkeypatch.setattr(desktop_layer, "process_image_name",
                        lambda h: "other.exe" if int(h or 0) == 100 else "explorer.exe")
    assert desktop_layer.find_top_desktop_layer() == 200


def test_没有explorer自己的就退回第一个(monkeypatch):
    _fake_candidates(monkeypatch, [100, 200])
    monkeypatch.setattr(desktop_layer, "process_image_name", lambda h: "other.exe")
    assert desktop_layer.find_top_desktop_layer() == 100


def test_没有候选时返回0(monkeypatch):
    _fake_candidates(monkeypatch, [])
    assert desktop_layer.find_top_desktop_layer() == 0


def test_插入失败会换下一个候选(monkeypatch):
    _fake_candidates(monkeypatch, [100, 200])
    # 先试 explorer 自己的 200；它失败就换 100
    monkeypatch.setattr(desktop_layer, "process_image_name",
                        lambda h: "explorer.exe" if int(h or 0) == 200 else "other.exe")
    monkeypatch.setattr(desktop_layer, "class_name",
                        lambda h: {100: "WorkerW", 200: "Progman"}.get(int(h or 0), ""))
    tried: list[int] = []

    def fake_try(hwnd, target):
        tried.append(target)
        return 5 if target == 200 else 0        # 第一个候选被系统拒绝（错误码 5）

    monkeypatch.setattr(desktop_layer, "_try_insert_above", fake_try)
    target, cls, error = desktop_layer.insert_above_desktop(0x9001)

    assert (target, cls, error) == (100, "WorkerW", "")
    assert tried == [200, 100], "explorer 自己的候选必须先试，失败了才换下一个"


def test_所有候选都失败时报最后一次错误(monkeypatch):
    _fake_candidates(monkeypatch, [100, 200])
    monkeypatch.setattr(desktop_layer, "process_image_name", lambda h: "other.exe")
    monkeypatch.setattr(desktop_layer, "_try_insert_above", lambda hwnd, target: 5)

    target, cls, error = desktop_layer.insert_above_desktop(0x9001)

    assert target == 0 and cls == ""
    assert "错误码 5" in error


def test_显式指定参照时不换候选(monkeypatch):
    _fake_candidates(monkeypatch, [100, 200])
    tried: list[int] = []

    def fake_try(hwnd, target):
        tried.append(target)
        return 5

    monkeypatch.setattr(desktop_layer, "_try_insert_above", fake_try)
    desktop_layer.insert_above_desktop(0x9001, target=300)
    assert tried == [300]


def test_无候选时报找不到桌面层(monkeypatch):
    _fake_candidates(monkeypatch, [])
    target, cls, error = desktop_layer.insert_above_desktop(0x9001)
    assert target == 0 and "找不到桌面层" in error


def test_无效句柄不试任何候选(monkeypatch):
    _fake_candidates(monkeypatch, [100])
    monkeypatch.setattr(desktop_layer, "_try_insert_above",
                        lambda hwnd, target: pytest.fail("不该有任何尝试"))
    target, cls, error = desktop_layer.insert_above_desktop(0)
    assert target == 0 and error == "窗口句柄无效"


