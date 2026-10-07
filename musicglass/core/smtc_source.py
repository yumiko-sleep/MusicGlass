# -*- coding: utf-8 -*-
"""SMTC 媒体源：从 Windows 系统媒体传输控件读 QQ音乐 的播放信息。

── 为什么是这个设计（全部基于 tools/probe_smtc.py 在你机器上的实测结果）──────

实测事实（QQ音乐 PC 版）：
    能读到 : 歌名、歌手、专辑、总时长、播放进度、播放状态、封面（JPEG 约 17~28KB）
    读不到 : 专辑歌手、副标题、循环模式(auto_repeat_mode=None)、随机播放(None)、播放速率(None)
    不支持 : 拖进度条（is_playback_position_enabled 未声明，min/max_seek 都是 0）
    **事件   : 一个都收不到**（切了 8 首歌，MediaPropertiesChanged / PlaybackInfoChanged /
              TimelinePropertiesChanged 计数全是 0）

所以：
    1. **不依赖事件**，改成 500ms 轮询。每次轮询只做 3 次 COM 调用，开销可忽略。
    2. **进度靠本地插值**：SMTC 只给"某个时刻的进度快照 + 这个快照是什么时候测的"，
       所以显示进度 = 快照进度 + (现在 - 快照时刻) × 播放速率。每收到一次新快照就
       用上报值校准一次，不会累积漂移。
    3. **循环/随机/拖进度**都按"试一次，失败就明确告诉用户"处理，绝不假装成功。

── 线程模型 ──────────────────────────────────────────────────────────────
    Qt 主线程（UI）
        │  MediaSource 的 Qt 信号（跨线程自动排队投递）
        ▼
    SMTCWorker（QThread 子类）
        └─ 自己跑一个 asyncio 事件循环，在里面 await WinRT 的异步 API
           （WinRT 的 xxx_async() 必须被 await，不能同步调用）

为什么不让主线程直接 await：Qt 的事件循环和 asyncio 事件循环是两个东西，
硬塞在一起容易出现"界面卡住"或"回调不来"的怪问题。独立线程最干净，
而且以后要改成事件驱动也只是换这个线程里的实现，UI 完全不用动。
"""

from __future__ import annotations

import asyncio
import queue
import time
from dataclasses import dataclass, field
from datetime import timedelta

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..perf import monitor
from .cover_loader import CoverLoader, cover_key
from .models import LoopMode, PlaybackState, Track
from .source import MediaSource

# 认哪个播放器：AppUserModelId 里包含这些片段就算（不区分大小写）
DEFAULT_APP_MATCH = ("qqmusic",)
DEFAULT_POLL_MS = 500
INTERPOLATE_MS = 200
# 连续多少次读不到播放器，才认定"真的断开了"。
# 为什么要这个：QQ音乐 在换歌/切歌的一瞬间，会话会短暂读不出东西。
# 不宽容的话组件每次切歌都会先闪一下“未检测到 QQ音乐”，很胜。
FAILURE_TOLERANCE = 3          # 3 × 500ms = 1.5 秒
WATCHDOG_MS = 2000             # 看门狗检查间隔

# 没检测到播放器时给界面用的占位曲目。
# 直接呈现成一首"歌"而不是留空，体验上更好：用户一眼就知道要打开 QQ音乐。
PLACEHOLDER_TRACK = Track(
    title="未检测到 QQ音乐",
    artist="打开 QQ音乐 播放一首歌",
    album="",
    duration_s=0.0,
)


# ---------------------------------------------------------------------------
# 纯函数区（不碰 WinRT，方便单测）
# ---------------------------------------------------------------------------
def seconds_of(value) -> float | None:
    """把 WinRT 的时间值转成秒。

    坑：pywinrt 3.x 把 TimeSpan 转成了 Python 的 datetime.timedelta，
    直接 float() 会报 TypeError，必须走 total_seconds()。
    三种形态（timedelta / 带 duration 的对象 / 纳秒整数）都兼容一下。
    """
    if value is None:
        return None
    try:
        if hasattr(value, "total_seconds"):
            return float(value.total_seconds())
        if hasattr(value, "duration"):
            return float(value.duration) / 1e7
        return float(value) / 1e7
    except Exception:
        return None


