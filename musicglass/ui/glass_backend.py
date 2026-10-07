# -*- coding: utf-8 -*-
"""玻璃后端抽象：同一套接口，两种实现。

    PyGlassBackend   真折射（默认）。用 pyglass 的折射引擎 + 屏幕抓取。
    QtBlurBackend    备选。抓一小块屏幕 → 高斯模糊 → 半透明圆角。
                     观感是"毛玻璃"而不是"液态玻璃"（边缘不会弯），但依赖更少、更省 CPU。

为什么要有这层抽象：你说过"pyglass 不好用就退回 Qt 自带方案"。
有了它，换后端只是配置里改一行 `glass_backend`，UI 代码一行都不用动。

统一接口：
    configure()                      窗口有了原生句柄之后调用一次
    start() / stop()                 开始/停止抓取（隐藏窗口时应该 stop，别白烧 CPU）
    set_geometry(panel, radius)      面板尺寸变了（缩放）
    apply_interval(ms)               调整抓屏节奏
    begin_drag() / end_drag()        拖动前后（拖动期间降质提速）
    paint(painter, panel, radius, alpha)   把玻璃画到 painter 上
    changed                          信号：背景有更新，窗口该重绘了
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QPoint, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QApplication

from . import theme
from ..config import Config
from ..perf import monitor


class GlassBackend(QObject):
    """玻璃后端基类。"""

    changed = pyqtSignal()

    def __init__(self, widget, parent: QObject | None = None) -> None:
        super().__init__(parent or widget)
        self._widget = widget
        self._live = True

    # ---- 生命周期 ----------------------------------------------------
    def configure(self) -> None:
        """窗口已经有原生句柄（winId 有效）之后调用。"""

    def start(self) -> None:
        """开始提供背景帧。"""

    def stop(self) -> None:
        """停止抓取（窗口隐藏/退出时调用）。"""

    def cleanup(self) -> None:
        """彻底释放后台资源。退出前必须调。

        默认就是 stop()；子类要是握着更重的资源（比如 pyglass 的放大镜对象），
        就在这里把它显式释放掉。

        【为什么要显式释放】实测：不释放的话进程会在退出时 abort
        （退出码 0xC0000409，看起来像崩溃）—— 因为 Qt 退出时靠垃圾回收
        去销毁对象，而这些对象析构时会碰 Win32 资源，顺序和时机不可控。
        """
        self.stop()

    def set_live(self, on: bool) -> None:
        """临时暂停/恢复抓取。

        和 stop() 的区别：stop() 是"窗口不显示了"，set_live() 是"窗口虽然存在但
        被别的程序盖住了，没人看得见"。后者恢复时会立刻重抓一帧，保证画面不过期。
        """
        self._live = on
        if on:
            self.refresh()

    def refresh(self) -> None:
        """立刻重新抓一帧背景。"""

    # ---- 几何 --------------------------------------------------------
    def set_geometry(self, panel: QRectF, radius: float) -> None:
        """面板尺寸/圆角变化（缩放时调用）。"""

    def apply_interval(self, interval_ms: int) -> None:
        """调整抓屏间隔。"""

    def begin_drag(self) -> None:
        """开始拖动：可以降质提速。"""

    def end_drag(self) -> None:
        """拖动结束：恢复画质。"""

    # ---- 绘制 --------------------------------------------------------
    def drop_refraction(self) -> None:
        """丢掉折射缓存，接下来按"纯玻璃"画（不折射桌面）。

        【为什么必须有】放大镜抓的是**合成后的画面**：组件被别的窗口盖住时，
        抓到的就是那个窗口的内容，继续折射就会把上层窗口的白色染进玻璃里，
        而且那张图会一直留着（pyglass 只在"内容变了"时才重抓）。
        被盖住时把它清空，paint_glass 拿到 None 就只画色调/高光/边缘 ——
        一块干净的静态玻璃，比"染了别人颜色的玻璃"正确得多。
        """
        if hasattr(self, "_pixmap"):
            self._pixmap = None

    def paint(self, painter: QPainter, panel: QRectF, radius: float, alpha: float,
              interior: bool = False) -> None:
        """把玻璃面板画到 painter 上。

        alpha    ：整体透明度（0~1）。
        interior ：True 表示"这次只重画面板内部的一小块"（阶段4 的帧动画就是）。
                   面板外面的阴影/边缘线必然被裁掉，可以整块跳过，实测省 0.4ms/帧。
        """
        raise NotImplementedError

    # ---- 工具 --------------------------------------------------------
    def status_text(self) -> str:
        """给调试用的一行状态描述。"""
        return type(self).__name__


# ---------------------------------------------------------------------------
# 后端 1：pyglass 真折射
# ---------------------------------------------------------------------------
class PyGlassBackend(GlassBackend):
    """真折射后端。

    工作流程：
        ScreenBackdrop 定时抓取窗口周围的桌面像素（Windows 走放大镜 API，
        它会把本窗口过滤掉，所以玻璃不会折射自己）
            -> 发 changed 信号
            -> 我们调用 GlassRenderer.refract() 做物理折射，得到一张位图
            -> 窗口重绘时 paint_glass() 把这张位图贴进圆角面板里

    注意一个反直觉的点：**重绘窗口 ≠ 重新折射**。
    折射只在"背景真的变了"时做一次，之后重绘只是把缓存的位图贴上去。
    这就是为什么静态壁纸下 CPU 接近 0。
    """

    def __init__(self, widget, config: Config) -> None:
        super().__init__(widget)
        self._config = config
        from pyglass import GlassMaterial, GlassRenderer, GlassStyle, ScreenBackdrop

        # 抓屏方式：gdi 模式下屏蔽 pyglass 的放大镜抓屏。
        # pyglass 的放大镜初始化会抛访问违例（faulthandler 可见），虽然它自己兜住了，
        # 但留一个"绕开它"的开关总没坏处（代价是组件对截图/录屏隐形，见配置注释）。
        if config.capture_method == "gdi":
            self._disable_magnifier(ScreenBackdrop)

        panel = widget.panel_rect()
        self._material = GlassMaterial(thickness=config.glass_thickness, frost=config.glass_frost)
        self._style = GlassStyle(
            # 阴影层数从默认 14 降到 6：叠 14 层圆角矩形在小窗口上是纯浪费，
            # 6 层肉眼几乎看不出差别，但每帧少画 8 次。
            shadow_layers=6,
            shadow_step=3.6,
            shadow_alpha=6,
        )
        # 局部重绘专用样式：动画每帧只刷面板内部一小块（封面/频谱），
        # 而阴影层全在面板**外面**、必然被裁掉 —— 实测省 0.4ms/帧
        # （30fps 下约 1.2% 单核），视觉上完全一致。
        self._style_interior = GlassStyle(shadow_layers=0, shadow_step=3.6, shadow_alpha=6)
        self._renderer = GlassRenderer(
            self._material, panel.width(), panel.height(), theme.RADIUS
        )
        self._backdrop = ScreenBackdrop(widget, interval_ms=config.capture_interval_ms,
                                        capture_margin=int(config.capture_margin))
        self._backdrop.changed.connect(self._on_backdrop_changed)
        self._pixmap: QPixmap | None = None
        self._interval_from_config = config.capture_interval_ms

    # ---- 生命周期 ----------------------------------------------------
    @staticmethod
    def _disable_magnifier(backdrop_cls) -> None:
        """让 pyglass 跳过放大镜，回退到 BitBlt 抓屏 + SetWindowDisplayAffinity。

        _make_capturer() 返回 None 时，pyglass 自己会走回退分支（见其 configure() 源码）。
        代价：组件会从所有屏幕抓取里被排除（用户截图/录屏看不到它）。
        """
        if getattr(backdrop_cls, "_musicglass_gdi", False):
            return
        backdrop_cls._make_capturer = lambda self: None   # type: ignore[assignment]
        backdrop_cls._musicglass_gdi = True               # type: ignore[attr-defined]

    def configure(self) -> None:
        self._backdrop.configure()
        # configure() 成功时会自己把节奏重置成 120/300，所以配置的间隔要在它之后再落一次
        self.apply_interval(self._interval_from_config)

    def start(self) -> None:
        self._backdrop.start()

    def stop(self) -> None:
        self._backdrop.stop()

    def cleanup(self) -> None:
        """停抓取 + 关掉放大镜/截图器（退出时调，避免进程 abort）。"""
        try:
            self._backdrop.cleanup()
        except Exception as exc:
            print(f"[音璃] 释放抓屏资源时出错（可忽略）：{exc}")
        self._pixmap = None

    # ------------------------------------------------------------------
    @staticmethod
    def install_grab_timing() -> None:
        """给 pyglass 的抓屏函数包一层计时（只在 --perf 时用）。

        为什么得从它内部量：抓屏不是在 changed 信号里做的，
        而是 pyglass 自己定时器里做的（第一次量出来就吓了一跳）。
        """
        from pyglass.backdrop import ScreenBackdrop

        if getattr(ScreenBackdrop, "_musicglass_timed", False):
            return

        def make(func, label):
            def wrapper(self, *args, **kwargs):
                started = time.perf_counter()
                try:
                    return func(self, *args, **kwargs)
                finally:
                    monitor.record(label, (time.perf_counter() - started) * 1000.0)
            return wrapper

        for name, label in (("_grab_sync", "抓屏(GDI)"), ("_grab_capturer", "抓屏(放大镜)")):
            original = getattr(ScreenBackdrop, name, None)
            if original is not None:
                setattr(ScreenBackdrop, name, make(original, label))
        ScreenBackdrop._musicglass_timed = True

    def set_live(self, on: bool) -> None:
        self._live = on
        self._backdrop.set_live(on)
        if on:
            self._backdrop.refresh()

    def refresh(self) -> None:
        self._backdrop.refresh()

    # ---- 几何 --------------------------------------------------------
    def set_geometry(self, panel: QRectF, radius: float) -> None:
        # 尺寸变了必须重建折射核（核是按具体像素尺寸预计算的）
        self._renderer.set_geometry(panel.width(), panel.height(), radius)
        self._pixmap = None

    def apply_interval(self, interval_ms: int) -> None:
        self._interval_from_config = interval_ms
        timer = getattr(self._backdrop, "_timer", None)
        if timer is not None:
            timer.setInterval(interval_ms)
        self._backdrop._fast_ms = interval_ms
        self._backdrop._slow_ms = max(interval_ms * 2, 300)

    def begin_drag(self) -> None:
        self._backdrop.prepare_drag()

    def end_drag(self) -> None:
        self._backdrop.end_drag()
        self._refract()          # 落位后按最终位置重算一次
        self.changed.emit()

    # ---- 折射 --------------------------------------------------------
    def _on_backdrop_changed(self) -> None:
        started = time.perf_counter()
        self._refract()
        monitor.record("折射", (time.perf_counter() - started) * 1000.0)
        self.changed.emit()

    def _panel_origin(self) -> QPoint:
        """面板左上角在"背景数组坐标系"里的位置。

        背景数组是桌面的一小块截图，它的 (0,0) 不一定在屏幕 (0,0)，
        所以要减去 backdrop.global_origin() 换算过去。

        注意：这里不用 widget.mapToGlobal()。因为窗口贴到桌面层之后，
        原生父窗口是外部的 WorkerW/Progman，Qt 并不知道这件事，
        mapToGlobal() 会算错。所以一律走窗口自己提供的 panel_screen_pos()。
        """
        top_left = self._widget.panel_screen_pos()
        return top_left - self._backdrop.global_origin()

    def _refract(self, *, fast: bool = False) -> None:
        array = self._backdrop.array()
        if array is None:
            return
        # refract_fast：跳过霜化散射。实测这是折射耗时的大头之一，
        # 而默认霜化只有 0.15，视觉差别很小（拖动时本来就走 fast）。
        use_fast = fast or self._config.refract_fast
        self._pixmap = self._renderer.refract(
            array, self._panel_origin(), self._backdrop.dpr(), fast=use_fast
        )

    # ---- 绘制 --------------------------------------------------------
    def paint(self, painter: QPainter, panel: QRectF, radius: float, alpha: float,
              interior: bool = False) -> None:
        from pyglass import paint_glass

        # reveal=alpha 会同时缩放阴影/色调/高光/边缘线和折射层的透明度，
        # 正好实现"鼠标移出变半透明"。
        style = self._style_interior if interior else self._style
        paint_glass(painter, panel, radius, self._pixmap, style=style, reveal=alpha)

    def status_text(self) -> str:
        excluded = getattr(self._backdrop, "excluded", None)
        capturer = type(getattr(self._backdrop, "_capturer", None)).__name__
        if capturer == "NoneType":
            # 没截图器有两种情况：主动禁用了放大镜（excluded=True，走 WDA），
            # 或者已经 stop()/cleanup() 过（资源已释放）。别把两者混淆了。
            capturer = "BitBlt（放大镜已禁用）" if getattr(self._backdrop, "excluded", False) \
                else "无（已停止或初始化失败）"
        array = self._backdrop.array()
        shape = None if array is None else f"{array.shape[1]}x{array.shape[0]}"
        pixmap = "无" if self._pixmap is None else f"{self._pixmap.width()}x{self._pixmap.height()}"
        return (f"pyglass 真折射  抓屏={capturer}  实时={getattr(self._backdrop, 'live', '?')}"
                f"  排除自己={excluded}  可录屏={getattr(self._backdrop, 'recordable', '?')}"
                f"  背景数组={shape}  折射位图={pixmap}")


# ---------------------------------------------------------------------------
# 后端 2：Qt 半透明 + 模糊（备选）
# ---------------------------------------------------------------------------
class QtBlurBackend(GlassBackend):
    """不依赖 pyglass 的备选方案。

    抓取窗口所在的那一小块屏幕 → QGraphicsBlurEffect 高斯模糊 → 圆角裁剪后贴上来，
    再叠一层薄荷色和一条高光边。

    为了不让玻璃"抓到自己"（否则会出现无限镜像），这里会把自己从屏幕抓取里排除掉。
    副作用：用户用截图工具/录屏也看不到这个组件了。所以这是**备选**方案。
    """

    MARGIN_EXTRA = 0  # 抓取区域就是窗口本身（窗口已经自带 MARGIN 留白）

    def __init__(self, widget, config: Config) -> None:
        super().__init__(widget)
        self._blur_radius = 22
        self._pixmap: QPixmap | None = None
        self._dpr = 1.0
        self._interval_ms = max(200, int(config.capture_interval_ms) * 2)
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._refresh)

    # ---- 生命周期 ----------------------------------------------------
    def configure(self) -> None:
        from ..platform.winutil import set_capture_visible

        # 把自己从抓屏里排除，避免自我折射
        set_capture_visible(int(self._widget.winId()), visible=False)

    def start(self) -> None:
        self._refresh()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def cleanup(self) -> None:
        self._timer.stop()
        self._pixmap = None

    def set_live(self, on: bool) -> None:
        self._live = on
        if on:
            self._refresh()
            self._timer.start()
        else:
            self._timer.stop()

    def refresh(self) -> None:
        self._refresh()

    def apply_interval(self, interval_ms: int) -> None:
        self._timer.setInterval(self._interval_ms)

    # ---- 抓取 + 模糊 -------------------------------------------------
    def _refresh(self) -> None:
        if not self._widget.isVisible():
            return
        # 注意：这里不用 Qt 的 frameGeometry()。
        # 窗口贴到桌面层后，原生父窗口是外部的 WorkerW/Progman，Qt 并不知道，
        # 它算出来的屏幕坐标会偏。所以一律用窗口自己提供的 panel_screen_pos()。
        panel_w = int(self._widget.panel_rect().width())
        panel_h = int(self._widget.panel_rect().height())
        window_topleft = self._widget.panel_screen_pos() - QPoint(theme.MARGIN, theme.MARGIN)
        screen = QApplication.screenAt(window_topleft + QPoint(panel_w // 2, panel_h // 2))
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return
        dpr = screen.devicePixelRatio() or 1.0
        shot = screen.grabWindow(
            0, int(window_topleft.x() * dpr), int(window_topleft.y() * dpr),
            int(self._widget.width() * dpr), int(self._widget.height() * dpr),
        )
        if shot.isNull():
            return
        shot.setDevicePixelRatio(dpr)
        self._dpr = dpr
        self._pixmap = self._blur(shot)
        self.changed.emit()

    def _blur(self, pixmap: QPixmap) -> QPixmap:
        """用 QGraphicsScene + QGraphicsBlurEffect 做高斯模糊。

        这是 Qt 自带的能力，不需要 numpy / PIL，也不需要着色器。
        """
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(pixmap)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(self._blur_radius)
        effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(effect)
        scene.addItem(item)

        out = QImage(pixmap.size(), QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        scene.render(painter, QRectF(0, 0, pixmap.width(), pixmap.height()),
                     QRectF(0, 0, pixmap.width(), pixmap.height()))
        painter.end()
        scene.removeItem(item)
        blurred = QPixmap.fromImage(out)
        blurred.setDevicePixelRatio(pixmap.devicePixelRatio())
        return blurred

    # ---- 绘制 --------------------------------------------------------
    def paint(self, painter: QPainter, panel: QRectF, radius: float, alpha: float,
              interior: bool = False) -> None:
        # interior 对模糊后端不适用（它本来就没画阴影），参数只为接口一致
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        path = QPainterPath()
        path.addRoundedRect(panel, radius, radius)
        painter.setClipPath(path)
        painter.setOpacity(alpha)

        if self._pixmap is not None and not self._pixmap.isNull():
            dpr = self._dpr
            source = QRectF(panel.left() * dpr, panel.top() * dpr,
                            panel.width() * dpr, panel.height() * dpr)
            painter.drawPixmap(panel, self._pixmap, source)

        # 薄荷 -> 青蓝的色调叠加
        gradient = QLinearGradient(panel.topLeft(), panel.bottomRight())
        gradient.setColorAt(0.0, QColor(theme.MINT.red(), theme.MINT.green(), theme.MINT.blue(), 40))
        gradient.setColorAt(1.0, QColor(theme.CYAN.red(), theme.CYAN.green(), theme.CYAN.blue(), 30))
        painter.fillPath(path, gradient)

        # 高光边缘（模拟玻璃的菲涅尔反射）
        painter.setClipping(False)
        painter.setOpacity(alpha)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 82), 1.2))
        painter.drawPath(path)
        painter.restore()

    def status_text(self) -> str:
        return f"Qt 半透明+模糊  抓屏间隔={self._interval_ms}ms  模糊半径={self._blur_radius}"


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------
def create_backend(widget, config: Config) -> GlassBackend:
    """按配置造后端。造不出来（比如 pyglass 没装）就自动退回 Qt 方案。"""
    if config.glass_backend == "pyglass":
        try:
            return PyGlassBackend(widget, config)
        except Exception as exc:  # pyglass 导入失败 / 版本不兼容
            print(f"[音璃] pyglass 后端不可用（{exc}），退回 Qt 模糊方案")
    return QtBlurBackend(widget, config)
