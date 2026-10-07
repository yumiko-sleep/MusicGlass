# -*- coding: utf-8 -*-
"""阶段4：共享动画时钟 + 四个动画驱动。

【为什么是"一个时钟"而不是每个部件各开一个 QTimer】
    4 个部件各开一个 30fps 定时器 = 每秒 120 次唤醒；它们互不对齐，
    一帧里会变成 4 次独立的重绘请求（Qt 只在同一轮事件里合并 update）。
    共享时钟一个 tick 把所有动画一起推进，重绘请求也能合并成同一帧。
    更关键的是**一致性**：暂停 / 隐藏 / 被别的窗口盖住时，
    只要把时钟停掉，所有动画一起停 —— 不会出现"某个部件忘了停、悄悄烧 CPU"。

【为什么 30fps 而不是 60fps】
    这台机器只有 4 个逻辑核心，而真正贵的是玻璃重绘。
    对"慢慢转的唱片 / 跳动的频谱 / 呼吸的光晕"来说 30fps 已经够顺；
    帧率翻倍 ≈ CPU 翻倍，视觉收益几乎看不到。
    想试 60fps 可以改配置 animation_fps（或启动参数 --fps 60）。

【按真实 dt 推进，而不是"每帧固定步进"】
    驱动拿到的是真实的帧间隔（秒）。这样某一帧卡了（比如刚好在折射），
    动画速度也不会变慢 —— 只是那一帧的步长大一点。

【谁会开、谁会停】（判断逻辑在 GlassWindow._sync_animations）
    * 播放中：封面转、频谱跳、氛围光呼吸；
    * 暂停：三者一起停，画面冻结（不额外烧 CPU，也不会"突然变回另一种样子"）；
    * 窗口隐藏 / 被别的窗口盖住：整个时钟挂起（定时器直接停掉）；
    * 动画总开关关掉（右键菜单 / 托盘 / --no-anim）：回到阶段3 的静态外观。
"""

from __future__ import annotations

import math
import time
import traceback

from PyQt6.QtCore import QObject, QTimer

from ..perf import monitor
from . import theme
from .widgets.equalizer import Equalizer


# ---------------------------------------------------------------------------
# 驱动基类
# ---------------------------------------------------------------------------
class ClockConsumer:
    """被时钟驱动的东西。子类实现 tick(dt)。

    active   由外面（窗口）统一设置，时钟只驱动 active 的驱动。
    min_step_s 最短推进间隔：不是每个动画都需要 30fps。
               氛围光呼吸一圈 5.5 秒，每 100ms 更新一次（10fps）
               肉眼完全看不出差别，但重绘次数直接少 2/3。
               时钟会把攒下的时间一次性交给 tick，所以动画速度不会变慢。
    """

    name = "consumer"
    min_step_s = 0.0

    def __init__(self) -> None:
        self.active = False
        self._accum = 0.0

    def take_step(self, dt: float) -> float | None:
        """攒够 min_step_s 才返回累积的时间；否则返回 None（这一帧不推进）。"""
        self._accum += max(0.0, dt)
        if self.min_step_s > 0.0 and self._accum < self.min_step_s:
            return None
        step, self._accum = self._accum, 0.0
        return step

    def tick(self, dt: float) -> None:  # noqa: D401
        """推进 dt 秒（dt 已经被时钟限幅，不会出现"一大步跳很远"）。"""


