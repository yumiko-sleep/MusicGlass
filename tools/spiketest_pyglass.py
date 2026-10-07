# -*- coding: utf-8 -*-
"""
音璃 MusicGlass — 阶段 0.5：液态玻璃「冒烟测试」

这个脚本只有一个目的：在写任何 UI 正式代码之前，先确认两件事——
  1. 你机器上 pyglass 的「真折射」到底能不能正常工作（能不能抓屏、能不能把自己排除掉）；
  2. 它的 CPU / 内存代价是多少，够不够我们项目的性能预算（CPU 空载≈0%、内存≤150MB）。

它不依赖项目里的任何模块，可以单独跑。

─────────────────────────── 运行方式 ───────────────────────────
    python tools/spiketest_pyglass.py                 # 测 pyglass 真折射（默认）
    python tools/spiketest_pyglass.py --mode qt       # 测备选方案（半透明 + 抓屏模糊）
    python tools/spiketest_pyglass.py --interval 60   # 抓屏间隔改 60ms（更费 CPU）
    python tools/spiketest_pyglass.py --quick         # 每个场景减半，约 15 秒跑完
    python tools/spiketest_pyglass.py --auto-quit     # 测完自动关窗口（无人值守）

─────────────────────────── 测什么 ───────────────────────────
依次跑 4 个场景并打印数据：
    A 静置（抓屏开）   —— 组件挂在桌面没动时的开销
    B 动画中（抓屏开） —— 封面在转、频谱在跳时的开销（最坏情况，30fps 全量重绘）
    C 静置（抓屏关）   —— 抓屏停掉后的"地板"开销
    D 动画中（抓屏关） —— 只做绘制、不抓屏的开销

跑完窗口不会自动关（除非加 --auto-quit），你可以拖着玩、调旋钮，
按 ESC 退出。

─────────────────────────── 快捷键 ───────────────────────────
    [ / ]   减 / 加 thickness（玻璃厚度：越大折射越强、边缘越弯）
    - / =   减 / 加 frost（霜化：越大越像磨砂玻璃、底色越糊）
    R       强制重新抓一帧背景
    L       开关「实时抓屏」（关掉 = 冻结背景，省 CPU）
    ESC     退出
"""

from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import sys
import time
from ctypes import wintypes

# ---------------------------------------------------------------------------
# 0. 环境准备
# ---------------------------------------------------------------------------
# 让控制台按 UTF-8 输出，避免中文在 GBK 代码页下直接抛 UnicodeEncodeError。
# 注意：本脚本一律不用 emoji（GBK 里没有，会直接崩），只用 ASCII 符号。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

# 高 DPI：必须在 QApplication 创建之前设置，否则跨显示器缩放比例会算错物理像素。
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

from PyQt6.QtCore import PYQT_VERSION_STR, QObject, QPoint, QRectF, QT_VERSION_STR, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QLabel,
    QVBoxLayout,
    QWidget,
)

# 主题色（与将来 ui/theme.py 保持一致）
MINT = QColor(0x3D, 0xDC, 0x97)     # 薄荷绿
CYAN = QColor(0x22, 0xC1, 0xDC)     # 青蓝

# ---------------------------------------------------------------------------
# 1. Windows 原生进程指标（不装 psutil，纯 ctypes，零额外依赖）
# ---------------------------------------------------------------------------
_kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
_psapi = ctypes.WinDLL("psapi")      # type: ignore[attr-defined]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    """GetProcessMemoryInfo 返回的结构体。"""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


# 关键坑（踩过一次，务必保留）：必须显式声明参数类型和返回类型。
# HANDLE 在 64 位下是 8 字节。如果不声明，ctypes 会把 GetCurrentProcess() 返回的伪句柄
# -1 当 32 位 int 处理，回传给 API 时高位补零 → 变成非法句柄，
# 于是 GetProcessTimes / GetProcessMemoryInfo 全部返回 0（失败），指标永远是 0.0。
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
_kernel32.GetProcessTimes.restype = wintypes.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
    wintypes.DWORD,
]
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _get_memory_info() -> tuple[float, float]:
    """返回 (工作集 MB, 私有提交 MB)。

    工作集(WorkingSet)  = 物理内存里实际占着的部分，任务管理器「内存」列显示的就是它。
    私有提交(Pagefile)  = 该进程独占的虚拟内存，更能反映"真实占用"。
    """
    pmc = _PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    handle = _kernel32.GetCurrentProcess()
    if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
        print("[!] GetProcessMemoryInfo 调用失败，内存数据不可信", flush=True)
    mb = 1024.0 * 1024.0
    return pmc.WorkingSetSize / mb, pmc.PagefileUsage / mb


