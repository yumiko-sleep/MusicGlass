# -*- coding: utf-8 -*-
"""音璃 MusicGlass — 阶段 2 第 1 步：SMTC 探测脚本（只读，不改任何东西）

目的：在动手写 SMTCSource 之前，先搞清楚**你这台机器上的 QQ音乐到底上报了哪些字段**。
因为不同版本/不同播放器对 SMTC（Windows 系统媒体传输控件，任务栏那个媒体浮窗用的就是它）
的实现差异很大：
    * 有的只上报歌名/歌手，封面永远为空；
    * 有的根本不推送进度更新（Position 一直是 0），必须靠本地计时推进；
    * 循环模式/随机播放有的完全不实现。

所以这个脚本干三件事：
    1. 列出所有正在上报的媒体会话（确认 QQ音乐在不在里面、它的 AppUserModelId 长什么样）；
    2. 把当前会话的每个字段都打印出来（含封面能不能取到）；
    3. 监听一段时间，看**事件会不会来**、进度会不会自己走动。

用法（在项目根目录）：
    .\\.venv\\Scripts\\python.exe tools\\probe_smtc.py
    .\\.venv\\Scripts\\python.exe tools\\probe_smtc.py --seconds 40
    .\\.venv\\Scripts\\python.exe tools\\probe_smtc.py --cover 我的封面.png
    .\\.venv\\Scripts\\python.exe tools\\probe_smtc.py --try-controls   # 慎用：会真的控制播放器

【重要】默认是**只读**的：只读取、不发送任何控制指令。
不加 --try-controls 就绝不会动你的播放。
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import sys
import time
from datetime import datetime
from datetime import timedelta as datetime_imported

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# WinRT 导入
# ---------------------------------------------------------------------------
try:
    from winrt import runtime as winrt_runtime
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    from winrt.windows.storage.streams import DataReader
except ImportError as exc:  # pragma: no cover
    print("[X] 缺少 winrt 依赖：", exc)
    print("    请在项目根目录执行：")
    print("    .\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    raise SystemExit(2) from exc

SEP = "=" * 78
SUB = "-" * 78


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def fmt_enum(value) -> str:
    """把 WinRT 枚举打印成 "PLAYING(4)" 这种好看的形式。"""
    if value is None:
        return "None"
    try:
        return f"{value.name}({int(value)})"
    except Exception:
        return repr(value)


def seconds_of(value) -> float | None:
    """把 WinRT 的时间值统一转成秒。

    踩坑记录：pywinrt 3.x 把 TimeSpan 直接转成了 Python 的 datetime.timedelta，
    而不是纳秒整数，所以 float(值) 会报 TypeError。三种形态都要兼顾。
    """
    if value is None:
        return None
    try:
        if isinstance(value, datetime_imported) or hasattr(value, "total_seconds"):
            return float(value.total_seconds())          # datetime.timedelta
        if hasattr(value, "duration"):
            return float(value.duration) / 1e7            # 纯 TimeSpan(100ns)
        return float(value) / 1e7                         # 纳秒整数
    except Exception:
        return None


def fmt_span(value) -> str:
    """WinRT 的时间值打印成 "123.456s"。"""
    seconds = seconds_of(value)
    return "None" if seconds is None else f"{seconds:.3f}s"


def attr(obj, name: str, default="—"):
    """安全取属性：拿不到就返回占位符，绝不让探测脚本自己崩。"""
    try:
        value = getattr(obj, name)
        return default if value is None else value
    except Exception as exc:
        return f"<读取失败 {exc}>"


def pump_windows_messages(times: int = 20) -> None:
    """抽一下 Windows 消息队列。

    WinRT 的事件回调要在有消息循环的线程上才会被投递。
    我们的 main 线程跑的是 asyncio 循环，不是 Windows 消息泵，
    所以主动 PeekMessage/DispatchMessage 一下，尽量把回调挤出来。
    （如果这样还是收不到事件，说明是环境限制，不代表播放器没发通知。）
    """
    user32 = ctypes.windll.user32
    msg = ctypes.create_string_buffer(64)          # MSG 结构体大小足够
    for _ in range(times):
        if not user32.PeekMessageW(msg, None, 0, 0, 1):   # PM_REMOVE
            break
        user32.TranslateMessage(msg)
        user32.DispatchMessageW(msg)


async def read_thumbnail_bytes(ref) -> tuple[bytes | None, str]:
    """把 WinRT 的缩略图流读成 bytes。返回 (数据, 说明)。"""
    if ref is None:
        return None, "没有缩略图对象"
    try:
        stream = await ref.open_read_async()
        if stream is None:
            return None, "open_read_async 返回空"
        size = int(stream.size)
        if size <= 0:
            return None, "流长度是 0"
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        # 两条读取路径，任选其一（不同版本 API 略有差异）
        try:
            data = bytearray(size)
            # 注意：read_bytes() 填满缓冲区但返回 None，不能用返回值判断成败
            reader.read_bytes(data)
            reader.close()
            return bytes(data), "read_bytes 路径"
        except Exception:
            buffer = reader.read_buffer(size)
            reader.close()
            try:
                return bytes(memoryview(buffer)), f"read_buffer 路径，{size} 字节"
            except Exception:
                return bytes(buffer), f"read_buffer 路径（直接 bytes）"
    except Exception as exc:
        return None, f"读取异常：{type(exc).__name__}: {exc}"


def guess_image(data: bytes) -> str:
    """按魔数猜图片格式（封面通常就是 jpg/png）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WebP"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    return f"未知（前 8 字节 {data[:8].hex()}）"


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------
def print_media_properties(props) -> None:
    print("  [媒体属性 GetMediaProperties]")
    print(f"    title         歌名     : {attr(props, 'title')}")
    print(f"    artist        歌手     : {attr(props, 'artist')}")
    print(f"    album_artist  专辑歌手 : {attr(props, 'album_artist')}")
    print(f"    album_title   专辑     : {attr(props, 'album_title')}")
    print(f"    subtitle      副标题   : {attr(props, 'subtitle')}")
    print(f"    track_number  曲目号   : {attr(props, 'track_number')}")
    print(f"    album_track_count 专辑曲目数 : {attr(props, 'album_track_count')}")
    try:
        genres = props.genres
        print(f"    genres        风格     : {list(genres) if genres else '空'}")
    except Exception as exc:
        print(f"    genres        风格     : <读取失败 {exc}>")
    print(f"    playback_type 媒体类型 : {fmt_enum(attr(props, 'playback_type', None))}")


