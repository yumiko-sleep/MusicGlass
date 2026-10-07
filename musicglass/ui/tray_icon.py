# -*- coding: utf-8 -*-
"""托盘图标：纯代码绘制。

延续项目的原则：仓库里不放任何二进制素材。
好处是图标永远清晰（每个系统缩放档都现画一份）、改配色不用重新导出资源。

图标样子：一块"玻璃小方块"（薄荷绿→青蓝渐变）+ 白色音符。
    播放中 = 彩色渐变；暂停/停止 = 去饱和的灰蓝。
这样一眼就能从托盘区看出 QQ音乐 是不是在放。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPixmap

from . import icons, theme

# Windows 托盘在各种 DPI/缩放档下会挑这些尺寸里最合适的一张
ICON_SIZES = (16, 20, 24, 32, 48, 64)


def render_pixmap(size: int = 32, playing: bool = True) -> QPixmap:
    """画一张指定尺寸的托盘图标位图。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # 留一点内边距：Windows 的托盘图标区本来就窄，画满会显得很"堵"
    inset = max(1.0, size * 0.055)
    body = QRectF(inset, inset, size - 2 * inset, size - 2 * inset)
    radius = body.width() * 0.28

    gradient = QLinearGradient(body.topLeft(), body.bottomRight())
    if playing:
        gradient.setColorAt(0.0, theme.MINT)
        gradient.setColorAt(1.0, theme.CYAN)
    else:
        # 暂停态：同样形状但去饱和，远处也能一眼区分
        gradient.setColorAt(0.0, QColor(150, 172, 178))
        gradient.setColorAt(1.0, QColor(96, 118, 132))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(body, radius, radius)

    # 顶部一条淡淡的高光，模拟玻璃的菲涅尔反射
    highlight = QLinearGradient(body.topLeft(), body.bottomLeft())
    highlight.setColorAt(0.0, QColor(255, 255, 255, 96))
    highlight.setColorAt(0.45, QColor(255, 255, 255, 0))
    painter.setBrush(highlight)
    painter.drawRoundedRect(body, radius, radius)

    # 白色音符（复用 ui/icons 里的矢量绘制）
    glyph = QRectF(0, 0, body.width() * 0.72, body.height() * 0.72)
    glyph.moveCenter(body.center())
    # 小尺寸下线条要稍微加粗，否则 16px 时糊成一团
    icons.paint_note(painter, glyph, QColor(255, 255, 255, 242))
    painter.end()
    return pixmap


def build_icon(playing: bool = True) -> QIcon:
    """造一个包含多种尺寸的 QIcon（Windows 会按当前缩放自动选）。"""
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(render_pixmap(size, playing))
    return icon