# ---------------------------------------------------------------------------
# 时钟
# ---------------------------------------------------------------------------
class AnimationClock(QObject):
    """一个 QTimer 驱动所有动画。"""

    def __init__(self, fps: int = theme.ANIMATION_FPS, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._consumers: dict[str, ClockConsumer] = {}
        self._suspended = False
        self._last_tick = 0.0
        self._ticks = 0
        self._dt_ema = 0.0
        self._worst_dt = 0.0        # 距离上次体检之间"最差的一帧"有多久（限幅前的原始值）
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self.set_fps(fps)

    # ---- 参数 --------------------------------------------------------
    def set_fps(self, fps: int) -> None:
        self._fps = int(max(5, min(120, int(fps))))
        self._timer.setInterval(max(8, int(round(1000.0 / self._fps))))

    @property
    def fps(self) -> int:
        return self._fps

    @property
    def running(self) -> bool:
        """时钟是不是真的在滴答（不是"有没有动画"）。"""
        return self._timer.isActive()

    @property
    def ticks(self) -> int:
        """累计 tick 数（--debug / 自检用：能证明"真的在跑"和"真的停了"）。"""
        return self._ticks

    @property
    def worst_dt(self) -> float:
        """最差一帧有多久（秒，不重置）—— 只给诊断输出看，体检用 take_worst_dt()。"""
        return self._worst_dt

    def take_worst_dt(self) -> float:
        """取出"距上次体检之间最差的一帧有多久"（秒），并清零。

        为什么用它而不是平均帧间隔：抓屏是**一次性**占住主线程的，
        平均值会被大量正常帧稀释；"最差那一帧"才是用户眼睛看到的"卡一下"。
        """
        worst, self._worst_dt = self._worst_dt, 0.0
        return worst

    @property
    def nominal_dt(self) -> float:
        """目标帧间隔（秒）。"""
        return self._timer.interval() / 1000.0

    @property
    def frame_starve_ratio(self) -> float:
        """实际帧间隔 / 目标帧间隔。1.0 = 准点，>2 = 被严重拖慢。

        用它判断"动画卡顿"比看 CPU 靠谱：抓屏阻塞主线程时 CPU 不一定高
        （大部分时间在等放大镜 API / GPU），但帧间隔一定会变大。
        """
        nominal = self.nominal_dt
        if nominal <= 0 or self._dt_ema <= 0:
            return 1.0
        return self._dt_ema / nominal

    def active_names(self) -> list[str]:
        return [name for name, item in self._consumers.items() if item.active]

    # ---- 注册与开关 --------------------------------------------------
    def add(self, consumer: ClockConsumer) -> None:
        self._consumers[consumer.name] = consumer
        self._refresh()

    def remove(self, name: str) -> None:
        self._consumers.pop(name, None)
        self._refresh()

    def set_active(self, name: str, active: bool) -> None:
        consumer = self._consumers.get(name)
        if consumer is None or consumer.active == bool(active):
            return
        consumer.active = bool(active)
        self._refresh()

    def set_suspended(self, suspended: bool) -> None:
        """挂起/恢复。

        挂起是**把定时器整个停掉**，而不是"跑着但什么都不做"——
        后者每秒还是会白醒 30 次。
        """
        suspended = bool(suspended)
        if suspended == self._suspended:
            return
        self._suspended = suspended
        self._refresh()

    @property
    def suspended(self) -> bool:
        return self._suspended

    # ---- 内部 --------------------------------------------------------
    def _refresh(self) -> None:
        wanted = not self._suspended and any(c.active for c in self._consumers.values())
        if wanted and not self._timer.isActive():
            self._last_tick = time.perf_counter()
            self._timer.start()
        elif not wanted and self._timer.isActive():
            self._timer.stop()

    def _on_tick(self) -> None:
        started = time.perf_counter() if monitor.enabled else 0.0
        now = time.perf_counter()
        dt = now - self._last_tick
        self._last_tick = now
        # dt 限幅 0.25s：系统休眠/窗口卡顿之后 dt 可能非常大，
        # 一次性"补齐"会让动画猛跳，而且那一帧本身就贵。丢掉更好。
        # 原始间隔先记下来再限幅：限幅是为了防止动画猛跳，
        # 但"被抓屏阻塞了 2 秒"这个事实必须留给体检用（见 take_worst_dt）
        self._worst_dt = max(self._worst_dt, dt)
        dt = min(max(0.0, dt), 0.25)
        self._ticks += 1
        # 真实帧间隔的滑动平均：给"抓屏卡顿"当探针用。
        # 【为什么需要】pyglass 的抓屏是在 GUI 线程的定时器回调里**同步**做的，
        # 一帧抓 60~100ms 的时候动画时钟就被拖到十几 fps 了（开着录屏软件时实测如此）。
        # 帧间隔 EMA 一旦明显超过目标间隔，就说明我们被别人卡住了 ——
        # 窗口据此自动降级，见 GlassWindow._watch_capture_health。
        if self._dt_ema <= 0.0:
            self._dt_ema = dt
        else:
            self._dt_ema += (dt - self._dt_ema) * 0.15
        for consumer in list(self._consumers.values()):
            if not consumer.active:
                continue
            step = consumer.take_step(dt)
            if step is None:
                continue
            try:
                consumer.tick(step)
            except Exception:
                # 【必须兜住】PyQt 在槽函数里碰到未捕获的 Python 异常会 abort 整个进程
                # （退出码 0xC0000409）。一个动画算错不该让用户的组件凭空消失。
                consumer.active = False
                print(f"[音璃] 动画「{consumer.name}」出错，已停用：")
                traceback.print_exc()
        if monitor.enabled and started:
            monitor.record("动画时钟", (time.perf_counter() - started) * 1000.0)


# ---------------------------------------------------------------------------
# 驱动 1：封面旋转
# ---------------------------------------------------------------------------
class DiscSpin(ClockConsumer):
    """唱片旋转：只累加角度，具体怎么画交给 AlbumDisc。"""

    name = "disc"

    def __init__(self, degrees_per_second: float, on_frame) -> None:
        super().__init__()
        self._speed = float(degrees_per_second)
        self._on_frame = on_frame
        self.angle = 0.0

    def tick(self, dt: float) -> None:
        if dt <= 0.0:
            return
        # 取模 360：角度一直涨下去会丢掉浮点精度（跑几天之后就不准了）
        self.angle = (self.angle + self._speed * dt) % 360.0
        self._on_frame(self.angle)


# ---------------------------------------------------------------------------
# 驱动 2：频谱条跳动
# ---------------------------------------------------------------------------
class EqualizerBars(ClockConsumer):
    """频谱条跳动：推进相位，用 Equalizer.wave_heights 算出一组高度。

    为什么用正弦叠加而不是随机数：相邻两根条之间要有相关性才"像音频"，
    纯随机会变成抽搐的噪点（而且每帧都要重新算随机，观感也不稳定）。
    """

    name = "equalizer"

    def __init__(self, on_frame, speed: float = theme.EQ_PHASE_SPEED) -> None:
        super().__init__()
        self._speed = float(speed)
        self._on_frame = on_frame
        self.phase = 0.0
        self.heights = list(Equalizer.DEFAULT_HEIGHTS)

    def tick(self, dt: float) -> None:
        self.phase += max(0.0, dt) * self._speed
        self.heights = Equalizer.wave_heights(self.phase)
        self._on_frame(self.heights)


# ---------------------------------------------------------------------------
# 驱动 3：氛围光呼吸
# ---------------------------------------------------------------------------
class GlowBreath(ClockConsumer):
    """氛围光呼吸：让光晕亮度在 BASE±AMP 之间缓慢起伏。

    注意强度是"乘法系数"（1.0 = 阶段3 的静态亮度），起点特意选在 cos 的
    波峰上：这样从"静态"切到"呼吸"时亮度是连续的，不会先暗一下再亮起来。

    更新频率降到 10fps（min_step_s）：呼吸一圈 5.5 秒，每帧亮度只差 0.2/255，
    30fps 纯属浪费 —— 而它是四个动画里**单帧最贵**的（12 层柔光描边）。
    """

    name = "glow"
    min_step_s = theme.GLOW_BREATH_MIN_STEP_S

    def __init__(self, on_frame) -> None:
        super().__init__()
        self._on_frame = on_frame
        self.phase = math.pi / 2.0      # 从波峰开始 = 从 1.0 亮度开始
        self.strength = 1.0

    def tick(self, dt: float) -> None:
        self.phase += max(0.0, dt) * theme.GLOW_BREATH_SPEED
        # 取模 2π：跑很久也不会丢精度
        if self.phase > 2 * math.pi:
            self.phase -= 2 * math.pi
        self.strength = (theme.GLOW_BREATH_BASE
                         + theme.GLOW_BREATH_AMP * math.sin(self.phase))
        self._on_frame(self.strength)


# ---------------------------------------------------------------------------
# 驱动 4：切歌过渡
# ---------------------------------------------------------------------------
class TrackTransition(ClockConsumer):
    """切歌过渡：旧内容淡出 -> 在看不见的时候换内容 -> 新内容淡入。

    【为什么不用 QTimer.singleShot 去"到点换内容"】
        singleShot 的延时和动画时长是两条独立的时钟，快速连点"下一首"时会错拍
        （内容已经换了、alpha 还停在半路）。这里用一个状态机 + 共享时钟推进，
        时序天然一致；连切多首也只是把"待换的那首"更新成最新的那一首。

    缓动方向是刻意的：淡出用 (1-k)^2（先慢后快，像退场），
    淡入用 1-(1-k)^2（先快后慢，像入场）。
    """

    name = "transition"

    def __init__(self, fade_out_s: float, fade_in_s: float, on_alpha, on_swap) -> None:
        super().__init__()
        self._out_s = max(0.01, float(fade_out_s))
        self._in_s = max(0.01, float(fade_in_s))
        self._on_alpha = on_alpha
        self._on_swap = on_swap
        self._from = 1.0
        self.phase = "idle"          # idle / out / in
        self.t = 0.0

    # ---- 控制 --------------------------------------------------------
    def start(self, *, fade_in_only: bool = False, from_alpha: float = 1.0) -> None:
        """开始过渡。

        fade_in_only：启动后的第一首（没有"旧内容"可以淡出，直接入场）；
        from_alpha  ：被打断时的当前透明度（淡入到一半又换歌，别跳回 1.0 再淡出）。
        """
        self.t = 0.0
        if fade_in_only:
            self.phase = "in"
            self._from = 0.0
            self._on_alpha(0.0)
        else:
            self.phase = "out"
            self._from = max(0.0, min(1.0, float(from_alpha)))
        self.active = True

    def finish(self) -> None:
        """立刻结束（动画被关掉时调用）：该换的内容换掉，透明度拉回 1.0。"""
        if not self.active and self.phase == "idle":
            return
        if self.phase == "out":
            self._on_swap()          # 还没到中点，但用户关掉了动画：内容必须换
        self.phase = "idle"
        self.t = 0.0
        self.active = False
        self._on_alpha(1.0)

    # ---- 时钟驱动 ----------------------------------------------------
    def tick(self, dt: float) -> None:
        self.t += max(0.0, dt)
        if self.phase == "out":
            k = min(1.0, self.t / self._out_s)
            self._on_alpha(self._from * (1.0 - k) ** 2)
            if k >= 1.0:
                self._on_swap()      # 视觉上已经全透明，换内容看不见
                self.phase = "in"
                self.t = 0.0
            return
        k = min(1.0, self.t / self._in_s)
        self._on_alpha(1.0 - (1.0 - k) ** 2)
        if k >= 1.0:
            self.phase = "idle"
            self.active = False
            self._on_alpha(1.0)
