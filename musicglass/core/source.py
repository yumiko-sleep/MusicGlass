# -*- coding: utf-8 -*-
"""媒体源抽象接口。

整个项目的"插拔点"：UI 只认识 MediaSource 这一组信号和方法，
底下到底是假数据、SMTC、还是窗口标题解析，UI 完全不知道。

    MockSource       阶段1  假数据（用来跑通 UI）
    SMTCSource       阶段2  Windows 系统媒体传输控件，读 QQ音乐
    WindowTitleSource 阶段2 回退方案：解析 QQ音乐窗口标题

所有实现都必须遵守两条约定：
1. 信号在 **Qt 主线程** 里发（跨线程用 QObject 的信号队列自动转发）；
2. 任何读取失败都不许抛异常，最多发一个空的 Track / UNKNOWN 状态。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal

from .models import LoopMode, PlaybackState, Track


class MediaSource(QObject):
    """媒体源基类。"""

    # 换歌了（Track 对象；读取失败时是一个"未知歌曲"的空 Track）
    track_changed = pyqtSignal(object)
    # 播放进度变化（秒）
    position_changed = pyqtSignal(float)
    # 播放状态变化（PlaybackState）
    state_changed = pyqtSignal(object)
    # 循环模式变化（LoopMode）
    loop_changed = pyqtSignal(object)
    # 产生了一条给用户看的提示（例如"当前播放器不支持随机播放"）
    notice = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._track = Track()
        self._state = PlaybackState.UNKNOWN
        self._loop = LoopMode.LIST
        self._position = 0.0

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def track(self) -> Track:
        return self._track

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def loop_mode(self) -> LoopMode:
        return self._loop

    @property
    def position(self) -> float:
        return self._position

    @property
    def is_available(self) -> bool:
        """当前是否真的连上了播放器。UI 用它决定要不要显示"未检测到 QQ音乐"。"""
        return True

    # ------------------------------------------------------------------
    # 能力声明
    # ------------------------------------------------------------------
    # 不同播放器（以及不同版本的 QQ音乐）对这些操作的支持差别很大，实测：
    # QQ音乐 只支持 播放/暂停/上下首，**不支持**拖进度、切循环/随机。
    # UI 靠这三个开关决定"按钮是干活还是给一句明确的提示"，
    # 而不是假装成功——那会让用户以为是自己点错了。
    supports_seek: bool = True
    supports_loop_mode: bool = True
    supports_shuffle: bool = True

    def disc_pixmap(self, track: Track, diameter: int, dpr: float = 1.0):
        """可选的性能入口：返回"裁好 + 圆形遮罩烘好"的圆盘位图。

        默认返回 None，表示"没有预处理好，UI 自己裁"。
        真实数据源会返回缓存好的位图（阶段4 旋转时省掉每帧缩放/裁剪）。
        """
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """开始读取（订阅事件、起轮询定时器等）。"""

    def stop(self) -> None:
        """停止读取并释放资源。"""

    # ------------------------------------------------------------------
    # 控制指令（阶段3 用；阶段1 假数据源也实现它们，这样按钮不会是死的）
    # ------------------------------------------------------------------
    def play_pause(self) -> None:
        """播放 / 暂停切换。"""

    def next_track(self) -> None:
        """下一首。"""

    def previous_track(self) -> None:
        """上一首。"""

    def seek(self, position_s: float) -> None:
        """跳到指定秒数。"""

    def cycle_loop_mode(self) -> None:
        """切到下一个循环模式。"""

    def set_loop_mode(self, mode: LoopMode) -> None:
        """指定循环模式（不支持时要发 notice 提示，而不是假装成功）。"""

    # ------------------------------------------------------------------
    # 给子类用的小工具
    # ------------------------------------------------------------------
    def _emit_track(self, track: Track) -> None:
        self._track = track
        self.track_changed.emit(track)

    def _emit_state(self, state: PlaybackState) -> None:
        if state is self._state and state is not PlaybackState.UNKNOWN:
            return
        self._state = state
        self.state_changed.emit(state)

    def _emit_loop(self, mode: LoopMode) -> None:
        self._loop = mode
        self.loop_changed.emit(mode)

    def _emit_position(self, position_s: float) -> None:
        self._position = max(0.0, float(position_s))
        self.position_changed.emit(self._position)
