# -*- coding: utf-8 -*-
"""音璃 MusicGlass —— 启动入口。

用 .pyw 扩展名：双击时由 pythonw.exe 解释，不会弹出黑色控制台窗口，
这是"静默启动"的前提（阶段5 打包成 exe 后由 --noconsole 保证同样效果）。

命令行参数（都是可选的，平时双击即可）：
    --backend pyglass|qt   临时切换玻璃后端（不写回配置）
    --scale 1.4            临时指定缩放
    --reset-config         忽略已保存的配置，恢复到默认并覆盖
    --debug                打印一行后端/窗口状态，方便排查
    --screenshot 路径      把窗口自绘内容导出成 png 后退出（自动化验证布局用）
    --exit-after 秒        多少秒后自动退出
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

from musicglass import __version__
from musicglass.config import Config, config_path
from musicglass.core.mock_source import MockSource
from musicglass.core.smtc_source import SMTCSource
from musicglass.ui.glass_window import GlassWindow


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="MusicGlass", description="音璃 · 桌面音乐小组件")
    parser.add_argument("--backend", choices=("pyglass", "qt"), help="玻璃后端（临时覆盖配置）")
    parser.add_argument("--source", choices=("smtc", "mock"),
                        help="数据源：smtc=真实读 QQ音乐（默认）；mock=假数据")
    parser.add_argument("--no-tray", action="store_true", help="不启用系统托盘")
    parser.add_argument("--silent", action="store_true",
                        help="静默启动（开机自启用的那条命令）：绝不抢焦点、不打印任何东西")
    parser.add_argument("--stats-file", metavar="PATH",
                        help="把内部计数器周期写进 jsonl 文件（挂机稳定性测试用）")
    parser.add_argument("--stats-interval", type=float, default=10.0,
                        metavar="SEC", help="写统计的间隔秒数（默认 10）")
    parser.add_argument("--capture", choices=("auto", "gdi"),
                        help="抓屏方式：auto=放大镜（默认，组件能被录屏）；gdi=BitBlt（组件对截图隐形）")
    parser.add_argument("--perf", type=float, metavar="SEC",
                        help="性能诊断：每 SEC 秒打印一行各项耗时（折射/抓屏/重绘）")
    parser.add_argument("--selftest-tray", action="store_true",
                        help="自检托盘：打印菜单、试隐藏/显示、试开机自启开关（含注册表往返，会自动恢复）")
    parser.add_argument("--on-top", action="store_true", help="强制置顶（浮在其它窗口之上）")
    parser.add_argument("--no-desktop-layer", action="store_true", help="不贴桌面层（普通窗口，方便对比）")
    parser.add_argument("--layer-mode", choices=("zorder", "workerw"),
                        help="贴桌面的具体做法：zorder=Z序插入（默认）；workerw=SetParent 到桌面")
    parser.add_argument("--scale", type=float, help="缩放系数，例如 1.2（临时覆盖配置）")
    parser.add_argument("--reset-config", action="store_true", help="重置配置为默认值")
    parser.add_argument("--debug", action="store_true", help="打印后端与窗口状态")
    parser.add_argument("--pos", metavar="X,Y", help="强制面板屏幕坐标，例如 120,300（开发调试用）")
    parser.add_argument("--force-hover", action="store_true",
                        help="强制显示为悬停态（不透时 100%%，验证界面用）")
    parser.add_argument("--no-anim", action="store_true",
                        help="关掉帧动画（封面不转/频谱不动/光晕不呼吸/切歌硬切），回到阶段3 的静态样子")
    parser.add_argument("--fps", type=int, metavar="N",
                        help="动画帧率（默认 30；60 更顺但 CPU 几乎翻倍）")
    parser.add_argument("--cycle-songs", type=float, metavar="SEC",
                        help="每 SEC 秒自动下一首（验证切歌过渡动画用，配合 --source mock）")
    parser.add_argument("--screenshot", metavar="PATH", help="导出窗口自绘内容为 png 后退出")
    parser.add_argument("--exit-after", type=float, metavar="SEC", help="SEC 秒后自动退出")
    return parser.parse_args(argv)


def _ensure_std_streams() -> None:
    """把 sys.stdout / sys.stderr 接上 os.devnull（如果它们是 None）。

    【为什么必须有】打包成 --noconsole 的 exe 之后（阶段5），Python 的
    sys.stdout / sys.stderr 会是 None —— 而整个程序里到处都有 print（诊断日志、
    贴桌面失败提醒、通知消息……），一 print 就 AttributeError。
    组件是常驻桌面的，不能因为“没控制台”就在某个分支上静默崩溃。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        sink = open(os.devnull, "w", encoding="utf-8")
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = sink
    if sys.stderr is None:
        sys.stderr = sink