def print_playback_info(info) -> None:
    print("  [播放信息 GetPlaybackInfo]")
    print(f"    playback_status 播放状态 : {fmt_enum(attr(info, 'playback_status', None))}")
    print(f"    playback_type   媒体类型 : {fmt_enum(attr(info, 'playback_type', None))}")
    print(f"    auto_repeat_mode 循环模式: {fmt_enum(attr(info, 'auto_repeat_mode', None))}"
          "   （NONE=不循环 / TRACK=单曲 / LIST=列表）")
    print(f"    is_shuffle_active 随机播放: {attr(info, 'is_shuffle_active')}"
          "    （None 就是『播放器不上报』）")
    print(f"    playback_rate   播放速率 : {attr(info, 'playback_rate')}")
    controls = attr(info, "controls", None)
    position_ok = False
    if controls is not None and not isinstance(controls, str):
        flags = []
        for flag in ("is_play_enabled", "is_pause_enabled", "is_next_enabled", "is_previous_enabled",
                     "is_stop_enabled", "is_shuffle_enabled", "is_repeat_enabled",
                     "is_playback_position_enabled", "is_playback_rate_enabled",
                     "is_fast_forward_enabled", "is_rewind_enabled",
                     "is_channel_up_enabled", "is_channel_down_enabled", "is_record_enabled"):
            if attr(controls, flag, False) is True:
                flags.append(flag.replace("is_", "").replace("_enabled", ""))
        position_ok = attr(controls, "is_playback_position_enabled", False) is True
        print(f"    可用控件（播放器声明支持的）: {', '.join(flags) if flags else '（一个都没声明）'}")
        print(f"    → 能否拖进度条：{'能 ✓' if position_ok else '不能 ✗（TryChangePlaybackPositionAsync 基本会失败）'}")
    else:
        print(f"    controls              : {controls}")


