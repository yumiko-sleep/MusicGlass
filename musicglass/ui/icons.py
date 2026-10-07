# -*- coding: utf-8 -*-
"""矢量图标：全部用 QPainterPath 现画。

为什么不放 png/svg 文件？
1. 项目里不带任何二进制素材，开源仓库干净、也没有版权问题；
2. 图标跟着面板尺寸等比缩放，永远清晰，不用准备 @2x/@3x；
3. 颜色可以直接跟着状态（悬停/激活）变，不用准备多套图标。

每个函数签名统一是 paint_xxx(painter, rect, color)，rect 是图标的外接方框。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen

# ---------------------------------------------------------------------------
# 基础图形
# ---------------------------------------------------------------------------


def _triangle(cx: float, cy: float, size: float, direction: int) -> QPainterPath:
    """造一个指向左(-1)或右(+1)的三角，重心大致落在 (cx, cy)。"""
    half_h = size * 0.52
    width = size * 0.86
    path = QPainterPath()
    if direction > 0:
        path.moveTo(cx - width * 0.45, cy - half_h)
        path.lineTo(cx + width * 0.55, cy)
        path.lineTo(cx - width * 0.45, cy + half_h)
    else:
        path.moveTo(cx + width * 0.45, cy - half_h)
        path.lineTo(cx - width * 0.55, cy)
        path.lineTo(cx + width * 0.45, cy + half_h)
    path.closeSubpath()
    return path


def _bar(cx: float, cy: float, width: float, height: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(cx - width / 2, cy - height / 2, width, height), width / 2, width / 2)
    return path


def _arrow_head(tip: QPointF, angle_rad: float, size: float) -> QPainterPath:
    """在 tip 处造一个箭头三角，angle_rad 是箭头"指向"的方向（屏幕坐标，y 向下）。"""
    path = QPainterPath()
    back = size
    left = QPointF(
        tip.x() - math.cos(angle_rad) * back + math.sin(angle_rad) * size * 0.55,
        tip.y() - math.sin(angle_rad) * back - math.cos(angle_rad) * size * 0.55,
    )
    right = QPointF(
        tip.x() - math.cos(angle_rad) * back - math.sin(angle_rad) * size * 0.55,
        tip.y() - math.sin(angle_rad) * back + math.cos(angle_rad) * size * 0.55,
    )
    path.moveTo(tip)
    path.lineTo(left)
    path.lineTo(right)
    path.closeSubpath()
    return path


# ---------------------------------------------------------------------------
# 控制按钮图标
# ---------------------------------------------------------------------------


def paint_play(painter: QPainter, rect: QRectF, color: QColor) -> None:
    size = min(rect.width(), rect.height())
    painter.fillPath(
        _triangle(rect.center().x() + size * 0.05, rect.center().y(), size, +1), color
    )


def paint_pause(painter: QPainter, rect: QRectF, color: QColor) -> None:
    size = min(rect.width(), rect.height())
    cx, cy = rect.center().x(), rect.center().y()
    bar_w = size * 0.20
    gap = size * 0.20
    painter.fillPath(_bar(cx - gap / 2 - bar_w / 2, cy, bar_w, size * 0.82), color)
    painter.fillPath(_bar(cx + gap / 2 + bar_w / 2, cy, bar_w, size * 0.82), color)


def paint_prev(painter: QPainter, rect: QRectF, color: QColor) -> None:
    size = min(rect.width(), rect.height())
    cx, cy = rect.center().x(), rect.center().y()
    painter.fillPath(_triangle(cx + size * 0.14, cy, size, -1), color)
    painter.fillPath(_bar(cx - size * 0.36, cy, size * 0.13, size * 0.78), color)


def paint_next(painter: QPainter, rect: QRectF, color: QColor) -> None:
    size = min(rect.width(), rect.height())
    cx, cy = rect.center().x(), rect.center().y()
    painter.fillPath(_triangle(cx - size * 0.14, cy, size, +1), color)
    painter.fillPath(_bar(cx + size * 0.36, cy, size * 0.13, size * 0.78), color)


def paint_repeat(painter: QPainter, rect: QRectF, color: QColor, *, once: bool = False) -> None:
    """循环图标：一个带缺口的圆环 + 箭头；once=True 时中间加一个 "1"。"""
    size = min(rect.width(), rect.height())
    pen = QPen(color, max(1.2, size * 0.115))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.save()
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    inner = QRectF(0, 0, size * 0.72, size * 0.72)
    inner.moveCenter(rect.center())
    # Qt 的角度单位是 1/16 度，0° 在 3 点方向、逆时针为正
    painter.drawArc(inner, 55 * 16, 285 * 16)

    # 箭头落在弧的起点（55°）附近，沿切线方向指向"逆时针前进"的方向
    radius = inner.width() / 2
    theta = math.radians(55)
    tip = QPointF(
        inner.center().x() + radius * math.cos(theta),
        inner.center().y() - radius * math.sin(theta),
    )
    painter.fillPath(_arrow_head(tip, theta - math.pi / 2, size * 0.19), color)

    if once:
        font = QFont()
        font.setPixelSize(max(6, int(size * 0.46)))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(inner, int(Qt.AlignmentFlag.AlignCenter), "1")
    painter.restore()


def paint_shuffle(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """随机图标：两条交叉的箭头。"""
    size = min(rect.width(), rect.height())
    pen = QPen(color, max(1.2, size * 0.115))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.save()
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    left = rect.center().x() - size * 0.42
    right = rect.center().x() + size * 0.42
    top = rect.center().y() - size * 0.26
    bottom = rect.center().y() + size * 0.26

    path = QPainterPath()
    path.moveTo(left, bottom)
    path.cubicTo(rect.center().x(), bottom, rect.center().x(), top, right, top)
    painter.drawPath(path)
    painter.fillPath(_arrow_head(QPointF(right, top), 0.0, size * 0.20), color)

    path = QPainterPath()
    path.moveTo(left, top)
    path.cubicTo(rect.center().x(), top, rect.center().x(), bottom, right, bottom)
    painter.drawPath(path)
    painter.fillPath(_arrow_head(QPointF(right, bottom), 0.0, size * 0.20), color)
    painter.restore()


def paint_note(painter: QPainter, rect: QRectF, color: QColor) -> None:
    """一个音符图形（用于"未检测到播放器"的空状态）。"""
    size = min(rect.width(), rect.height())
    pen = QPen(color, max(1.2, size * 0.10))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.save()
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    stem_x = rect.center().x() + size * 0.16
    painter.drawLine(QPointF(stem_x, rect.center().y() + size * 0.26),
                     QPointF(stem_x, rect.center().y() - size * 0.34))
    path = QPainterPath()
    path.moveTo(stem_x, rect.center().y() - size * 0.34)
    path.cubicTo(stem_x + size * 0.16, rect.center().y() - size * 0.16,
                 stem_x + size * 0.26, rect.center().y() - size * 0.06,
                 stem_x + size * 0.30, rect.center().y() + size * 0.06)
    painter.drawPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QPointF(stem_x - size * 0.06, rect.center().y() + size * 0.30),
                        size * 0.15, size * 0.12)
    painter.restore()


# 名字 -> 绘制函数。UI 只认名字，方便按循环模式切换图标
ICONS = {
    "play": paint_play,
    "pause": paint_pause,
    "prev": paint_prev,
    "next": paint_next,
    "repeat": paint_repeat,
    "shuffle": paint_shuffle,
    "note": paint_note,
}


def paint_icon(painter: QPainter, name: str, rect: QRectF, color: QColor, *,
               once: bool = False) -> None:
    """按名字画图标。未知名字画一个占位圆，方便一眼发现打错字。"""
    if name == "repeat":
        paint_repeat(painter, rect, color, once=once)
        return
    func = ICONS.get(name)
    if func is None:
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)
        return
    func(painter, rect, color)
