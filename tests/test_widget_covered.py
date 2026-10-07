# -*- coding: utf-8 -*-
"""遮挡判定、"两个闸门"、抓屏体检容错的回归测试。

【三轮 bug 的来龙去脉】
1. 组件被别的窗口盖住后挂起动画是对的，但**挪开窗口之后的恢复很随机**：
   `_widget_covered` 原来只在两个偶发时机重算（前台身份翻转 / 拖动缩放结束）。
   修法：挂到 1 秒巡检上（`_refresh_widget_covered`），且只在状态真翻转时才动后端。
2. 还是不稳，实测"点桌面一下才恢复"。根因是**第二个闸门** `_occluded` 也参与决定
   动画挂起，而它只在 800ms 巡检发现前台**身份翻转**时才更新。
   修法（方案 A）：两个闸门拆开 —— `_occluded` 只停抓屏，动画只由 `_widget_covered`
   + 可见性决定；workerw 模式也纳入采样。
3. 点任务栏托盘区后：动画继续转（对），但**液态玻璃没了、点桌面才恢复**。
   两个独立原因：
   * `is_desktop_in_front` 还在用老的 4 个类名，认不出外壳**浮窗**（托盘溢出/操作中心）
     -> `_occluded=True` -> 抓屏停（前台判据已在 desktop_layer 里合并，这里测玻璃窗口侧）；
   * 抓屏体检太敏感：单帧 >120ms 就算不健康、连续 2 轮就降档，2 档 = 抓屏全停；
     而恢复路径又写了 `not self._occluded`，外壳浮窗在前台时永远回不来。
   修法：体检阈值放宽 + 连续 3 轮才降档；恢复改走唯一的门控入口并清回 0 档；
   同时把"外壳表面"分成两级 —— **常驻的不算遮挡、浮窗只对抓屏算遮挡**
   （否则玻璃里映别人的窗口）。

这些用例不建真窗口：用一个只带必要状态的替身对象，把 GlassWindow 的真方法
绑上去调用，所以离线、无事件循环也能跑。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, QRectF

from musicglass.platform import desktop_layer
from musicglass.ui import glass_window
from musicglass.ui.glass_window import GlassWindow

OURS = 0x9001          # 组件自己的句柄
BROWSER = 0xB0B0       # 盖在组件上的浏览器
TRAY = 0xC0C0          # 任务栏本体（常驻外壳表面）
POPUP = 0xD0D0         # 外壳浮窗（窗口预览 / 托盘溢出）


class _Clock:
    def __init__(self) -> None:
        self.suspended = False
        self.running = True
        self.worst = 0.0
        self.ratio = 1.0

    def set_suspended(self, flag: bool) -> None:
        self.suspended = bool(flag)

    def take_worst_dt(self) -> float:
        value, self.worst = self.worst, 0.0
        return value

    @property
    def frame_starve_ratio(self) -> float:
        return self.ratio


class _Backend:
    def __init__(self) -> None:
        self.live = True
        self.refreshes = 0
        self.drops = 0
        self.intervals: list[int] = []

    def set_live(self, on: bool) -> None:
        self.live = bool(on)

    def drop_refraction(self) -> None:
        self.drops += 1

    def refresh(self) -> None:
        self.refreshes += 1

    def apply_interval(self, ms: int) -> None:
        self.intervals.append(int(ms))


class _FakeSignal:
    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)


class _FakeTimer:
    """顶掉真 QTimer：只记下谁被连上、按什么顺序、间隔多少。"""

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.timeout = _FakeSignal()
        self.interval = 0
        self.started = False

    def setInterval(self, ms: int) -> None:  # noqa: N802
        self.interval = int(ms)

    def start(self) -> None:
        self.started = True


class _Stub:
    """只提供真方法需要的最小接口（不建真窗口，避免依赖平台插件和后端）。"""

    hwnd = OURS
    ACTIVE_IDLE_SECONDS = GlassWindow.ACTIVE_IDLE_SECONDS
    PERF_WORST_S = GlassWindow.PERF_WORST_S
    PERF_RATIO = GlassWindow.PERF_RATIO
    PERF_BAD_STREAK = GlassWindow.PERF_BAD_STREAK
    PERF_GOOD_STREAK = GlassWindow.PERF_GOOD_STREAK
    PERF_HEALTH_WORST_S = GlassWindow.PERF_HEALTH_WORST_S
    PERF_HEALTH_RATIO = GlassWindow.PERF_HEALTH_RATIO
    PERF_MAX_LEVEL = GlassWindow.PERF_MAX_LEVEL
    PERF_FORCE_RECOVER_S = GlassWindow.PERF_FORCE_RECOVER_S

    # 把要测的真方法绑上来（staticmethod 要包一层，否则会多传一个 self）
    _refresh_widget_covered = GlassWindow._refresh_widget_covered
    _update_widget_covered = GlassWindow._update_widget_covered
    _apply_background_gate = GlassWindow._apply_background_gate
    _capture_blocked_now = GlassWindow._capture_blocked_now
    _force_capture_recovery = GlassWindow._force_capture_recovery
    _sync_clock_suspension = GlassWindow._sync_clock_suspension
    _suspend_reason = GlassWindow._suspend_reason
    _probe_applicable = GlassWindow._probe_applicable
    _probe_points = staticmethod(GlassWindow._probe_points)
    _update_cadence = GlassWindow._update_cadence
    _watch_capture_health = GlassWindow._watch_capture_health
    _on_position_changed = GlassWindow._on_position_changed
    _on_state_changed = GlassWindow._on_state_changed
    _on_loop_changed = GlassWindow._on_loop_changed
    _on_background_changed = GlassWindow._on_background_changed

    def __init__(self, top_hwnd: int = BROWSER, visible: bool = True,
                 mode: str = "zorder") -> None:
        self.top_hwnd = top_hwnd          # 采样点上"最上层窗口"的句柄
        self.shell_kinds: dict[int, str] = {}   # 句柄 -> shell_surface_kind 的返回值
        self._visible = visible
        self._config = SimpleNamespace(
            always_on_top=False, desktop_layer=True, desktop_layer_mode=mode,
            capture_interval_ms=200, capture_interval_idle_ms=1000,
            adaptive_capture=True, animations=True,
        )
        self._widget_covered = False
        self._capture_covered = False
        self._capture_blocked = False
        self._probe_exposed = 0
        self._probe_capture_exposed = 0
        self._probe_total = 0
        self._occluded = False
        self._hover_inside = False
        self._perf_level = 0
        self._perf_bad = 0
        self._perf_good = 0
        self._perf_degraded_at = None
        self._perf_force_used = False
        self._cadence_current = 0
        self._seeking = False
        self.clock = _Clock()
        self._clock = self.clock
        self._transition = SimpleNamespace(active=False, finish=lambda: None)
        self.backend = _Backend()
        self._backend = self.backend
        self.paints = 0
        self.bottom_repaints = 0
        self.sync_animations_calls = 0
        self.probes = 0

    # ---- 真方法用到的几个 Qt / 内部接口 -------------------------------
    def winId(self) -> int:  # noqa: N802
        return self.hwnd

    def isVisible(self) -> bool:  # noqa: N802
        return self._visible

    def layout_info(self):
        return SimpleNamespace(panel=QRectF(56.0, 56.0, 400.0, 120.0))

    def panel_rect(self) -> QRectF:
        return QRectF(56.0, 56.0, 400.0, 120.0)

    def _window_screen_pos(self) -> QPoint:
        return QPoint(0, 0)

    def update(self, *args) -> None:  # noqa: N802
        self.paints += 1

    def _update_bottom_area(self) -> None:
        self.bottom_repaints += 1

    def _sync_animations(self) -> None:
        self.sync_animations_calls += 1


@pytest.fixture
def probe(monkeypatch):
    """把 Win32 采样换成"当前句柄 = stub.top_hwnd"，并记录前台判据有没有被用到。"""

    def install(win: _Stub):
        def fake_window_at(x, y):
            win.probes += 1
            return win.top_hwnd

        monkeypatch.setattr(desktop_layer, "window_at", fake_window_at)
        monkeypatch.setattr(desktop_layer, "shell_surface_kind",
                            lambda hwnd: win.shell_kinds.get(int(hwnd or 0), ""))
        foreground_calls: list[int] = []
        monkeypatch.setattr(
            desktop_layer, "is_desktop_in_front",
            lambda *a, **k: (foreground_calls.append(1), True)[1],
        )
        return foreground_calls

    return install


# ---------------------------------------------------------------------------
# 回归一：遮挡解除后，靠 1 秒巡检自己恢复（不依赖任何前台窗口身份变化）
# ---------------------------------------------------------------------------
def test_遮挡解除后周期性重采样恢复动画(probe):
    win = _Stub(top_hwnd=BROWSER)
    foreground_calls = probe(win)

    # 第一次巡检：浏览器完全盖住组件 -> 挂起（这是对的）
    GlassWindow._refresh_widget_covered(win)
    assert win._widget_covered is True
    assert win.clock.suspended is True, "被盖住时必须挂起动画"
    assert win.backend.live is False, "被盖住时必须停止抓屏"

    # 把窗口挪开：前台窗口身份没有变（浏览器还是前台），只是遮挡没了
    win.top_hwnd = OURS
    GlassWindow._refresh_widget_covered(win)

    assert win._widget_covered is False
    assert win.clock.suspended is False, "周期性重采样必须能让动画自己恢复"
    assert win.backend.live is True, "恢复后必须重新开始抓屏"
    assert win.backend.refreshes >= 1, "恢复时应该立刻重抓一帧，画面不过期"
    assert foreground_calls == [], "恢复不能依赖前台窗口身份变化（这正是原来的 bug）"


def test_反复巡检也稳定恢复(probe):
    """模拟真实时序：被盖住 -> 连续几次巡检都盖着 -> 挪开 -> 下一次巡检恢复。"""
    win = _Stub(top_hwnd=BROWSER)
    probe(win)
    for _ in range(3):
        GlassWindow._refresh_widget_covered(win)
    assert win.clock.suspended is True

    win.top_hwnd = OURS
    GlassWindow._refresh_widget_covered(win)
    assert win.clock.suspended is False


def test_被盖住时反复巡检不会重复折腾后端(probe):
    """状态没翻转就不许动后端 / 重绘：本函数现在每秒都会被调用一次。"""
    win = _Stub(top_hwnd=BROWSER)
    probe(win)
    GlassWindow._refresh_widget_covered(win)
    baseline = (win.backend.drops, win.backend.refreshes, win.paints)

    for _ in range(5):
        GlassWindow._refresh_widget_covered(win)

    assert (win.backend.drops, win.backend.refreshes, win.paints) == baseline
    assert win.clock.suspended is True


def test_窗口藏着时巡检不采样(probe):
    win = _Stub(top_hwnd=BROWSER, visible=False)
    probe(win)
    GlassWindow._refresh_widget_covered(win)
    assert win.probes == 0, "托盘里藏着的时候不该每秒去抓 15 个采样点"


def test_置顶模式不会被巡检反复刷新(probe):
    """不适用的模式（置顶/普通窗口）里，巡检每次都不该动后端。"""
    win = _Stub(top_hwnd=BROWSER)
    probe(win)
    win._config.always_on_top = True
    for _ in range(5):
        GlassWindow._refresh_widget_covered(win)
    assert (win.backend.refreshes, win.backend.drops, win.paints) == (0, 0, 0)
    assert win._widget_covered is False
    assert win._capture_covered is False


# ---------------------------------------------------------------------------
# 回归四：外壳表面分两级 —— 常驻的都不算遮挡，浮窗只对"抓屏"算遮挡
# ---------------------------------------------------------------------------
def test_常驻外壳表面不算遮挡(probe):
    """任务栏本体/桌面压在采样点上：动画和抓屏都不该受影响。"""
    win = _Stub(top_hwnd=TRAY)
    probe(win)
    win.shell_kinds = {TRAY: desktop_layer.SHELL_PERSISTENT}

    GlassWindow._refresh_widget_covered(win)

    assert win._widget_covered is False
    assert win._capture_covered is False
    assert win.clock.suspended is False
    assert win.backend.live is True


def test_外壳浮窗放过动画但停抓屏(probe):
    """核心：窗口预览/托盘溢出压住组件时，动画照转，但玻璃不能映出它。"""
    win = _Stub(top_hwnd=POPUP)
    probe(win)
    win.shell_kinds = {POPUP: desktop_layer.SHELL_TRANSIENT}

    GlassWindow._refresh_widget_covered(win)

    assert win._widget_covered is False, "外壳浮窗不该冻结动画"
    assert win.clock.suspended is False
    assert win._capture_covered is True, "外壳浮窗必须让抓屏停下（否则玻璃映别人的窗口）"
    assert win.backend.live is False
    assert win.backend.drops >= 1


def test_外壳浮窗消失后抓屏自己回来(probe):
    win = _Stub(top_hwnd=POPUP)
    probe(win)
    win.shell_kinds = {POPUP: desktop_layer.SHELL_TRANSIENT}
    GlassWindow._refresh_widget_covered(win)
    assert win.backend.live is False

    win.top_hwnd = OURS
    GlassWindow._refresh_widget_covered(win)
    assert win._capture_covered is False
    assert win.backend.live is True
    assert win.backend.refreshes >= 1, "恢复抓屏时要立刻重抓一帧"


def test_普通窗口两边都算遮挡(probe):
    win = _Stub(top_hwnd=BROWSER)
    probe(win)
    GlassWindow._refresh_widget_covered(win)
    assert win._widget_covered is True
    assert win._capture_covered is True
    assert win.clock.suspended is True
    assert win.backend.live is False


# ---------------------------------------------------------------------------
# 回归二（方案 A）：_occluded 只停抓屏，不再停动画、也不再吞掉重绘
# ---------------------------------------------------------------------------
def test_前台是别的程序但组件露着时动画和抓屏都照常(probe):
    """方案 A 核心：前台是浏览器（_occluded=True）但组件没被盖住 -> 动画照跑、**抓屏也照跑**。

    以前这里抓屏会被停掉，于是"最小化上方的窗口后玻璃一直是静态的、要点桌面才恢复"。
    """
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._occluded = True                     # 前台是别的程序
    win._widget_covered = False              # 但组件完整露着
    win._capture_covered = False

    GlassWindow._apply_background_gate(win)

    assert win.clock.suspended is False, "前台是别的程序不该冻结动画"
    assert win.backend.live is True, "组件露着就该继续抓屏（正判据看真实重叠）"


def test_没有采样能力时仍然用前台启发式兜底(probe):
    """置顶 / 普通窗口模式没有 15 点采样，退回 _occluded，别让被盖住时还白抓屏。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._config.always_on_top = True          # 采样不可用
    win._occluded = True
    win._capture_covered = False

    assert win._probe_applicable() is False
    assert win._capture_blocked_now() is True
    GlassWindow._apply_background_gate(win)
    assert win.backend.live is False