def _process_cpu_seconds() -> float:
    """返回本进程累计消耗的 CPU 时间（用户态 + 内核态，秒）。

    这是"累计值"：两次采样的差值 ÷ 墙钟时间 = 这段时间的 CPU 占用率。
    """
    creation, exit_, kernel, user = (
        wintypes.FILETIME(),
        wintypes.FILETIME(),
        wintypes.FILETIME(),
        wintypes.FILETIME(),
    )
    handle = _kernel32.GetCurrentProcess()
    if not _kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        print("[!] GetProcessTimes 调用失败，CPU 数据不可信", flush=True)
    # FILETIME 单位是 100 纳秒，拼成 64 位整数后除以 1e7 得秒
    ticks = ((kernel.dwHighDateTime << 32) | kernel.dwLowDateTime) + (
        (user.dwHighDateTime << 32) | user.dwLowDateTime
    )
    return ticks / 1e7


CPU_COUNT = os.cpu_count() or 1

# ---------------------------------------------------------------------------
# 2. 全局统计（由计时包装器写入，每个场景开始时清空）
# ---------------------------------------------------------------------------
STATS: dict[str, list[float]] = {"refract_ms": [], "grab_ms": []}
PAINT_COUNT = 0  # paintEvent 调用次数，用来算真实重绘帧率


