# -*- coding: utf-8 -*-
"""音璃主窗口。

这个文件是整个 UI 的"总装车间"，负责四件事：

1. **几何**：窗口 = 面板 + 一圈透明留白（MARGIN），留白用来放阴影和氛围光。
2. **绘制顺序**：氛围光 → 玻璃 → 面板内容（封面/文字/进度/按钮/频谱）→ 缩放手柄。
3. **交互**：拖动窗口、右下角缩放、滚轮缩放、悬停高亮、点击按钮、拖拽进度条。
4. **性能**：只在必要时重绘，且尽量只重绘"脏区域"。

关于性能的关键设计（阶段0.5 实测出来的教训：整块重合成 512x232 在 4 核机器上要 20% 单核）：

    * 进度更新（每 200ms 一次）**只重绘底部那一行**，不重绘整块玻璃；
    * 玻璃的折射结果被后端缓存，重绘窗口 ≠ 重新折射；
    * 鼠标透明度过渡结束后就停止重绘，静态时 CPU 基本为 0。
"""

from __future__ import annotations

import sys
import time

from PyQt6.QtCore import QEasingCurve, QPoint, QPointF, QRectF, QTimer, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen, QRegion
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from .. import __display_name__, __version__
from ..config import Config
from ..core.models import PlaybackState
from ..core.source import MediaSource
from ..perf import monitor
from ..platform import autostart, desktop_layer
from ..platform.winutil import system_idle_seconds
from . import theme
from .animation import AnimationClock, DiscSpin, EqualizerBars, GlowBreath, TrackTransition
from .glass_backend import create_backend
from .layout import PanelLayout
from .widgets.album_disc import AlbumDisc
from .widgets.ambient_glow import AmbientGlow
from .widgets.controls import BTN_LOOP, BTN_NEXT, BTN_PLAY, BTN_PREV, Controls
from .widgets.equalizer import Equalizer
from .widgets.progress_bar import ProgressBar
from .widgets.text_panel import TextPanel, draw_text

# 窗口距屏幕边缘多少像素（首次启动自动定位用）
SCREEN_INSET = 28
# 保存配置的防抖延迟：拖动中不要每像素都写盘
SAVE_DEBOUNCE_MS = 700


