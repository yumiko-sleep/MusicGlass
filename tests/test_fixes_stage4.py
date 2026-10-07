# -*- coding: utf-8 -*-
"""阶段4 修三个问题时新增逻辑的单测（对应问题1/2/3）。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRectF  # noqa: E402

from musicglass.core import smtc_source  # noqa: E402
from musicglass.core.models import Track  # noqa: E402
from musicglass.core.smtc_source import SMTCSnapshot, SMTCWorker  # noqa: E402
from musicglass.ui.animation import AnimationClock  # noqa: E402
from musicglass.ui.glass_window import GlassWindow  # noqa: E402


def _worker_stub() -> SMTCWorker:
    """不建线程、不跑事件循环，只要那几个纯计算方法。"""
    worker = SMTCWorker.__new__(SMTCWorker)
    worker._cover_key = ""          # 正常情况下由 __init__ 设置
    return worker


def _snap(title="泡沫", artist="邓紫棋", status="PLAYING", cover=b"jpg"):
    return SMTCSnapshot(available=True, title=title, artist=artist,
                        status_name=status, cover_data=cover)


# ---- 问题3：同一首歌、封面后到 ------------------------------------------
def test_同一首歌只换封面时算同一首():
    old = Track(title="泡沫", artist="邓紫棋", duration_s=259.0)
    same = Track(title="泡沫", artist="邓紫棋", duration_s=259.0)
    other = Track(title="光年之外", artist="邓紫棋", duration_s=235.0)
    assert GlassWindow._same_song(old, same) is True
    assert GlassWindow._same_song(old, other) is False
    assert GlassWindow._same_song(old, None) is False


def test_时长读不到时只比歌名歌手():
    old = Track(title="泡沫", artist="邓紫棋", duration_s=0.0)
    new = Track(title="泡沫", artist="邓紫棋", duration_s=259.0)
    assert GlassWindow._same_song(old, new) is True
    assert GlassWindow._same_song(old, Track(title="泡沫", artist="别人")) is False


# ---- 问题3：自适应快轮询 ------------------------------------------------
def test_换歌时进入快轮询():
    worker = _worker_stub()
    settle, hard, ident = worker._poll_pace(_snap(), 0.0, 0.0, "")
    assert settle > time.monotonic()
    assert hard > settle, "硬上限必须比预计稳定时刻更靠后"


def test_CHANGING状态也进入快轮询():
    worker = _worker_stub()
    settle, _hard, _ = worker._poll_pace(_snap(status="CHANGING"), 0.0, 0.0, "同一个标识")
    assert settle > time.monotonic()


def test_稳定状态回到普通轮询():
    worker = _worker_stub()
    ident = _snap().identity()
    worker._cover_key = smtc_source.cover_key("泡沫", "邓紫棋", "")
    settle, _hard, ident2 = worker._poll_pace(_snap(), 0.0, 0.0, ident)
    assert settle == 0.0, "稳定状态必须回到普通轮询（省 CPU）"
    assert ident2 == ident


def test_封面没到会再盯一会儿():
    worker = _worker_stub()
    ident = _snap().identity()
    now = time.monotonic()
    settle, _hard, _ = worker._poll_pace(_snap(cover=None), now + 3.0, now + 5.0, ident)
    assert now < settle <= now + 0.55, "每轮只续半秒"


def test_没有封面的歌不会一直快轮询(monkeypatch):
    """回归测试：修过的 bug —— cover_data 在后续轮询里本来就是 None。

    第一版用 cover_data is None 当判据，结果永远成立，快轮询无限延长
    （打包后实测 7.75 次/秒）。现在换成"封面 key 还没读到"+ 硬上限。
    """
    worker = _worker_stub()
    clock = {"t": 1000.0}
    monkeypatch.setattr(smtc_source.time, "monotonic", lambda: clock["t"])

    settle = hard = 0.0
    seen = ""
    fast_rounds = 0
    for _ in range(200):                  # 200 轮 x 120ms = 24 秒
        clock["t"] += 0.12
        settle, hard, seen = worker._poll_pace(_snap(cover=None), settle, hard, seen)
        if clock["t"] < settle:
            fast_rounds += 1
    assert clock["t"] >= settle, "24 秒后必须已经回到普通轮询"
    assert fast_rounds <= 60, f"快轮询轮数必须有上限，实际 {fast_rounds}"


def test_读不到会话时不动轮询节奏():
    worker = _worker_stub()
    settle, hard, ident = worker._poll_pace(SMTCSnapshot(available=False), 0.0, 0.0, "")
    assert settle == 0.0 and hard == 0.0 and ident == ""


# ---- 问题2：帧间隔探针 --------------------------------------------------
def test_帧间隔探针_没跑过时是1():
    clock = AnimationClock(30)
    assert clock.frame_starve_ratio == pytest.approx(1.0)
    assert clock.nominal_dt == pytest.approx(1 / 30, abs=0.002)
    assert clock.worst_dt == 0.0


def test_帧间隔探针_能反映被拖慢_且记录最差帧():
    clock = AnimationClock(30)
    clock._dt_ema = clock.nominal_dt * 3.0
    clock._worst_dt = 0.42
    assert clock.frame_starve_ratio == pytest.approx(3.0, abs=0.05)
    assert clock.worst_dt == pytest.approx(0.42)
    assert clock.take_worst_dt() == pytest.approx(0.42)
    assert clock.take_worst_dt() == 0.0, "取走后必须清零（否则体检会一直报警）"


# ---- 问题1：遮挡采样点 --------------------------------------------------
def test_遮挡采样点覆盖整块面板():
    panel = QRectF(56, 56, 400, 120)
    points = GlassWindow._probe_points(panel)
    assert len(points) == 15, "5 列 x 3 行"
    xs = [p.x() for p in points]
    ys = [p.y() for p in points]
    assert min(xs) > panel.left() and max(xs) < panel.right()
    assert min(ys) > panel.top() and max(ys) < panel.bottom()
    assert min(xs) < panel.left() + panel.width() * 0.15, "四角附近必须有采样点"
    assert max(ys) > panel.bottom() - panel.height() * 0.15