def test_抓屏开着时背景变化仍然重画(probe):
    """回归：以前 _occluded 会吞掉背景重绘，抓屏开着但玻璃不更新。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._occluded = True
    GlassWindow._on_background_changed(win)
    assert win.paints == 1


def test_真被盖住时动画还是要停(probe):
    win = _Stub(top_hwnd=BROWSER)
    probe(win)
    win._occluded = False
    GlassWindow._refresh_widget_covered(win)     # 采样判出"真被盖住"
    assert win.clock.suspended is True
    assert win.backend.live is False


def test_前台是别的程序时进度条仍然重绘(probe):
    """回归：以前 _occluded 会吞掉进度条重绘，动画跑着但界面是旧的。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._occluded = True
    GlassWindow._on_position_changed(win, 12.5)
    assert win.bottom_repaints == 1

    win._widget_covered = True
    GlassWindow._on_position_changed(win, 13.0)
    assert win.bottom_repaints == 1, "真被盖住时不该重绘"


def test_前台是别的程序时状态变化仍然重绘(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._occluded = True
    before = win.paints
    GlassWindow._on_state_changed(win, None)
    GlassWindow._on_loop_changed(win, None)
    assert win.paints == before + 2
    assert win.sync_animations_calls == 1


def test_抓屏门控停着时不重画背景(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._capture_covered = True              # 真被压住 -> 抓屏停着
    GlassWindow._on_background_changed(win)
    assert win.paints == 0

    win._capture_covered = False
    win._visible = False                     # 隐藏着也不画
    GlassWindow._on_background_changed(win)
    assert win.paints == 0


def test_挂起原因能说清楚(probe):
    """诊断用：挂起=是 的时候必须能看出是哪个判据干的。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    assert win._suspend_reason() == "否"

    win._widget_covered = True
    win.clock.suspended = True
    assert "盖住" in win._suspend_reason()

    win._widget_covered = False
    win._visible = False
    assert "隐藏" in win._suspend_reason()


# ---------------------------------------------------------------------------
# 回归三：workerw 模式也纳入采样
# ---------------------------------------------------------------------------
def test_workerw模式也采样(probe):
    """回归：workerw 模式以前被排除在采样之外，动画只受前台启发式管。"""
    win = _Stub(top_hwnd=BROWSER, mode="workerw")
    probe(win)
    assert win._probe_applicable() is True

    GlassWindow._refresh_widget_covered(win)
    assert win.probes > 0, "workerw 模式必须真的去采样"
    assert win._widget_covered is True
    assert win.clock.suspended is True

    win.top_hwnd = OURS
    GlassWindow._refresh_widget_covered(win)
    assert win._widget_covered is False
    assert win.clock.suspended is False


# ---------------------------------------------------------------------------
# 回归五：抓屏体检的容错与恢复
# ---------------------------------------------------------------------------
def _tick(win, worst=0.0, ratio=1.0):
    win.clock.worst = worst
    win.clock.ratio = ratio
    GlassWindow._watch_capture_health(win)


def test_体检不再被前台启发式挡掉(probe):
    """回归：以前 _occluded 会让 _watch_capture_health 早退，动画在跑却测不到卡顿。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._occluded = True
    _tick(win)
    assert win._perf_good == 1, "前台是别的程序时体检必须照常执行"

    win._widget_covered = True
    GlassWindow._watch_capture_health(win)
    assert win._perf_good == 1, "组件真被盖住时才跳过（没在画动画）"


def test_单帧超时不降档(probe):
    """回归：以前单帧 >120ms 就算不健康、连续 2 轮就降档。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    _tick(win, worst=0.30)
    assert win._perf_bad == 1 and win._perf_level == 0, "一轮超时不许降档"
    _tick(win, worst=0.30)
    assert win._perf_bad == 2 and win._perf_level == 0, "两轮也不许（阈值是 3 轮）"
    _tick(win, worst=0.30)
    assert win._perf_level == 1, "连续 3 轮才降到 1 档"


def test_瞬时卡顿被阈值挡在外面(probe):
    """120~200ms 的偶发一帧不再算"卡顿"（原来 0.12 就报）。

    注意它落在"灰区"（不健康判据不成立、健康判据也不成立），所以只是**不降档**，
    也不会被当成健康轮去攒恢复计数。
    """
    win = _Stub(top_hwnd=OURS)
    probe(win)
    _tick(win, worst=0.15)
    assert win._perf_bad == 0, "150ms 不该算'持续卡顿'"
    assert win._perf_good == 0, "灰区也不该算健康轮"
    assert win._perf_level == 0

    _tick(win, worst=0.15)
    _tick(win, worst=0.15)
    assert win._perf_level == 0, "再来两轮灰区也不降档"


def test_健康判据放宽到100ms(probe):
    """回归：以前"健康"要求最差一帧 <60ms，机器稍抖就永远攒不满恢复计数。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    _tick(win, worst=0.09)                   # 90ms：新判据算健康，旧判据是灰区
    assert win._perf_good == 1
    assert win._perf_bad == 0


def test_五轮健康就升一档(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = 1
    win._capture_blocked = True
    win.backend.live = False
    for _ in range(GlassWindow.PERF_GOOD_STREAK - 1):
        _tick(win)
    assert win._perf_level == 1, "还没攒够，不升档"
    _tick(win)
    assert win._perf_level == 0
    assert win.backend.live is True, "升到 0 档且门控不拦 -> 立刻恢复抓屏"


# ---------------------------------------------------------------------------
# 回归六：时间兜底（降级超过 3 秒强制恢复，不管健康计数）
# ---------------------------------------------------------------------------
def test_降级超过3秒强制恢复抓屏(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = GlassWindow.PERF_MAX_LEVEL
    win._perf_degraded_at = time.monotonic() - (GlassWindow.PERF_FORCE_RECOVER_S + 0.5)
    win.backend.live = False
    win._capture_covered = False

    GlassWindow._force_capture_recovery(win)

    assert win._perf_level == 0, "不管健康计数，档位必须清回 0"
    assert win.backend.live is True, "并且真的重新开抓屏"
    assert win._perf_force_used is True, "每次降级只强制一次"


def test_降级没到3秒不强制(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = 1
    win._perf_degraded_at = time.monotonic()
    win.backend.live = False

    GlassWindow._force_capture_recovery(win)
    assert win._perf_level == 1 and win.backend.live is False


def test_门控该拦着时不强制开抓屏(probe):
    win = _Stub(top_hwnd=BROWSER)             # 组件真被盖住
    probe(win)
    GlassWindow._refresh_widget_covered(win)
    win._perf_level = GlassWindow.PERF_MAX_LEVEL
    win._perf_degraded_at = time.monotonic() - 10.0

    GlassWindow._force_capture_recovery(win)

    assert win._perf_level == GlassWindow.PERF_MAX_LEVEL, "该拦着就不清档"
    assert win.backend.live is False
    assert win._perf_force_used is False


def test_每次降级只强制一次(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = 1
    win._perf_degraded_at = time.monotonic() - 10.0
    GlassWindow._force_capture_recovery(win)
    assert win._perf_force_used is True

    win._perf_level = 1                        # 又降级了（同一次降级期间）
    win.backend.live = False
    GlassWindow._force_capture_recovery(win)
    assert win._perf_level == 1, "同一次降级不再强制第二次"
    assert win.backend.live is False


def test_新一轮降级会重新允许强制(probe):
    """降级起点被重新打点（体检里每降一档都会打）-> 允许再强制一次。"""
    win = _Stub(top_hwnd=OURS)
    probe(win)
    for _ in range(GlassWindow.PERF_BAD_STREAK):
        _tick(win, worst=0.5)
    assert win._perf_level == 1
    assert win._perf_degraded_at is not None
    assert win._perf_force_used is False


def test_降级档位归零后强制资格复位(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = 1
    win._perf_force_used = True
    win.backend.live = False
    for _ in range(GlassWindow.PERF_GOOD_STREAK):
        _tick(win)
    assert win._perf_level == 0
    assert win._perf_force_used is False, "恢复自然发生后，下一轮降级可以再用兜底"


def test_健康一轮就清掉坏计数(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    _tick(win, worst=0.30)
    _tick(win, worst=0.30)
    _tick(win)                      # 中间来一轮健康
    assert win._perf_bad == 0
    _tick(win, worst=0.30)
    _tick(win, worst=0.30)
    assert win._perf_level == 0, "坏计数被清零后不该累计到 3 轮"


def test_降级到2档会停抓屏(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    for _ in range(6):              # 3 轮 -> 1 档，再 3 轮 -> 2 档
        _tick(win, worst=0.30)
    assert win._perf_level == GlassWindow.PERF_MAX_LEVEL
    assert win.backend.live is False, "2 档 = 只保留动画"
    assert win.backend.drops >= 1


def test_恢复走门控而不是自己判断前台(probe):
    """回归：以前恢复时写死 `not self._occluded`，还自己 set_live(True)。

    现在恢复只降档位，要不要真开抓屏交给唯一的门控入口决定。
    """
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._perf_level = GlassWindow.PERF_MAX_LEVEL
    win._perf_good = GlassWindow.PERF_GOOD_STREAK - 1
    win._capture_blocked = True
    win.backend.live = False
    win._occluded = False            # 门控允许抓屏
    win._capture_covered = False

    _tick(win)                        # 第 8 轮健康 -> 降回 1 档 -> 门控重开抓屏

    assert win._perf_level == 0, "抓屏重新放开时档位要清回 0"
    assert win.backend.live is True, "恢复必须真的把抓屏打开"


def test_门控不允许时恢复不会硬开抓屏(probe):
    """组件真被压住（外壳浮窗）时抓屏本来就该停着，恢复只降档位、不硬开。"""
    win = _Stub(top_hwnd=POPUP)
    probe(win)
    win.shell_kinds = {POPUP: desktop_layer.SHELL_TRANSIENT}
    GlassWindow._refresh_widget_covered(win)      # 动画放过、抓屏算遮挡
    assert win._capture_covered is True and win.backend.live is False

    win._perf_level = GlassWindow.PERF_MAX_LEVEL
    win._perf_good = GlassWindow.PERF_GOOD_STREAK - 1
    win._capture_blocked = True
    win.backend.live = False

    _tick(win)

    assert win._perf_level == GlassWindow.PERF_MAX_LEVEL - 1, "档位该降（体检恢复正常了）"
    assert win.backend.live is False, "但门控不允许时不该硬开抓屏"


def test_抓屏重新放开时清掉降级档位(probe):
    win = _Stub(top_hwnd=OURS)
    probe(win)
    win._capture_blocked = True       # 上一轮被挡住
    win._perf_level = 2
    win.backend.live = False
    win._occluded = False
    win._capture_covered = False

    GlassWindow._apply_background_gate(win)

    assert win._perf_level == 0, "否则频率会一直卡在 750*档位 ms"
    assert win._perf_bad == 0 and win._perf_good == 0
    assert win.backend.live is True


def test_清档位顺手把抓屏频率还原(monkeypatch):
    win = _Stub()
    monkeypatch.setattr(glass_window, "QTimer", _FakeTimer)
    GlassWindow._setup_cadence(win)          # 让 _update_cadence 真的跑起来
    win._hover_inside = True                 # 固定走"快档"，否则空闲时会走慢档间隔
    win._capture_blocked = True
    win._perf_level = 2
    win._occluded = False
    win._capture_covered = False

    GlassWindow._apply_background_gate(win)

    assert win._perf_level == 0
    assert win.backend.intervals[-1] == win._config.capture_interval_ms


# ---------------------------------------------------------------------------
# 接线：重算必须挂在 1 秒巡检上，且排在体检之前
# ---------------------------------------------------------------------------
def test_一秒巡检里重算排在体检之前(monkeypatch):
    win = _Stub()
    monkeypatch.setattr(glass_window, "QTimer", _FakeTimer)
    GlassWindow._setup_cadence(win)

    timer = win._cadence_timer
    assert timer.interval == 1000
    assert timer.started is True
    names = [getattr(slot, "__name__", "?") for slot in timer.timeout.slots]
    assert names == ["_update_cadence", "_refresh_widget_covered", "_watch_capture_health",
                     "_force_capture_recovery"], names
