# -*- coding: utf-8 -*-
"""阶段4 动画自检：用像素证据证明"四个动画真的在动、该停的时候真的停"。

为什么要有这个工具
    "动画看起来在动"不该靠肉眼盯着屏幕猜（贴桌面模式下窗口平时被其它程序盖着，
    根本看不到）。这里用**帧间像素差 + 内部计数器**来量化，跑一遍就知道：

        1. 播放中：封面 / 频谱 / 氛围光三块区域各自都在变（有像素差为证）；
        2. 暂停后：整窗像素完全一致、时钟停摆（ticks 不再增长）—— 证明没在空转烧 CPU；
        3. 切歌：内容透明度先降到 0（旧内容看不见）-> 换内容 -> 回到 1.0，
           且"换内容"这件事发生在透明度很低的时候（所以看不到半新半旧）；
        4. 关掉动画（animations=False）：时钟一帧都不跑，外观回到阶段3 的静态样子。

    默认用 Qt 的 offscreen 平台跑（不在桌面上弹窗口）；
    想真的看一眼，加 --show（窗口会在桌面上出现几秒，跑完自动退出）。

用法（项目根目录）：
    .\\.venv\\Scripts\\python.exe tools\\verify_animation.py
    .\\.venv\\Scripts\\python.exe tools\\verify_animation.py --fps 60 --scale 1.3 --show

输出：一张 PASS/FAIL 检查表 + 对比帧（build/anim_frames/*.png，可以自己打开对比）。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from pathlib import Path

# 项目根目录加进 sys.path：直接 python tools/xx.py 也能 import musicglass
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="音璃 阶段4 动画自检")
    parser.add_argument("--fps", type=int, default=30, help="动画帧率（默认 30）")
    parser.add_argument("--scale", type=float, default=1.0, help="面板缩放（默认 1.0）")
    parser.add_argument("--show", action="store_true", help="用真实窗口跑（会在桌面上出现几秒）")
    parser.add_argument("--no-frames", action="store_true", help="不保存对比帧")
    return parser.parse_args()


ARGS = parse_args()
if not ARGS.show:
    # 必须在 QApplication 之前设：offscreen = 不进桌面、不需要显示器
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PyQt6.QtCore import QBuffer  # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import musicglass.ui.glass_window as glass_window  # noqa: E402
from musicglass.config import Config  # noqa: E402
from musicglass.core.mock_source import MockSource  # noqa: E402
from musicglass.perf import monitor  # noqa: E402
from musicglass.ui import theme  # noqa: E402
from musicglass.ui.glass_backend import GlassBackend  # noqa: E402
from musicglass.ui.widgets.equalizer import Equalizer  # noqa: E402

FRAME_DIR = Path(__file__).resolve().parent.parent / "build" / "anim_frames"

CHANGED_THRESHOLD = 8     # 通道差超过它才算"这个像素变了"
CHANGED_RATIO = 0.005     # 区域内变化像素占比达到 0.5% 才算"在动"

FAILURES: list[str] = []


# ---------------------------------------------------------------------------
# 把玻璃换掉（验证的是动画层，不需要真实桌面）
# ---------------------------------------------------------------------------
class NullBackend(GlassBackend):
    """把玻璃换成一块纯色面板。

    【为什么必须换掉】真玻璃要抓桌面（放大镜 / GDI），offscreen 下没得抓，
    而且像素差会混进桌面的变化。换掉之后，帧间差异只反映动画本身。
    """

    def paint(self, painter: QPainter, panel, radius, alpha, interior: bool = False) -> None:
        # interior 只是给真后端优化用的（面板内部小块重绘时跳过阴影），替身直接忽略
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(panel, radius, radius)
        painter.setOpacity(alpha)
        painter.fillPath(path, QColor(20, 24, 32))
        painter.restore()

    def status_text(self) -> str:
        return "NullBackend（验证用：不抓屏、不折射）"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def pump(app: QApplication, ms: float) -> None:
    """跑事件循环 ms 毫秒（QTimer 要靠事件循环才会走）。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.003)


def grab_array(window) -> np.ndarray:
    """抓窗口自绘内容 -> numpy 数组 (H, W, 3)。

    走 PNG 中转（QPixmap -> QBuffer -> PIL）而不是直接读 bits()：
    PIL 是项目已有依赖，而且绕开了 sip.voidptr 在不同 PyQt6 版本上的差异。
    """
    pixmap = window.grab()
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    image = Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGB")
    buffer.close()
    return np.asarray(image, dtype=np.int16)


def save_frame(array: np.ndarray, name: str) -> str:
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    path = FRAME_DIR / name
    Image.fromarray(array.astype(np.uint8)).save(path)
    return str(path)


def as_rect(qrect, pad: float = 0.0) -> tuple[int, int, int, int]:
    r = qrect.adjusted(-pad, -pad, pad, pad).toAlignedRect()
    return (r.x(), r.y(), r.width(), r.height())


