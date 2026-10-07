# -*- coding: utf-8 -*-
"""布局与自绘部件里"纯数学"部分的测试。

这些测试不需要 QApplication（只用 QRectF/QPointF），所以跑得飞快，
而且能在没有显示器的环境里跑 —— 回归成本极低。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPointF, QRectF

from musicglass.ui import theme
from musicglass.ui.layout import PanelLayout
from musicglass.ui.widgets.equalizer import Equalizer
from musicglass.ui.widgets.progress_bar import ProgressBar


def make_layout(scale: float = 1.0) -> PanelLayout:
    panel = QRectF(
        theme.MARGIN, theme.MARGIN,
        theme.BASE_PANEL_W * scale, theme.BASE_PANEL_H * scale,
    )
    return PanelLayout.compute(panel)


def test_layout_units():
    assert make_layout(1.0).u == pytest.approx(1.0)
    assert make_layout(2.0).u == pytest.approx(2.0)
    assert make_layout(0.8).u == pytest.approx(0.8)


def test_disc_is_square_and_inside_panel():
    info = make_layout()
    assert info.disc.width() == pytest.approx(info.disc.height())   # 必须是正圆
    assert info.panel.contains(info.disc)


def test_controls_inside_panel_and_ordered_left_to_right():
    info = make_layout()
    assert len(info.buttons) == 4
    for rect in info.buttons:
        assert info.panel.contains(rect)
    lefts = [r.left() for r in info.buttons]
    assert lefts == sorted(lefts)                                   # 从左到右排列


def test_text_area_does_not_overlap_disc_or_controls():
    info = make_layout()
    assert info.title.left() >= info.disc.right()
    assert info.title.right() <= info.controls.left() + 0.01


def test_bottom_row_elements_inside_panel_and_not_overlapping():
    info = make_layout()
    for rect in (info.elapsed, info.progress, info.total, info.equalizer, info.grip):
        assert info.panel.contains(rect), f"{rect} 跑出面板了"
    assert info.progress.right() <= info.total.left() + 0.01
    assert info.total.right() <= info.equalizer.left() + 1.0


@pytest.mark.parametrize("scale", [0.55, 1.0, 1.5, 2.2])
def test_layout_survives_all_scales(scale):
    """缩放范围内所有尺寸都要排得开（不能出现负宽度）。"""
    info = make_layout(scale)
    assert info.progress.width() > 0
    assert info.title.width() > 0
    assert info.artist.width() > 0


# ---------------------------------------------------------------------------
# 进度条数学
# ---------------------------------------------------------------------------
def test_progress_ratio():
    assert ProgressBar.ratio(0, 100) == 0.0
    assert ProgressBar.ratio(50, 100) == 0.5
    assert ProgressBar.ratio(200, 100) == 1.0       # 超出也要夹住
    assert ProgressBar.ratio(-5, 100) == 0.0
    assert ProgressBar.ratio(10, 0) == 0.0          # 时长未知时不能除零


def test_progress_seconds_at():
    bar = ProgressBar()
    rect = QRectF(0, 0, 200, 4)
    assert bar.seconds_at(0, rect, 200) == pytest.approx(0.0)
    assert bar.seconds_at(100, rect, 200) == pytest.approx(100.0)
    assert bar.seconds_at(400, rect, 200) == pytest.approx(200.0)   # 夹在右端


def test_progress_hit_test_expands_target():
    bar = ProgressBar()
    rect = QRectF(0, 0, 200, 4)
    # 视觉上只有 4px 高，但命中区要扩大到能点中
    assert bar.hit_test(QPointF(100, 12), rect) is True
    assert bar.hit_test(QPointF(100, 60), rect) is False


# ---------------------------------------------------------------------------
# 频谱条
# ---------------------------------------------------------------------------
def test_equalizer_wave_heights_range():
    for phase in range(0, 200, 7):
        heights = Equalizer.wave_heights(phase / 10.0)
        assert len(heights) == theme.EQ_BARS
        assert all(0.0 < value <= 1.0 for value in heights)


def test_equalizer_wave_changes_over_time():
    first = Equalizer.wave_heights(0.0)
    later = Equalizer.wave_heights(1.7)
    assert first != later          # 会动，才有"在跳"的观感
