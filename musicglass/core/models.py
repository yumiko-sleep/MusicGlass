# -*- coding: utf-8 -*-
"""数据层：媒体信息模型。

这一层刻意不依赖任何 Windows / SMTC / 播放器细节，只描述"一首歌是什么样"。
好处：阶段1 的假数据源、阶段2 的 SMTC 源、阶段2 的窗口标题回退源，
产出的是同一个 Track 对象，UI 一行代码都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from PyQt6.QtGui import QPixmap


class PlaybackState(Enum):
    """播放状态。"""

    UNKNOWN = 0
    STOPPED = 1
    PLAYING = 2
    PAUSED = 3

    @property
    def label(self) -> str:
        return {
            PlaybackState.UNKNOWN: "未知",
            PlaybackState.STOPPED: "已停止",
            PlaybackState.PLAYING: "播放中",
            PlaybackState.PAUSED: "已暂停",
        }[self]


class LoopMode(Enum):
    """循环模式（QQ音乐的三种：列表 / 单曲 / 随机）。"""

    LIST = 0      # 列表循环
    TRACK = 1     # 单曲循环
    SHUFFLE = 2   # 随机播放

    @property
    def label(self) -> str:
        return {
            LoopMode.LIST: "列表循环",
            LoopMode.TRACK: "单曲循环",
            LoopMode.SHUFFLE: "随机播放",
        }[self]

    def next_mode(self) -> "LoopMode":
        """点击循环按钮时的轮转顺序：列表 -> 单曲 -> 随机 -> 列表。"""
        order = (LoopMode.LIST, LoopMode.TRACK, LoopMode.SHUFFLE)
        return order[(order.index(self) + 1) % len(order)]


def format_time(seconds: float | int | None) -> str:
    """把秒数格式化成 m:ss（超过一小时是 h:mm:ss）。

    传 None 或负数返回 "--:--"，UI 就不用到处判空。
    """
    if seconds is None:
        return "--:--"
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "--:--"
    if total < 0:
        return "--:--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Track:
    """一首歌的全部展示信息。

    cover 直接放 QPixmap（已解码好），而不是原始字节：
    解码这件事在阶段2 由 core/cover_loader.py 统一做，UI 拿到就能画。
    compare=False 让"封面换了"不影响 dataclass 的相等判断（避免无意义的重绘）。
    """

    title: str = "未知歌曲"
    artist: str = "未知歌手"
    album: str = ""
    duration_s: float = 0.0
    cover: QPixmap | None = field(default=None, compare=False, repr=False)
    # 上报这条信息的来源应用（阶段2 用来区分是不是 QQ音乐）
    source_app: str = ""

    @property
    def duration_text(self) -> str:
        return format_time(self.duration_s)

    @property
    def subtitle(self) -> str:
        """歌手 · 专辑，专辑为空时只显示歌手。"""
        if self.album:
            return f"{self.artist} · {self.album}"
        return self.artist

    def is_empty(self) -> bool:
        return not self.title or self.title == "未知歌曲"