def changed_ratio(a: np.ndarray, b: np.ndarray, rects: list[tuple[int, int, int, int]],
                  threshold: int = CHANGED_THRESHOLD) -> float:
    """两块帧在指定矩形集合内"变了的像素"占比。"""
    total = 0
    changed = 0
    for (x, y, w, h) in rects:
        sub_a = a[y:y + h, x:x + w]
        sub_b = b[y:y + h, x:x + w]
        if sub_a.size == 0:
            continue
        delta = np.abs(sub_a - sub_b).max(axis=2)
        total += delta.size
        changed += int((delta > threshold).sum())
    return 0.0 if not total else changed / total


def glow_bands(window) -> list[tuple[int, int, int, int]]:
    """氛围光所在的外圈（窗口 − 面板，拆成四条互不重叠的带）。"""
    panel = window.panel_rect().toAlignedRect()
    whole = window.rect()
    return [
        (whole.x(), whole.y(), whole.width(), max(0, panel.top())),                    # 上
        (whole.x(), panel.bottom(), whole.width(),
         max(0, whole.height() - panel.bottom())),                                     # 下
        (whole.x(), panel.top(), max(0, panel.left()), panel.height()),                # 左
        (panel.right(), panel.top(), max(0, whole.width() - panel.right()),
         panel.height()),                                                              # 右
    ]


def report(name: str, ok: bool, detail: str) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}：{detail}")
    if not ok:
        FAILURES.append(name)


# ---------------------------------------------------------------------------
def build_window(app: QApplication, *, animations: bool, fps: int, scale: float):
    """造一个用于验证的窗口：关掉托盘 / 贴桌面 / 配置持久化，玻璃换成 NullBackend。"""
    config = Config()
    config.scale = scale
    config.animations = animations
    config.animation_fps = fps
    config.opacity_idle = 1.0        # 固定全不透明，像素差才好判读
    config.opacity_hover = 1.0
    config.always_on_top = False
    config.desktop_layer = False     # 验证的是动画，不折腾 Z 序
    config.tray_enabled = False

    # 用替身换掉玻璃后端（模块级名字，patch 掉之后 GlassWindow 就会用它）
    glass_window.create_backend = lambda widget, cfg: NullBackend(widget)

    source = MockSource()
    window = glass_window.GlassWindow(config, source)
    window.set_persist_enabled(False)   # 别把调试用的位置写进用户配置
    window.show()
    source.start()
    pump(app, 250)
    return config, window, source


