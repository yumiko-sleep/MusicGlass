# -*- coding: utf-8 -*-
"""SMTC 数据源的纯逻辑测试。

这些函数是"读到的原始值 -> 我们自己的模型"的翻译层，也是最容易出错的地方
（时间单位、过渡态、边界钳制）。它们不碰 WinRT，所以能在任何环境下跑。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from musicglass.core.models import LoopMode, PlaybackState
from musicglass.core.smtc_source import (
    PLACEHOLDER_TRACK,
    SMTCSnapshot,
    app_matches,
    interpolate,
    map_status,
    seconds_of,
)


# ---------------------------------------------------------------------------
# 时间换算（踩过坑的地方）
# ---------------------------------------------------------------------------
def test_seconds_of_timedelta():
    """pywinrt 把 WinRT 的 TimeSpan 转成了 datetime.timedelta，必须走 total_seconds。"""
    assert seconds_of(timedelta(seconds=106.45)) == pytest.approx(106.45)
    assert seconds_of(timedelta(0)) == pytest.approx(0.0)


def test_seconds_of_nanoseconds():
    """也兼容"纳秒整数"形态（别的绑定会这么给）。"""
    assert seconds_of(1_064_500_000) == pytest.approx(106.45)


def test_seconds_of_bad_values():
    assert seconds_of(None) is None
    assert seconds_of("abc") is None


# ---------------------------------------------------------------------------
# 状态映射
# ---------------------------------------------------------------------------
def test_map_status_basic():
    assert map_status("PLAYING", PlaybackState.UNKNOWN) is PlaybackState.PLAYING
    assert map_status("PAUSED", PlaybackState.PLAYING) is PlaybackState.PAUSED


def test_map_status_stopped_is_paused():
    """QQ音乐换歌瞬间会闪一下 STOPPED；归到 PAUSED 可以避免图标乱跳。"""
    assert map_status("STOPPED", PlaybackState.PLAYING) is PlaybackState.PAUSED


@pytest.mark.parametrize("name", ["CHANGING", "", "什么鬼"])
def test_map_status_keeps_previous(name):
    """过渡态/未知值一律保持上一个状态，不要瞎改。"""
    assert map_status(name, PlaybackState.PLAYING) is PlaybackState.PLAYING
    assert map_status(name, PlaybackState.PAUSED) is PlaybackState.PAUSED


# ---------------------------------------------------------------------------
# 进度插值
# ---------------------------------------------------------------------------
def test_interpolate_playing_advances():
    # 基准进度 10 秒（基准时刻 100.0），到 103.0 时已过 3 秒 -> 显示 13 秒
    result = interpolate(10.0, base_monotonic=100.0, now_monotonic=103.0,
                         playing=True, duration=200.0)
    assert result == pytest.approx(13.0)


def test_interpolate_paused_frozen():
    result = interpolate(10.0, 100.0, 999.0, playing=False, duration=200.0)
    assert result == pytest.approx(10.0)


def test_interpolate_clamped_to_duration():
    """插值不能超过总时长（否则进度条会冲出去）。"""
    assert interpolate(199.0, 100.0, 110.0, True, 200.0) == pytest.approx(200.0)


def test_interpolate_never_negative():
    assert interpolate(1.0, 100.0, 90.0, True, 200.0) >= 0.0


def test_interpolate_unknown_duration_not_clamped():
    result = interpolate(5.0, 0.0, 10.0, True, 0.0)
    assert result == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# 会话匹配
# ---------------------------------------------------------------------------
def test_app_matches():
    assert app_matches("QQMusic.exe", ("qqmusic",)) is True
    assert app_matches("Tencent.QQMusic_8wekyb3d8bbwe!App", ("qqmusic",)) is True
    assert app_matches("chrome.exe", ("qqmusic",)) is False
    assert app_matches("", ("qqmusic",)) is False


def test_snapshot_identity_changes_per_song():
    """换歌靠 identity 判断，所以它必须随歌名/歌手/专辑变化。"""
    a = SMTCSnapshot(title="断了的弦", artist="周杰伦", album="寻找周杰伦")
    b = SMTCSnapshot(title="断了的弦", artist="周杰伦", album="寻找周杰伦")
    c = SMTCSnapshot(title="晴天", artist="周杰伦", album="叶惠美")
    assert a.identity() == b.identity()
    assert a.identity() != c.identity()


def test_snapshot_identity_stable_when_fields_missing():
    """专辑为空时（QQ音乐有时不给）也要稳定，不能每次轮询都当成换歌。"""
    a = SMTCSnapshot(title="泡沫", artist="邓紫棋")
    b = SMTCSnapshot(title="泡沫", artist="邓紫棋", album="")
    assert a.identity() == b.identity()


def test_placeholder_track_is_not_a_real_song():
    """没检测到播放器时显示的占位"歌"：不能伪装成一首真歌。"""
    assert "未检测到" in PLACEHOLDER_TRACK.title
    assert PLACEHOLDER_TRACK.duration_s == 0.0
    assert PLACEHOLDER_TRACK.cover is None


def test_default_loop_mode_roundtrip():
    """循环模式轮转在 UI 和源之间要一致（QQ音乐不支持，但本地状态仍要正确）。"""
    assert LoopMode.LIST.next_mode() is LoopMode.TRACK