_STATUS_BY_NAME = {
    "PLAYING": PlaybackState.PLAYING,
    "PAUSED": PlaybackState.PAUSED,
    # STOPPED 和 PAUSED 对界面来说是同一种样子（都显示"播放"图标），
    # 而且 QQ音乐 在换歌瞬间会闪一下 STOPPED —— 归到一起可以避免图标乱跳。
    "STOPPED": PlaybackState.PAUSED,
    "CLOSED": PlaybackState.STOPPED,
    "OPENED": PlaybackState.STOPPED,
}


def map_status(name: str, previous: PlaybackState) -> PlaybackState:
    """把 SMTC 的状态名映射成本项目的 PlaybackState。

    CHANGING（换歌瞬间的过渡态）刻意"保持上一个状态"——
    否则每次切歌图标都会闪一下。
    """
    if not name:
        return previous
    if name == "CHANGING":
        return previous
    return _STATUS_BY_NAME.get(name, previous)


def interpolate(base_position: float, base_monotonic: float, now_monotonic: float,
                playing: bool, duration: float) -> float:
    """本地插值：算出"现在"应该显示的进度（秒）。

    播放中才推进；暂停/停止时冻结在最后一次上报值。
    """
    position = base_position + (now_monotonic - base_monotonic) if playing else base_position
    if duration and duration > 0:
        position = min(position, duration)
    return max(0.0, position)


