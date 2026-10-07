# -*- coding: utf-8 -*-
"""圆形旋转封面。

两个细节决定了它"像不像唱片"：
1. 封面上叠一圈很淡的白色弧光（模拟黑胶反光）；
2. 中心挖一个深色小孔（唱片轴心），边缘再描一圈细白线。

阶段4 的旋转只是 `painter.rotate`（数据源已经把封面裁圆、烘好遮罩，
圆盘旋转不变形、也不会露角）。

性能处理（阶段4 实测）：
    高光、描边、轴心这三样**不随旋转变化**，却是每帧固定的开销
    （渐变构建 + 裁剪路径 + 描边 + 两次椭圆，共 0.42ms）。
    把它们烘成一张静态叠加层位图，之后每帧只 drawPixmap 两张
    —— 实测 0.13ms，快 3 倍，逐像素对比最大差异 ≤ 1/255。
    另外封面改成"取源图中间那一块、直接贴进圆盘"，不再每帧设裁剪路径，
    观感与旧写法完全一致（见 _center_crop 的说明）。
    注意：叠加层的几何必须按"圆盘的实际直径"画，而不是按放大后的外框画，
    否则描边和轴心会跟着放大 6%。
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from .. import icons
from .. import theme
from ...core.cover_loader import DISC_OVERSCALE


class AlbumDisc:
    """画一张圆形封面。"""

    # 封面取景系数：只取源图中间 1/1.06（与 cover_loader 烘焙圆盘位图时一致）
    OVERSCALE = DISC_OVERSCALE

    def __init__(self) -> None:
        self._overlay_cache: QPixmap | None = None
        self._overlay_origin = QPointF()
        self._overlay_key: tuple | None = None
        self.overlay_renders = 0     # 实际重烘次数（自检用）

    # ------------------------------------------------------------------
    def paint(self, painter: QPainter, rect: QRectF, track, angle_deg: float = 0.0,
              playing: bool = True, prepared: QPixmap | None = None,
              dpr: float = 1.0) -> None:
        """画封面。

        prepared：数据源预先裁圆、缩好、带 DPR 的位图。
        有它时就只做"旋转 + 贴"；没有则现场裁剪缩放（假数据源走这条）。
        阶段4 旋转时，这个差异是能不能省下一半 CPU 的关键。

        dpr：设备像素比。只在"烘叠加层"时用到（高分屏要按物理像素烘才不糊）。
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        cover = getattr(track, "cover", None) if track is not None else None
        # 贴图目标就是圆盘本身：源图取景已经提前烘好（见 _center_crop），
        # 所以这里是一步 1:1 贴图 —— 阶段4 之前要先放大 6% 再用圆路径裁回来，
        # 两者画面完全一致，但后者每帧多花 3 倍时间。
        target = QRectF(-rect.width() / 2, -rect.height() / 2, rect.width(), rect.height())

        if prepared is not None and not prepared.isNull():
            # 预烘好的位图：圆形遮罩已烘进 alpha，而且它和圆盘严格同尺寸
            # （1:1 像素映射，不缩放）、不需要裁剪路径
            painter.save()
            painter.translate(rect.center())
            painter.rotate(angle_deg)
            painter.drawPixmap(target, prepared, QRectF(prepared.rect()))
            painter.restore()
        elif cover is not None and not cover.isNull():
            # 原始封面：必须裁圆，否则会露出四角
            circle = QPainterPath()
            circle.addEllipse(rect)
            painter.save()
            painter.setClipPath(circle)
            # 绕圆心旋转：先把坐标原点平移到圆心，转完再画
            painter.translate(rect.center())
            painter.rotate(angle_deg)
            painter.drawPixmap(target, cover, self._center_crop(cover))
            painter.restore()
        else:
            # 没有封面：画一个薄荷->青蓝的渐变圆盘 + 音符
            circle = QPainterPath()
            circle.addEllipse(rect)
            gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gradient.setColorAt(0.0, theme.MINT)
            gradient.setColorAt(1.0, theme.CYAN)
            painter.fillPath(circle, gradient)
            icon_rect = QRectF(0, 0, rect.width() * 0.4, rect.height() * 0.4)
            icon_rect.moveCenter(rect.center())
            icons.paint_icon(painter, "note", icon_rect, QColor(255, 255, 255, 230))

        # 静态叠加层：唱片表面的弧形反光 + 边缘细白环 + 中心轴孔
        overlay = self._overlay(rect, dpr)
        if overlay is not None:
            pixmap, origin = overlay
            painter.drawPixmap(origin, pixmap)

        painter.restore()

    @staticmethod
    def _center_crop(pixmap: QPixmap) -> QRectF:
        """取源图中间 1/OVERSCALE 那块。

        阶段4 之前的取景方式是"把源图放大 6% 画进外框、再用圆形裁剪路径剪回圆盘"，
        效果等价于"只取源图中间 94% 那块"。直接按后者取源，可以一步贴到位。
        注意：这里的坐标是**位图的原始像素**（不是逻辑像素），
        因为 drawPixmap(target, pixmap, source) 的 source 走原始像素。
        """
        width = pixmap.width() / AlbumDisc.OVERSCALE
        height = pixmap.height() / AlbumDisc.OVERSCALE
        return QRectF((pixmap.width() - width) / 2.0, (pixmap.height() - height) / 2.0,
                      width, height)

    # ------------------------------------------------------------------
    # 静态叠加层（缓存）
    # ------------------------------------------------------------------
    def _overlay(self, rect: QRectF, dpr: float) -> tuple[QPixmap, QPointF] | None:
        """按当前尺寸取（必要时先烘）"反光 + 描边 + 轴心"位图，返回 (位图, 贴图位置)。

        两个容易踩的坑：
        1. 位图原点要吸附到**设备像素整数位置**：否则 Qt 会为了对齐小数位置
           对整张位图做重采样（发糊 + 变慢）；
        2. 位图只需要覆盖圆盘本身的方形，不需要圆盘外那圈（反光被裁在圆内，
           描边和轴心都在圆内）—— 覆盖多了反而浪费。
        """
        if rect.width() <= 2:
            return None
        dpr = max(1.0, float(dpr or 1.0))
        px = max(8, int(round(rect.width() * dpr)))
        side = px / dpr          # 位图实际能表示的逻辑边长（与 rect.width() 差 <0.5px）
        origin = QPointF(round(rect.left() * dpr) / dpr, round(rect.top() * dpr) / dpr)
        center = QPointF(rect.center().x() - origin.x(), rect.center().y() - origin.y())
        key = (px, round(center.x(), 3), round(center.y(), 3), round(dpr, 3))
        if key == self._overlay_key and self._overlay_cache is not None:
            return self._overlay_cache, self._overlay_origin

        pixmap = QPixmap(px, px)
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        disc = QRectF(0, 0, side, side)
        disc.moveCenter(center)
        circle = QPainterPath()
        circle.addEllipse(disc)

        # 唱片表面的弧形反光（跟着圆心，不跟着封面转 —— 那是"灯光反光"）
        sheen = QLinearGradient(disc.topLeft(), disc.bottomRight())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 46))
        sheen.setColorAt(0.45, QColor(255, 255, 255, 0))
        sheen.setColorAt(1.0, QColor(0, 0, 0, 26))
        painter.setClipPath(circle)
        painter.fillPath(circle, sheen)
        painter.setClipping(False)

        # 边缘细白环
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 78), max(0.8, side * 0.012)))
        painter.drawEllipse(disc.adjusted(0.5, 0.5, -0.5, -0.5))

        # 中心轴孔
        hole = side * 0.085
        hole_center = QPointF(disc.center().x(), disc.center().y())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(12, 14, 18, 150))
        painter.drawEllipse(hole_center, hole, hole)
        painter.setBrush(QColor(255, 255, 255, 60))
        painter.drawEllipse(hole_center, hole * 0.45, hole * 0.45)
        painter.end()

        self._overlay_cache = pixmap
        self._overlay_origin = origin
        self._overlay_key = key
        self.overlay_renders += 1
        return pixmap, origin