def print_timeline(timeline) -> None:
    print("  [进度 GetTimelineProperties]")
    position = attr(timeline, "position", None)
    end = attr(timeline, "end_time", None)
    print(f"    position   当前进度 : {fmt_span(position)}")
    print(f"    start_time 开始     : {fmt_span(attr(timeline, 'start_time', None))}")
    print(f"    end_time   总时长   : {fmt_span(end)}")
    print(f"    min_seek / max_seek : {fmt_span(attr(timeline, 'min_seek_time', None))}"
          f" / {fmt_span(attr(timeline, 'max_seek_time', None))}")
    last = attr(timeline, "last_updated_time", None)
    print(f"    last_updated_time   : {last}")
    try:
        # last_updated_time 是"这个 position 是什么时候测的"，
        # 我们据此可以本地插值推进进度（播放器不推送更新时的救命稻草）
        stamp = last.timestamp() if hasattr(last, "timestamp") else None
        if stamp:
            age = time.time() - stamp
            print(f"    → 距这次快照已过 {age:.2f} 秒"
                  f"（本地插值公式：显示进度 = position + 已过时间 × 速率）")
    except Exception:
        pass


def print_cover_result(data: bytes | None, note: str, path: str, elapsed_ms: float) -> None:
    print("  [封面 thumbnail]")
    if not data:
        print(f"    ✗ 取不到封面：{note}")
        print("      → 阶段2 需要准备\"无封面时显示默认渐变圆盘\"的降级方案")
        return
    print(f"    ✓ 取到 {len(data)} 字节，格式={guess_image(data)}，耗时 {elapsed_ms:.1f}ms（{note}）")
    try:
        with open(path, "wb") as handle:
            handle.write(data)
        print(f"    ✓ 已保存到 {path}，直接双击打开看一眼是不是正确的专辑封面")
    except Exception as exc:
        print(f"    ✗ 保存失败：{exc}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def run(args: argparse.Namespace) -> int:
    print(SEP)
    print("音璃 MusicGlass — SMTC 探测（只读）")
    print(SEP)
    print(f"  Python : {sys.version.split()[0]}  ({sys.executable})")
    try:
        from importlib.metadata import version as pkg_version

        print(f"  winrt  : winrt-runtime {pkg_version('winrt-runtime')}"
              f" / winrt-Windows.Media.Control {pkg_version('winrt-Windows.Media.Control')}")
    except Exception:
        pass
    print(f"  时间   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # COM 套间：WinRT 需要先初始化 COM（不同版本行为的差异就在这里）
    try:
        winrt_runtime.init_apartment(winrt_runtime.ApartmentType.MULTI_THREADED)
        print("  COM    : 已初始化（MULTI_THREADED）")
    except Exception as exc:
        print(f"  COM    : init_apartment 跳过（{type(exc).__name__}: {exc}）")

    print(SUB)
    manager = await SessionManager.request_async()
    if manager is None:
        print("[X] 拿不到 SMTC 会话管理器（系统版本太低？需要 Win10 1809+）")
        return 2

    sessions = list(manager.get_sessions())
    current = manager.get_current_session()
    print(f"当前共有 {len(sessions)} 个媒体会话：")
    for index, session in enumerate(sessions):
        app_id = attr(session, "source_app_user_model_id", "?")
        is_current = "← 当前会话" if current is not None and session is current else ""
        print(f"  [{index}] {app_id}  {is_current}")
    if not sessions:
        print("  （一个都没有）")
        print(SUB)
        print("""
[!] 没有任何播放器向系统上报媒体信息。请依次确认：
    1. QQ音乐**正在播放**（暂停有时也会上报，但有些版本要真的在播才注册）；
    2. QQ音乐不是网页面（网页版走浏览器自己的会话，AppUserModelId 会是浏览器）；
    3. Windows 设置 → 隐私和安全性 → 后台应用 / 媒体 里没被限制（Win11 才有开关）；
    4. 任务栏的媒体浮窗能显示这首歌吗？能的话说明系统收到了，脚本也应该收到。
       —— 如果任务栏浮窗有、但脚本看不到，那就是 winrt 权限/版本问题，告诉我。
    5. 如果 QQ音乐确实完全不注册 SMTC，我们就用备选方案：读窗口标题解析歌名。
""")
        return 0

    # 选一个会话来看详情：默认当前会话，其次第一个
    target = current if current is not None else sessions[0]
    if args.session is not None:
        if not 0 <= args.session < len(sessions):
            print(f"[X] --session {args.session} 超出范围（0~{len(sessions) - 1}）")
            return 2
        target = sessions[args.session]

    print(SUB)
    print(f"详细字段（会话：{attr(target, 'source_app_user_model_id', '?')}）")
    print(SUB)
    try:
        props = await target.try_get_media_properties_async()
        print_media_properties(props)
    except Exception as exc:
        props = None
        print(f"  ✗ 读取媒体属性失败：{type(exc).__name__}: {exc}")

    try:
        info = target.get_playback_info()
        print_playback_info(info)
    except Exception as exc:
        print(f"  ✗ 读取播放信息失败：{exc}")

    try:
        print_timeline(target.get_timeline_properties())
    except Exception as exc:
        print(f"  ✗ 读取进度失败：{exc}")

    if props is not None:
        started = time.perf_counter()
        data, note = await read_thumbnail_bytes(attr(props, "thumbnail", None))
        print_cover_result(data, note, args.cover, (time.perf_counter() - started) * 1000)

    # ---------------- 监听 ----------------
    print(SUB)
    print(f"开始监听 {args.seconds} 秒（同时统计事件 + 每秒打一次快照）")
    print("  建议：这几秒里**切一首歌 / 暂停一下 / 拖一下进度条**，才能看出上报能力")
    print(SUB)

    counters = {"media": 0, "playback": 0, "timeline": 0, "sessions": 0, "current": 0}
    events_seen: list[str] = []

    def make_handler(kind: str, label: str):
        def handler(_sender, _args):  # noqa: ANN001
            counters[kind] += 1
            stamp = datetime.now().strftime("%H:%M:%S")
            events_seen.append(f"{stamp} {label}")
            print(f"    [事件 {stamp}] {label}", flush=True)
        return handler

    handlers = [
        (target.add_media_properties_changed, make_handler("media", "媒体属性变了（换歌/改信息）")),
        (target.add_playback_info_changed, make_handler("playback", "播放信息变了（播放/暂停/循环）")),
        (target.add_timeline_properties_changed, make_handler("timeline", "进度变了（进度条推进）")),
        (manager.add_sessions_changed, make_handler("sessions", "会话列表变了（播放器开关/切换）")),
        (manager.add_current_session_changed, make_handler("current", "当前会话变了")),
    ]

    positions: list[tuple[float, float]] = []      # (墙钟时间, 上报进度秒)
    titles: list[str] = []                          # 出现过的歌曲名（看换歌有没有被上报）

    try:
        deadline = time.monotonic() + args.seconds
        tick = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            pump_windows_messages()
            tick += 1
            if tick % 2:                            # 每 1 秒打一行快照
                continue
            try:
                timeline = target.get_timeline_properties()
                info = target.get_playback_info()
                current_props = await target.try_get_media_properties_async()
                title = (current_props.title or "") if current_props else ""
                if title and (not titles or titles[-1] != title):
                    titles.append(title)
                position = seconds_of(timeline.position)
                end = seconds_of(timeline.end_time)
                status = fmt_enum(info.playback_status)
                positions.append((time.monotonic(), position))
                line = (f"    {datetime.now().strftime('%H:%M:%S')}  {status:<16}"
                        f" 进度 {position:8.2f} / {end:8.2f}s"
                        f"  循环={fmt_enum(info.auto_repeat_mode)}"
                        f"  随机={info.is_shuffle_active}"
                        f"  {title[:24]}")
                print(line, flush=True)
            except Exception as exc:
                print(f"    [快照失败] {exc}", flush=True)
    finally:
        # 取消订阅，别留着回调
        for remover, handler in zip(
            (target.remove_media_properties_changed, target.remove_playback_info_changed,
             target.remove_timeline_properties_changed),
            [h for _, h in handlers[:3]],
        ):
            try:
                remover(handler)
            except Exception:
                pass

    # ---------------- 结论 ----------------
    print(SUB)
    print("事件统计（本进程实际收到的）")
    print(f"  媒体属性变化 {counters['media']} 次 | 播放信息变化 {counters['playback']} 次 | "
          f"进度变化 {counters['timeline']} 次")
    print(f"  会话列表变化 {counters['sessions']} 次 | 当前会话变化 {counters['current']} 次")

    moving = False
    if len(positions) >= 3:
        first_t, first_p = positions[0]
        last_t, last_p = positions[-1]
        delta_wall = last_t - first_t
        delta_pos = last_p - first_p
        moving = delta_pos > 0.5
        print(f"\n进度是否自己走动：{'是 ✓' if moving else '否 ✗'}")
        print(f"  {delta_wall:.1f} 秒里上报进度从 {first_p:.2f}s 变到 {last_p:.2f}s"
              f"（变化 {delta_pos:+.2f}s）")
        if not moving:
            print("  → 说明播放器**不推送进度更新**。阶段2 必须本地插值：")
            print("     显示进度 = 最后一次上报的 position + (现在 - last_updated_time) × 播放速率")
        if counters["timeline"] == 0:
            print("  注：本进程没收到任何 TimelinePropertiesChanged 事件。")
            print("      可能是播放器不发，也可能是 WinRT 回调在这个环境投递不过来。")
            print("      阶段2 会用\"轮询 + 事件\"双保险，不依赖事件一定到达。")
    else:
        print("\n进度样本太少，无法判断（监听时间太短？）")

    print(f"\n监听期间出现的歌曲名：{titles if titles else '（本次没换歌）'}")

    # ---------------- 能力清单（阶段2 的设计依据）----------------
    print(SUB)
    print("能力清单（这份表比任何描述都重要，阶段2 照它写）")
    try:
        final_props = await target.try_get_media_properties_async()
        final_info = target.get_playback_info()
        final_timeline = target.get_timeline_properties()
        final_controls = attr(final_info, "controls", None)
        end_seconds = seconds_of(attr(final_timeline, "end_time", None)) or 0.0
        can_seek = (final_controls is not None and not isinstance(final_controls, str)
                    and attr(final_controls, "is_playback_position_enabled", False) is True)
        rows = [
            ("歌名", bool(attr(final_props, "title", ""))),
            ("歌手", bool(attr(final_props, "artist", ""))),
            ("专辑", bool(attr(final_props, "album_title", ""))),
            ("专辑歌手", bool(attr(final_props, "album_artist", ""))),
            ("封面", bool(data)),
            ("总时长", end_seconds > 0),
            ("播放进度数值", seconds_of(attr(final_timeline, "position", None)) is not None),
            ("进度自己推进", moving),
            ("拖进度条 seek", can_seek),
            ("循环模式", attr(final_info, "auto_repeat_mode", None) is not None),
            ("随机播放状态", attr(final_info, "is_shuffle_active", None) is not None),
            ("事件推送", sum(counters.values()) > 0),
        ]
        for name, ok in rows:
            print(f"    {name:<14} {'✓ 有' if ok else '✗ 没有'}")
    except Exception as exc:
        print(f"    （能力清单生成失败：{exc}）")

    print(SEP)
    print("""
请把上面整段输出贴给我（尤其是下面这几项），我据此写 SMTCSource：
  1. 会话列表里 QQ音乐 的 AppUserModelId 是什么（用来精确认准它，避免认错浏览器）；
  2. 歌名/歌手/专辑 能不能读到；
  3. 封面那一行是 ✓ 还是 ✗；
  4. 循环模式打印出来的枚举值（NONE/TRACK/LIST）和你播放器里实际设置对不对得上；
  5. 「进度是否自己走动」是是还是否；
  6. 事件统计里那几个数字（决定阶段2 用事件还是轮询）。
""")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="音璃 · SMTC 探测脚本（只读）")
    parser.add_argument("--seconds", type=float, default=20.0, help="监听时长（秒），默认 20")
    parser.add_argument("--session", type=int, default=None, help="看第几个会话的详情（默认当前会话）")
    parser.add_argument("--cover", default="probe_cover.png", help="封面保存路径")
    parser.add_argument("--try-controls", action="store_true",
                        help="【慎用】发送真实控制指令测试播放/暂停等（会操作你的播放器）")
    args = parser.parse_args()

    if args.try_controls:
        print("[!] --try-controls 会真的操作你的播放器，本步骤你不需要它，建议先不加。\n")

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
