# -*- coding: utf-8 -*-
"""假数据源（阶段1 用）。

它的价值和"真"数据源完全一样：UI 只吃 MediaSource 的信号，
所以先用它把界面、拖动、缩放、透明度、进度条全部跑通，
阶段2 换成 SMTC 时，UI 代码一行都不用改。

封面不是图片文件，而是用 QPainter 现画的（渐变 + 唱片纹路），
这样项目里不需要带任何二进制素材，也就没有任何版权问题。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QPointF, QRectF, QTimer, Qt
from PyQt6.QtGui import (
    QColor,
    QConicalGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .models import LoopMode, PlaybackState, Track
from .source import MediaSource

COVER_SIZE = 256  # 假封面边长（像素）


def make_fake_cover(index: int, size: int = COVER_SIZE) -> QPixmap:
    """用代码画一张"专辑封面"：斜向渐变 + 一圈唱片纹路 + 中心高光。

    index 决定色相偏移，让每首歌的封面颜色不同，方便一眼看出换歌了。
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 色相偏移：从薄荷绿开始绕色环取色，让假封面跟主题（薄荷绿→青蓝）协调
    hue = (150 + index * 47) % 360
    color_a = QColor.fromHsv(hue, 150, 235)
    color_b = QColor.fromHsv((hue + 70) % 360, 175, 175)

    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, color_a)
    gradient.setColorAt(1.0, color_b)
    painter.fillRect(0, 0, size, size, gradient)

    # 唱片纹路：一圈圈半透明白色细线
    painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    center = size / 2
    center_point = QPointF(center, center)
    for step in range(10, int(center), 9):
        painter.drawEllipse(center_point, step, step)

    # 中心高光：锥形渐变，模拟唱片反光
    conical = QConicalGradient(center, center, hue)
    conical.setColorAt(0.0, QColor(255, 255, 255, 60))
    conical.setColorAt(0.5, QColor(255, 255, 255, 0))
    conical.setColorAt(1.0, QColor(255, 255, 255, 60))
    painter.setBrush(conical)
    painter.drawEllipse(center_point, center, center)

    # 两片扇形反光（模拟灯光打在黑胶上的亮带）—— 负责让“旋转”看得出来。
    # 【为什么必须加这个】同心圆纹路绕圆心旋转是“转完还是自己”，
    # 画面上几乎没有变化（实测 200ms 只有 0.06% 的像素变了）——
    # 用户会以为封面没转。只有不对称的元素才能看出转动。
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 40))
    for start_angle in (18.0, 198.0):
        wedge = QPainterPath()
        wedge.moveTo(center_point)
        wedge.arcTo(QRectF(0, 0, size, size), start_angle, 32.0)
        wedge.closeSubpath()
        painter.drawPath(wedge)

    painter.end()
    return pixmap


class MockSource(MediaSource):
    """假数据源：4 首歌循环播放，进度条真实走动。"""

    TICK_MS = 200  # 进度刷新间隔（200ms 足够顺滑，也不浪费 CPU）

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._index = 0
        self._tracks: list[Track] = [
            Track("泡沫", "邓紫棋", "Xposed", 259.0, make_fake_cover(0), "MockPlayer"),
            Track("夜空中最亮的星（Live 巡演特别版）", "逃跑计划", "世界", 248.0,
                  make_fake_cover(1), "MockPlayer"),
            Track("Count On Me", "Connie Talbot", "Beautiful World", 187.0,
                  make_fake_cover(2), "MockPlayer"),
            Track("起风了", "买辣椒也用券", "起风了", 325.0, make_fake_cover(3), "MockPlayer"),
        ]
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._emit_track(self._tracks[self._index])
        self._emit_state(PlaybackState.PLAYING)
        self._emit_loop(LoopMode.LIST)
        self._emit_position(0.0)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _on_tick(self) -> None:
        """每次 tick 推进进度；放完自动下一首。"""
        if self._state is not PlaybackState.PLAYING:
            return
        position = self._position + self.TICK_MS / 1000.0
        duration = self._track.duration_s or 0.0
        if duration and position >= duration:
            self.next_track()
            return
        self._emit_position(position)

    # ------------------------------------------------------------------
    # 控制指令
    # ------------------------------------------------------------------
    def play_pause(self) -> None:
        if self._state is PlaybackState.PLAYING:
            self._emit_state(PlaybackState.PAUSED)
        else:
            self._emit_state(PlaybackState.PLAYING)

    def next_track(self) -> None:
        if self._loop is LoopMode.TRACK:
            # 单曲循环：不换歌，从头再来
            self._emit_position(0.0)
            return
        self._index = (self._index + 1) % len(self._tracks)
        if self._loop is LoopMode.SHUFFLE:
            self._index = (self._index + 2) % len(self._tracks)  # 假装随机
        self._emit_track(self._tracks[self._index])
        self._emit_position(0.0)

    def previous_track(self) -> None:
        self._index = (self._index - 1) % len(self._tracks)
        self._emit_track(self._tracks[self._index])
        self._emit_position(0.0)

    def seek(self, position_s: float) -> None:
        duration = self._track.duration_s or 0.0
        self._emit_position(max(0.0, min(position_s, duration)))

    def set_loop_mode(self, mode: LoopMode) -> None:
        self._emit_loop(mode)

    def cycle_loop_mode(self) -> None:
        self.set_loop_mode(self._loop.next_mode())
