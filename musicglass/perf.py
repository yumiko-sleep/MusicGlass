# -*- coding: utf-8 -*-
"""轻量性能计数器（只在 --perf 打开时启用）。

为什么需要它：
    「--on-top 模式下 CPU 43%」这种问题靠猜是猜不出来的。
    折射 / 抓屏 / 整块重绘 / 局部重绘 / 插值 tick —— 得分别计时才知道该优化谁。
    第一次量完就发现：真正的大头是放大镜抓屏，不是我们的绘制。

开销：每条记录就是几次 perf_counter()（纳秒级），关闭时只有一次 if 判断。
    所以它可以长期留在代码里，而不是"临时插桩、用完删掉"。

用法：
    在 main.pyw 里 --perf 5 表示每 5 秒打印一行汇总。
    代码里用 `perf.monitor.record("名字", 毫秒)` 或 `with perf.monitor.timer("名字"):`
    计时器没启动时 record/timer 都是空操作。
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from PyQt6.QtCore import QObject, QTimer


class PerfMonitor(QObject):
    """按名字累计「次数 + 总耗时」，周期性打印并清零。"""

    def __init__(self, interval_s: float = 5.0, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled = False
        self._interval = max(0.5, float(interval_s))
        self._stats: dict[str, list[float]] = {}      # 名字 -> [次数, 总毫秒, 最大毫秒]
        self._timer = QTimer(self)
        self._timer.setInterval(int(self._interval * 1000))
        self._timer.timeout.connect(self._report)

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self) -> None:
        self._enabled = True
        self._timer.start()

    def configure(self, interval_s: float) -> None:
        """改汇总间隔（main 里根据 --perf 参数调用）。"""
        self._interval = max(0.5, float(interval_s))
        self._timer.setInterval(int(self._interval * 1000))

    def stop(self) -> None:
        self._enabled = False
        self._timer.stop()

    # ------------------------------------------------------------------
    def record(self, name: str, milliseconds: float) -> None:
        if not self._enabled:
            return
        slot = self._stats.get(name)
        if slot is None:
            self._stats[name] = [1.0, float(milliseconds), float(milliseconds)]
        else:
            slot[0] += 1.0
            slot[1] += float(milliseconds)
            if milliseconds > slot[2]:
                slot[2] = float(milliseconds)

    @contextmanager
    def timer(self, name: str):
        """with 用法：自动计时并在退出时记录。"""
        if not self._enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - started) * 1000.0)

    # ------------------------------------------------------------------
    def _report(self) -> None:
        if not self._stats:
            print(f"[性能] 最近 {self._interval:.0f} 秒：没有任何记录（可能窗口被盖住、一切都在睡觉）")
            return
        parts = []
        for name, (count, total, worst) in sorted(self._stats.items(),
                                                  key=lambda item: -item[1][1]):
            parts.append(f"{name}: {count:.0f}次 共{total:.1f}ms 平均{total / count:.2f}ms "
                         f"峰值{worst:.1f}ms")
        print(f"[性能] 最近 {self._interval:.0f} 秒 -> " + " | ".join(parts))
        self._stats.clear()


# 全局单例：各模块直接 import 用，不需要到处传引用。
monitor = PerfMonitor()
