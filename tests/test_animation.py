# -*- coding: utf-8 -*-
"""阶段4：共享动画时钟 + 四个驱动的单元测试。

这些测试刻意**不建窗口**：时钟和驱动都是纯逻辑（QObject + QTimer），
直接调 clock._on_tick() 就能验证，不需要事件循环转起来。
"画面到底有没有动"是另一回事 —— 那由 tools/verify_animation.py 用像素差来证明。

（pytest 会按文件名顺序收集，这个文件排在 test_cover_loader 前面，
 所以 QApplication 由它先建好：注意必须用 offscreen 平台，
 否则会在桌面上弹出一个真窗口。）
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from musicglass.ui import theme  # noqa: E402
from musicglass.ui.animation import (  # noqa: E402
    AnimationClock,
    ClockConsumer,
    DiscSpin,
    EqualizerBars,
    GlowBreath,
    TrackTransition,
)
from musicglass.ui.widgets.equalizer import Equalizer  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ---------------------------------------------------------------------------
# 测试用驱动
# ---------------------------------------------------------------------------
class Recorder(ClockConsumer):
    def __init__(self, name: str = "recorder") -> None:
        super().__init__()
        self.name = name
        self.dts: list[float] = []

    def tick(self, dt: float) -> None:
        self.dts.append(dt)


class Exploder(ClockConsumer):
    def __init__(self) -> None:
        super().__init__()
        self.name = "exploder"
        self.calls = 0

    def tick(self, dt: float) -> None:
        self.calls += 1
        raise RuntimeError("模拟动画里算错了")


# ---------------------------------------------------------------------------
# 时钟：什么时候跑、什么时候停
# ---------------------------------------------------------------------------
def test_clock_only_runs_when_something_is_active():
    clock = AnimationClock(30)
    assert not clock.running, "没有驱动时不该白跑定时器"

    consumer = Recorder()
    clock.add(consumer)
    assert not clock.running, "驱动没激活时也不该跑"

    clock.set_active(consumer.name, True)
    assert clock.running and clock.active_names() == ["recorder"]

    clock.set_active(consumer.name, False)
    assert not clock.running, "全部停掉后定时器必须关掉（不是空转）"

    clock.set_active(consumer.name, True)
    clock.remove(consumer.name)
    assert not clock.running and clock.active_names() == []


def test_suspend_stops_timer_and_resumes():
    clock = AnimationClock(30)
    consumer = Recorder()
    clock.add(consumer)
    clock.set_active(consumer.name, True)
    assert clock.running

    clock.set_suspended(True)
    assert not clock.running and clock.suspended

    clock.set_suspended(False)
    assert clock.running and not clock.suspended


def test_tick_calls_only_active_consumers():
    clock = AnimationClock(30)
    quiet, busy = Recorder("quiet"), Recorder("busy")
    clock.add(quiet)
    clock.add(busy)
    clock.set_active("busy", True)

    clock._on_tick()
    assert busy.dts and not quiet.dts
    assert clock.ticks == 1


def test_tick_clamps_huge_dt():
    """系统休眠/卡顿后 dt 会很大：必须限幅，否则动画会一次跳很远。"""
    import time

    clock = AnimationClock(30)
    consumer = Recorder()
    clock.add(consumer)
    clock.set_active(consumer.name, True)
    clock._last_tick = time.perf_counter() - 30.0     # 假装卡了 30 秒

    clock._on_tick()
    assert consumer.dts[0] == pytest.approx(0.25, abs=1e-3)


def test_broken_consumer_is_disabled_but_others_keep_running():
    clock = AnimationClock(30)
    broken, healthy = Exploder(), Recorder("healthy")
    clock.add(broken)
    clock.add(healthy)
    clock.set_active("exploder", True)
    clock.set_active("healthy", True)

    clock._on_tick()          # 不该抛出去（抛出去会让整个进程 abort）

    assert broken.calls == 1
    assert not broken.active, "出错的驱动要被摘掉，别每帧都抛一次"
    assert healthy.dts, "其它动画不该被连累"


def test_fps_is_clamped_and_sets_interval():
    clock = AnimationClock(30)
    assert clock.fps == 30
    clock.set_fps(1000)
    assert clock.fps == 120
    clock.set_fps(1)
    assert clock.fps == 5


def test_min_step_throttles_without_slowing_down():
    """降频驱动：攒不够时间就不推进，但攒下的时间要一次性给 tick（否则动画会变慢）。"""
    class Slow(ClockConsumer):
        name = "slow"
        min_step_s = 0.15

        def __init__(self) -> None:
            super().__init__()
            self.steps: list[float] = []

        def tick(self, dt: float) -> None:
            self.steps.append(dt)

    slow = Slow()
    for _ in range(4):
        assert slow.take_step(1 / 30) is None
    step = slow.take_step(1 / 30)
    assert step == pytest.approx(5 / 30, abs=0.01)
    assert slow.take_step(0.0) is None


def test_clock_feeds_throttled_consumer_accumulated_time():
    import time

    class Slow(ClockConsumer):
        name = "slow"
        min_step_s = 0.09

        def __init__(self) -> None:
            super().__init__()
            self.steps: list[float] = []

        def tick(self, dt: float) -> None:
            self.steps.append(dt)

    clock = AnimationClock(30)
    slow = Slow()
    clock.add(slow)
    clock.set_active(slow.name, True)
    for _ in range(3):
        clock._last_tick = time.perf_counter() - 1 / 30
        clock._on_tick()
    assert len(slow.steps) == 1, "3 帧（0.1s）才够 0.09s 的门槛，只该推一次"
    assert slow.steps[0] == pytest.approx(0.1, abs=0.02)


def test_glow_breath_is_the_throttled_one():
    """氛围光必须是降频的那个：它单帧最贵（12 层柔光描边），而亮度变化最慢。"""
    assert GlowBreath.min_step_s == pytest.approx(theme.GLOW_BREATH_MIN_STEP_S)
    assert GlowBreath.min_step_s > 0
    assert DiscSpin.min_step_s == 0.0 and EqualizerBars.min_step_s == 0.0


# ---------------------------------------------------------------------------
# 驱动 1：封面旋转
# ---------------------------------------------------------------------------
def test_disc_spin_accumulates_and_wraps():
    seen: list[float] = []
    spin = DiscSpin(18.0, seen.append)

    spin.tick(10.0)
    assert spin.angle == pytest.approx(180.0)
    spin.tick(20.0)
    assert spin.angle == pytest.approx(180.0), "超过一圈要取模，不然浮点精度会慢慢丢掉"
    assert 0.0 <= spin.angle < 360.0
    assert seen == [180.0, 180.0]

    spin.tick(0.0)
    assert len(seen) == 2, "dt=0 不该触发一次重绘"


def test_disc_speed_matches_theme():
    """18 度/秒 = 20 秒转一圈：数值变了要显式确认，别悄悄把观感改掉。"""
    assert theme.DISC_SPEED_DEG_S == pytest.approx(360.0 / 20.0)


# ---------------------------------------------------------------------------
# 驱动 2：频谱条
# ---------------------------------------------------------------------------
def test_equalizer_bars_move_and_stay_in_range():
    frames: list[list[float]] = []
    bars = EqualizerBars(frames.append, theme.EQ_PHASE_SPEED)
    assert bars.heights == list(Equalizer.DEFAULT_HEIGHTS)

    for _ in range(30):
        bars.tick(1 / 30)
    assert len(bars.heights) == theme.EQ_BARS
    for heights in frames:
        assert all(0.12 <= value <= 1.0 for value in heights), "高度要夹在合法区间里"
    assert frames[0] != frames[-1], "跑了一秒应该已经跳到别的形状了"
    assert bars.phase > 0


# ---------------------------------------------------------------------------
# 驱动 3：氛围光呼吸
# ---------------------------------------------------------------------------
def test_glow_breath_starts_at_static_brightness_then_oscillates():
    seen: list[float] = []
    glow = GlowBreath(seen.append)
    assert glow.strength == pytest.approx(1.0), "起点就是阶段3 的静态亮度，切换时不闪"

    glow.tick(theme.GLOW_BREATH_PERIOD_S / 2)      # 半个周期 -> 到波谷
    assert seen[-1] == pytest.approx(theme.GLOW_BREATH_BASE - theme.GLOW_BREATH_AMP, abs=1e-6)

    glow.tick(theme.GLOW_BREATH_PERIOD_S / 2)      # 再半个周期 -> 回到波峰
    assert seen[-1] == pytest.approx(1.0, abs=1e-6)

    for _ in range(200):
        glow.tick(1 / 30)
        assert (theme.GLOW_BREATH_BASE - theme.GLOW_BREATH_AMP - 1e-6
                <= glow.strength
                <= theme.GLOW_BREATH_BASE + theme.GLOW_BREATH_AMP + 1e-6)


# ---------------------------------------------------------------------------
# 驱动 4：切歌过渡
# ---------------------------------------------------------------------------
def make_transition(out_s: float = 0.15, in_s: float = 0.28):
    alphas: list[float] = []
    events: list[tuple[str, float]] = []

    def on_alpha(value: float) -> None:
        alphas.append(value)

    def on_swap() -> None:
        events.append(("swap", alphas[-1] if alphas else 1.0))

    return TrackTransition(out_s, in_s, on_alpha, on_swap), alphas, events


def test_transition_fades_out_swaps_invisible_then_fades_in():
    transition, alphas, events = make_transition()
    transition.start()
    assert transition.active and transition.phase == "out"

    # 按 30fps 推 0.6 秒：足够跑完 0.15 + 0.28
    for _ in range(18):
        transition.tick(1 / 30)

    assert len(events) == 1, "内容只换一次"
    assert events[0][1] == pytest.approx(0.0), "换内容时必须已经淡到看不见"
    assert min(alphas) == pytest.approx(0.0)
    # 淡出段必须单调下降（缓动是 (1-k)^2），到那一帧正好是 0
    cut = alphas.index(0.0)
    out_part = alphas[:cut]
    assert len(out_part) >= 3 and out_part == sorted(out_part, reverse=True)
    assert alphas[-1] == pytest.approx(1.0)
    assert not transition.active and transition.phase == "idle"


def test_transition_fade_in_only_skips_black_out():
    """启动后第一首：没有旧内容可淡出，直接淡入。"""
    transition, alphas, events = make_transition()
    transition.start(fade_in_only=True)
    assert alphas == [0.0] and transition.phase == "in"

    for _ in range(12):
        transition.tick(1 / 30)
    assert events == [], "淡入不走换内容那一步"
    assert alphas[-1] == pytest.approx(1.0)
    assert not transition.active


def test_transition_interrupted_does_not_jump_to_full_opacity():
    """淡入到一半又换歌：必须从当前透明度接着淡，不能跳回 1.0（那会闪一下）。"""
    transition, alphas, _events = make_transition()
    transition.start(fade_in_only=True)
    transition.tick(0.05)
    midway = alphas[-1]
    assert 0.0 < midway < 1.0

    transition.start(from_alpha=midway)
    transition.tick(1 / 30)
    assert alphas[-1] <= midway + 1e-9


def test_transition_finish_swaps_content_and_restores_opacity():
    """动画被关掉时立刻收尾：内容必须换掉，透明度必须回到 1.0。"""
    transition, alphas, events = make_transition()
    transition.start()
    transition.tick(0.02)
    assert alphas[-1] > 0.5

    transition.finish()
    assert len(events) == 1 and events[0][1] > 0.5
    assert alphas[-1] == pytest.approx(1.0)
    assert not transition.active and transition.phase == "idle"

    transition.finish()          # 再调一次不该再换一次内容
    assert len(events) == 1
