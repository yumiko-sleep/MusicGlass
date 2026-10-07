# -*- coding: utf-8 -*-
"""封面加载器测试。

QPixmap 必须有 QGuiApplication 才能创建，所以这里用 Qt 的 offscreen 平台插件
起一个"无界面"的 QApplication —— 这样在没显示器的环境下也能测。
"""

from __future__ import annotations

import io
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from musicglass.core.cover_loader import (  # noqa: E402
    CoverLoader,
    cover_key,
    decode_image,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def make_png(width: int = 64, height: int = 64) -> bytes:
    """用 Pillow 现场造一张 PNG（不往仓库里塞二进制素材）。"""
    from PIL import Image

    image = Image.new("RGB", (width, height), (61, 220, 151))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 缓存 key
# ---------------------------------------------------------------------------
def test_cover_key_stable_and_discriminating():
    assert cover_key("泡沫", "邓紫棋", "Xposed") == cover_key("泡沫", "邓紫棋", "Xposed")
    assert cover_key("泡沫", "邓紫棋") != cover_key("泡沫", "邓紫棋", "Xposed")
    assert cover_key("泡沫", "邓紫棋", "Xposed") != cover_key("泡沫", "邓紫棋", "另一个专辑")


def test_cover_key_handles_none_and_spaces():
    assert cover_key(None, None, None) == "\x00\x00"
    assert cover_key(" 泡沫 ", " 邓紫棋 ") == cover_key("泡沫", "邓紫棋")


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------
def test_decode_image_ok(qapp):
    image = decode_image(make_png(64, 64))
    assert isinstance(image, QImage)
    assert (image.width(), image.height()) == (64, 64)


def test_decode_image_limits_size(qapp):
    """超大封面要被缩到上限内，避免内存被吃光。"""
    image = decode_image(make_png(1000, 400), max_px=256)
    assert max(image.width(), image.height()) == 256


def test_decode_image_garbage_returns_none(qapp):
    # 注意：bytes 字面量不能直接写中文，要先 encode
    assert decode_image("这不是图片".encode()) is None
    assert decode_image(b"") is None
    assert decode_image(None) is None


# ---------------------------------------------------------------------------
# 缓存与圆盘位图
# ---------------------------------------------------------------------------
def test_load_caches_and_counts(qapp):
    loader = CoverLoader(capacity=2)
    data = make_png()

    first = loader.load("a", data)
    assert first is not None
    assert len(loader._cache) == 1            # noqa: SLF001 (测试就是要看内部状态)

    # 再传 None 应该命中缓存，而不是算失败
    again = loader.load("a", None)
    assert again is not None
    assert loader.hits == 1


def test_load_bad_data_counted_as_failure(qapp):
    loader = CoverLoader()
    assert loader.load("bad", "垃圾数据".encode()) is None
    assert loader.decode_failures == 1


def test_cache_evicts_oldest(qapp):
    loader = CoverLoader(capacity=2)
    data = make_png()
    loader.load("a", data)
    loader.load("b", data)
    loader.load("c", data)
    assert len(loader._cache) == 2
    assert loader.get("a") is None            # 最旧的被淘汰
    assert loader.get("c") is not None


def test_disc_pixmap_is_circular_and_sized(qapp):
    loader = CoverLoader()
    source = loader.load("a", make_png(200, 200))
    disc = loader.disc_pixmap("a", source, diameter=92, dpr=1.0)
    assert disc is not None
    # 阶段4：位图边长必须**严格等于**圆盘直径 —— UI 那边是一步 1:1 贴图
    # （多的那 6% 取景已经烘进位图里了，不再在外层放大）
    assert disc.width() == 92 and disc.height() == 92
    # 圆形遮罩：四个角必须是透明的，中心必须不透明
    image = disc.toImage()
    assert image.pixelColor(1, 1).alpha() == 0
    assert image.pixelColor(disc.width() // 2, disc.height() // 2).alpha() == 255


def test_disc_pixmap_cached(qapp):
    loader = CoverLoader()
    source = loader.load("a", make_png())
    first = loader.disc_pixmap("a", source, 92, 1.0)
    second = loader.disc_pixmap("a", source, 92, 1.0)
    assert first is second          # 同一张：说明没有重复裁剪


def test_disc_pixmap_none_safe(qapp):
    loader = CoverLoader()
    assert loader.disc_pixmap("a", None, 92, 1.0) is None
    assert loader.disc_pixmap("a", loader.load("a", make_png()), 0, 1.0) is None


def test_clear(qapp):
    loader = CoverLoader()
    loader.load("a", make_png())
    loader.clear()
    assert loader.get("a") is None
    assert loader.disc_pixmap("a", None, 92) is None