def _install_timing_hooks() -> None:
    """给 pyglass 内部两个热点函数打"只读计时补丁"。

    - GlassRenderer.refract                       把背景像素做物理折射（numpy，最贵）
    - ScreenBackdrop._grab_sync / _grab_capturer  抓屏本身（grabWindow 或放大镜 API）

    只记时间，不改任何逻辑。
    """
    try:
        from pyglass import effect
        from pyglass.backdrop import ScreenBackdrop
    except Exception as exc:  # pragma: no cover
        print(f"[!] 无法导入 pyglass，跳过计时钩子：{exc}")
        return

    orig_refract = effect.GlassRenderer.refract

    def timed_refract(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            return orig_refract(self, *args, **kwargs)
        finally:
            STATS["refract_ms"].append((time.perf_counter() - t0) * 1000.0)

    effect.GlassRenderer.refract = timed_refract  # type: ignore[assignment]

    def make_timed(original):  # 用闭包捕获原函数，避免循环变量串味
        def timed(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            t0 = time.perf_counter()
            try:
                return original(self, *args, **kwargs)
            finally:
                STATS["grab_ms"].append((time.perf_counter() - t0) * 1000.0)

        return timed

    for method_name in ("_grab_sync", "_grab_capturer"):
        original = getattr(ScreenBackdrop, method_name, None)
        if original is not None:
            setattr(ScreenBackdrop, method_name, make_timed(original))


# ---------------------------------------------------------------------------
# 3. 后端 A：pyglass 真折射面板
# ---------------------------------------------------------------------------
def build_refract_pane(panel_size: tuple[int, int], radius: int, interval_ms: int,
                       thickness: float, frost: float):
    """构造一个继承自 pyglass.GlassPane 的面板。

    为什么要继承而不是包一层：GlassPane 自己就是顶层窗口（无父窗口时），
    再套一个 QWidget 只会让重绘关系和坐标变乱。直接继承、只覆盖
    paintEvent（数帧）最干净。
    """
    from pyglass import GlassMaterial, GlassPane

    class SpyGlassPane(GlassPane):
        def __init__(self) -> None:
            super().__init__(
                parent=None,               # 无父窗口 = 顶层窗口，折射真实桌面
                panel_size=panel_size,
                radius=radius,
                margin=56,                 # 玻璃外围留白，给阴影用
                material=GlassMaterial(thickness=thickness, frost=frost),
                draggable=True,
            )
            # 内容放在 self.content 里
            self._title = QLabel("音璃 MusicGlass")
            self._sub = QLabel("折射冒烟测试 · 拖动我试试")
            self._dials_label = QLabel("")
            for label, size, weight, alpha in (
                (self._title, 15, QFont.Weight.Bold, 255),
                (self._sub, 9, QFont.Weight.Normal, 170),
                (self._dials_label, 8, QFont.Weight.Normal, 120),
            ):
                font = QFont("Microsoft YaHei UI", size)
                font.setWeight(weight)
                label.setFont(font)
                label.setStyleSheet(f"color: rgba(255,255,255,{alpha}); background: transparent;")
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

            layout = QVBoxLayout(self.content)
            layout.setContentsMargins(24, 16, 24, 16)
            layout.setSpacing(2)
            layout.addWidget(self._title)
            layout.addWidget(self._sub)
            layout.addStretch(1)
            layout.addWidget(self._dials_label)
            self._on_dials_changed()

        # --- 统计 ---
        def paintEvent(self, event) -> None:  # noqa: N802
            global PAINT_COUNT
            PAINT_COUNT += 1
            super().paintEvent(event)

        # --- 与 Bench 约定的接口 ---
        def set_live(self, on: bool) -> None:
            """开关实时抓屏：关掉后背景冻结，省下抓屏 + 折射的开销。"""
            self.backdrop.set_live(on)

        def apply_interval(self, ms: int) -> None:
            """调整抓屏节奏。

            必须在窗口 show() 之后再调用：showEvent 里会执行 backdrop.configure()，
            而 configure() 成功时会自行把 fast_ms/slow_ms 重写为 120/300。
            """
            timer = getattr(self.backdrop, "_timer", None)
            if timer is not None:
                timer.setInterval(ms)
            self.backdrop._fast_ms = ms
            self.backdrop._slow_ms = max(ms * 2, 300)

        def _on_dials_changed(self) -> None:
            material = self.material
            self._dials_label.setText(
                f"thickness={material.thickness:.2f}  frost={material.frost:.2f}"
                f"   [ ] 调厚度   - = 调霜化"
            )

    return SpyGlassPane()


# ---------------------------------------------------------------------------
# 4. 后端 B：Qt 备选方案（半透明 + 抓屏模糊）
# ---------------------------------------------------------------------------
class QtBlurPane(QWidget):
    """不用 pyglass 的备选做法。

    无边框透明窗口 + 定时抓一小块桌面 → QGraphicsBlurEffect 高斯模糊 →
    圆角裁剪 + 薄荷色半透明叠加 + 高光边框。
    代价低得多，但没有真实折射（边缘不会弯、没有色散）。
    """

    MARGIN = 30

    def __init__(self, panel_size: tuple[int, int], radius: int, interval_ms: int) -> None:
        super().__init__(None)
        self._panel_w, self._panel_h = panel_size
        self._radius = radius
        self._blur_radius = 22
        self._bg: QPixmap | None = None
        self._drag_offset: QPoint | None = None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(panel_size[0] + 2 * self.MARGIN, panel_size[1] + 2 * self.MARGIN)

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._refresh_bg)

        font = QFont("Microsoft YaHei UI", 13)
        font.setWeight(QFont.Weight.Bold)
        self._label = QLabel("音璃 MusicGlass（Qt 备选后端）", self)
        self._label.setFont(font)
        self._label.setStyleSheet("color: rgba(255,255,255,240); background: transparent;")
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._label.setGeometry(self.MARGIN + 24, self.MARGIN + 28, panel_size[0] - 48, 30)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    # --- 抓屏 + 模糊 ---
    def _refresh_bg(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        dpr = screen.devicePixelRatio()
        geo = self.frameGeometry()
        t0 = time.perf_counter()
        pm = screen.grabWindow(
            0, int(geo.x() * dpr), int(geo.y() * dpr), int(geo.width() * dpr), int(geo.height() * dpr)
        )
        STATS["grab_ms"].append((time.perf_counter() - t0) * 1000.0)
        if pm.isNull():
            return
        t1 = time.perf_counter()
        self._bg = self._blur(pm)
        STATS["refract_ms"].append((time.perf_counter() - t1) * 1000.0)  # 复用同一统计桶
        self.update()

    def _blur(self, pm: QPixmap) -> QPixmap:
        """QGraphicsScene + QGraphicsBlurEffect 做高斯模糊（纯 Qt，不需要 PIL/numpy）。"""
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(pm)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(self._blur_radius)
        effect.setBlurHints(QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        out = QImage(pm.size(), QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        scene.render(
            painter, QRectF(0, 0, pm.width(), pm.height()), QRectF(0, 0, pm.width(), pm.height())
        )
        painter.end()
        scene.removeItem(item)
        return QPixmap.fromImage(out)

    # --- 绘制 ---
    def _panel_rect(self) -> QRectF:
        return QRectF(self.MARGIN, self.MARGIN, self._panel_w, self._panel_h)

    def paintEvent(self, event) -> None:  # noqa: N802
        global PAINT_COUNT
        PAINT_COUNT += 1
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        path = QPainterPath()
        path.addRoundedRect(self._panel_rect(), self._radius, self._radius)

        painter.save()
        painter.setClipPath(path)
        if self._bg is not None and not self._bg.isNull():
            painter.drawPixmap(self._panel_rect(), self._bg, QRectF(self._bg.rect()))
        gradient = QLinearGradient(self._panel_rect().topLeft(), self._panel_rect().bottomRight())
        gradient.setColorAt(0.0, QColor(MINT.red(), MINT.green(), MINT.blue(), 46))
        gradient.setColorAt(1.0, QColor(CYAN.red(), CYAN.green(), CYAN.blue(), 34))
        painter.fillPath(path, QBrush(gradient))
        painter.restore()

        painter.setBrush(Qt.BrushStyle.NoBrush)          # 高光边缘
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
        painter.drawPath(path)
        painter.end()

    # --- 拖动 ---
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_bg()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    # --- 与 Bench 约定的接口 ---
    def set_live(self, on: bool) -> None:
        if on:
            self._timer.start()
        else:
            self._timer.stop()

    def apply_interval(self, ms: int) -> None:
        self._timer.setInterval(ms)


# ---------------------------------------------------------------------------
# 5. 测试流程（状态机）
# ---------------------------------------------------------------------------
def _avg(values: list[float]) -> str:
    return f"{statistics.fmean(values):5.2f}" if values else "    -"


class Bench(QObject):
    """按顺序跑完 4 个场景，每个场景结束打印一行结果。"""

    def __init__(self, app: QApplication, pane, anim: QTimer,
                 phases: list[tuple[str, int, bool, bool]], auto_quit: bool) -> None:
        super().__init__()
        self._app = app
        self._pane = pane
        self._anim = anim
        self._phases = phases
        self._auto_quit = auto_quit
        self._i = 0
        self._timer = QTimer(self)
        self._timer.setInterval(500)          # 每 500ms 采一次内存峰值
        self._timer.timeout.connect(self._collect)
        self._cpu0 = 0.0
        self._wall0 = 0.0
        self._peak_ws = 0.0
        self._peak_priv = 0.0
        self._rows: list[str] = []

    def start(self) -> None:
        QTimer.singleShot(1500, self._begin)  # 等窗口显示、首帧抓屏完成

    def _begin(self) -> None:
        if self._i >= len(self._phases):
            self._finish()
            return
        name, duration_s, live, animate = self._phases[self._i]
        self._pane.set_live(live)
        if animate:
            self._anim.start()
        else:
            self._anim.stop()
        STATS["refract_ms"].clear()
        STATS["grab_ms"].clear()
        global PAINT_COUNT
        PAINT_COUNT = 0

        ws, priv = _get_memory_info()
        self._peak_ws, self._peak_priv = ws, priv
        self._cpu0 = _process_cpu_seconds()
        self._wall0 = time.perf_counter()
        print(f"\n>>> 场景 {name}（{duration_s}s  抓屏={'开' if live else '关'}"
              f"  动画={'开' if animate else '关'}）...", flush=True)
        self._timer.start()
        QTimer.singleShot(duration_s * 1000, self._end)

    def _collect(self) -> None:
        ws, priv = _get_memory_info()
        self._peak_ws = max(self._peak_ws, ws)
        self._peak_priv = max(self._peak_priv, priv)

    def _end(self) -> None:
        self._timer.stop()
        wall = max(time.perf_counter() - self._wall0, 1e-6)
        cpu = _process_cpu_seconds() - self._cpu0
        self._collect()

        one_core = cpu / wall * 100.0        # 占一个逻辑核心的百分比
        all_cores = one_core / CPU_COUNT     # 占整机 CPU 的百分比
        fps = PAINT_COUNT / wall

        row = (
            f"{self._phases[self._i][0]:<18}"
            f"CPU {one_core:5.1f}%（整机 {all_cores:4.1f}%）"
            f" | 内存 工作集 {self._peak_ws:6.1f}MB  私有 {self._peak_priv:6.1f}MB"
            f" | 重绘 {fps:5.1f}fps"
            f" | 抓屏 {len(STATS['grab_ms']):3d}次 {_avg(STATS['grab_ms'])}ms"
            f" | 折射/模糊 {len(STATS['refract_ms']):3d}次 {_avg(STATS['refract_ms'])}ms"
        )
        self._rows.append(row)
        print("    " + row, flush=True)

        self._i += 1
        QTimer.singleShot(800, self._begin)

    def _finish(self) -> None:
        self._anim.stop()
        self._pane.set_live(True)
        print("\n" + "=" * 112)
        print("阶段 0.5 结果汇总")
        print("=" * 112)
        for row in self._rows:
            print("  " + row)
        print("=" * 112)
        print(f"""
说明：
  * CPU 百分比 = 本进程 CPU 时间 / 墙钟时间；100% = 占满一个逻辑核心，不是整机。
  * 本机逻辑核心数：{CPU_COUNT}。「整机」列 = 单核占比 / 核心数。
  * 抓屏/折射耗时是"每次调用"的平均毫秒数，调用次数由抓屏间隔决定。

请肉眼自检这 5 条（数字看不出来的部分）：
  1. 玻璃边缘有没有明显的折射/弯折/彩色边？（有 = 真折射生效）
  2. 玻璃里有没有看到它自己（重影，或边缘出现自己的边框）？（有 = 排除失败，效果会脏）
  3. 拖动跟手吗？有没有闪烁？
  4. 按 [ ] 和 - = 调旋钮，哪个组合最好看？把数字告诉我（会写进正式项目默认值）
  5. 背景是静态壁纸时静置 CPU 多少？背景放个视频/游戏时又是多少？
""", flush=True)
        if self._auto_quit:
            print("[--auto-quit] 3 秒后自动关闭窗口…", flush=True)
            QTimer.singleShot(3000, self._app.quit)
        else:
            print("窗口留着不关，随便玩，按 ESC 退出。", flush=True)


# ---------------------------------------------------------------------------
# 6. 入口
# ---------------------------------------------------------------------------
def env_report(app: QApplication, pane) -> None:
    import platform

    import numpy

    screen = app.primaryScreen()
    geo = screen.geometry()
    print("=" * 112)
    print("音璃 MusicGlass — 阶段 0.5 液态玻璃冒烟测试")
    print("=" * 112)
    print(f"  Python           : {platform.python_version()}  ({sys.executable})")
    print(f"  操作系统         : {platform.platform()}")
    print(f"  Qt / PyQt6       : {QT_VERSION_STR} / {PYQT_VERSION_STR}")
    try:
        import pyglass

        print(f"  pyglass-qt       : {pyglass.__version__}")
    except Exception as exc:
        print(f"  pyglass-qt       : 导入失败 -> {exc}")
    print(f"  numpy            : {numpy.__version__}")
    print(f"  屏幕             : {geo.width()}x{geo.height()}  DPR={screen.devicePixelRatio()}"
          f"  逻辑核心数={CPU_COUNT}")
    print(f"  Qt 平台插件      : {app.platformName()}")

    try:
        from pyglass._magnifier import available as magnifier_available

        magnifier = magnifier_available()
    except Exception:
        magnifier = False
    print(f"  Win 放大镜API可用: {magnifier}   （True = GPU 直取桌面，最干净的实时方案）")

    backdrop = getattr(pane, "backdrop", None)
    if backdrop is not None:
        print(f"  抓屏后端         : {type(backdrop).__name__}")
        print(f"  live / excluded  : {getattr(backdrop, 'live', '?')} / {getattr(backdrop, 'excluded', '?')}")
        print(f"  可被录屏         : {getattr(backdrop, 'recordable', '?')}")
        print(f"  自适应间隔       : fast={getattr(backdrop, '_fast_ms', '?')}ms"
              f"  slow={getattr(backdrop, '_slow_ms', '?')}ms")
        if getattr(backdrop, "excluded", None) is False:
            print("  [!] 排除自己失败 -> 玻璃会折射自己，可能重影/闪烁（阶段1要准备降级方案）")
    else:
        print("  抓屏后端         : Qt grabWindow + QGraphicsBlurEffect（备选，无真实折射）")
    print("=" * 112, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="音璃 MusicGlass 阶段0.5 冒烟测试")
    parser.add_argument("--mode", choices=("refract", "qt"), default="refract",
                        help="refract = pyglass 真折射（默认）；qt = 半透明+模糊备选方案")
    parser.add_argument("--size", default="400x120", help="面板尺寸，如 400x120")
    parser.add_argument("--radius", type=int, default=26, help="圆角半径")
    parser.add_argument("--interval", type=int, default=120, help="抓屏间隔 ms（越小越耗 CPU）")
    parser.add_argument("--thickness", type=float, default=0.55, help="玻璃厚度 0~1（中性值 0.5）")
    parser.add_argument("--frost", type=float, default=0.15, help="霜化 0~1（0 = 最通透）")
    parser.add_argument("--quick", action="store_true", help="每个场景减半时长（约 15 秒跑完）")
    parser.add_argument("--auto-quit", action="store_true", help="测完自动关窗口（无人值守）")
    args = parser.parse_args()

    try:
        panel_w, panel_h = (int(v) for v in args.size.lower().split("x"))
    except Exception:
        print(f"[!] --size 格式错误：{args.size}，应为 400x120")
        return 2

    app = QApplication(sys.argv)

    if args.mode == "refract":
        _install_timing_hooks()
        pane = build_refract_pane(
            (panel_w, panel_h), args.radius, args.interval, args.thickness, args.frost
        )
    else:
        pane = QtBlurPane((panel_w, panel_h), args.radius, args.interval)

    pane.show_() if hasattr(pane, "show_") else pane.show()
    if hasattr(pane, "apply_interval"):
        pane.apply_interval(args.interval)

    # 放到屏幕右下角，像一个真正的桌面小组件。
    # 注意：必须在 show() 之后 move()，因为 GlassPane.showEvent() 会把顶层窗口居中。
    screen_geo = app.primaryScreen().availableGeometry()
    pane.move(
        screen_geo.right() - pane.width() - 24,
        screen_geo.bottom() - pane.height() - 24,
    )
    pane.raise_()
    pane.activateWindow()

    env_report(app, pane)

    # 动画模拟：33ms 重绘一次 ≈ 30fps，模拟"封面在转、频谱在跳"的最坏开销。
    # 注意：GlassPane 的内容是半透明子控件，Qt 会连带重绘父窗口的玻璃层，
    # 所以这个 update() 等价于"整块玻璃每帧重新合成"的代价（保守估计）。
    anim = QTimer()
    anim.setInterval(33)
    anim.timeout.connect(pane.update)

    scale = 0.5 if args.quick else 1.0
    phases = [
        ("A 静置(抓屏开)", int(8 * scale), True, False),
        ("B 动画中(抓屏开)", int(8 * scale), True, True),
        ("C 静置(抓屏关)", int(5 * scale), False, False),
        ("D 动画中(抓屏关)", int(5 * scale), False, True),
    ]
    bench = Bench(app, pane, anim, phases, args.auto_quit)
    bench.start()
    print("\n测试即将开始（A/B/C/D 四个场景）…", flush=True)

    code = app.exec()
    working_set, private = _get_memory_info()
    print(f"\n窗口已关闭。退出前：工作集 {working_set:.1f}MB  私有 {private:.1f}MB")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
