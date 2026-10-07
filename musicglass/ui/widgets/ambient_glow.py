# -*- coding: utf-8 -*-
"""氛围光：组件四周一圈浅绿色的柔和光晕。

实现方式：由外向内叠画 N 层"放大的圆角矩形描边"，
每层透明度递减 —— 叠起来就是一圈自然的柔光。
比真正的高斯模糊便宜得多（不用卷积、不用离屏缓冲）。

阶段4 的"呼吸"改用**位图缓存**：
    12 层描边本身很贵（实测 1.2~1.5ms/次），而呼吸只是整体亮度在变。
    于是把 12 层按"满亮度"烘成一张位图，之后每帧只 drawPixmap 一次
    （实测 0.12ms），由 painter.setOpacity(强度) 来调亮度。

    这样做的唯一差别：叠层时 alpha 是复合的（1-(1-a)(1-b)...），
    整体缩放透明度不完全等价于逐层缩放。实测两个极值（亮度 0.72 / 1.00）
    的像素差最大 2/255 —— 肉眼不可能看出来，换来 12 倍的速度。
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap

from .. import theme


class AmbientGlow:
    def __init__(self) -> None:
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None
        self.renders = 0        # 实际重画次数（自检用：动画期间应该一直是同一个值）

    # ------------------------------------------------------------------
    def paint(self, painter: QPainter, panel: QRectF, radius: float, strength: float = 1.0,
              bounds: QRectF | None = None, dpr: float = 1.0) -> None:
        """把光晕画到 painter 上。

        bounds / dpr：窗口矩形与设备像素比。给了就走"位图缓存"路径；
        没给（比如离屏渲染、测试）就现场叠 12 层，结果一样、只是慢一点。
        """
        if strength <= 0.01:
            return
        if bounds is None:
            self._layers(painter, panel, radius, strength)
            return

        pixmap = self._pixmap(panel, radius, bounds, dpr)
        if pixmap is None:
            return
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, strength)))
        painter.drawPixmap(bounds.topLeft(), pixmap)
        painter.restore()

    # ------------------------------------------------------------------
    # 位图缓存
    # ------------------------------------------------------------------
    def _pixmap(self, panel: QRectF, radius: float, bounds: QRectF, dpr: float) -> QPixmap | None:
        """按当前几何取（必要时先烘）光晕位图。

        key 里含面板位置/尺寸/圆角、窗口尺寸、DPR —— 这四项任一项变了都得重烘
        （缩放窗口、换显示器、改 DPI 都会变）。
        """
        dpr = max(1.0, float(dpr or 1.0))
        key = (
            round(panel.x(), 2), round(panel.y(), 2),
            round(panel.width(), 2), round(panel.height(), 2),
            round(radius, 2), round(bounds.width(), 2), round(bounds.height(), 2),
            round(dpr, 3),
            # 调参值也进 key：位图里烘的是“当时的调参”，改了参数必须重烘
            # （否则会出现“改了 theme 却毫无变化”的诡异性问题 —— 调参工具就踩过）
            theme.AMBIENT_ALPHA, theme.AMBIENT_LAYERS, round(theme.AMBIENT_SPREAD, 2),
            theme.AMBIENT.red(), theme.AMBIENT.green(), theme.AMBIENT.blue(),
        )
        if key == self._cache_key and self._cache is not None:
            return self._cache

        width = max(1, int(round(bounds.width() * dpr)))
        height = max(1, int(round(bounds.height() * dpr)))
        pixmap = QPixmap(width, height)
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._layers(painter, panel, radius, 1.0)
        painter.end()

        self._cache = pixmap
        self._cache_key = key
        self.renders += 1
        return pixmap

    def invalidate(self) -> None:
        """丢掉缓存位图（改了 theme 里的调参值后调一下，下一帧会重烘）。"""
        self._cache = None
        self._cache_key = None

    # ------------------------------------------------------------------
    # 真正的叠层绘制
    # ------------------------------------------------------------------
    @staticmethod
    def layer_plan(radius: float, strength: float = 1.0) -> tuple[float, list[tuple[float, int]]]:
        """算出“每层外扩多远、alpha 多少”。

        【绘制和自检工具共用这一份】：两边各算一遍迟早会不一致，
        到时候工具说“看得见”、画面却是另一个样子。

        关键点：**衰减按“距离窗口边缘还有多远”算**，而不是按层序号。
        因为窗口留白 MARGIN 是不跟面板缩放的，而层间距会 —— 面板放大到 1.4 倍时，
        按序号算的衰减还没归零就已经被窗口裁掉了，会在四周切出一道硬光边
        （实测 1.42 倍下边缘残余亮度 10/255，很明显）。

        返回：(层间距, [(外扩距离, alpha), ...] 从外到内)
        """
        unit = radius / theme.RADIUS if theme.RADIUS else 1.0
        # 层间距最多铺满窗口留白。
        # 【为什么要封顶】窗口留白 MARGIN 是**不随面板缩放**的：
        # 面板放大到 2 倍以上后，还按比例放大层间距的话，光晕大半会被窗口裁掉，
        # 反而变淡（实测 2.2 倍时静置亮度从 50 掉到 37、呼吸幅度从 20 掉到 14）。
        step = min(theme.AMBIENT_SPREAD * unit,
                   theme.MARGIN / theme.AMBIENT_LAYERS)
        plan: list[tuple[float, int]] = []
        for index in range(theme.AMBIENT_LAYERS, 0, -1):
            spread = index * step
            # 到窗口边缘（MARGIN）时正好衰减到 0
            fade = max(0.0, 1.0 - spread / theme.MARGIN)
            alpha = int(theme.AMBIENT_ALPHA * max(0.0, strength)
                        * fade ** theme.AMBIENT_FALLOFF)
            plan.append((spread, alpha))
        return step, plan

    def _layers(self, painter: QPainter, panel: QRectF, radius: float, strength: float) -> None:
        step, plan = self.layer_plan(radius, strength)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        for spread, alpha in plan:
            if alpha <= 0:
                continue
            painter.setPen(QPen(QColor(theme.AMBIENT.red(), theme.AMBIENT.green(),
                                       theme.AMBIENT.blue(), alpha), step * 1.8))
            painter.drawRoundedRect(
                panel.adjusted(-spread, -spread, spread, spread),
                radius + spread, radius + spread,
            )
        painter.restore()
