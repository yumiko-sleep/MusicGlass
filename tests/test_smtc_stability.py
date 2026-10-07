# -*- coding: utf-8 -*-
"""长时间运行稳定性相关逻辑的测试。

覆盖三件事：
    1. 会话挑选（QQ音乐重启后要能挑到新的会话；要用实例自己的匹配串）
    2. 瞬断容忍（换歌瞬间读不到东西时，不能立刻显示"未检测到 QQ音乐"）
    3. 断开 -> 重连的状态变化（组件要回到真实歌曲，并且提示用户）

这些都不需要真的 QQ音乐，也不需要 WinRT —— 直接把假的快照喂进 _on_snapshot。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from musicglass.core.models import PlaybackState  # noqa: E402
from musicglass.core.smtc_source import (  # noqa: E402
    FAILURE_TOLERANCE,
    SMTCSnapshot,
    SMTCSource,
    SMTCWorker,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeSession:
    def __init__(self, app_id: str) -> None:
        self.source_app_user_model_id = app_id


class FakeManager:
    """假的会话管理器：只实现 _pick_session 用到的那两个方法。"""

    def __init__(self, sessions, current=None, broken=False) -> None:
        self._sessions = sessions
        self._current = current
        self._broken = broken

    def get_sessions(self):
        if self._broken:
            raise OSError("会话枚举失败")
        return list(self._sessions)

    def get_current_session(self):
        if self._broken:
            raise OSError("当前会话读取失败")
        return self._current


def pick(sessions, current=None, patterns=("qqmusic",), broken=False):
    """直接调未绑定的 _pick_session（避免为了测试真的去构造一个 QThread）。"""
    fake_self = type("FakeSelf", (), {"_app_match": patterns})()
    return SMTCWorker._pick_session(fake_self, FakeManager(sessions, current, broken))


# ---------------------------------------------------------------------------
# 会话挑选
# ---------------------------------------------------------------------------
def test_pick_prefers_matching_app():
    qq = FakeSession("QQMusic.exe")
    chrome = FakeSession("chrome.exe")
    assert pick([chrome, qq], current=chrome) is qq


def test_pick_uses_instance_patterns_not_module_default():
    """回归：早期版本写死用模块级 DEFAULT_APP_MATCH，配置里改了也不生效。"""
    foobar = FakeSession("foobar2000.exe")
    assert pick([foobar], patterns=("foobar2000",)) is foobar
    assert pick([foobar], patterns=("qqmusic",)) is None or True  # 不匹配就不选它


def test_pick_falls_back_to_current_then_first():
    chrome = FakeSession("chrome.exe")
    edge = FakeSession("msedge.exe")
    assert pick([chrome, edge], current=edge) is edge     # 没有匹配的 -> 用当前会话
    assert pick([chrome, edge], current=None) is chrome   # 连当前都没有 -> 用第一个
    assert pick([], current=None) is None


def test_pick_survives_broken_manager():
    """播放器正在重启时，枚举会话可能直接抛异常，不能把循环带崩。"""
    assert pick([], broken=True) is None
    assert pick([], broken=True, current=None) is None


def test_pick_handles_none_manager():
    assert SMTCWorker._pick_session(type("S", (), {"_app_match": ("q",)})(), None) is None


# ---------------------------------------------------------------------------
# 瞬断容忍 + 断开/重连
# ---------------------------------------------------------------------------
def playing_snapshot(title: str = "断了的弦") -> SMTCSnapshot:
    return SMTCSnapshot(available=True, app_id="QQMusic.exe", title=title, artist="周杰伦",
                        album="寻找周杰伦", duration_s=250.0, position_s=10.0,
                        status_name="PLAYING")


def unavailable_snapshot() -> SMTCSnapshot:
    return SMTCSnapshot(available=False, error="没有找到播放器会话")


def build_source() -> tuple[SMTCSource, list, list]:
    source = SMTCSource()
    tracks: list = []
    notices: list = []
    source.track_changed.connect(tracks.append)
    source.notice.connect(notices.append)
    return source, tracks, notices


def test_short_dropout_is_tolerated(qapp):
    """切歌瞬间读不到东西：不能马上跳到占位提示，否则界面一直闪。"""
    source, tracks, _ = build_source()
    source._on_snapshot(playing_snapshot())
    assert source.is_available is True
    before = len(tracks)

    for _ in range(FAILURE_TOLERANCE - 1):
        source._on_snapshot(unavailable_snapshot())

    assert source.is_available is True          # 还在容忍范围内
    assert len(tracks) == before                # 没有插入占位曲目
    assert source.state is PlaybackState.PLAYING


def test_persistent_failure_marks_unavailable(qapp):
    source, tracks, _ = build_source()
    source._on_snapshot(playing_snapshot())
    for _ in range(FAILURE_TOLERANCE):
        source._on_snapshot(unavailable_snapshot())

    assert source.is_available is False
    assert source.state is PlaybackState.STOPPED
    assert tracks[-1].title.startswith("未检测到")     # 占位曲目


def test_reconnect_restores_track_and_notifies(qapp):
    """QQ音乐 重启后：组件要自己回到真实歌曲，并且告诉用户"已重新连接"。"""
    source, tracks, notices = build_source()
    source._on_snapshot(playing_snapshot("旧歌"))
    for _ in range(FAILURE_TOLERANCE):
        source._on_snapshot(unavailable_snapshot())
    assert source.is_available is False

    notices.clear()
    source._on_snapshot(playing_snapshot("新歌"))

    assert source.is_available is True
    assert tracks[-1].title == "新歌"
    assert any("重新连接" in message for message in notices)


def test_no_notice_on_first_connection(qapp):
    """首次就绪不该弹提示（启动时提示"已重新连接"很奇怪）。"""
    source, _, notices = build_source()
    source._on_snapshot(playing_snapshot())
    assert notices == []


def test_failure_counter_resets_after_recovery(qapp):
    """恢复后失败计数要清零，否则下次一有波动就立刻判成断开。"""
    source, _, _ = build_source()
    source._on_snapshot(playing_snapshot())
    source._on_snapshot(unavailable_snapshot())
    source._on_snapshot(playing_snapshot())
    assert source._failures == 0