class GlassWindow(QWidget):
    """无边框、透明、液态玻璃的桌面小组件。"""

    def __init__(self, config: Config, source: MediaSource, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._source = source

        # ---- 窗口外观：无边框 + 不进任务栏 ----
        # 注意：默认**不加** WindowStaysOnTopHint。那会让组件一直挡在微信/浏览器上面。
        # 默认走"贴桌面层"（见 _apply_layer_mode）：压在桌面之上、普通窗口之下。
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            # 贴桌面模式：不接受焦点。原因见 platform/desktop_layer.set_no_activate
            # —— 不这样的话，点一下组件就会被"激活"并抬到其它普通窗口前面去。
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowFlags(flags)
        # 强制创建原生窗口：贴桌面要用 winId 操原生句柄
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 不按键也要收到 MouseMove，才能精确判断鼠标是否在面板上
        self.setMouseTracking(True)
        self.setWindowTitle(f"{__display_name__} MusicGlass v{__version__}")

        # ---- 面板尺寸（按缩放系数从设计基准换算）----
        self._panel_w = max(120, int(round(theme.BASE_PANEL_W * config.scale)))
        self._panel_h = max(60, int(round(theme.BASE_PANEL_H * config.scale)))
        self._layout_cache: PanelLayout | None = None

        # ---- 玻璃后端 ----
        self._backend = create_backend(self, config)
        self._backend.changed.connect(self._on_background_changed)

        # ---- 自绘部件 ----
        self._disc = AlbumDisc()
        self._text = TextPanel()
        self._progress = ProgressBar()
        self._controls = Controls()
        self._equalizer = Equalizer()
        self._glow = AmbientGlow()
        # ---- 阶段4：动画 ----
        # 一个共享时钟 + 四个驱动（详见 ui/animation.py 顶部的说明）。
        # 驱动只负责"算状态"，重绘区域由这里的回调决定：
        # 封面只重画圆盘那一块、频谱只重画那一排、光晕只重画面板外的那一圈。
        self._eq_heights = list(Equalizer.DEFAULT_HEIGHTS)
        self._disc_angle = 0.0
        self._glow_strength = 1.0      # 1.0 = 阶段3 的静态亮度
        self._content_alpha = 1.0      # 切歌过渡用的内容透明度（玻璃不跟着淡）
        self._display_track = None     # 当前在画的歌（过渡期间是"旧的"那首）
        self._pending_track = None     # 淡出结束后要换成的那首
        self._glow_band_key = None     # 氛围光重绘区域（外圈）的缓存 key
        self._glow_band_cache = QRegion()

        self._clock = AnimationClock(config.animation_fps, self)
        self._disc_spin = DiscSpin(theme.DISC_SPEED_DEG_S, self._on_disc_angle)
        self._bars = EqualizerBars(self._on_eq_heights, theme.EQ_PHASE_SPEED)
        self._glow_breath = GlowBreath(self._on_glow_strength)
        self._transition = TrackTransition(theme.TRANSITION_OUT_S, theme.TRANSITION_IN_S,
                                           self._on_transition_alpha, self._on_transition_swap)
        for consumer in (self._disc_spin, self._bars, self._glow_breath, self._transition):
            self._clock.add(consumer)

        # ---- 交互状态 ----
        self._hover_inside = False
        self._hovered_button = -1
        self._hovered_grip = False
        self._pressed_button = -1
        self._dragging = False
        self._drag_offset = QPoint()
        self._resizing = False
        self._resize_origin_mouse = QPointF()
        self._resize_origin_w = 0
        self._seeking = False
        self._preview_seconds: float | None = None
        self._persist_enabled = True
        self._paint_errors = 0            # 绘制异常计数（--debug 会报）
        # 托盘模式：开启后"关闭窗口"= 隐藏到托盘（而不是退出）
        self._tray_mode = False
        self._quitting = False

        # ---- 拖动（用鼠标位移驱动，不用绝对坐标）----
        self._drag_mouse_origin = QPoint()
        self._drag_window_origin = QPoint()

        # ---- 桌面层（贴桌面，而不是浮在顶层）----
        self._shown_once = False
        self._desktop_parent = 0            # workerw 模式：SetParent 后的父窗口句柄
        self._desktop_parent_class = ""
        self._desktop_attached = False      # workerw 模式是否已贴
        self._desktop_target = 0            # zorder 模式：Z 序参照窗口（桌面层最上面那个）
        self._desktop_target_class = ""
        self._in_desktop_layer = False      # zorder 模式是否已就位
        self._occluded = False              # 桌面被别的程序盖住（只用来停抓屏，不停动画）
        self._widget_covered = False        # 组件被别的窗口盖住吗（**动画**判据：放过外壳浮窗）
        self._capture_covered = False       # 抓屏判据：外壳浮窗也算压住了（否则玻璃映出别人的窗口）
        self._capture_blocked = False       # 上一轮门控是否拦住了抓屏（用来识别"重新放开"那一刻）
        self._perf_degraded_at: float | None = None   # 抓屏降级是从什么时候开始的（时间兜底用）
        self._perf_force_used = False       # 这一轮降级是否已经用过"强制恢复"
        self._probe_exposed = 0             # 采样点里"露在外面"的个数（动画判据，诊断用）
        self._probe_capture_exposed = 0     # 采样点里对"抓屏"算露出的个数（诊断用）
        self._probe_total = 0
        self._perf_level = 0                # 抓屏降级档位：0 正常 / 1 降频 / 2 只动画
        self._perf_bad = 0                  # 连续多少次巡检发现动画被卡
        self._perf_good = 0
        self._watcher = None

        # ---- 透明度动画 ----
        # 不用 setWindowOpacity：那种做法在"分层窗口 + 逐像素 alpha"下行为不稳，
        # 而且没法只对玻璃生效。这里用一个 0~1 的系数，绘制时逐层乘上去。
        self._alpha = config.opacity_idle
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(theme.FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._on_fade_value)

        # ---- 保存配置的防抖定时器 ----
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._persist)

        # ---- 临时提示条（toast）----
        # 用来把"播放器不支持这个操作"这类信息直接显示在组件上。
        # 为什么必须有它：贴桌面模式下窗口不拿键盘焦点，也没法弹 MessageBox，
        # 不提示的话用户只会觉得"点了没反应"。
        self._toast_text = ""
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._clear_toast)

        # ---- 接线：数据源 ----
        self._source.track_changed.connect(self._on_track_changed)
        self._source.position_changed.connect(self._on_position_changed)
        self._source.state_changed.connect(self._on_state_changed)
        self._source.loop_changed.connect(self._on_loop_changed)
        self._source.notice.connect(self._on_notice)

        # ---- 尺寸与位置 ----
        self._apply_panel_size()
        self._restore_position()
        # 数据源此刻还是 UNKNOWN，_sync_animations 会得出"谁都不用动"，时钟不启动
        self._sync_animations()

    # ==================================================================
    # 几何
    # ==================================================================
    def panel_rect(self) -> QRectF:
        """玻璃面板在窗口坐标系里的矩形。"""
        return QRectF(theme.MARGIN, theme.MARGIN, self._panel_w, self._panel_h)

    def layout_info(self) -> PanelLayout:
        """取布局结果（带缓存：同一尺寸只算一次）。"""
        if self._layout_cache is None or self._layout_cache.panel.size() != self.panel_rect().size():
            self._layout_cache = PanelLayout.compute(self.panel_rect())
        return self._layout_cache

    def _apply_panel_size(self) -> None:
        """面板尺寸 -> 窗口尺寸。窗口比面板大两圈 MARGIN。"""
        self.resize(self._panel_w + 2 * theme.MARGIN, self._panel_h + 2 * theme.MARGIN)
        self._layout_cache = None
        self._backend.set_geometry(self.panel_rect(), theme.RADIUS * (self._panel_h / theme.BASE_PANEL_H))

    def _restore_position(self) -> None:
        """恢复上次的位置；没有记录就放到屏幕右下角。"""
        area = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        x, y = self._config.pos_x, self._config.pos_y
        if x is None or y is None:
            if area is not None:
                x = area.right() - self.width() - SCREEN_INSET + theme.MARGIN
                y = area.bottom() - self.height() - SCREEN_INSET + theme.MARGIN
            else:
                x = y = 200
        # 配置里存的是"面板左上角"的屏幕坐标
        self._place_panel_at_screen(int(x), int(y))

    # ------------------------------------------------------------------
    # 屏幕坐标换算
    # ------------------------------------------------------------------
    # 为什么要自己做这套换算：
    # 窗口被 SetParent 到 WorkerW/Progman 之后，原生父窗口就不归 Qt 管了，
    # Qt 依然认为自己是个顶层窗口 —— 于是 mapToGlobal() 会少算父窗口的偏移，
    # 算出来的"屏幕坐标"是错的（折射会错位、配置会存错位置）。
    # 所以：屏幕坐标 = 父窗口原点 + 窗口自身坐标。
    def _parent_origin(self) -> QPoint:
        if self._desktop_attached and self._desktop_parent:
            x, y = desktop_layer.parent_screen_origin(self._desktop_parent)
            return QPoint(x, y)
        return QPoint(0, 0)

    def _window_screen_pos(self) -> QPoint:
        """窗口（含 MARGIN 留白）左上角的屏幕坐标。"""
        return self.pos() + self._parent_origin()

    def panel_screen_pos(self) -> QPoint:
        """面板（玻璃本体）左上角的屏幕坐标。折射后端和配置保存都靠它。"""
        return self._window_screen_pos() + QPoint(theme.MARGIN, theme.MARGIN)

    def _place_panel_at_screen(self, x: int, y: int) -> None:
        """把面板左上角摆到指定的屏幕坐标。"""
        origin = self._parent_origin()
        self.move(QPoint(x, y) - origin - QPoint(theme.MARGIN, theme.MARGIN))

    # ------------------------------------------------------------------
    # 桌面层
    # ------------------------------------------------------------------
    def _apply_layer_mode(self) -> None:
        """按配置决定层级：置顶 / 贴桌面 / 普通窗口（三者互斥）。"""
        if self._config.always_on_top:
            # 置顶模式：永远是顶层窗口，所以"被盖住"这个概念不成立，
            # 要确保抓屏是开着的（否则切到置顶后玻璃会停在冻结的画面上）
            self._leave_desktop_layer()
            desktop_layer.set_topmost(int(self.winId()), True)
            self._set_occluded(False)
            if self._watcher is not None:
                self._watcher.stop()
        elif self._config.desktop_layer:
            self._enter_desktop_layer()
            self._start_layer_watch()
        else:
            self._leave_desktop_layer()
            desktop_layer.set_topmost(int(self.winId()), False)
            self._set_occluded(False)
            if self._watcher is not None:
                self._watcher.stop()
        # 层级换了之后"组件到底露不露在桌面上"也跟着变（置顶模式直接就露着）
        self._update_widget_covered()

    # ---- 贴桌面（默认做法：Z 序插入）--------------------------------
    def _enter_desktop_layer(self) -> bool:
        """把窗口放到"桌面之上、所有普通窗口之下"。

        为什么不用 SetParent（虽然那是更常见的"桌面壁纸"做法）：
        实测发现 SetParent 到 WorkerW 后，窗口会被桌面图标层 SysListView32 压住——
        WindowFromPoint 在那个位置返回的是 SysListView32，意味着
        **鼠标点不到组件**（播放/拖动全失效）。
        Z 序插入没有这个副作用：它依旧是普通顶层窗口，鼠标、透明渲染都正常。
        （想回到 SetParent 做法：配置里把 desktop_layer_mode 改成 workerw）
        """
        if sys.platform != "win32":
            return False
        hwnd = int(self.winId())

        if self._config.desktop_layer_mode == "workerw":
            return self._set_parent_to_workerw(hwnd)

        # 先把可能存在的置顶去掉
        desktop_layer.set_topmost(hwnd, False)
        # 再补上 WS_EX_NOACTIVATE：防止"点一下组件，它就被抬到其它窗口前面"
        desktop_layer.set_no_activate(hwnd, True)
        if self._in_desktop_layer and desktop_layer.is_above_desktop_layer(hwnd):
            return True
        target, target_class, error = desktop_layer.insert_above_desktop(hwnd)
        if not target:
            self._in_desktop_layer = False
            print(f"[音璃] 贴桌面层失败：{error}；组件将作为普通窗口显示")
            return False
        self._desktop_target = target
        self._desktop_target_class = target_class
        self._in_desktop_layer = True
        return True

    def _set_parent_to_workerw(self, hwnd: int) -> bool:
        """workerw 模式：SetParent 到桌面 WorkerW（更"融进桌面"，但鼠标会被图标层挡）。"""
        if self._desktop_attached and self._desktop_parent:
            if desktop_layer.verify(hwnd, self._desktop_parent):
                return True
        target_screen = self.panel_screen_pos()
        parent, parent_class, error = desktop_layer.attach(hwnd)
        if not parent:
            self._desktop_attached = False
            print(f"[音璃] 贴桌面层失败：{error}；组件将作为普通窗口显示")
            return False
        self._desktop_parent = parent
        self._desktop_parent_class = parent_class
        self._desktop_attached = True
        # 贴上去之后坐标系变了，把面板摆回原来的屏幕位置
        self._place_panel_at_screen(target_screen.x(), target_screen.y())
        QTimer.singleShot(300, self._verify_desktop_attach)
        return True

    def _verify_desktop_attach(self) -> None:
        """核验 SetParent 是否还成立；不成立就重新贴一次。

        （Qt 在显示流程里会碰原生窗口，父子关系可能被丢掉，这是实测踩出来的坑）
        """
        if not self._desktop_attached or not self._desktop_parent:
            return
        hwnd = int(self.winId())
        if desktop_layer.verify(hwnd, self._desktop_parent):
            return
        target_screen = self.panel_screen_pos()
        parent, parent_class, error = desktop_layer.attach(hwnd)
        if parent:
            self._desktop_parent = parent
            self._desktop_parent_class = parent_class
            self._place_panel_at_screen(target_screen.x(), target_screen.y())
            print(f"[音璃] 父子关系被重置，已重新贴到桌面层（父窗口 {parent_class}）")
        else:
            self._desktop_attached = False
            self._desktop_parent = 0
            print(f"[音璃] 重新贴桌面失败：{error}；组件将作为普通窗口显示")

    def _leave_desktop_layer(self) -> None:
        """从桌面层摘出来（切置顶/普通窗口时）。"""
        if not self._desktop_attached and not self._in_desktop_layer:
            return
        hwnd = int(self.winId())
        if self._desktop_attached:
            target_screen = self.panel_screen_pos()
            desktop_layer.detach(hwnd)
            self._desktop_attached = False
            self._desktop_parent = 0
            self._desktop_parent_class = ""
            self._place_panel_at_screen(target_screen.x(), target_screen.y())
        desktop_layer.set_no_activate(hwnd, False)
        self._in_desktop_layer = False
        self._desktop_target = 0

    def _reassert_layer(self) -> None:
        """把 Z 序拉回正确位置。

        何时会跑偏：按 Win+D、点桌面、切全屏程序…… 系统都可能重新排 Z 序，
        把组件顶到奇怪的位置。所以每次轮询都顺手校一下，代价近乎为零。
        """
        if self._config.always_on_top or not self._config.desktop_layer:
            return
        if self._config.desktop_layer_mode != "zorder":
            return
        hwnd = int(self.winId())
        if desktop_layer.is_above_desktop_layer(hwnd):
            return
        target, target_class, error = desktop_layer.insert_above_desktop(hwnd)
        if target:
            self._desktop_target = target
            self._desktop_target_class = target_class
            self._in_desktop_layer = True
        elif error:
            print(f"[音璃] 重新贴桌面失败：{error}")

    def set_always_on_top(self, enabled: bool) -> None:
        """切换"置顶"。默认是关的。（阶段5 的托盘菜单 / 右键菜单会调它）

        修改窗口标志会让 Qt 销毁并重建原生窗口，所以必须重新贴一次桌面。
        """
        if enabled == self._config.always_on_top:
            return
        self._config.always_on_top = enabled
        visible = self.isVisible()
        target_screen = self.panel_screen_pos()
        # 重建原生窗口之前先把状态清干净，免得旧句柄的残留状态影响判断
        self._desktop_attached = False
        self._desktop_parent = 0
        self._in_desktop_layer = False

        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        if visible:
            self.show()
        self._place_panel_at_screen(target_screen.x(), target_screen.y())
        self._apply_layer_mode()
        self._schedule_save()

    def set_desktop_layer(self, enabled: bool) -> None:
        """切换"贴桌面"（右键菜单用）。和置顶互斥。"""
        if enabled == self._config.desktop_layer:
            return
        self._config.desktop_layer = enabled
        if enabled:
            self._config.always_on_top = False
        self.set_always_on_top(self._config.always_on_top)
        self._apply_layer_mode()
        self._schedule_save()

    def _start_layer_watch(self) -> None:
        """贴桌面后，每 800ms 盯两件事：

        1. 桌面有没有被别的程序盖住 —— 盖住就把抓屏停掉、重绘也跳过，CPU 归零；
           桌面重新露出来时再恢复，并立刻重抓一帧，保证画面不过期。
        2. Z 序有没有跑偏（Win+D、点桌面之后可能变），跑偏就拉回来。
        """
        if sys.platform != "win32":
            return
        if self._watcher is None:
            self._watcher = desktop_layer.DesktopVisibilityWatcher(int(self.winId()), 800, self)
            self._watcher.visibility_changed.connect(self._on_desktop_visibility)
        self._watcher.start()
    def _on_desktop_visibility(self, visible: bool) -> None:
        self._set_occluded(not visible)
        self._reassert_layer()
        self._update_widget_covered()

    def _set_occluded(self, occluded: bool) -> None:
        """桌面整体被盖住（阶段3 的前台窗口判据）：停了抓屏，露出时重画。

        只影响**抓屏**。动画是否挂起由 _widget_covered（真实重叠）决定，
        因为"前台是别的程序"并不等于"组件看不见"（见 _sync_clock_suspension）。
        """
        if occluded == self._occluded:
            return
        self._occluded = occluded
        self._apply_background_gate()

    def _capture_blocked_now(self) -> bool:
        """现在该不该**停抓屏**（正判据，方案 A）。

        * **有采样能力时**（贴桌面的 zorder / workerw 模式，_probe_applicable）：
          只看**真实重叠** _capture_covered + 可见性。前台是不是别的程序**不影响**它。
          这就是这次的 bug 修复：以前"前台是浏览器"就停抓屏，哪怕组件完整露着 ——
          最小化/挪开窗口后前台还是别的程序，抓屏就再也不开，玻璃一直是静态的，
          非得点一下桌面才恢复。
        * **没有采样能力时**（置顶 / 普通窗口模式）：退回前台启发式 _occluded 兜底，
          否则被最大化窗口完全盖住时还会一直抓屏（白烧 CPU）。
        """
        if not self.isVisible():
            return True
        if self._probe_applicable():
            return self._capture_covered
        return self._occluded

    def _apply_background_gate(self) -> None:
        """把"到底该不该**抓屏**"统一落到后端上（问题1 的核心）。

        三种情况都算"不该抓"（见 _capture_blocked_now）：
          1. 组件自己被盖住（_capture_covered：普通窗口和外壳浮窗都算）；
          2. 没有采样能力时，桌面整体被别的程序盖住（_occluded，前台启发式）；
          3. 窗口被藏起来了。

        注意抓屏判据用的是 _capture_covered 而不是 _widget_covered：
        外壳浮窗（任务栏预览、托盘溢出）会真的压在组件上，抓屏必须停
        （否则玻璃里映的是别人的窗口），但它们转瞬即逝，动画不该跟着停。
        动画挂起只看 _widget_covered + 可见性（见 _sync_clock_suspension）。

        被盖住时必须做两件事，缺一不可：
          * set_live(False) —— 停止继续抓屏；
          * drop_refraction() —— **清掉已经抓进来的那张图**。
        少了后者就会出现用户看到的现象：上层白窗口的内容被折射进玻璃里，
        而且因为"内容没再变"，pyglass 不会重抓，那张白图就一直挂在上面，
        要等下一次桌面变化（或几次点击/窗口切换）才恢复。
        """
        blocked = self._capture_blocked_now()
        rearming = self._capture_blocked and not blocked
        self._capture_blocked = blocked
        if rearming and self._perf_level:
            # 抓屏重新打开时顺手把降级档位清回 0。否则 _update_cadence 里
            # wanted = max(wanted, 750) * _perf_level，玻璃虽然回来了却卡在 1.5 秒一帧，
            # 而且还要再等 8 轮健康体检才肯升档（bug 修复）。
            self._perf_level = 0
            self._perf_bad = 0
            self._perf_good = 0
            self._perf_force_used = False
            self._update_cadence()
        self._backend.set_live(not blocked)
        if blocked:
            self._backend.drop_refraction()
            self.update()          # 重画一次：把"染色的玻璃"换成"纯静态玻璃"
        else:
            self._backend.refresh()
            self.update()
        self._sync_clock_suspension()

    def _sync_clock_suspension(self) -> None:
        """决定**动画时钟**该不该停。只由两件事决定：

        1. 窗口被隐藏了（托盘里收起来了）；
        2. **组件本身被别的窗口盖住**（_widget_covered，15 点真实重叠采样）。

        【为什么不再看 _occluded】_occluded 是"前台不是桌面/任务栏"的启发式，
        只在 800ms 巡检发现"前台窗口**身份翻转**"时才更新。于是把盖住组件的窗口
        挪开、甚至点组件，都不会让它变回 False（贴桌面模式给窗口加了
        WS_EX_NOACTIVATE，点它根本不改变前台）—— 动画就卡在挂起状态，
        用户看到的是"恢复得很随机、有时候要点桌面、有时候要点两下"。
        组件到底露没露出来，15 点采样说得更准，而且现在是 1 秒一次的周期性重算。

        另外：挂起时要把一次性动画（切歌过渡）直接收尾 —— 它不能被冻在半路，
        否则组件会以"淡到一半"的半透明样子一直停着。
        """
        suspended = self._widget_covered or not self.isVisible()
        if suspended and self._transition.active:
            self._transition.finish()
        self._clock.set_suspended(suspended)

    def _suspend_reason(self) -> str:
        """动画时钟为什么挂起（诊断用）：把判据摊开，免得再靠猜。"""
        if not self._clock.suspended:
            return "否"
        reasons = []
        if self._widget_covered:
            reasons.append("组件被别的窗口盖住")
        if not self.isVisible():
            reasons.append("窗口被隐藏")
        return "是（" + "、".join(reasons) + "）" if reasons else "是（判据都不成立？）"

    def _probe_applicable(self) -> bool:
        """当前模式会不会做遮挡采样（贴桌面的两种模式都要）。

        * zorder：Z 序插到桌面之上、普通窗口之下，会被别的窗口压住 —— 必须采样；
        * workerw：SetParent 到桌面层，同样会被别的窗口压住 —— 也要采样
          （这一条是后补的：以前 workerw 被排除在采样之外，动画只受前台启发式管，
           于是犯的是同一类"恢复了才怪"的毛病）；
        * 置顶模式：本来就浮在所有窗口之上，不会被盖住，不需要；
        * 普通窗口模式：没贴桌面，采样没有意义（窗口位置/层级不归桌面层管）。
        """
        return (sys.platform == "win32" and not self._config.always_on_top
                and bool(self._config.desktop_layer))

    def _update_widget_covered(self) -> None:
        """判断"组件是不是真的露在桌面上"（有没有被别的窗口盖住），据此挂起动画。

        【为什么必须单独判断】阶段3 的"桌面可见"只看**前台窗口**是不是桌面/任务栏，
        但前台恰好是任务栏/桌面、而组件又被其它窗口盖住的情况非常常见
        （实测：开着 Chatbox 时，前台是任务栏，组件其实完全被盖住）。
        对抓屏来说这最多是多抓几轮（本来就走慢档，代价小），
        但对 30fps 的动画来说就是白烧 5% 单核。

        判据（阶段4 修正）：在面板上铺一张 5x3 的采样网（含四角和中点），
        **只要有一个采样点被别的窗口压住**，就认为"被盖住"。

        为什么从"三个点、任一露出就算可见"改成这么严：
        放大镜抓到的是合成后的画面，只要组件有一块被上层窗口压着，
        那块窗口的颜色就会被折射进玻璃（实测：白色窗口压上去，整块玻璃发白）。
        宁可保守一点显示静态玻璃，也不要显示一块"别人的窗口"。
        """
        if not self._probe_applicable():
            # 置顶模式 / 普通窗口模式：不适用（置顶时它本来就浮在最上面）
            was_covered = self._widget_covered or self._capture_covered
            self._widget_covered = False
            self._capture_covered = False
            self._probe_exposed = 0
            self._probe_capture_exposed = 0
            self._probe_total = 0
            if was_covered:
                # 只有真的从"被盖住"变成"不适用"时才动后端：本函数现在每秒都会被
                # 巡检调用一次，无条件调用等于静态时也每秒白抓一帧、白整窗重绘一次。
                self._apply_background_gate()
            return

        hwnd = int(self.winId())
        info = self.layout_info()
        origin = self._window_screen_pos()
        exposed = 0            # 动画判据：外壳浮窗也算"露出"
        capture_exposed = 0    # 抓屏判据：外壳浮窗算"被压住"
        points = self._probe_points(info.panel)
        for point in points:
            probe = origin + QPoint(int(point.x()), int(point.y()))
            top = desktop_layer.window_at(probe.x(), probe.y())
            if top == hwnd:
                exposed += 1
                capture_exposed += 1
                continue
            # 【bug 修复】任务栏/桌面这类**常驻外壳表面**压在采样点上不算遮挡：
            # 点一下任务栏它就会浮到组件之上，15 个采样点里落在任务栏范围内的那几个
            # 原来会被判成"组件被盖住" —— 于是时钟挂起，氛围灯和频谱条一起停摆。
            kind = desktop_layer.shell_surface_kind(top)
            if kind == desktop_layer.SHELL_PERSISTENT:
                exposed += 1
                capture_exposed += 1
            elif kind == desktop_layer.SHELL_TRANSIENT:
                # 外壳浮窗（窗口预览 / 托盘溢出 / 操作中心）：转瞬即逝 -> 动画照跑；
                # 但它确实压住了组件，抓屏要停，否则玻璃里映的是别人的窗口。
                exposed += 1
            # 普通窗口：两边都算遮挡
        self._probe_exposed = exposed
        self._probe_capture_exposed = capture_exposed
        self._probe_total = len(points)
        covered = exposed < len(points)
        capture_covered = capture_exposed < len(points)
        changed = (covered != self._widget_covered) or (capture_covered != self._capture_covered)
        self._widget_covered = covered
        self._capture_covered = capture_covered
        if changed:
            # 只在状态真的翻转时才动后端 / 重绘（理由同上：本函数现在每秒被调用一次）
            self._apply_background_gate()

    @staticmethod
    def _probe_points(panel: QRectF) -> list[QPointF]:
        """在面板上铺一张采样网：5 列 x 3 行（含四角、中点）。

        为什么是这张网：覆盖"封面/标题/控制按钮"三个功能区的四角和中点，
        15 个点足够判出"有没有窗口压在上面"，而 WindowFromPoint 每次只要几微秒，
        即使 800ms 巡检一轮也完全无感。
        """
        inset = min(panel.width(), panel.height()) * 0.06
        box = panel.adjusted(inset, inset, -inset, -inset)
        cols, rows = 5, 3
        points: list[QPointF] = []
        for row in range(rows):
            for col in range(cols):
                x = box.left() + box.width() * col / (cols - 1)
                y = box.top() + box.height() * row / (rows - 1)
                points.append(QPointF(x, y))
        return points

    def _refresh_widget_covered(self) -> None:
        """1 秒巡检里的周期性重算："组件还露在桌面上吗"（bug 修复）。

        【为什么必须有它】遮挡状态原来只在两个**偶发**时机重算：
        * 前台窗口身份翻转 —— DesktopVisibilityWatcher 是边沿触发，只在
          is_desktop_in_front() 结果变化时才发信号（见 desktop_layer._poll）；
        * 拖动/缩放结束 —— _schedule_save()。

        而"把盖住组件的窗口挪开"通常**不改变前台窗口身份**（那个窗口还是前台），
        于是信号不发、采样不重跑，_widget_covered 永远停在 True ——
        动画一直挂起，直到碰巧有前台翻转或拖动，表现成"挪开窗口后恢复得随机"。
        挂到 1 秒巡检上之后，恢复有了确定的时间上界（≤1 秒）。

        代价可控：只有真正用得上的模式（贴桌面，zorder / workerw 都算）才会真的去采样，
        一次 15 个 WindowFromPoint（微秒级）；窗口藏着（托盘）时直接跳过。
        """
        if not self.isVisible():
            return
        self._update_widget_covered()

    def set_scale(self, scale: float) -> None:
        """等比缩放：面板左上角保持不动，右下角往外长。"""
        scale = max(self._config.MIN_SCALE, min(self._config.MAX_SCALE, float(scale)))
        if abs(scale - self._config.scale) < 1e-4:
            return
        panel_left_top = self.panel_screen_pos()
        self._config.scale = scale
        self._panel_w = max(120, int(round(theme.BASE_PANEL_W * scale)))
        self._panel_h = max(60, int(round(theme.BASE_PANEL_H * scale)))
        self._apply_panel_size()
        # 让面板左上角回到原来的屏幕位置
        self._place_panel_at_screen(panel_left_top.x(), panel_left_top.y())
        self.update()
        self._schedule_save()

    # ==================================================================
    # 窗口事件
    # ==================================================================
    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        first_show = not self._shown_once
        self._shown_once = True
        # 必须在有原生句柄之后再配置后端（抓屏排除需要 winId）
        QTimer.singleShot(0, self._backend.configure)
        QTimer.singleShot(0, self._backend.start)
        # 贴桌面要等窗口真正映射出来再做（首次多一些延迟），
        # 因为 SetParent 之后要重新摆位置，太早做可能窗口内容不刷。
        QTimer.singleShot(60 if first_show else 0, self._apply_layer_mode)
        if not hasattr(self, "_cadence_timer"):
            # 抓屏节流（跟着用户操作走）：只在第一次显示时建一个定时器
            self._setup_cadence()
        if self._config.always_on_top:
            self.raise_()
        # 重新显示：恢复动画（把挂起状态重新算一遍）
        self._sync_clock_suspension()
        self._sync_animations()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._backend.stop()   # 隐藏时别白烧 CPU 抓屏
        # 动画也一起挂起：定时器整个停掉，隐藏后 CPU 归零
        self._sync_clock_suspension()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 有托盘时，"关闭"应读作"隐藏"：否则用户按一下 ESC 组件就真没了，
        # 还得重新启动一次，体验很差。真正退出走托盘的"退出音璃"。
        if self._tray_mode and not self._quitting:
            event.ignore()
            self.hide_window()
            return
        if self._watcher is not None:
            self._watcher.stop()
        self._backend.stop()
        self._source.stop()
        self._persist()
        super().closeEvent(event)

    # ==================================================================
    # 绘制
    # ==================================================================
    def paintEvent(self, event) -> None:  # noqa: N802
        """绘制。外面包一层 try/except 是刻意的，理由见下方注释。"""
        try:
            if monitor.enabled:
                started = time.perf_counter()
                self._paint_impl(event)
                cost = (time.perf_counter() - started) * 1000.0
                # 分开计"整块重绘"和"局部重绘"：局部重绘是省 CPU 的核心手段，
                # 得能一眼看出它到底生效了没有。
                # 【注意】不能用 event.rect() 判断：Qt 给的是"更新区域的外接矩形"，
                # 而氛围光的更新区域是"外圈"（中间带洞），外接矩形正好是整个窗口，
                # 那样每次光晕动画都会被误算成"整块重绘"。要拿 event.region() 比。
                whole = event.region() == QRegion(self.rect())
                monitor.record("整块重绘" if whole else "局部重绘", cost)
            else:
                self._paint_impl(event)
        except Exception:
            # 【为什么必须兜住】PyQt6 在槽函数/虚函数里遇到未捕获的 Python 异常时
            # 会直接 abort() 整个进程（退出码 0xC0000409）。
            # 对桌面常驻组件来说，一个绘制 bug 不应该让用户的组件凭空消失，
            # 所以这里打印栈（方便排查）然后继续跑。
            self._paint_errors += 1
            if self._paint_errors in (1, 2, 5) or self._paint_errors % 200 == 0:
                import traceback

                print(f"[音璃] 绘制出错（第 {self._paint_errors} 次）：")
                traceback.print_exc()

    def _glow_band(self, info: PanelLayout) -> QRegion:
        """氛围光的重绘区域：窗口外圈（窗口 − 面板）。

        为什么要单独算它：氛围光完全画在面板**外面**那一圈透明留白里，
        呼吸动画每帧只需要重画这一圈。如果直接 update(整个窗口)，
        面板内部也会被重画一遍（白烧 CPU）。
        结果缓存起来，只在尺寸/位置变化时重算。
        """
        key = (round(info.panel.x(), 2), round(info.panel.y(), 2),
               round(info.panel.width(), 2), round(info.panel.height(), 2))
        if key != self._glow_band_key:
            outer = QRegion(QRectF(info.panel).adjusted(
                -theme.MARGIN, -theme.MARGIN, theme.MARGIN, theme.MARGIN).toAlignedRect())
            outer = outer.intersected(QRegion(self.rect()))
            self._glow_band_cache = outer.subtracted(QRegion(info.panel.toAlignedRect()))
            self._glow_band_key = key
        return self._glow_band_cache

    def _interior_only(self, parts: set[str]) -> bool:
        """这次重绘是不是"只有面板内部的东西变了"。

        判据：更新区域没碰到氛围光外圈，也没碰到缩放手柄。
        因为窗口 = 面板 + 外圈留白，所以"区域没碰到外圈"就等价于"区域在面板内部"。
        """
        return bool(parts) and not (parts & {"glow", "grip"})

    def _dirty_parts(self, region: QRegion, info: PanelLayout) -> set[str]:
        """根据"这次要重画的区域"算出哪些部件必须画。

        【为什么必须这么做】Qt 只会把**输出**裁到更新区域，但我的绘制代码照样会跑：
        每个部件都有路径构造、裁剪、抗锯齿的开销。实测：进度条每 200ms 刷新一次，
        即使只更新底部一小条，也白跑了整套 2.16ms 的绘制。
        先算相交关系、不相交就整块跳过，局部刷新才真正省 CPU。

        阶段4 把入参从"一个矩形"改成了"区域"：动画每次只重画很小的一块
        （圆盘/频谱/外圈光晕），而 QRegion 能精确表达"外圈这一圈"这种带洞的形状，
        避免"用外接矩形表示它"把面板内部也算进去。
        """
        pad = info.u * 10.0
        # 频谱条画得很紧（竖条都在 info.equalizer 里面），用不着 10u 的余量：
        # 给大了会让它的重绘区域和右边“总时长”文字、进度条交叠，
        # 于是每一帧都白画一次时间和进度条（实测 0.34ms/帧 ≈ 1% 单核）。
        eq_pad = info.u * 3.0
        areas = {
            "glass": info.panel,
            "disc": info.disc.adjusted(-2.0, -2.0, 2.0, 2.0),
            "title": info.title,
            "artist": info.artist,
            "time": info.elapsed.united(info.total).adjusted(-2.0, -2.0, 2.0, 2.0),
            "progress": info.progress.adjusted(-pad, -pad, pad, pad),
            "controls": info.controls.adjusted(-pad, -pad, pad, pad),
            "equalizer": info.equalizer.adjusted(-eq_pad, -eq_pad, eq_pad, eq_pad),
            "grip": info.grip.adjusted(-pad, -pad, pad, pad),
        }
        parts = {name for name, area in areas.items()
                 if region.intersects(area.toAlignedRect())}
        # 氛围光只画在面板外面那一圈：这一圈与更新区域相交时才画
        # （实测省 0.43ms/次）
        if region.intersects(self._glow_band(info)):
            parts.add("glow")
        return parts

    def _paint_impl(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        info = self.layout_info()
        panel, radius = info.panel, info.radius
        # 过渡期间画的是"旧的"那首（到淡出结束才换成新的，见 _on_track_changed）
        track = self._track_for_display()
        parts = self._dirty_parts(event.region(), info)

        # ① 氛围光（在玻璃下面，露出一圈柔和光晕）——只更新底部时它完全不用画
        #    强度 = 悬停透明度 × 呼吸系数（呼吸系数在暂停时保持冻结值）
        #    传 bounds / dpr：它会把 12 层柔光烘成一张位图缓存，
        #    之后每帧只是 drawPixmap（实测 1.5ms -> 0.12ms）。
        if "glow" in parts:
            with monitor.timer("绘制·氛围光"):
                self._glow.paint(painter, panel, radius,
                                 strength=self._alpha * self._glow_strength,
                                 bounds=QRectF(self.rect()),
                                 dpr=self.devicePixelRatioF())

        # ② 玻璃本体（reveal=alpha 让它跟着一起淡）
        if "glass" in parts:
            with monitor.timer("绘制·玻璃"):
                # interior：这次只重画面板内部的一小块（动画的局部重绘就是）时，
                # 面板外面的阴影/边缘线必然被裁掉，直接跳过（实测省 0.4ms/帧）。
                self._backend.paint(painter, panel, radius, self._alpha,
                                    interior=self._interior_only(parts))

        if not parts:
            painter.end()
            return

        # ③ 面板内容：整块内容用同一个透明度
        #    悬停透明度 × 切歌过渡透明度（过渡时内容淡到看不见，玻璃本身不淡）
        painter.setOpacity(self._alpha * self._content_alpha)
        playing = self._source.state is PlaybackState.PLAYING
        if "disc" in parts:
            prepared = None
            if track is not None and track.cover is not None:
                # 预先裁好 + 圆形遮罩烘好的位图（真实数据源会做缓存）
                prepared = self._source.disc_pixmap(
                    track, int(info.disc.width()), self.devicePixelRatioF()
                )
            with monitor.timer("绘制·封面"):
                self._disc.paint(painter, info.disc, track, self._disc_angle, playing,
                                 prepared=prepared, dpr=self.devicePixelRatioF())
        if "title" in parts or "artist" in parts:
            with monitor.timer("绘制·文字"):
                self._text.paint(painter, info.title, info.artist, track, info.u)
        if "time" in parts:
            with monitor.timer("绘制·时间"):
                self._paint_time_texts(painter, info)
        if "progress" in parts:
            with monitor.timer("绘制·进度条"):
                self._progress.paint(painter, info.progress, self._position_for_display(),
                                     getattr(track, "duration_s", 0.0), info.u,
                                     hovered=self._hover_inside)
        if "controls" in parts:
            with monitor.timer("绘制·控制按钮"):
                self._controls.paint(painter, info.buttons, self._source.state,
                                     self._source.loop_mode, info.u,
                                     hovered=self._hovered_button)
        if "equalizer" in parts:
            with monitor.timer("绘制·频谱条"):
                self._equalizer.paint(painter, info.equalizer, info.u, self._eq_heights,
                                      active=playing)

        # ④ 临时提示条（画在面板下方，压在内容之上）——很小，不折腾相交判断
        if "grip" in parts or "glass" in parts or self._toast_text:
            self._paint_toast(painter, info)

        # ⑤ 缩放手柄（右下角两道斜线）
        if "grip" in parts:
            self._paint_grip(painter, info)
        painter.setOpacity(1.0)
        painter.end()

    def _paint_time_texts(self, painter: QPainter, info: PanelLayout) -> None:
        """左下角已播时间 + 右下角总时长。"""
        from ..core.models import format_time

        painter.save()
        painter.setPen(theme.TEXT_TERTIARY)
        font = theme.font(theme.FONT_TIME * info.u)
        draw_text(painter, info.elapsed, format_time(self._position_for_display()), font,
                  theme.TEXT_TERTIARY, info.u, align=Qt.AlignmentFlag.AlignLeft, elide=False)
        draw_text(painter, info.total, getattr(self._track_for_display(), "duration_text", "--:--"),
                  font, theme.TEXT_TERTIARY, info.u, align=Qt.AlignmentFlag.AlignRight,
                  elide=False)
        painter.restore()

    def _paint_grip(self, painter: QPainter, info: PanelLayout) -> None:
        """右下角缩放手柄：两道斜线，鼠标靠近时变亮。"""
        painter.save()
        alpha = 150 if self._hovered_grip else 70
        pen = QPen(QColor(255, 255, 255, alpha), max(1.0, 1.25 * info.u))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        grip = info.grip
        for offset in (0.0, 0.42):
            painter.drawLine(
                QPointF(grip.left() + grip.width() * offset, grip.bottom()),
                QPointF(grip.right(), grip.top() + grip.height() * offset),
            )
        painter.restore()

    # ==================================================================
    # 临时提示条（toast）
    # ==================================================================
    def show_toast(self, text: str, ms: int = 3000) -> None:
        """在组件上显示一条临时提示。

        用途：把"QQ音乐不支持这个操作"这类反馈直接呈现给用户。
        贴桌面模式下窗口不拿键盘焦点、也不适合弹对话框，
        不提示的话用户只会觉得"点了没反应"。
        """
        if not text:
            return
        self._toast_text = text
        self._toast_timer.start(max(600, int(ms)))
        self.update()

    def _clear_toast(self) -> None:
        if not self._toast_text:
            return
        self._toast_text = ""
        self.update()

    def _paint_toast(self, painter: QPainter, info: PanelLayout) -> None:
        if not self._toast_text:
            return
        u = info.u
        font = theme.font(theme.FONT_TOAST * u)
        painter.save()
        # 提示条故意不跟着"鼠标移出变半透明"一起淡，否则看不清
        painter.setOpacity(1.0)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(font)
        metrics = QFontMetrics(font)

        max_width = info.panel.width() - 4 * theme.PAD * u
        text = metrics.elidedText(self._toast_text, Qt.TextElideMode.ElideRight, int(max_width))
        height = max(18 * u, metrics.height() + 7 * u)
        width = min(max_width, metrics.horizontalAdvance(text) + 20 * u)
        pill = QRectF(0, 0, width, height)
        # 挂在面板**下方**的透明留白里：不遮挡歌名/封面，也不会被面板圆角切掉
        pill.moveCenter(QPointF(info.panel.center().x(),
                                info.panel.bottom() + 4 * u + height / 2))
        path = QPainterPath()
        path.addRoundedRect(pill, height / 2, height / 2)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(16, 18, 24, 218))
        painter.drawPath(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 46), max(0.8, 1.0 * u)))
        painter.drawPath(path)

        painter.setPen(theme.TEXT_PRIMARY)
        painter.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()

    # ==================================================================
    # 交互
    # ==================================================================
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        info = self.layout_info()

        # 1. 右下角手柄 -> 缩放
        if info.grip.adjusted(-4, -4, 4, 4).contains(pos):
            self._resizing = True
            self._resize_origin_mouse = pos
            self._resize_origin_w = self._panel_w
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
            return

        # 2. 进度条 -> 拖拽跳转（前提是播放器支持；QQ音乐实测不支持）
        if self._progress.hit_test(pos, info.progress):
            if not getattr(self._source, "supports_seek", True):
                self.show_toast("QQ音乐不支持拖动进度条")
                return
            self._seeking = True
            self._preview_seconds = self._progress.seconds_at(
                pos.x(), info.progress, self._source.track.duration_s)
            self._update_bottom_area()
            return

        # 3. 控制按钮 -> 记下按下的是哪个，松开时才真的触发（更符合按钮手感）
        button = Controls.hit(pos, info.buttons)
        if button >= 0:
            self._pressed_button = button
            self.update(self._button_rect(button).toAlignedRect())
            return

        # 4. 其它地方 -> 拖动窗口
        #    用“鼠标位移”驱动而不是绝对坐标：贴桌面层后 Qt 的全局坐标认知会偏，
        #    位移（差值）则完全可靠。
        self._dragging = True
        self._drag_mouse_origin = event.globalPosition().toPoint()
        self._drag_window_origin = self.pos()
        self._backend.begin_drag()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        info = self.layout_info()

        # ---- 1. 正在缩放 ----
        if self._resizing:
            delta_x = pos.x() - self._resize_origin_mouse.x()
            target_w = self._resize_origin_w + delta_x
            self.set_scale(target_w / theme.BASE_PANEL_W)
            return

        # ---- 2. 正在拖动窗口 ----
        if self._dragging:
            delta = event.globalPosition().toPoint() - self._drag_mouse_origin
            self.move(self._drag_window_origin + delta)
            return

        # ---- 3. 正在拖拽进度条 ----
        if self._seeking:
            self._preview_seconds = self._progress.seconds_at(
                pos.x(), info.progress, self._source.track.duration_s)
            self._update_bottom_area()
            return

        # ---- 4. 悬停状态刷新（不按键也会走到这里，因为开了 mouseTracking）----
        inside = info.panel.contains(pos)
        if inside != self._hover_inside:
            self._hover_inside = inside
            self._fade_to(self._config.opacity_hover if inside else self._config.opacity_idle)
            self._update_cadence()      # 悬停/离开时立刻切抓屏频率（不用等下一轮巡检）

        hovered_button = Controls.hit(pos, info.buttons) if inside else -1
        hovered_grip = info.grip.adjusted(-4, -4, 4, 4).contains(pos) if inside else False
        if hovered_button != self._hovered_button or hovered_grip != self._hovered_grip:
            self._hovered_button = hovered_button
            self._hovered_grip = hovered_grip
            self.update()

        # 光标形状：手柄处显示斜向缩放光标
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if hovered_grip
                       else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._resizing:
            self._resizing = False
            self._schedule_save()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        if self._dragging:
            self._dragging = False
            self._backend.end_drag()        # 落位后重算一次高质量折射
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._schedule_save()
            return

        if self._seeking:
            self._seeking = False
            if self._preview_seconds is not None:
                self._source.seek(self._preview_seconds)
            self._preview_seconds = None
            self._update_bottom_area()
            return

        if self._pressed_button >= 0:
            pos = event.position()
            pressed = self._pressed_button
            self._pressed_button = -1
            # 只有在同一个按钮上松开才算"点击"
            if Controls.hit(pos, self.layout_info().buttons) == pressed:
                self._trigger_button(pressed)
            else:
                self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        """鼠标彻底离开窗口：取消悬停、淡化。"""
        self._hover_inside = False
        self._hovered_button = -1
        self._hovered_grip = False
        self._fade_to(self._config.opacity_idle)
        self._update_cadence()
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """滚轮缩放（每格 5%）。"""
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.set_scale(self._config.scale + steps * theme.WHEEL_STEP)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif key == Qt.Key.Key_Space:
            self._source.play_pause()
        elif key == Qt.Key.Key_Left:
            self._source.seek(max(0.0, self._source.position - 5.0))
        elif key == Qt.Key.Key_Right:
            self._source.seek(self._source.position + 5.0)
        elif key == Qt.Key.Key_Up:
            self.set_scale(self._config.scale + theme.WHEEL_STEP)
        elif key == Qt.Key.Key_Down:
            self.set_scale(self._config.scale - theme.WHEEL_STEP)
        else:
            super().keyPressEvent(event)

    # ==================================================================
    # 右键菜单
    # ==================================================================
    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键菜单。

        为什么需要它：贴桌面模式用了 WS_EX_NOACTIVATE（防止点一下就被抬到其它窗口前面），
        代价是窗口拿不到键盘焦点，ESC 这类快捷键就失效了。
        所以必须提供鼠标入口来切换模式和退出。（阶段5 的托盘菜单会复用同一套动作）
        """
        menu = QMenu(self)
        act_top = menu.addAction("置顶显示（浮在其它窗口之上）")
        act_top.setCheckable(True)
        act_top.setChecked(self._config.always_on_top)
        act_desktop = menu.addAction("只贴桌面（会被其它窗口盖住）")
        act_desktop.setCheckable(True)
        act_desktop.setChecked(self._config.desktop_layer and not self._config.always_on_top)
        menu.addSeparator()
        act_anim = menu.addAction("动画效果（旋转封面 / 频谱 / 呼吸光）")
        act_anim.setCheckable(True)
        act_anim.setChecked(self._config.animations)
        menu.addSeparator()
        act_center = menu.addAction("回到屏幕右下角")
        menu.addSeparator()
        act_quit = menu.addAction("退出音璃")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen is act_top:
            self.set_always_on_top(True)
        elif chosen is act_desktop:
            self._config.desktop_layer = True
            if self._config.always_on_top:
                self.set_always_on_top(False)     # 顺手重建窗口并重新贴桌面
            else:
                self._apply_layer_mode()
        elif chosen is act_center:
            self.move_to_default_position()
        elif chosen is act_anim:
            # 行动作被点过之后 checked 已经是新状态了
            self.set_animations(act_anim.isChecked())
        elif chosen is act_quit:
            self.quit_application()

    def move_to_default_position(self) -> None:
        """把面板摆回屏幕右下角（面板位置不变被玩坏时的救命按钮）。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self._place_panel_at_screen(
            area.right() - self._panel_w - SCREEN_INSET,
            area.bottom() - self._panel_h - SCREEN_INSET,
        )
        self._schedule_save()

    # ==================================================================
    # 内部工具
    # ==================================================================
    # ==================================================================
    # 抓屏节流：跟着"用户有没有在操作"走
    # ==================================================================
    ACTIVE_IDLE_SECONDS = 3.0      # 3 秒内有输入就算"用户在用电脑"
    # ---- 抓屏体检的容错参数（bug 修复：以前单帧超时就能把抓屏降到 2 档）----
    PERF_WORST_S = 0.20            # 单帧超过 200ms 才算"这一轮不健康"（原 0.12，太敏感）
    PERF_RATIO = 2.5               # 平均帧间隔倍数（原 1.8，太敏感）
    PERF_BAD_STREAK = 3            # 连续 3 轮不健康才降 1 档（原 2）
    PERF_GOOD_STREAK = 5           # 连续 5 轮健康才升回 1 档（原 8，太难怪攒不满）
    PERF_HEALTH_WORST_S = 0.10     # 判"健康"的帧间隔上限（原 0.06，太严）
    PERF_HEALTH_RATIO = 1.5        # 判"健康"的平均帧间隔倍数（原 1.25，太严）
    PERF_MAX_LEVEL = 2             # 0 正常 / 1 降频 / 2 只保留动画
    PERF_FORCE_RECOVER_S = 3.0     # 降级后最多这么多秒就强制恢复抓屏（时间兜底）

    def _setup_cadence(self) -> None:
        """启动 1 秒巡检：抓屏节流 + 遮挡重算 + 动画体检。"""
        self._cadence_timer = QTimer(self)
        self._cadence_timer.setInterval(1000)
        self._cadence_timer.timeout.connect(self._update_cadence)
        # 【bug 修复】周期性重算"组件露出来了没有"。
        # 必须排在 _watch_capture_health 之前：体检在 _widget_covered 为真时会早退
        # （见 _watch_capture_health 开头），先重算才能让它这一轮就拿到正确的状态。
        self._cadence_timer.timeout.connect(self._refresh_widget_covered)
        # 同一条巡检里顺手体检"动画有没有被抓屏卡住"，卡了就自动降级
        self._cadence_timer.timeout.connect(self._watch_capture_health)
        # 最后兜底：降级超过 3 秒还没靠体检升回来，就强制恢复抓屏
        self._cadence_timer.timeout.connect(self._force_capture_recovery)
        self._cadence_timer.start()
        self._cadence_current = 0
        self._update_cadence()

    def _update_cadence(self) -> None:
        """按"用户是否在操作"切换抓屏频率。

        【实测依据】屏幕抓取的耗时**与面积无关**（固定 ~21ms 延迟，
        32x32 和 978x492 差不多），所以降低 CPU 的唯一有效手段是"少抓"。
        而"用户多久没输入"是一个几乎零成本又非常准的信号：
        没人动键盘鼠标的时候，桌面上的东西也不会自己变。

        于是：悬停中 / 3 秒内有输入 -> 快档（看起来跟得紧）；
              用户已经静下来   -> 慢档（背景根本不会变，抓了也白抓）。
        """
        if not hasattr(self, "_cadence_timer"):
            return
        idle = system_idle_seconds()
        active = self._hover_inside or idle < self.ACTIVE_IDLE_SECONDS
        wanted = (self._config.capture_interval_ms if active
                  else self._config.capture_interval_idle_ms)
        if self._perf_level >= 1:
            # 降级档位 1：把抓屏频率压低（被录屏软件之类抢资源时用）
            wanted = max(wanted, 750) * self._perf_level
        if wanted == self._cadence_current:
            return
        self._cadence_current = wanted
        self._backend.apply_interval(wanted)
        if monitor.enabled:
            print(f"[性能] 抓屏节流 -> {wanted}ms（空闲 {idle:.1f}s，悬停={self._hover_inside}）")

    def _watch_capture_health(self) -> None:
        """体检：动画是不是被抓屏卡住了？卡了就自动降级（问题2）。

        【判断依据】用动画时钟的真实帧间隔（EMA）÷ 目标帧间隔。
        * >1.8：说明这一轮里主线程被别的东西占了很久 —— 抓屏是主线程里同步做的，
          所以"帧间隔变大"几乎就是"抓屏变慢"的同义词（开机录屏软件时实测如此）；
        * <1.3：恢复正常。

        降级分两级（每级都要连续多次体检才动，避免抖动）：
        * 1 级：抓屏频率压到 750ms 以上 —— 抓得少一半，动画立刻顺回来；
        * 2 级：干脆暂停折射（set_live(False) + 清缓存），**只保留动画** ——
          玻璃变成纯静态，但组件还是"活着"的，手感优先。
        恢复是反过来的：连续 8 次健康就升回一级。
        """
        if not self._config.adaptive_capture or not self.isVisible():
            return
        if self._widget_covered:
            # 没在画动画，没有可测的信号。
            # 【注意】这里不能再看 _occluded：它只决定抓屏，动画在前台是别的程序时
            # 照样可能在跑（见 _sync_clock_suspension），看它会让体检永远不执行。
            return
        clock = self._clock
        if not clock.running:
            return

        # 两个信号：最差一帧有多久（主判据）+ 平均帧间隔倍数（辅助）
        worst = clock.take_worst_dt()
        ratio = clock.frame_starve_ratio
        if worst > self.PERF_WORST_S or ratio > self.PERF_RATIO:
            self._perf_bad += 1
            self._perf_good = 0
            if self._perf_bad >= self.PERF_BAD_STREAK and self._perf_level < self.PERF_MAX_LEVEL:
                self._perf_level += 1
                self._perf_bad = 0
                # 记下降级起点，并允许这一轮用一次"时间兜底强制恢复"
                self._perf_degraded_at = time.monotonic()
                self._perf_force_used = False
                if self._perf_level >= self.PERF_MAX_LEVEL:
                    self._backend.set_live(False)
                    self._backend.drop_refraction()
                    self._capture_blocked = True
                    print("[性能] 抓屏卡顿：已暂停折射，只保留动画"
                          f"（最差一帧 {worst * 1000:.0f}ms，平均 {ratio:.1f} 倍）")
                else:
                    print(f"[性能] 抓屏卡顿：先把抓屏降到 {self._perf_level} 档"
                          f"（最差一帧 {worst * 1000:.0f}ms，平均 {ratio:.1f} 倍）")
                self._update_cadence()
                self.update()
        elif ratio < self.PERF_HEALTH_RATIO and worst < self.PERF_HEALTH_WORST_S:
            self._perf_good += 1
            self._perf_bad = 0
            if self._perf_good >= self.PERF_GOOD_STREAK and self._perf_level > 0:
                self._perf_level -= 1
                self._perf_good = 0
                if self._perf_level == 0:
                    self._perf_force_used = False
                print(f"[性能] 抓屏恢复正常，降级档位 -> {self._perf_level}")
                # 交给唯一的门控入口决定要不要重新开抓屏：它看的是
                # _capture_covered / 可见性（没有采样能力时才看 _occluded），
                # **不再**在这里另写一份判据（以前这里写了 `not self._occluded`，
                # 于是外壳浮窗在前台时永远回不来）。
                self._apply_background_gate()
                self._update_cadence()
                self.update()

    def _force_capture_recovery(self) -> None:
        """时间兜底：抓屏降级之后最多 3 秒就强制恢复一次（bug 修复，C）。

        为什么需要它：健康判据是"这一秒最差一帧 < 100ms"，机器稍有抖动就永远攒不满
        连续 5 轮 —— 于是档位一直停在 1/2 档，抓屏再也不开；而恢复动作原来只在
        "健康计数达标"或"门控翻转"时才发生。这里给一个**与计数无关的时间上界**。

        每次降级只强制一次（_perf_force_used），避免"降级 -> 强制 -> 立刻又降级"抖动。
        门控本来就该拦着（组件被盖住 / 窗口隐藏）时不硬开。
        """
        if self._perf_level <= 0 or self._perf_force_used:
            return
        if self._perf_degraded_at is None:
            return
        if time.monotonic() - self._perf_degraded_at < self.PERF_FORCE_RECOVER_S:
            return
        if self._capture_blocked_now():
            return                       # 该拦着就拦着，等门控放开的时机
        self._perf_force_used = True
        self._perf_level = 0
        self._perf_bad = 0
        self._perf_good = 0
        print(f"[性能] 降级已持续 {self.PERF_FORCE_RECOVER_S:.0f} 秒，强制恢复抓屏")
        self._apply_background_gate()    # 关门控重新开抓屏（set_live(True) + refresh）
        self._update_cadence()
        self.update()

    def _on_background_changed(self) -> None:
        """背景（桌面）变了：**只重画面板那一块**。

        为什么可以这么省：面板外面的阴影、氛围光是静态的，不随桌面内容变化，
        全窗口重画纯属浪费（实测整块 5.4ms 里一大半花在面板外那圈像素上）。
        Qt 的 update(rect) 会把绘制自动裁到这块区域，外面的像素保持原样。
        """
        if self._capture_blocked_now():
            # 抓屏停着的时候再重画面板没有意义（而且抓来的图是别人的窗口）。
            # 用正判据：前台是别的程序但组件露着时，抓屏是开着的，背景变化当然要重画。
            return
        self.update(self.panel_rect().toAlignedRect().adjusted(-1, -1, 1, 1))

    def force_hover(self) -> None:
        """强制进入"悬停态"（不透明）。仅开发/截图验证用。"""
        self._hover_inside = True
        self._alpha = self._config.opacity_hover
        self.update()

    # ---- 显示/隐藏（托盘菜单和左键单击都走这里）----
    def set_tray_mode(self, enabled: bool) -> None:
        self._tray_mode = enabled

    def hide_window(self) -> None:
        """隐藏到托盘。

        隐藏时 hideEvent 会停掉抓屏（玻璃后端），所以隐藏后基本不占 CPU；
        SMTC 轮询会继续（每 500ms 三次 COM 调用，可忽略），
        这样托盘提示里能一直看到在放什么歌。
        """
        self._persist()
        self.hide()

    def show_window(self) -> None:
        self.show()
        if self._config.always_on_top:
            self.raise_()
        self.update()

    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide_window()
        else:
            self.show_window()

    def quit_application(self) -> None:
        """真正退出（托盘菜单用）。"""
        self._quitting = True
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def set_persist_enabled(self, enabled: bool) -> None:
        """关掉配置回写（用 --pos 等调试参数时，不该把临时位置写进配置）。"""
        self._persist_enabled = enabled

    def _trigger_button(self, index: int) -> None:
        """按钮动作分发。阶段1 直接打到假数据源上，所以按钮不是死的。"""
        if index == BTN_PREV:
            self._source.previous_track()
        elif index == BTN_PLAY:
            self._source.play_pause()
        elif index == BTN_NEXT:
            self._source.next_track()
        elif index == BTN_LOOP:
            self._source.cycle_loop_mode()
        self.update()

    def _button_rect(self, index: int) -> QRectF:
        buttons = self.layout_info().buttons
        if 0 <= index < len(buttons):
            return buttons[index]
        return QRectF()

    def _position_for_display(self) -> float:
        """拖动进度条时显示"手指下"的位置，而不是真实播放位置。"""
        if self._preview_seconds is not None:
            return self._preview_seconds
        return self._source.position

    def _fade_to(self, target: float) -> None:
        self._fade.stop()
        self._fade.setStartValue(float(self._alpha))
        self._fade.setEndValue(float(target))
        self._fade.start()

    def _on_fade_value(self, value) -> None:
        self._alpha = float(value)
        self.update()

    def _update_bottom_area(self) -> None:
        """只重绘底部一行（进度条 + 时间），这是本窗口最频繁的重绘。

        为什么能省 CPU：Qt 会把 painter 裁剪到这个矩形，
        所以玻璃、封面、按钮都不会被重画 —— 但视觉效果完全一致。
        """
        info = self.layout_info()
        region = info.elapsed.united(info.progress).united(info.total)
        region = region.adjusted(-3, -3, 3, 3)
        self.update(region.toAlignedRect())

    # ==================================================================
    # 阶段4：动画
    # ==================================================================
    # 设计思路：所有动画都是"共享时钟（30fps）推状态 -> 回调里只重画一小块"。
    # 每个回调都刻意只 update 自己那块区域，靠 _dirty_parts 把别的部件整块跳过。
    @staticmethod
    def _same_song(old, new) -> bool:
        """两首歌是不是同一首（用于"只是封面后到"的静默刷新）。

        比标题+歌手+时长；时长可能是 0（读不到），那就只比前两项。
        """
        if old is None or new is None:
            return False
        if getattr(old, "title", None) != getattr(new, "title", None):
            return False
        if getattr(old, "artist", None) != getattr(new, "artist", None):
            return False
        old_d = getattr(old, "duration_s", 0.0) or 0.0
        new_d = getattr(new, "duration_s", 0.0) or 0.0
        return abs(old_d - new_d) < 0.5 if (old_d and new_d) else True

    def _track_for_display(self):
        """当前该画的那首歌。

        切歌过渡期间它还是"旧的"那首（直到淡出结束、在中点换内容），
        平时就是数据源当前那首。为什么需要这个概念：数据源在换歌的瞬间
        就已经是新歌了，而 UI 需要自己控制"什么时候把新内容显出来"。
        """
        return self._display_track if self._display_track is not None else self._source.track

    def _sync_animations(self) -> None:
        """决定哪些动画该跑、哪些该停（唯一的开关入口）。

        【判断依据】
        * 播放中：封面转 + 频谱跳 + 氛围光呼吸。
        * 暂停：三个一起停，画面**冻结在当前位置**（不重置）。
          为什么不重置：重置会让画面"跳"一下（频谱突然变回设计高度、光晕突然变亮），
          而冻结既看不出异样，也完全不用重绘。
        * 窗口隐藏/组件真被别的窗口盖住：整个时钟挂起（定时器停掉，
          见 _update_widget_covered / hideEvent；前台是不是别的程序不影响它）。
        * 动画总开关关掉：停掉全部动画，并把界面恢复成阶段3 的静态外观。
        """
        playing = self._source.state is PlaybackState.PLAYING
        if not self._config.animations:
            self._transition.finish()          # 过渡中关动画：内容得先换完
            for name in ("disc", "equalizer", "glow"):
                self._clock.set_active(name, False)
            return
        self._clock.set_active("disc", playing)
        self._clock.set_active("equalizer", playing)
        self._clock.set_active("glow", playing)

    def set_animations(self, enabled: bool) -> None:
        """动画总开关（右键菜单 / 托盘菜单调它）。"""
        enabled = bool(enabled)
        if enabled == self._config.animations:
            return
        self._config.animations = enabled
        self._sync_animations()
        self._schedule_save()
        self.update()

    # ---- 驱动回调：每个只重画自己那一小块 ---------------------------------
    def _on_disc_angle(self, angle: float) -> None:
        """封面转了一点点：只重画圆盘。"""
        self._disc_angle = angle
        self.update(self.layout_info().disc.toAlignedRect().adjusted(-2, -2, 2, 2))

    def _on_eq_heights(self, heights) -> None:
        """频谱高度变了：只重画那一排竖条（实测约 0.06ms/次）。"""
        self._eq_heights = list(heights)
        info = self.layout_info()
        pad = info.u * 3.0     # 余量给大了会和右边的时间/进度条区域交叠（见 _dirty_parts）
        self.update(info.equalizer.adjusted(-pad, -pad, pad, pad).toAlignedRect())

    def _on_glow_strength(self, strength: float) -> None:
        """呼吸亮度变了一点：只重画面板**外面**那一圈（QRegion 带洞，不会画到面板）。"""
        self._glow_strength = strength
        self.update(self._glow_band(self.layout_info()))

    def _on_transition_alpha(self, value: float) -> None:
        """切歌过渡的透明度：内容是整块在变，只能整体重绘。"""
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._content_alpha) < 0.004:
            self._content_alpha = value
            return
        self._content_alpha = value
        self.update()

    def _on_transition_swap(self) -> None:
        """淡出结束（视觉上已经完全看不见了）：在这里把内容换成新歌。"""
        if self._pending_track is not None:
            self._display_track = self._pending_track

    def _schedule_save(self) -> None:
        # 顺手重算一次"组件露出来了吗"：拖动/缩放之后位置变了，
        # 800ms 的轮询虽然也能发现，但拖到一个被盖住的位置时早点停动画更好。
        self._update_widget_covered()
        self._save_timer.start()      # 每次调用都会重置计时，实现"防抖"

    def _persist(self) -> None:
        if not self._persist_enabled:
            return
        panel_top_left = self.panel_screen_pos()
        self._config.pos_x = int(panel_top_left.x())
        self._config.pos_y = int(panel_top_left.y())
        self._config.save()

    # ==================================================================
    # 数据源回调
    # ==================================================================
    def _on_track_changed(self, track) -> None:
        """换歌：走一次"淡出 -> 换内容 -> 淡入"的过渡。

        为什么要过渡：直接硬切会让人看不出换歌了，尤其是两张封面风格接近时。
        过渡期间 UI 画的是旧 Track（封面/歌名/时长都是旧的），
        到中点（视觉上已经完全淡出）才换成新的 —— 所以永远不会看到"半新半旧"。
        """
        self._pending_track = track
        # 只是"封面补上了"（歌名/歌手/时长都没变）：静默换上，不要再来一段淡出淡入
        if self._display_track is not None and self._same_song(self._display_track, track):
            self._display_track = track
            self.update()
            return
        if self._display_track is None:
            # 启动后的第一首：没有"旧内容"可以淡出，直接淡入
            self._display_track = track
            if self._config.animations:
                self._transition.start(fade_in_only=True)
            self.update()
            return
        if not self._config.animations:
            self._display_track = track
            self._on_transition_alpha(1.0)
            if not self._widget_covered:
                self.update()
            return
        # 已经在淡出中就别重新开始（连点"下一首"时等到中点直接换成最新的那首）；
        # 正在淡入时换歌则重新淡出，但不能从 1.0 重新开始，要从当前的透明度接着淡。
        if not (self._transition.active and self._transition.phase == "out"):
            self._transition.start(from_alpha=self._content_alpha)
        if not self._widget_covered:
            self.update()

    def _on_position_changed(self, position: float) -> None:
        if self._seeking or self._widget_covered:
            return                    # 正在拖拽进度 / 组件真的被盖住：不用画
        self._update_bottom_area()

    def _on_state_changed(self, state) -> None:
        # 播放/暂停直接决定哪些动画该跑（暂停时冻结，不烧 CPU）
        self._sync_animations()
        if not self._widget_covered:
            self.update()

    def _on_loop_changed(self, mode) -> None:
        if not self._widget_covered:
            self.update()

    def _on_notice(self, message: str) -> None:
        """播放器给的反馈：控制台记一笔，同时在组件上弹一条提示。"""
        print(f"[音璃] {message}")
        self.show_toast(message)

    # ==================================================================
    # 调试信息
    # ==================================================================
    def debug_lines(self) -> list[str]:
        """打印一行环境/状态信息（--debug 启动时用）。"""
        if self._config.always_on_top:
            layer = "置顶（浮在所有窗口之上）"
        elif self._in_desktop_layer:
            layer = f"贴桌面层·Z序插入（参照窗口 {self._desktop_target_class}）"
        elif self._desktop_attached:
            layer = f"贴桌面层·SetParent（父窗口 {self._desktop_parent_class}）"
        elif self._config.desktop_layer:
            layer = "贴桌面失败，当前是普通窗口"
        else:
            layer = "普通窗口（未贴桌面）"

        hwnd = int(self.winId())
        center = self.panel_screen_pos() + QPoint(self._panel_w // 2, self._panel_h // 2)
        top = desktop_layer.window_at(center.x(), center.y())
        top_text = "就是本组件（说明真的露在桌面上，且能点到）" if top == hwnd \
            else f"{desktop_layer.class_name(top)}({top}) —— 组件被它盖住了"
        below = desktop_layer.window_below(hwnd)
        return [
            f"玻璃后端 : {self._backend.status_text()}",
            f"数据源   : {self._source.status_text() if hasattr(self._source, 'status_text') else type(self._source).__name__}",
            f"封面     : {self._source.cover_stats() if hasattr(self._source, 'cover_stats') else '—'}",
            f"能力     : 拖进度={'可以' if getattr(self._source, 'supports_seek', True) else '不支持'}"
            f"  切循环={'可以' if getattr(self._source, 'supports_loop_mode', True) else '不支持'}"
            f"  随机={'可以' if getattr(self._source, 'supports_shuffle', True) else '不支持'}",
            f"窗口层级 : {layer}  (模式={self._config.desktop_layer_mode})",
            f"Z序核验 : 紧下方窗口={desktop_layer.class_name(below) or '无'}({below})"
            f"  已压在桌面层上={desktop_layer.is_above_desktop_layer(hwnd)}",
            f"该点最上层窗口 : {top_text}",
            f"抓屏门控 : {'开' if not self._capture_blocked_now() else '关'}"
            f"（采样可用={self._probe_applicable()} 组件被压住={self._capture_covered}"
            f" 前台是别的程序={self._occluded} 窗口可见={self.isVisible()}）",
            f"面板尺寸 : {self._panel_w}x{self._panel_h}  (缩放 {self._config.scale:.2f})"
            f"  绘制异常={self._paint_errors} 次",
            f"托盘     : {'已启用' if self._tray_mode else '未启用'}",
            f"动画     : {'开' if self._config.animations else '关'}"
            f"  时钟={'运行中' if self._clock.running else '已停'}"
            f"  驱动={','.join(self._clock.active_names()) or '无'}"
            f"  挂起={self._suspend_reason()}"
            f"  本次已 {self._clock.ticks} 帧",
            f"组件露出 : {'否（被别的窗口盖住，动画已挂起）' if self._widget_covered else '是'}",
            f"遮挡探测 : {self._probe_total - self._probe_exposed}/{self._probe_total}"
            f" 个采样点被压住（动画判据，放过外壳浮窗）"
            f"  抓屏={self._probe_total - self._probe_capture_exposed}/{self._probe_total}"
            f" 个（外壳浮窗也算）"
            f"  抓屏降级={self._perf_level}"
            f"  帧间隔/目标={self._clock.frame_starve_ratio:.2f}"
            f"  最差一帧={self._clock.worst_dt * 1000:.0f}ms",
            f"封面角度 : {self._disc_angle:.1f}°"
            f"  切歌过渡={self._transition.phase}"
            f"  内容透明度={self._content_alpha:.2f}",
            f"开机自启 : {autostart.status_text()}",
            f"面板屏幕坐标 : {self.panel_screen_pos().x()},{self.panel_screen_pos().y()}",
            f"透明度   : 静置 {self._config.opacity_idle:.2f} / 悬停 {self._config.opacity_hover:.2f}",
        ]
