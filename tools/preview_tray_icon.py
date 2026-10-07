# -*- coding: utf-8 -*-
"""把托盘图标各个尺寸渲染成一张预览图（开发用）。

托盘区在任务栏里，位置随系统设置变，用截图去验证图标很不靠谱；
直接把它画出来放大看，最省事也最准确。

用法：
    .\\.venv\\Scripts\\python.exe tools\\preview_tray_icon.py
生成 tray_icon_preview.png
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 注意：这里不用 offscreen 平台插件。实测 offscreen 下字体解析不出来，
# 预览图里的中文会全变成方框。本工具只画 QPixmap、不开窗口，不影响你桌面。

from PyQt6.QtCore import QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from musicglass.ui.tray_icon import ICON_SIZES, render_pixmap  # noqa: E402

SCALE = 4          # 放大倍数（用最近邻，才能看清 16px 下的真实像素）


def main() -> int:
    app = QApplication([])  # noqa: F841 (QPixmap 需要它存在)

    sizes = [16, 20, 24, 32]
    upscaled = [render_pixmap(size, True).scaled(
        size * SCALE, size * SCALE, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation) for size in sizes]

    row_h = max(p.width() for p in upscaled)
    pad = 18
    width = sum(p.width() + pad for p in upscaled) + pad + 260
    height = row_h + 150

    canvas = QPixmap(width, height)
    canvas.fill(QColor(28, 30, 36))          # 深色背景，接近任务栏
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    font = QFont("Microsoft YaHei UI", 11)
    painter.setFont(font)
    painter.setPen(QColor(220, 226, 235))
    painter.drawText(QRectF(0, 6, width, 22), int(Qt.AlignmentFlag.AlignCenter),
                     f"托盘图标预览   左边=放大 {SCALE} 倍（看形状）   右边=原始尺寸（看小图观感）")

    # 左：放大版
    x = pad
    for size, pixmap in zip(sizes, upscaled):
        painter.drawPixmap(x, 40, pixmap)
        painter.setPen(QColor(150, 158, 170))
        painter.drawText(QRectF(x, 40 + row_h + 2, pixmap.width(), 20),
                         int(Qt.AlignmentFlag.AlignCenter), f"{size}px ×{SCALE}")
        x += pixmap.width() + pad

    # 右：原始尺寸 + 播放/暂停两种状态
    strip_x = x + 20
    painter.setPen(QColor(150, 158, 170))
    painter.drawText(QRectF(strip_x, 40, 240, 20), int(Qt.AlignmentFlag.AlignLeft), "原始尺寸")
    cx = strip_x
    for playing in (True, False):
        for size in ICON_SIZES:
            if size > 32:
                continue
            painter.drawPixmap(cx, 66, render_pixmap(size, playing))
            cx += size + 8
        cx += 16
    painter.setPen(QColor(150, 158, 170))
    painter.drawText(QRectF(strip_x, 110, 240, 20), int(Qt.AlignmentFlag.AlignLeft),
                     "左=播放中（薄荷绿渐变）  右=暂停（去饱和）")
    painter.end()

    path = "tray_icon_preview.png"
    canvas.save(path)
    print(f"已生成 {path}  ({canvas.width()}x{canvas.height()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