def run_tray_selftest(window, tray, config) -> int:
    """托盘自检：菜单、隐藏/显示、开机自启（含注册表往返）。

    为什么提供这个：托盘的东西靠"截图看图标"很不靠谱（托盘区在任务栏里，
    位置随系统设置变），而这些行为其实都可以用代码直接验证。
    注意：它会真的写一次注册表，但结束前会把状态恢复成原样。
    """
    from PyQt6.QtWidgets import QSystemTrayIcon

    from musicglass.platform import autostart

    print("=" * 72)
    print("音璃 托盘自检")
    print("=" * 72)
    if tray is None:
        print("[X] 托盘没有建立（--no-tray 或系统托盘不可用）")
        return 1

    print(f"系统托盘可用   : {QSystemTrayIcon.isSystemTrayAvailable()}")
    print(f"图标已显示     : {tray.is_visible}")
    print(f"提示文字       : {tray._tray.toolTip().replace(chr(10), ' / ')}")
    print(f"菜单项目       : " + "  |  ".join(tray.menu_titles()))
    print(f"窗口处于托盘模式: {window._tray_mode}   (True = 关闭窗口只是隐藏，不会退出)")

    print("\n[1] 隐藏 / 显示")
    window.hide_window()
    print(f"    隐藏后 isVisible={window.isVisible()}")
    print(f"    玻璃后端状态 : {window._backend.status_text()[:60]}…")
    window.show_window()
    print(f"    显示后 isVisible={window.isVisible()}")
    print(f"    托盘菜单首项 : {tray.menu_titles()[0]}   (应该变回『隐藏组件』)")

    print("\n[2] 开机自启开关（注册表 HKCU\\...\\Run）")
    original_enabled = autostart.is_enabled()
    print(f"    原始状态     : {autostart.status_text()}")
    print(f"    将写入的命令行: {autostart.launch_command()}")
    print(f"    开启结果     : {'成功' if autostart.enable() else '失败'}")
    print(f"    注册表实际值 : {autostart.current_value()}")
    print(f"    再查一次     : {autostart.is_enabled()}")
    restored = autostart.set_enabled(original_enabled)
    print(f"    恢复原状     : {'成功' if restored else '失败'}"
          f"   现在 is_enabled={autostart.is_enabled()}（原始={original_enabled}）")
    print(f"    最终状态     : {autostart.status_text()}")
    print("=" * 72)
    return 0


