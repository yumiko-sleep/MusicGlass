# -*- coding: utf-8 -*-
"""歌名 + 歌手文本，以及一个"带描影的文字"绘制工具。

为什么需要描影：
    玻璃是透明的，背后可能是纯白网页、也可能是深色游戏画面。
    纯白文字压在亮背景上会直接"消失"。做法是先画一层半透明黑影、再画正文，
    这样无论底图明暗，文字都有稳定对比度。
    比起给整个面板加暗色蒙版，描影只影响文字所在的那一小块，不破坏玻璃的干净感。

长标题用 elidedText 做省略号截断（阶段1 先不滚动，阶段4 再给超长标题加跑马灯）。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter

from .. import theme

# 描影的相对偏移（按缩放单位 u 计算）与透明度
SHADOW_OFFSET_X = 0.03
SHADOW_OFFSET_Y = 0.075
SHADOW_ALPHA = 110


def draw_text(painter: QPainter, rect: QRectF, text: str, font: QFont, color: QColor,
              u: float, *, align=Qt.AlignmentFlag.AlignLeft, elide: bool = True) -> None:
    """画一行文字（可省略号截断），自动带一层暗色描影保证可读性。"""
    flags = int(align | Qt.AlignmentFlag.AlignVCenter)
    if elide:
        text = QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width()))

    painter.save()
    painter.setFont(font)

    # 第一层：黑影，向右下偏移一点点（越小越像"内描影"，越大越像"投影"）
    painter.setPen(QColor(0, 0, 0, SHADOW_ALPHA))
    painter.drawText(rect.translated(SHADOW_OFFSET_X * u, SHADOW_OFFSET_Y * u), flags, text)

    # 第二层：正文
    painter.setPen(color)
    painter.drawText(rect, flags, text)
    painter.restore()


class TextPanel:
    """两行文本：歌名（加粗、高对比）+ 歌手 · 专辑（半透明）。"""

    def paint(self, painter: QPainter, title_rect: QRectF, artist_rect: QRectF,
              track, u: float) -> None:
        title = (getattr(track, "title", "") or "未知歌曲") if track is not None else "未知歌曲"
        subtitle = (getattr(track, "subtitle", "") or "") if track is not None else ""

        draw_text(painter, title_rect, title, theme.font(theme.FONT_TITLE * u, bold=True),
                  theme.TEXT_PRIMARY, u)

        if subtitle:
            draw_text(painter, artist_rect, subtitle, theme.font(theme.FONT_ARTIST * u),
                      theme.TEXT_SECONDARY, u)
