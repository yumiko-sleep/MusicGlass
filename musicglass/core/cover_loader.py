# -*- coding: utf-8 -*-
"""封面加载与缓存。

职责边界（很重要）：
    * 从 QQ音乐 拿封面是 smtc_source.py 的事；
    * **把字节变成能画的 QPixmap、并且别每次都重新解码/缩放**，是这个文件的事。

为什么要单独一个文件：
    1. **QPixmap 只能在 GUI 线程创建**（QImage 则任何线程都行）。
       SMTC 的封面字节是在工作线程里下载的，所以那里的解码只能产出 QImage，
       转成 QPixmap 必须回到主线程 —— 这个边界集中在这里，别处就不会踩坑。
    2. 封面每换一首歌才变一次，但**每一帧都要画**（阶段4 还要旋转）。
       如果把"缩放成 92 像素圆盘"放在每帧绘制里做，30fps 下就是每秒 30 次
       高质量缩放，纯浪费。所以这里缓存"按目标尺寸裁好、圆形遮罩烘好"的位图，
       绘制时只做一次 drawPixmap。

缓存策略：LRU + 限制解码尺寸上限。
    一张 300x300 的封面解码后是 360KB；如果不限制，听一小时歌能吃掉几十 MB。
    所以解码后立刻缩到 MAX_SOURCE_PX（默认 256，足够 2.2 倍缩放下的圆盘用），
    并且只保留最近 CAPACITY 张。
"""

from __future__ import annotations

from collections import OrderedDict

from PyQt6.QtCore import QObject, QRectF, QSize, Qt
from PyQt6.QtGui import QImage, QPainter, QPainterPath, QPixmap

# 解码后源图的最大边长。圆盘最大直径约 2.2×92 ≈ 202px，256 有富余。
MAX_SOURCE_PX = 256
DEFAULT_CAPACITY = 16
DEFAULT_SCALED_CAPACITY = 24

# 圆盘"取景"系数：可见区域 = 源图中间的 1/1.06。
#
# 【为什么要这个数】阶段4 之前，UI 是把源图**放大 6%** 画进外框、再用圆形裁剪
# 路径剪回圆盘 —— 效果等价于"只取源图中间 94% 那块"（边缘 6% 看不到）。
# 阶段4 改成两步烘焙时，如果直接烘整张源图，封面就会比原来"多出" 6% 的视野
# （逐像素对比过：圆盘边缘会溢出一圈）—— 所以把这个取景提前烘进位图，
# 让 UI 侧变成一步 1:1 贴图（不缩放、不裁剪），观感与之前完全一致。
DISC_OVERSCALE = 1.06


def cover_key(title: str, artist: str = "", album: str = "") -> str:
    """给一首歌生成稳定的标识，用来判断"换歌了"和做缓存 key。

    用 \\x00 当分隔符，避免歌手名里带 " - " 这类分隔符导致撞车。
    """
    return f"{(title or '').strip()}\x00{(artist or '').strip()}\x00{(album or '').strip()}"


def decode_image(data: bytes | None, max_px: int = MAX_SOURCE_PX) -> QImage | None:
    """字节 -> QImage（**线程安全，可以在工作线程里调用**）。

    做三件事：解码、限制尺寸、失败返回 None（绝不抛异常）。
    """
    if not data:
        return None
    try:
        image = QImage.fromData(data)
        if image.isNull():
            return None
        # 限制内存：把超大图缩到 max_px 以内（等比）
        if max(image.width(), image.height()) > max_px:
            image = image.scaled(
                max_px, max_px,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return image
    except Exception:
        return None


class CoverLoader(QObject):
    """封面缓存 + 圆盘位图制作。所有方法都在 GUI 线程调用。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cache: OrderedDict[str, QPixmap] = OrderedDict()
        self._disc_cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._capacity = capacity
        self.hits = 0
        self.misses = 0
        self.decode_failures = 0

    # ------------------------------------------------------------------
    # 源图（整张封面）
    # ------------------------------------------------------------------
    def load(self, key: str, data: bytes | None) -> QPixmap | None:
        """按 key 取缓存；没有则用 data 解码并缓存。

        传 data=None 时表示"只查缓存"，不会产生新条目。
        """
        if not key:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            self._cache.move_to_end(key)
            return cached
        if data is None:
            self.misses += 1
            return None

        image = decode_image(data)
        if image is None:
            self.decode_failures += 1
            self.misses += 1
            return None
        pixmap = QPixmap.fromImage(image)
        self._cache[key] = pixmap
        self._cache.move_to_end(key)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        self.misses += 1
        return pixmap

    def get(self, key: str) -> QPixmap | None:
        return self._cache.get(key)

    # ------------------------------------------------------------------
    # 圆盘位图（裁好 + 圆形遮罩烘好，绘制时直接用）
    # ------------------------------------------------------------------
    def disc_pixmap(self, key: str, source: QPixmap | None, diameter: int,
                   dpr: float = 1.0) -> QPixmap | None:
        """返回一张"边长 = diameter、已按圆裁好、带 DPR"的位图。

        圆形遮罩烘进去之后，阶段4 旋转它就只需要 painter.rotate()，
        既不用每帧 setClipPath，也不会有旋转露角的问题（正圆旋转不变）。
        位图是 1:1 于 diameter 的（没有多余像素），UI 直接整张贴上去即可。
        """
        if source is None or source.isNull() or diameter <= 2:
            return None
        px = max(8, int(round(diameter * max(1.0, dpr))))
        cache_key = (key, px)
        cached = self._disc_cache.get(cache_key)
        if cached is not None:
            self._disc_cache.move_to_end(cache_key)
            return cached

        # 1. 居中裁成正方形（封面一般就是方的，这一步主要是防意外）
        side = min(source.width(), source.height())
        square = source.copy(
            (source.width() - side) // 2, (source.height() - side) // 2, side, side
        )
        # 2. 再取中间 1/DISC_OVERSCALE 那块 = 圆盘实际能看到的取景
        crop = max(8, int(round(side / DISC_OVERSCALE)))
        if crop < side:
            offset = (side - crop) // 2
            square = square.copy(offset, offset, crop, crop)
        # 3. 缩放到目标像素（边长与圆盘严格相等，UI 那边就是 1:1）
        scaled = square.scaled(
            px, px,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 4. 烘一层圆形遮罩
        result = QPixmap(scaled.size())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, scaled.width(), scaled.height()))
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, scaled)
        painter.end()
        result.setDevicePixelRatio(max(1.0, dpr))

        self._disc_cache[cache_key] = result
        self._disc_cache.move_to_end(cache_key)
        while len(self._disc_cache) > DEFAULT_SCALED_CAPACITY:
            self._disc_cache.popitem(last=False)
        return result

    # ------------------------------------------------------------------
    def clear(self) -> None:
        self._cache.clear()
        self._disc_cache.clear()

    def stats(self) -> str:
        return (f"封面缓存 {len(self._cache)} 张 / 圆盘位图 {len(self._disc_cache)} 张"
                f"（命中 {self.hits} 次，实际解码 {self.misses} 次，失败 {self.decode_failures} 次）")

    @property
    def cache_size(self) -> int:
        """当前缓存了几张封面（挂机测试看它稳不稳定）。"""
        return len(self._cache)

    @property
    def disc_cache_size(self) -> int:
        return len(self._disc_cache)

    @staticmethod
    def size_of(pixmap: QPixmap | None) -> QSize:
        return QSize(0, 0) if pixmap is None else pixmap.size()