def shutdown(window, source, tray) -> None:
    """退出前的统一收尾（顺序很重要）。

    【为什么必须显式做】实测：不做的话进程会在退出时 abort（0xC0000409，看起来像崩溃）。
    原因是 Qt 退出时靠 Python 的垃圾回收去销毁对象，而：
      * 媒体源的工作线程可能还在发信号（信号发到已被销毁的窗口上就炸）；
      * pyglass 的放大镜/抓屏对象析构时要碰 Win32 资源，时机不可控。
    所以按"先断信号源 -> 再放后台资源 -> 最后收 UI"的顺序来。
    整个函数不抛异常：退出路径上再出错没有任何好处。
    """
    try:
        source.stop()          # 会 wait 到线程退出，之后不会再有信号进来
    except Exception:
        pass
    try:
        backend = getattr(window, "_backend", None)
        if backend is not None:
            backend.cleanup()  # 关掉放大镜/截图器
    except Exception:
        pass
    try:
        if tray is not None:
            tray.hide()        # 别在托盘区留一个点不动的幽灵图标
    except Exception:
        pass
    try:
        window.set_tray_mode(False)   # 之后 close() 不该再被拦成"隐藏"
        window.close()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()          # 打包成无控制台 exe 后，这一步必须先做
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # 高 DPI 必须在 QApplication 创建之前设置：
    # PassThrough 保留非整数缩放（比如 125%），窗口在高分屏上不会被取整到模糊。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("MusicGlass")
    app.setApplicationDisplayName("音璃")
    app.setApplicationVersion(__version__)
    # 有托盘时，关掉窗口不能把程序也退掉（否则"隐藏到托盘"就没意义了）。
    # 后面如果发现托盘建不起来，会再改回 True。
    app.setQuitOnLastWindowClosed(False)

    # ---- 配置 ----
    config = Config.load()
    if args.reset_config:
        config = Config()
        config.save()
    if args.backend:
        config.glass_backend = args.backend
    if args.scale:
        config.scale = args.scale
    if args.on_top:
        config.always_on_top = True
    if args.no_desktop_layer:
        config.desktop_layer = False
    if args.layer_mode:
        config.desktop_layer_mode = args.layer_mode
    if args.source:
        config.media_source = args.source
    if args.capture:
        config.capture_method = args.capture
    if args.pos:
        try:
            px, py = (int(v) for v in args.pos.split(","))
            config.pos_x, config.pos_y = px, py
        except Exception:
            print(f"[音璃] --pos 格式错误: {args.pos}，应为 120,300")
    if args.force_hover:
        config.opacity_idle = 1.0
    if args.no_anim:
        config.animations = False
    if args.fps:
        config.animation_fps = args.fps

    # ---- 数据源 ----
    # 阶段2：默认接真实数据（SMTC 读 QQ音乐）；加 --source mock 可以切回假数据调试 UI。
    # 为什么不做"自动回退"：切换数据源需要在运行中替换对象并重连信号，
    # 容易出隐蔽 bug；而已知 QQ音乐 关闭时组件会显示"未检测到 QQ音乐"，
    # 这比"默默显示假歌"更诚实也更好排查。
    if config.media_source == "mock":
        source = MockSource()
    else:
        source = SMTCSource(poll_ms=config.smtc_poll_ms)

    # ---- 主窗口 ----
    window = GlassWindow(config, source)
    if args.silent:
        # 开机自启的场合：用户开机后可能立刻在打字/点东西，
        # 组件绝对不能把焦点抢过来。注意这个属性必须在 show() 之前设。
        window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    window.show()
    source.start()
    if args.force_hover:
        window.force_hover()      # 强制悬停态（不透明），验证配色/排版用

    # ---- 可选：定时自动下一首（验切歌过渡用）----
    # 为什么需要它：一首歌要放三五分钟才换，想看一眼切歌过渡得等很久。
    # 配 --source mock 用：每 SEC 秒强制换一首，一直循环。
    if args.cycle_songs:
        cycle_timer = QTimer()
        cycle_timer.setInterval(int(max(0.5, args.cycle_songs) * 1000))
        cycle_timer.timeout.connect(source.next_track)
        cycle_timer.start()
        print(f"[音璃] 每 {args.cycle_songs:.1f} 秒自动下一首（验证切歌过渡）")

    # 调试参数都是"临时覆盖"，绝不能写回配置文件。
    # 【踩坑记录】不这样做的话，用 --on-top 调试一次，退出时就会把
    # always_on_top=true 永久写进配置 —— 用户下次正常启动就变成置顶了。
    overridden = any((args.backend, args.scale, args.pos, args.on_top,
                      args.no_desktop_layer, args.layer_mode, args.source,
                      args.force_hover, args.no_anim, args.fps))
    if overridden:
        window.set_persist_enabled(False)

    # ---- 系统托盘 ----
    tray = None
    if config.tray_enabled and not args.no_tray:
        from musicglass.platform.tray import TrayIcon

        tray = TrayIcon(window, config, source)
        if tray.show():
            window.set_tray_mode(True)          # 关闭窗口 = 隐藏到托盘
            app.aboutToQuit.connect(tray.hide)  # 退出时别留下一个幽灵图标
        else:
            tray = None
    if tray is None:
        # 没托盘就回到"关窗即退出"，不留后台进程
        app.setQuitOnLastWindowClosed(True)
        window.set_tray_mode(False)

    if args.debug:
        def _dump_debug() -> None:
            # 延后打印：后端是在 showEvent 里通过 singleShot(0) 才 configure/start 的，
            # 立刻打印会看到"还没开始抓屏"的假象。
            print(f"音璃 MusicGlass v{__version__}  (配置: {config_path()})")
            for line in window.debug_lines():
                print("  " + line)
            if tray is not None:
                print("  托盘菜单 : " + " ｜ ".join(tray.menu_titles()))
            print("  快捷键: ESC 退出 | 空格 播放/暂停 | ←/→ 快退快进 | ↑/↓ 或滚轮 缩放")
            print("  托盘  : 左键单击=显示/隐藏 | 中键=播放/暂停 | 右键=菜单")

        QTimer.singleShot(1500, _dump_debug)

    if args.selftest_tray:
        code = run_tray_selftest(window, tray, config)
        shutdown(window, source, tray)
        return code

    if args.screenshot:
        # 截图要比退出早一点：两者用同一个延时的话会同时触发，
        # 谁先执行看运气 —— 之前就有几次截图静默丢失（不报错、也不生成文件）。
        delay = int(max(0.5, (args.exit_after or 2.5) - 0.5) * 1000)

        def _shot() -> None:
            pixmap = window.grab()          # 取窗口自绘结果（不含桌面背景）
            ok = pixmap.save(args.screenshot)
            print(f"[音璃] 截图已保存: {args.screenshot} ({'成功' if ok else '失败'})")
            app.quit()

        QTimer.singleShot(delay, _shot)
    if args.exit_after:
        QTimer.singleShot(int(args.exit_after * 1000), app.quit)

    # ---- 可选：性能诊断 ----
    # 只在显式传 --perf 时打开。它是"先量后优化"的前提：
    # 43% CPU 到底是花在折射、抓屏还是重绘上，不看数字是猜不出来的。
    if args.perf:
        from musicglass.perf import monitor
        from musicglass.ui.glass_backend import PyGlassBackend

        monitor.configure(args.perf)
        PyGlassBackend.install_grab_timing()
        monitor.start()
        print(f"[性能] 已开启，每 {args.perf:.0f} 秒打印一行")

    app.aboutToQuit.connect(lambda: shutdown(window, source, tray))

    # ---- 可选：把内部计数器周期写盘（挂机稳定性测试用）----
    # 为什么需要：内存/句柄是外部指标，但"读了多少次快照、封面缓存了几张、
    # 绘制出了几次错"只有程序自己知道。两者放一起才能判断是不是泄漏。
    if args.stats_file:
        started_at = time.monotonic()
        stats_timer = QTimer()
        stats_timer.setInterval(int(max(1.0, args.stats_interval) * 1000))

        def _write_stats() -> None:
            loader = getattr(source, "cover_loader", None)
            payload = {
                # 真实进程的 PID。
                # 【为什么必须写出来】Windows 上 venv 的 python.exe 只是个"转发器"：
                # 它只有几 MB 内存、几乎不吃 CPU，真正跑应用的是它启的子进程。
                # 挂机测试工具如果不拿这个 PID，量到的就是那个空壳。
                "pid": os.getpid(),
                "up_s": round(time.monotonic() - started_at, 1),
                "state": source.state.name,
                "available": bool(getattr(source, "is_available", True)),
                "snapshots": getattr(source, "_snapshot_count", 0),
                "failures": getattr(source, "_failures", 0),
                "thread_restarts": getattr(source, "_restarts", 0),
                "cover_cache": getattr(loader, "cache_size", 0),
                "disc_cache": getattr(loader, "disc_cache_size", 0),
                "paint_errors": window._paint_errors,
                "occluded": window._occluded,
                "toast": bool(window._toast_text),
            }
            try:
                with open(args.stats_file, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except OSError:
                pass

        stats_timer.timeout.connect(_write_stats)
        stats_timer.start()

    try:
        return app.exec()
    finally:
        shutdown(window, source, tray)


if __name__ == "__main__":
    raise SystemExit(main())