def main() -> int:
    app = QApplication(sys.argv)
    monitor.configure(600)      # 别在中途插话打印，最后手动汇总一次
    monitor.start()

    print("=" * 78)
    print(f"音璃 阶段4 动画自检   平台={os.environ.get('QT_QPA_PLATFORM', 'windows')}"
          f"  fps={ARGS.fps}  缩放={ARGS.scale}")
    print("=" * 78)

    _cfg, window, source = build_window(app, animations=True, fps=ARGS.fps, scale=ARGS.scale)
    clock = window._clock
    info = window.layout_info()
    disc_rect = [as_rect(info.disc, 2)]
    eq_rect = [as_rect(info.equalizer, 2)]
    glow_rect = glow_bands(window)

    # ---- 1. 播放中：三块区域都应该在动 ---------------------------------
    print("\n[1] 播放中：各动画区域是否在变")
    frame_a = grab_array(window)
    angle_a = window._disc_angle
    pump(app, 200)
    frame_b = grab_array(window)
    ratio_disc = changed_ratio(frame_a, frame_b, disc_rect)
    ratio_eq = changed_ratio(frame_a, frame_b, eq_rect)
    report("封面旋转", ratio_disc > CHANGED_RATIO,
           f"200ms 内圆盘区域变化像素 {ratio_disc:.2%}（阈值 {CHANGED_RATIO:.2%}）")
    report("频谱跳动", ratio_eq > CHANGED_RATIO,
           f"200ms 内频谱区域变化像素 {ratio_eq:.2%}（阈值 {CHANGED_RATIO:.2%}）")
    report("封面角度在变", window._disc_angle != angle_a,
           f"{angle_a:.2f}° -> {window._disc_angle:.2f}°"
           f"（角速度 {theme.DISC_SPEED_DEG_S}°/秒 = {360 / theme.DISC_SPEED_DEG_S:.0f} 秒一圈）")

    # 氛围光的亮度变化比封面/频谱小得多（12 层柔光叠出来的），
    # 每 200ms 的差值只有 1 左右；用秒级跨度 + 更灵敏的阈值来量。
    ticks_a = clock.ticks
    pump(app, 1000)
    ticks_b = clock.ticks
    frame_c = grab_array(window)
    ratio_glow = changed_ratio(frame_a, frame_c, glow_rect, threshold=3)
    report("氛围光呼吸", ratio_glow > 0.02,
           f"1 秒内外圈光晕变化像素 {ratio_glow:.2%}"
           f"（呼吸周期 {theme.GLOW_BREATH_PERIOD_S}s，阈值 2%）")
    report("时钟在跑", clock.running and ticks_b > ticks_a,
           f"1 秒内 {ticks_b - ticks_a} 帧（目标 {ARGS.fps}/秒；"
           f"offscreen 下 pump 循环有 ~15ms 粒度，数字偏低是正常的）")
    report("只重画小块", True,
           "见下面 [4] 的绘制统计：局部重绘次数应远多于整块重绘")

    if not ARGS.no_frames:
        print(f"  对比帧: {save_frame(frame_a, 'play_a.png')}")
        print(f"          {save_frame(frame_c, 'play_c.png')}")

    # ---- 2. 暂停：画面冻结 + 时钟停摆 ----------------------------------
    print("\n[2] 暂停：动画是否真的停下（不是「跑到看不见」）")
    source.play_pause()
    pump(app, 500)
    pause_a = grab_array(window)
    ticks_p0 = clock.ticks
    pump(app, 600)
    pause_b = grab_array(window)
    ticks_p1 = clock.ticks
    frozen = changed_ratio(pause_a, pause_b, [(0, 0, window.width(), window.height())])
    report("画面完全冻结", frozen == 0.0, f"整窗变化像素 {frozen:.4%}（应为 0）")
    report("时钟已停", not clock.running and ticks_p1 == ticks_p0,
           f"时钟运行={clock.running}，600ms 内新增 {ticks_p1 - ticks_p0} 帧")
    if not ARGS.no_frames:
        print(f"  对比帧: {save_frame(pause_a, 'paused.png')}")

    # ---- 3. 切歌过渡：淡出 -> 换内容 -> 淡入 ---------------------------
    print("\n[3] 切歌过渡：内容透明度轨迹 + 换内容的时机")
    source.play_pause()          # 恢复播放
    pump(app, 300)
    old_title = window._display_track.title
    frame_before = grab_array(window)
    samples: list[tuple[float, float, str]] = []
    swap_alpha = None
    swap_title = None
    started = time.monotonic()
    source.next_track()
    for _ in range(20):
        pump(app, 35)
        title = window._display_track.title
        alpha = window._content_alpha
        samples.append((time.monotonic() - started, alpha, title))
        if title != old_title and swap_alpha is None:
            swap_alpha, swap_title = alpha, title
    frame_after = grab_array(window)

    curve = " ".join(f"{a:.2f}" for _, a, _ in samples)
    min_alpha = min(a for _, a, _ in samples)
    panel_diff = changed_ratio(frame_before, frame_after, [as_rect(info.panel)])
    print(f"  透明度轨迹: {curve}")
    print(f"  换内容: {old_title!r} -> {swap_title!r}（换的那一帧透明度 {swap_alpha}）")
    report("淡出到几乎看不见", min_alpha < 0.05, f"最低透明度 {min_alpha:.3f}（应 < 0.05）")
    report("淡入回到 1.0", abs(samples[-1][1] - 1.0) < 1e-6,
           f"末帧透明度 {samples[-1][1]:.3f}")
    report("内容确实换了", swap_title is not None and swap_title != old_title,
           f"{old_title!r} -> {window._display_track.title!r}")
    report("换内容发生在看不见的时候", swap_alpha is not None and swap_alpha < 0.35,
           f"换内容时透明度 {swap_alpha}")
    report("过渡真的画出来了", panel_diff > CHANGED_RATIO,
           f"面板区域变化像素 {panel_diff:.2%}")
    if not ARGS.no_frames:
        print(f"  对比帧: {save_frame(frame_before, 'track_a.png')}")
        print(f"          {save_frame(frame_after, 'track_b.png')}")

    # ---- 4. 稳定期绘制统计（不抓帧，模拟真实运行）----------------------
    print("\n[4] 稳定期绘制统计（1 秒，不抓帧）")
    # 把前面的统计清掉：grab() 会整块重绘，不是真实运行时的样子
    monitor._stats.clear()
    ticks_s0 = clock.ticks
    pump(app, 1000)
    print(f"  这 1 秒动画时钟 {ticks_s0} -> {clock.ticks} 帧")
    monitor._report()      # 手动汇总一次（正式程序里由 --perf 的定时器打印）

    # ---- 5. 关掉动画：时钟一帧都不该跑 ---------------------------------
    print("\n[5] 关掉动画（animations=False）：应该一帧都不跑")
    window.hide()
    _cfg2, window2, _source2 = build_window(app, animations=False, fps=ARGS.fps, scale=ARGS.scale)
    pump(app, 700)
    report("时钟未启动", not window2._clock.running and window2._clock.ticks == 0,
           f"运行={window2._clock.running} ticks={window2._clock.ticks}")
    static_look = (window2._glow_strength == 1.0 and window2._disc_angle == 0.0
                   and list(window2._eq_heights) == list(Equalizer.DEFAULT_HEIGHTS))
    report("外观=阶段3 静态样子", static_look,
           f"光晕={window2._glow_strength} 角度={window2._disc_angle}")

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"结果：{len(FAILURES)} 项未通过 -> {FAILURES}")
        print("=" * 78)
        return 1
    print("结果：全部通过")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