def app_matches(app_id: str, patterns: tuple[str, ...]) -> bool:
    """AppUserModelId 是否命中我们关心的播放器。"""
    if not app_id:
        return False
    lowered = app_id.lower()
    return any(pattern in lowered for pattern in patterns)


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
@dataclass
class SMTCSnapshot:
    """一次轮询的结果。字段全是基本类型（bytes 除外），跨线程传递很安全。"""

    available: bool = False          # 有没有找到可用的播放器会话
    app_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration_s: float = 0.0
    position_s: float = 0.0
    position_age_s: float = 0.0      # 这个进度快照距现在多少秒（用来校准插值）
    status_name: str = ""            # PLAYING / PAUSED / CHANGING / ...
    cover_data: bytes | None = None  # 只在换歌时才有值
    cover_key: str = ""
    error: str = ""

    def identity(self) -> str:
        return cover_key(self.title, self.artist, self.album)


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
class SMTCWorker(QThread):
    """在独立线程里跑 asyncio，轮询 SMTC 并接控制指令。"""

    snapshot = pyqtSignal(object)                 # SMTCSnapshot
    command_result = pyqtSignal(str, bool, str)   # (指令名, 是否成功, 说明)
    # 连接出问题（拿不到管理器 / 长循环异常退出）时发出来。
    # 注意：这不是"致命错误"—— 工作线程会自己重试，所以叫 reconnecting 而不是 fatal。
    # 早期版本这里是 fatal，一发出就 return，结果"开机时 SMTC 服务还没就绪"
    # 就会导致整个组件永远不再重连。这是长时间运行稳定性里最要命的一个坑。
    reconnecting = pyqtSignal(str)

    def __init__(self, poll_ms: int = DEFAULT_POLL_MS,
                 app_match: tuple[str, ...] = DEFAULT_APP_MATCH,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._poll_ms = max(120, int(poll_ms))
        self._app_match = tuple(p.lower() for p in app_match)
        self._running = True
        self._commands: queue.Queue[tuple[str, object]] = queue.Queue()
        self._session = None            # 当前用的 SMTC 会话对象
        self._manager = None            # 会话管理器（控制指令失效时可以重新挑会话）
        self._cover_key = ""            # 已经取过封面的歌（避免每轮都下载 20KB）
        self._rate = 1.0
        self.last_error = ""
        self._restart_count = 0         # 监督循环重启次数（--debug 会报）
        self._connect_attempts = 0      # 拿管理器的失败次数（连续失败会退避重试）

    # ---- 自适应轮询参数（问题3：切歌延迟）----
    # 换歌/状态切换的那几秒里，QQ音乐 的元数据和封面是"陆续就绪"的，
    # 固定 500ms 轮询会让用户等好几个回合（实测切歌后 1~2.5 秒才更新）。
    # 所以"变化期"改用 120ms 快轮询，稳定下来再回到 500ms（省 CPU）。
    FAST_POLL_MS = 120
    FAST_SETTLE_S = 4.0
    FAST_MAX_S = 6.0            # 变化期硬上限：超过就回普通轮询（防止无封面歌曲一直快轮询）

    def stats(self) -> str:
        """给 --debug 看的工作线程自检信息。"""
        return (f"重启={self._restart_count} 次 管理器重试={self._connect_attempts} 次"
                f" 最后错误={self.last_error or '无'}")

    # ---- 外部接口（都在主线程调用，线程安全）----
    def stop(self) -> None:
        self._running = False

    def submit(self, name: str, payload: object = None) -> None:
        """投递一条控制指令给工作线程执行。"""
        self._commands.put((name, payload))

    def cover_key_now(self) -> str:
        return self._cover_key

    # ---- 线程主体 ----
    def run(self) -> None:  # noqa: D102
        try:
            from winrt import runtime as winrt_runtime

            # WinRT 需要先初始化 COM。多线程套间(MTA)下回调可以直接投递，
            # 不需要消息泵 —— 这也是我们不用事件的原因之一（反正 QQ音乐 也不发）。
            winrt_runtime.init_apartment(winrt_runtime.ApartmentType.MULTI_THREADED)
        except Exception:
            pass   # 已经初始化过会抛错，忽略即可

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # ---- 监督循环 ----
            # 里面不管发生什么（拿不到管理器、COM 异常、异步任务被取消）
            # 都退到这里歇一会儿重来。这是"挂机几小时也不会烂掉"的关键：
            # 单个异常绝不能把读取线程搞死。
            while self._running:
                started = time.monotonic()
                try:
                    loop.run_until_complete(self._main())
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    if self._running:
                        self.reconnecting.emit(self.last_error)
                if not self._running:
                    break
                # 异常退出（跑得太快说明刚出问题）→ 退避 2 秒，避免疯狂重启烧 CPU；
                # 同时把旧事件循环丢掉重建，避免里面残留的待处理任务干扰下一轮。
                if time.monotonic() - started < 1.0:
                    self._restart_count += 1
                    time.sleep(2.0)
                    try:
                        loop.close()
                    except Exception:
                        pass
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _connect(self):
        """拿 SMTC 会话管理器；失败就退避重试，直到拿到或收到停止信号。

        为什么要重试：开机自启时组件可能比系统媒体服务先就绪，
        那一下拿不到管理器很正常，等两秒就好了 —— 不能因此就永远放弃。
        """
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        delay = 1.0
        while self._running:
            try:
                manager = await SessionManager.request_async()
                if manager is not None:
                    if self._connect_attempts:
                        self.reconnecting.emit(
                            f"已重新连接 SMTC（之前失败 {self._connect_attempts} 次）")
                    self._connect_attempts = 0
                    return manager
                self.last_error = "会话管理器返回空值"
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._connect_attempts += 1
            if self._connect_attempts in (1, 2, 5, 10, 30):
                self.reconnecting.emit(
                    f"暂时拿不到 SMTC 会话管理器（第 {self._connect_attempts} 次）：{self.last_error}")
            await asyncio.sleep(min(delay, 10.0))
            delay = min(delay * 1.7, 10.0)
        return None

    async def _main(self) -> None:
        manager = await self._connect()
        if manager is None:
            return
        self._manager = manager

        interval = self._poll_ms / 1000.0
        settle_until = 0.0          # 快轮询预计维持到哪个时刻
        hard_until = 0.0            # 快轮询硬上限（无论如何不超过）
        seen_identity = ""          # 上一轮看到的歌曲标识
        while self._running:
            started = time.monotonic()
            try:
                snap = await self._read(manager)
            except Exception as exc:
                # 单次读取失败很常见（比如播放器刚被关掉），转成"不可用"快照即可，
                # 千万不要往外抛 —— 那会把整个循环掀掉。
                snap = SMTCSnapshot(error=f"{type(exc).__name__}: {exc}")
            if self._running:
                self.snapshot.emit(snap)

            # ---- 自适应轮询：刚换歌/状态在变的那几秒用快轮询 ----
            settle_until, hard_until, seen_identity = self._poll_pace(
                snap, settle_until, hard_until, seen_identity)
            interval = (self.FAST_POLL_MS / 1000.0 if time.monotonic() < settle_until
                        else self._poll_ms / 1000.0)

            # 分片睡眠：既保证轮询间隔，又能让控制指令在 50ms 内被执行
            # （如果一觉睡 500ms，点按钮要等半秒才有反应，手感很差）
            deadline = started + interval
            while self._running and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                await self._apply_commands()

    def _poll_pace(self, snap, settle_until: float, hard_until: float,
                   seen_identity: str) -> tuple[float, float, str]:
        """算这一轮之后该用多快的轮询。返回（快轮询截止, 硬上限, 本轮歌曲标识）。

        进入"变化期"的迹象：
        * 歌曲标识变了 —— 换歌；
        * 播放状态是 CHANGING/OPENING —— QQ音乐 切歌时会闪一下这个状态；
        * 还在变化期、但这张封面还没成功读到 —— 再盯半秒。

        【两个截止时刻的区别】settle_until 是"预计什么时候稳定"，
        hard_until 是"无论如何都不许超过的快轮询上限"。
        少了后者，遇到"这首歌根本没有封面"的情况就会永远快轮询下去
        （实测过：7.75 次/秒，白烧 4 倍 CPU）。
        """
        now = time.monotonic()
        if not snap.available:
            return settle_until, hard_until, seen_identity
        identity = snap.identity()
        if identity and identity != seen_identity:
            return now + self.FAST_SETTLE_S, now + self.FAST_MAX_S, identity
        if (snap.status_name or "").strip().lower() in ("changing", "opening"):
            return (max(settle_until, now + 1.5),
                    max(hard_until, now + 1.5), identity)
        key = cover_key(snap.title or "", snap.artist or "", snap.album or "")
        if now < settle_until and now < hard_until and key and key != self._cover_key:
            return now + 0.5, hard_until, identity
        return settle_until, hard_until, identity

    async def _apply_commands(self) -> None:
        while True:
            try:
                name, payload = self._commands.get_nowait()
            except queue.Empty:
                return
            ok, message = False, ""
            try:
                ok, message = await self._apply_one(name, payload)
            except Exception as exc:
                # 播放器刚重启时，手里那个会话对象可能已经失效了。
                # 丢掉它、重新挑一个会话再试一次（QQ音乐重启后按钮还能用，靠的就是这个）。
                self._session = None
                try:
                    ok, message = await self._apply_one(name, payload)
                except Exception as exc2:
                    message = f"{type(exc2).__name__}: {exc2}"
                else:
                    if ok:
                        message = ""
            self.command_result.emit(name, bool(ok), message)

    async def _apply_one(self, name: str, payload: object) -> tuple[bool, str]:
        from winrt.windows.media import MediaPlaybackAutoRepeatMode

        # 手里没有会话就现从管理器里挑一个（不能只会说"没会话"然后摆烂）
        session = self._session or self._pick_session(self._manager)
        self._session = session
        if session is None:
            return False, "当前没有可控制的播放器会话"

        if name == "play_pause":
            ok = await session.try_toggle_play_pause_async()
            if not ok:
                # 有播放器只实现 play/pause 其中之一，那就两个都试一下
                ok = bool(await session.try_play_async()) or bool(await session.try_pause_async())
            return ok, "" if ok else "QQ音乐 拒绝了这个播放/暂停指令"

        if name == "next":
            ok = await session.try_skip_next_async()
            return ok, "" if ok else "切下一首失败"

        if name == "previous":
            ok = await session.try_skip_previous_async()
            return ok, "" if ok else "切上一首失败"

        if name == "seek":
            seconds = max(0.0, float(payload or 0.0))
            ok = await session.try_change_playback_position_async(timedelta(seconds=seconds))
            return ok, "" if ok else "QQ音乐 不支持通过系统接口拖动进度"

        if name == "loop":
            mode = payload
            if mode is LoopMode.TRACK:
                _ok = await session.try_change_shuffle_active_async(False)
                ok = await session.try_change_auto_repeat_mode_async(MediaPlaybackAutoRepeatMode.TRACK)
            elif mode is LoopMode.SHUFFLE:
                ok = await session.try_change_shuffle_active_async(True)
            else:
                _ok = await session.try_change_shuffle_active_async(False)
                ok = await session.try_change_auto_repeat_mode_async(MediaPlaybackAutoRepeatMode.LIST)
            return ok, "" if ok else "QQ音乐 不支持通过系统接口切换循环模式（实测它不上报也不接受）"

        return False, f"未知指令 {name}"

    # ---- 读一次 ----
    async def _read(self, manager) -> SMTCSnapshot:
        session = self._pick_session(manager)
        if session is None:
            self._session = None
            return SMTCSnapshot(available=False)

        self._session = session
        app_id = getattr(session, "source_app_user_model_id", "") or ""

        properties = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        timeline = session.get_timeline_properties()

        title = (getattr(properties, "title", "") or "") if properties else ""
        artist = (getattr(properties, "artist", "") or "") if properties else ""
        album = (getattr(properties, "album_title", "") or "") if properties else ""

        duration = seconds_of(getattr(timeline, "end_time", None)) or 0.0
        position = seconds_of(getattr(timeline, "position", None)) or 0.0

        # position 是"某个时刻的快照"，算出它距现在已经多久，供插值校准
        age = 0.0
        updated = getattr(timeline, "last_updated_time", None)
        try:
            if updated is not None:
                age = max(0.0, time.time() - updated.timestamp())
        except Exception:
            age = 0.0

        status_name = ""
        try:
            status_name = getattr(info.playback_status, "name", "") or ""
        except Exception:
            status_name = ""

        rate = getattr(info, "playback_rate", None)
        try:
            self._rate = float(rate) if rate else 1.0
        except Exception:
            self._rate = 1.0

        # 封面：只在换歌时下载一次（每轮都下会白烧网络和 CPU）
        key = cover_key(title, artist, album)
        cover_data = None
        if key and key != self._cover_key:
            cover_data = await self._read_thumbnail(properties)
            # 只有真拿到了才记下这首歌，否则下次继续重试
            # （之前把这一行写在前面，一次失败就永远不再尝试拿封面了）
            if cover_data:
                self._cover_key = key

        return SMTCSnapshot(
            available=True, app_id=app_id, title=title, artist=artist, album=album,
            duration_s=duration, position_s=position, position_age_s=age,
            status_name=status_name, cover_data=cover_data, cover_key=key,
        )

    def _pick_session(self, manager):
        """挑会话：优先命中我们关心的播放器，其次当前会话，最后随便取一个。

        为什么要挑：浏览器播歌、其它播放器都会各占一个会话，
        不挑的话组件可能显示成浏览器的标签页标题。
        注意：这里用实例自己的 _app_match，不要用模块级默认值
        （早期版本写成了 DEFAULT_APP_MATCH，导致配置里改了匹配串也不生效）。
        """
        if manager is None:
            return None
        try:
            sessions = list(manager.get_sessions())
        except Exception:
            sessions = []
        for session in sessions:
            app_id = getattr(session, "source_app_user_model_id", "") or ""
            if app_matches(app_id, self._app_match):
                return session
        try:
            current = manager.get_current_session()
        except Exception:
            current = None
        if current is not None:
            return current
        return sessions[0] if sessions else None

    @staticmethod
    async def _read_thumbnail(properties) -> bytes | None:
        """把封面缩略图读成 bytes（实测 13~17ms）。"""
        if properties is None:
            return None
        reference = getattr(properties, "thumbnail", None)
        if reference is None:
            return None
        try:
            from winrt.windows.storage.streams import DataReader

            stream = await reference.open_read_async()
            if stream is None:
                return None
            size = int(stream.size)
            if size <= 0:
                return None
            reader = DataReader(stream.get_input_stream_at(0))
            await reader.load_async(size)
            data = bytearray(size)
            # 【坑】pywinrt 的 read_bytes() 会把数据填进缓冲区，但**返回 None**。
            # 所以千万不要用返回值判断成败，也不要用它切片（切片[:None]恰好是全部，
            # 看着像对但其实靠运气）。直接拿预先分配好的整个缓冲区就对了。
            reader.read_bytes(data)
            reader.close()
            try:
                stream.close()      # 不关会漏 Win32 句柄（挂机几小时看得很明显）
            except Exception:
                pass
            return bytes(data)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 主线程侧的媒体源
# ---------------------------------------------------------------------------
class SMTCSource(MediaSource):
    """把 SMTCWorker 的快照变成一套 Qt 信号，并负责进度插值。"""

    # 实测：QQ音乐 不接受通过 SMTC 拖进度、也切不了循环/随机。
    # UI 靠这三个开关决定"按钮是干活还是给提示"，而不是假装成功。
    supports_seek = False
    supports_loop_mode = False
    supports_shuffle = False

    def __init__(self, parent: QObject | None = None,
                 poll_ms: int = DEFAULT_POLL_MS,
                 app_match: tuple[str, ...] = DEFAULT_APP_MATCH) -> None:
        super().__init__(parent)
        self.cover_loader = CoverLoader(parent=self)
        self._worker: SMTCWorker | None = None
        self._poll_ms = int(poll_ms)
        self._app_match = app_match
        self._app_id = ""
        self._available = False
        self._last_error = ""
        self._status_name = ""
        self._duration = 0.0
        self._rate = 1.0
        # 插值基准：base_position 是"快照上报的进度 + 快照已过的秒数"
        self._base_position = 0.0
        self._base_monotonic = 0.0
        self._last_snapshot_at = 0.0
        self._snapshot_count = 0
        self._identity = ""
        self._cover_seen = False
        # ---- 长时间运行稳定性相关的状态 ----
        self._stopped = True            # start() 之前算停止
        self._failures = 0              # 连续失败次数（用于容忍瞬断）
        self._restarts = 0              # 看门狗重启线程的次数
        self._dead_checks = 0           # 连续几次看到线程已死
        self._conn_state: bool | None = None   # 上一次的连接状态（None=还没就绪过）
        self._last_success_at = 0.0

        self._interpolator = QTimer(self)
        self._interpolator.setInterval(INTERPOLATE_MS)
        self._interpolator.timeout.connect(self._tick)

        # 看门狗：万一读取线程整个死掉（监督循环也救不回来），这里把它拉起来
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._check_worker)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._start_worker()
        self._interpolator.start()
        self._watchdog.start()

    def stop(self) -> None:
        self._stopped = True
        self._watchdog.stop()
        self._interpolator.stop()
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(2000)      # 给它 2 秒收尾，别拖住退出
            self._worker = None

    def _start_worker(self) -> None:
        self._worker = SMTCWorker(self._poll_ms, self._app_match, self)
        self._worker.snapshot.connect(self._on_snapshot)
        self._worker.command_result.connect(self._on_command_result)
        self._worker.reconnecting.connect(self._on_reconnecting)
        self._worker.start()

    def _check_worker(self) -> None:
        """看门狗：读取线程死了就拉起来。

        正常情况下工作线程内部的监督循环会自己重试，用不到这里；
        但万一整个 QThread 退出了（极端 COM 崩溃），不兜底的话
        组件会永远停在最后一帧上，而且什么提示都没有。
        """
        if self._stopped:
            return
        if self._worker is not None and self._worker.isRunning():
            self._dead_checks = 0
            return
        self._dead_checks += 1
        if self._dead_checks < 2:      # 连续两次（约 4 秒）都没活才动手，避免误判
            return
        self._dead_checks = 0
        self._restarts += 1
        print(f"[音璃] SMTC 读取线程已停止，正在重启（第 {self._restarts} 次）")
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(1000)
        self._start_worker()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def app_id(self) -> str:
        return self._app_id

    def status_text(self) -> str:
        state = "已连接" if self._available else "未检测到播放器"
        age = (f"  最后一次成功={time.monotonic() - self._last_success_at:.1f}s 前"
               if self._last_success_at else "")
        worker = self._worker.stats() if self._worker is not None else "已停止"
        return (f"SMTC {state}  应用={self._app_id or '—'}  轮询={self._poll_ms}ms"
                f"  快照={self._snapshot_count} 次  失败容忍={self._failures}/{FAILURE_TOLERANCE}"
                f"  线程重启={self._restarts} 次  [{worker}]{age}")

    def cover_stats(self) -> str:
        return self.cover_loader.stats()

    def disc_pixmap(self, track: Track, diameter: int, dpr: float = 1.0):
        """给 UI 一张预先裁好、圆形遮罩烘好的圆盘位图（带缓存）。"""
        if track is None or track.cover is None:
            return None
        key = cover_key(track.title, track.artist, track.album)
        return self.cover_loader.disc_pixmap(key, track.cover, diameter, dpr)

    # ------------------------------------------------------------------
    # 控制指令
    # ------------------------------------------------------------------
    def play_pause(self) -> None:
        self._submit("play_pause")

    def next_track(self) -> None:
        self._submit("next")

    def previous_track(self) -> None:
        self._submit("previous")

    def seek(self, position_s: float) -> None:
        if not self.supports_seek:
            self.notice.emit("QQ音乐不支持通过系统接口拖动进度")
            return
        self._submit("seek", position_s)

    def set_loop_mode(self, mode: LoopMode) -> None:
        if not self.supports_loop_mode:
            # 用户要求：点了也试一次，失败就明确提示（不假装成功）
            self._submit("loop", mode)
            return
        self._submit("loop", mode)

    def cycle_loop_mode(self) -> None:
        self._submit("loop", self._loop.next_mode())

    def _submit(self, name: str, payload: object = None) -> None:
        if self._worker is None:
            self.notice.emit("播放器还没连接上，稍等一下")
            return
        self._worker.submit(name, payload)

    # ------------------------------------------------------------------
    # 工作线程回调（都在主线程执行）
    # ------------------------------------------------------------------
    def _on_snapshot(self, snap: SMTCSnapshot) -> None:
        self._snapshot_count += 1
        if snap.error:
            self._last_error = snap.error
        if not snap.available:
            # 先宽容几次：QQ音乐 换歌的一瞬间会读不出东西，
            # 一上来就刷"未检测到"会让组件在切歌时不断闪。
            self._failures += 1
            if self._failures < FAILURE_TOLERANCE:
                return
            self._note_connection(False, snap.error)
            self._available = False
            self._interpolator.stop()
            self._base_position = 0.0
            if self._identity != "__placeholder__":
                self._identity = "__placeholder__"
                self._emit_track(PLACEHOLDER_TRACK)
            self._emit_state(PlaybackState.STOPPED)
            return

        self._failures = 0
        self._last_success_at = time.monotonic()
        self._note_connection(True, "")
        self._available = True
        self._app_id = snap.app_id
        self._duration = snap.duration_s
        self._status_name = snap.status_name

        # 进度基准（含"快照已过时间"的校准）
        self._base_position = snap.position_s + snap.position_age_s
        self._base_monotonic = time.monotonic()
        self._last_snapshot_at = self._base_monotonic

        # 换歌才动 track / 封面
        identity = snap.identity()
        # 【问题3 修】同一首歌"封面后到"也要发一次：
        # QQ音乐 换歌时元数据和封面不是一个回合就绪的，以前这里只判"歌变了没"，
        # 于是封面晚一步到就永远补不上（只能等下一首歌）—— 用户看到的就是
        # "歌名对了、封面还是上一张/渐变圆盘"。
        cover = self.cover_loader.load(identity, snap.cover_data)
        track_changed = (identity != self._identity
                         or self._track.title != (snap.title or "未知歌曲"))
        cover_arrived = cover is not None and not self._cover_seen
        if track_changed or cover_arrived:
            self._identity = identity
            self._cover_seen = cover is not None
            self._emit_track(Track(
                title=snap.title or "未知歌曲",
                artist=snap.artist or "未知歌手",
                album=snap.album or "",
                duration_s=snap.duration_s,
                cover=cover,
                source_app=snap.app_id,
            ))

        state = map_status(snap.status_name, self._state)
        if state is PlaybackState.PLAYING:
            if not self._interpolator.isActive():
                self._interpolator.start()
        else:
            self._interpolator.stop()
            self._emit_position(self._base_position)
        self._emit_state(state)

    def _on_command_result(self, name: str, ok: bool, message: str) -> None:
        """控制指令的结果：成功就什么都不用说，失败必须明确告诉用户。"""
        if ok:
            return
        label = {"play_pause": "播放/暂停", "next": "下一首", "previous": "上一首",
                 "seek": "拖动进度", "loop": "切换循环模式"}.get(name, name)
        self.notice.emit(f"{label}失败：{message}" if message else f"{label}失败：播放器不支持")

    def _on_reconnecting(self, message: str) -> None:
        """工作线程报告"正在重连"。这不是致命错误，所以只记日志。"""
        self._last_error = message
        print(f"[音璃] {message}")

    def _note_connection(self, available: bool, detail: str) -> None:
        """连接状态发生变化时记一笔（断开/重连）。

        首次就绪时什么都不说，不打扰用户。
        重新连上时弹一条提示：这是用户会关心的"恢复正常了"。
        """
        previous = self._conn_state
        self._conn_state = available
        if previous is None:
            return
        if available and not previous:
            print("[音璃] 已重新连接播放器")
            self.notice.emit("已重新连接播放器")
        elif previous and not available:
            print(f"[音璃] 播放器已断开（{detail or '会话消失'}），组件显示占位提示")

    def _tick(self) -> None:
        """插值定时器：每 200ms 推一次平滑进度。"""
        if monitor.enabled:
            with monitor.timer("插值tick"):
                self._tick_impl()
        else:
            self._tick_impl()

    def _tick_impl(self) -> None:
        """插值定时器：让进度条平滑走动（200ms 一次，UI 那边只重绘底部一行）。"""
        position = interpolate(
            self._base_position, self._base_monotonic, time.monotonic(),
            self._state is PlaybackState.PLAYING, self._duration,
        )
        self._emit_position(position)
