# -*- coding: utf-8 -*-
"""面板内部布局计算。

把"谁在哪儿"全部集中在一个地方算，好处：
1. 缩放时只改 u（缩放单位），所有元素自动等比重排；
2. 命中测试（点哪个按钮）和绘制用的是同一套矩形，不会出现"画在这儿、点在别处"的 bug。

坐标系说明：所有矩形都是 **窗口（widget）坐标系**，不是面板坐标系。
因为窗口比面板大一圈（外面有 MARGIN 的透明留白放阴影和氛围光）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PyQt6.QtCore import QRectF

from . import theme


@dataclass
class PanelLayout:
    """一次布局计算的全部结果。"""

    panel: QRectF                    # 玻璃面板本体
    u: float                         # 缩放单位（1.0 = 基准尺寸）
    radius: float                    # 当前圆角
    disc: QRectF                     # 圆形封面
    title: QRectF                    # 歌名文本区
    artist: QRectF                   # 歌手文本区
    controls: QRectF                 # 控制按钮组的整体框
    buttons: list[QRectF] = field(default_factory=list)   # [上一首, 播放/暂停, 下一首, 循环]
    progress: QRectF = field(default_factory=QRectF)      # 进度条轨道
    elapsed: QRectF = field(default_factory=QRectF)       # 已播时间文本区
    total: QRectF = field(default_factory=QRectF)         # 总时长文本区
    equalizer: QRectF = field(default_factory=QRectF)     # 频谱条
    grip: QRectF = field(default_factory=QRectF)          # 右下角缩放手柄

    @classmethod
    def compute(cls, panel: QRectF) -> "PanelLayout":
        u = panel.height() / theme.BASE_PANEL_H
        pad = theme.PAD * u
        gap = theme.GAP * u

        # ---- 顶行高度：总高 - 上下内边距 - 底部行 - 行间距 ----
        bottom_h = theme.BOTTOM_H * u
        top_h = panel.height() - 2 * pad - bottom_h - theme.V_GAP * u

        # ---- 左侧：圆形封面（正方形，边长等于顶行高）----
        disc = QRectF(panel.left() + pad, panel.top() + pad, top_h, top_h)

        # ---- 右侧：控制按钮组 ----
        btn = theme.BUTTON * u
        btn_gap = gap * 0.16
        controls_w = 4 * btn + 3 * btn_gap
        controls_x = panel.right() - pad - controls_w
        controls = QRectF(controls_x, panel.top() + pad + (top_h - btn) / 2, controls_w, btn)
        buttons = [
            QRectF(controls_x + i * (btn + btn_gap), controls.top(), btn, btn)
            for i in range(4)
        ]

        # ---- 中间：歌名 + 歌手（宽度 = 封面右边 到 按钮组左边）----
        text_x = disc.right() + gap * 1.15
        text_w = max(40 * u, controls.left() - gap * 1.3 - text_x)
        title_h = theme.FONT_TITLE * u * 1.42
        title = QRectF(text_x, panel.top() + pad + 2.0 * u, text_w, title_h)
        artist = QRectF(text_x, title.bottom() + 0.5 * u, text_w, theme.FONT_ARTIST * u * 1.6)

        # ---- 底行：已播时间 | 进度条 | 总时长 | 频谱条 | 缩放手柄 ----
        row_top = panel.top() + pad + top_h + theme.V_GAP * u
        right = panel.right() - pad

        grip_size = 11 * u
        grip = QRectF(right - grip_size, panel.bottom() - pad - grip_size, grip_size, grip_size)

        eq_w = (theme.EQ_BARS * theme.EQ_BAR_W + (theme.EQ_BARS - 1) * theme.EQ_BAR_GAP) * u
        eq_h = bottom_h * 0.92
        eq_left = right - grip_size - 7 * u - eq_w
        equalizer = QRectF(eq_left, row_top + (bottom_h - eq_h) / 2, eq_w, eq_h)

        time_w = 30 * u
        total = QRectF(equalizer.left() - 7 * u - time_w, row_top, time_w, bottom_h)
        elapsed = QRectF(panel.left() + pad, row_top, time_w, bottom_h)

        bar_x = elapsed.right() + 5 * u
        bar_w = max(20 * u, total.left() - 6 * u - bar_x)
        progress = QRectF(
            bar_x, row_top + (bottom_h - theme.PROGRESS_H * u) / 2, bar_w, theme.PROGRESS_H * u
        )

        return cls(
            panel=panel, u=u, radius=theme.RADIUS * u, disc=disc, title=title, artist=artist,
            controls=controls, buttons=buttons, progress=progress, elapsed=elapsed,
            total=total, equalizer=equalizer, grip=grip,
        )
