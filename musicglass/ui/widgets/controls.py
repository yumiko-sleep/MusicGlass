# -*- coding: utf-8 -*-
"""播放控制按钮：上一首 / 播放-暂停 / 下一首 / 循环模式。

按钮不用 QPushButton，而是自己画矩形 + 命中测试。原因：
- 十几个 QPushButton 会让这个小窗口的重绘变重（每个都是独立控件 + 事件循环）；
- 自绘才能做出"悬停时淡淡一圈圆形底"这种效果，且样式完全可控；
- 按钮和别的元素共用同一套坐标，缩放时不会错位。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter

from .. import icons
from .. import theme
from ...core.models import LoopMode, PlaybackState

# 按钮索引（顺序固定，命中测试返回的就是这个）
BTN_PREV = 0
BTN_PLAY = 1
BTN_NEXT = 2
BTN_LOOP = 3


class Controls:
    """四个按钮的绘制与命中测试。"""

    def paint(self, painter: QPainter, buttons: list[QRectF], state: PlaybackState,
              loop_mode: LoopMode, u: float, *, hovered: int = -1) -> None:
        if len(buttons) < 4:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        for index, rect in enumerate(buttons[:4]):
            name, once, color = self._icon_spec(index, state, loop_mode)

            # 悬停：画一个淡圆底
            if index == hovered:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.BUTTON_HOVER_BG)
                painter.drawRoundedRect(rect.adjusted(2 * u, 2 * u, -2 * u, -2 * u),
                                        rect.width() * 0.36, rect.width() * 0.36)

            side = rect.width() * 0.56
            icon_rect = QRectF(0, 0, side, side)
            icon_rect.moveCenter(QPointF(rect.center().x(), rect.center().y()))
            # 先画一层暗色描影：图标压在亮背景上也不会"消失"（和文字同一个道理）
            icons.paint_icon(painter, name, icon_rect.translated(0.03 * u, 0.075 * u),
                             QColor(0, 0, 0, 110), once=once)
            icons.paint_icon(painter, name, icon_rect, color, once=once)
        painter.restore()

    # ------------------------------------------------------------------
    def _icon_spec(self, index: int, state: PlaybackState, loop_mode: LoopMode):
        """返回 (图标名, 是否单曲循环, 颜色)。"""
        if index == BTN_PLAY:
            if state is PlaybackState.PLAYING:
                return "pause", False, theme.ICON_HOVER
            return "play", False, theme.ICON_HOVER
        if index == BTN_LOOP:
            if loop_mode is LoopMode.TRACK:
                return "repeat", True, theme.ICON_ACTIVE
            if loop_mode is LoopMode.SHUFFLE:
                return "shuffle", False, theme.ICON_ACTIVE
            return "repeat", False, theme.ICON_IDLE
        if index == BTN_PREV:
            return "prev", False, theme.ICON_IDLE
        return "next", False, theme.ICON_IDLE

    @staticmethod
    def hit(pos: QPointF, buttons: list[QRectF]) -> int:
        """命中测试：返回按钮下标，没点中任何按钮返回 -1。"""
        for index, rect in enumerate(buttons):
            if rect.contains(pos):
                return index
        return -1

    @staticmethod
    def tooltip(index: int, loop_mode: LoopMode) -> str:
        """鼠标悬停该显示什么（阶段5 的托盘/提示可以用）。"""
        return {
            BTN_PREV: "上一首",
            BTN_PLAY: "播放 / 暂停",
            BTN_NEXT: "下一首",
            BTN_LOOP: f"循环模式：{loop_mode.label}",
        }.get(index, "")
