# -*- coding: utf-8 -*-
"""models.py 的单元测试：时间格式化、循环模式轮转、Track 派生属性。"""

from __future__ import annotations

import pytest

from musicglass.core.models import LoopMode, PlaybackState, Track, format_time


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0:00"),
        (5, "0:05"),
        (59, "0:59"),
        (60, "1:00"),
        (187, "3:07"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
        (187.9, "3:07"),        # 小数向下取整，不能四舍五入（否则进度条会出现 3:08 但还没播完）
        ("42", "0:42"),         # 字符串数字也要能处理（配置/JSON 里常见）
    ],
)
def test_format_time(seconds, expected):
    assert format_time(seconds) == expected


@pytest.mark.parametrize("bad", [None, -1, -99, "abc", object()])
def test_format_time_bad_input(bad):
    """坏输入一律给 "--:--"，绝不能让 UI 崩。"""
    assert format_time(bad) == "--:--"


def test_loop_mode_cycle():
    """点三下循环按钮应该回到起点。"""
    mode = LoopMode.LIST
    order = []
    for _ in range(3):
        mode = mode.next_mode()
        order.append(mode)
    assert order == [LoopMode.TRACK, LoopMode.SHUFFLE, LoopMode.LIST]


def test_loop_mode_labels():
    assert LoopMode.LIST.label == "列表循环"
    assert LoopMode.TRACK.label == "单曲循环"
    assert LoopMode.SHUFFLE.label == "随机播放"


def test_track_derived_fields():
    track = Track("起风了", "买辣椒也用券", "起风了", 325.0)
    assert track.duration_text == "5:25"
    assert track.subtitle == "买辣椒也用券 · 起风了"
    assert track.is_empty() is False


def test_track_without_album():
    track = Track("无名", "某歌手", "", 10.0)
    assert track.subtitle == "某歌手"      # 专辑为空时不该出现多余的 " · "


def test_track_empty_title_detection():
    assert Track().is_empty() is True
    assert Track("未知歌曲").is_empty() is True


def test_playback_state_labels():
    assert PlaybackState.PLAYING.label == "播放中"
    assert PlaybackState.PAUSED.label == "已暂停"
