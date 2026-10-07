# -*- coding: utf-8 -*-
"""装饰性频谱条：不读真实音频，只是随机上下跳（阶段4 让它动起来）。

阶段1 先用一组固定高度把位置和观感定下来。
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainter

from .. import theme


class Equalizer:
    """一排小竖条，从底部向上长。"""

    # 阶段1 的静态高度（0~1）。阶段4 会用一个定时器不断刷新成随机值。
    DEFAULT_HEIGHTS = (0.42, 0.78, 0.55, 1.0, 0.63)

    def paint(self, painter: QPainter, rect: QRectF, u: float, heights=None, *,
              active: bool = True) -> None:
        values = list(heights) if heights else list(self.DEFAULT_HEIGHTS)
        bar_w = theme.EQ_BAR_W * u
        gap = theme.EQ_BAR_GAP * u
        color = theme.EQ_ACTIVE if active else theme.EQ_IDLE

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)

        count = min(len(values), theme.EQ_BARS)
        for index in range(count):
            value = max(0.08, min(1.0, float(values[index])))
            height = rect.height() * value
            x = rect.left() + index * (bar_w + gap)
            bar = QRectF(x, rect.bottom() - height, bar_w, height)
            painter.drawRoundedRect(bar, bar_w / 2, bar_w / 2)
        painter.restore()

    @staticmethod
    def wave_heights(phase: float, count: int = theme.EQ_BARS) -> list[float]:
        """用三个不同频率的正弦波叠加，生成"像在跳又不抽搐"的高度曲线。

        阶段4 的定时器只要不断增大 phase 就能让频谱条动起来。
        用正弦而不是纯随机，是为了让相邻两根条之间有相关性，看起来更像音频。
        """
        values = []
        for index in range(count):
            value = (
                0.50
                + 0.28 * math.sin(phase * 1.7 + index * 1.05)
                + 0.16 * math.sin(phase * 3.1 + index * 2.3)
                + 0.06 * math.sin(phase * 6.3 + index * 0.7)
            )
            values.append(max(0.12, min(1.0, value)))
        return values
