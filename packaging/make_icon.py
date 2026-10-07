# -*- coding: utf-8 -*-
"""生成 exe / 托盘的图标文件：packaging/musicglass.ico。

为什么要单独生成而不是画一个图片丢进仓库：
    托盘图标本来就是**纯代码画的**（musicglass/ui/tray_icon.py），
    再手绘一个 .ico 放进来，两边一定会慢慢长得不一样。
    这里直接调用同一份绘制代码，各尺寸单独渲染，保证图标与托盘一致、且每个尺寸都清晰。

为什么要多个尺寸：
    Windows 会在不同场合用不同尺寸（任务栏 16/32、资源管理器 48、Alt+Tab 64、
    大图标 128/256）。只放一张 256 再让系统缩，小尺寸会发糊。

用法：
    .\\.venv\\Scripts\\python.exe packaging\\make_icon.py
生成：packaging/musicglass.ico（多尺寸 PNG-in-ICO，Vista 以上都支持）
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from musicglass.ui.tray_icon import render_pixmap  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = Path(__file__).resolve().parent / "musicglass.ico"


def render_pngs() -> list[tuple[int, bytes]]:
    """把托盘图标在每个尺寸上**单独渲染**成 PNG 字节。

    注意用 render_pixmap 而不是 QIcon.pixmap()：QIcon 里只有 16~64 那几档，
    问它要大尺寸只会把 64 放大（发糊）。图标是矢量画的，多大都能直接画。
    """
    app = QApplication([])                     # noqa: F841  (Qt 需要它存在)
    result: list[tuple[int, bytes]] = []
    for size in SIZES:
        pixmap = render_pixmap(size, playing=True)   # 播放态：薄荷绿 -> 青蓝 + 白色音符
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        if not pixmap.save(buffer, "PNG"):
            raise SystemExit(f"渲染 {size}x{size} 失败")
        result.append((size, bytes(buffer.data())))
        buffer.close()
    return result


def write_ico(entries: list[tuple[int, bytes]], path: Path) -> None:
    """手写 ICO 容器：头 + 每个尺寸一条 16 字节目录项 + PNG 数据。

    Vista 之后 ICO 里可以直接放 PNG（不必是老的 BMP+掩码），
    所以这里只需要拼一个很简单的容器。
    """
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    payload = b""
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,     # 宽（256 要写 0）
            0 if size >= 256 else size,     # 高
            0,                              # 调色板颜色数（真彩色写 0）
            0,                              # 保留
            1,                              # 色彩平面
            32,                             # 位深
            len(data),
            offset,
        )
        payload += data
        offset += len(data)
    path.write_bytes(header + directory + payload)


def main() -> int:
    entries = render_pngs()
    write_ico(entries, OUT)
    print(f"已生成 {OUT}")
    for size, data in entries:
        print(f"  {size:3d}x{size:<3d} {len(data):6d} 字节")
    print(f"合计 {OUT.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
