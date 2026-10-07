# -*- coding: utf-8 -*-
"""进度条：底槽 + 渐变已播段 + 圆形滑块。

阶段1 只负责"显示"；命中测试（点/拖进度条跳转）的逻辑写在这里，
阶段3 接到真实播放器时直接用。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen

from .. import theme


class ProgressBar:
    """一个细长的圆角进度条。"""

    # 命中区域比视觉高度大一圈，否则这么细的条根本点不中
    HIT_PADDING = 9.0

    def paint(self, painter: QPainter, rect: QRectF, position_s: float, duration_s: float,
              u: float, *, hovered: bool = False) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)

        radius = rect.height() / 2
        # 底槽
        painter.setBrush(theme.PROGRESS_TRACK)
        painter.drawRoundedRect(rect, radius, radius)

        ratio = self.ratio(position_s, duration_s)
        if ratio <= 0:
            painter.restore()
            return

        fill_w = max(rect.height(), rect.width() * ratio)
        fill = QRectF(rect.left(), rect.top(), fill_w, rect.height())
        # 已播部分：薄荷 -> 青蓝渐变
        gradient = QLinearGradient(rect.left(), 0, rect.right(), 0)
        gradient.setColorAt(0.0, theme.PROGRESS_FILL_FROM)
        gradient.setColorAt(1.0, theme.PROGRESS_FILL_TO)
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, radius, radius)

        # 滑块：只在鼠标悬停时显示，平时保持清爽
        if hovered:
            knob_r = max(2.5, rect.height() * 1.35)
            knob = QPointF(fill.right(), rect.center().y())
            painter.setBrush(QColor(255, 255, 255, 60))
            painter.drawEllipse(knob, knob_r * 1.55, knob_r * 1.55)
            painter.setBrush(theme.PROGRESS_KNOB)
            painter.drawEllipse(knob, knob_r, knob_r)
            painter.setPen(QPen(QColor(0, 0, 0, 40), 0.8))
            painter.drawEllipse(knob, knob_r, knob_r)
        painter.restore()

    # ------------------------------------------------------------------
    @staticmethod
    def ratio(position_s: float, duration_s: float) -> float:
        """已播比例，永远落在 [0, 1]，duration 为 0 时返回 0（不除零）。"""
        if not duration_s or duration_s <= 0:
            return 0.0
        return max(0.0, min(1.0, float(position_s) / float(duration_s)))

    def hit_test(self, pos: QPointF, rect: QRectF) -> bool:
        """点到进度条（含扩大的命中区）了吗。"""
        return rect.adjusted(-self.HIT_PADDING, -self.HIT_PADDING,
                             self.HIT_PADDING, self.HIT_PADDING).contains(pos)

    def seconds_at(self, x: float, rect: QRectF, duration_s: float) -> float:
        """给定 x 坐标，算出对应的秒数（拖拽跳转用）。"""
        if rect.width() <= 0:
            return 0.0
        ratio = max(0.0, min(1.0, (x - rect.left()) / rect.width()))
        return ratio * float(duration_s or 0.0)
