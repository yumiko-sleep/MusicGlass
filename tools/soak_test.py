# -*- coding: utf-8 -*-
"""音璃 MusicGlass —— 挂机稳定性测试（开发用）

回答四个问题：
  1. 挂几个小时后，**内存**会不会一直涨？（工作集 / 私有提交）
  2. **句柄 / GDI 对象 / USER 对象**会不会泄漏？（Qt 重绘泄漏的典型症状就在这两项）
  3. **线程数**会不会涨？（工作线程没被回收的表征）
  4. 内部计数器正常吗？（快照次数、封面缓存、绘制异常、线程重启次数）

它会把主程序当子进程启动，按 --stats-file 让程序自己把内部计数器写盘，
同时从外部用 Win32 采内存/句柄/CPU（外部指标骗不了人）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\soak_test.py                     # 默认 30 分钟
    .\\.venv\\Scripts\\python.exe tools\\soak_test.py --minutes 3 --interval 5
    .\\.venv\\Scripts\\python.exe tools\\soak_test.py --source mock        # 不依赖 QQ音乐
    .\\.venv\\Scripts\\python.exe tools\\soak_test.py --csv soak.csv --minutes 60

判定标准（可改 THRESHOLDS）：
    内存：热身之后涨幅 > 15MB 或 > 15%  → 标记「疑似泄漏」
    句柄：涨幅 > 30      GDI/USER：涨幅 > 30      线程：涨幅 > 3
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

# 输出里带了 ✅/⚠️ 之类字符，而 Windows 重定向到文件时 Python 默认用 GBK（而不是 UTF-8），
# 会直接抛 UnicodeEncodeError 把整个报告打断。这里强制 UTF-8 + 遇到不认识的字也不报错。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Win32 采样（坑：HANDLE 相关的 argtypes/restype 必须显式声明，
#              否则 64 位下返回值会被截断，指标全变 0）
# ---------------------------------------------------------------------------
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPTHREAD = 0x00000004


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetProcessHandleCount.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME),
                                     ctypes.POINTER(wintypes.FILETIME)]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32Next.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                       wintypes.DWORD]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
user32.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
user32.GetGuiResources.restype = wintypes.DWORD


def _filetime_to_seconds(value: wintypes.FILETIME) -> float:
    return ((value.dwHighDateTime << 32) | value.dwLowDateTime) / 1e7


def sample(pid: int) -> dict:
    """采一次样。任何一项采不到就返回 0/None，绝不抛异常打断测试。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return {}
    result: dict = {}
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            result["ws_mb"] = counters.WorkingSetSize / 1048576
            result["private_mb"] = counters.PagefileUsage / 1048576

        count = wintypes.DWORD()
        if kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            result["handles"] = count.value

        result["gdi"] = user32.GetGuiResources(handle, 0)
        result["user"] = user32.GetGuiResources(handle, 1)

        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            result["cpu_s"] = _filetime_to_seconds(kernel) + _filetime_to_seconds(user)
    finally:
        kernel32.CloseHandle(handle)

    # 线程数（Toolhelp 快照）
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot not in (None, 0xFFFFFFFFFFFFFFFF):
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        threads = 0
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                threads += 1
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
        result["threads"] = threads
    return result


def read_stats(path: Path) -> dict:
    """读程序自己写的最新一条内部统计。"""
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return {}


def growth(values: list[float]) -> float:
    """用\"后半段中位数 - 前半段中位数\"衡量涨幅，比首尾相减抗抖动。"""
    if len(values) < 4:
        return 0.0
    half = len(values) // 2
    return statistics.median(values[half:]) - statistics.median(values[:half])


THRESHOLDS = {
    "ws_mb": ("内存(工作集)", 15.0),        # MB
    "private_mb": ("内存(私有)", 15.0),     # MB
    "handles": ("句柄", 30.0),
    "gdi": ("GDI 对象", 30.0),
    "user": ("USER 对象", 30.0),
    "threads": ("线程", 3.0),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="音璃挂机稳定性测试")
    parser.add_argument("--minutes", type=float, default=30.0, help="挂多久（分钟）")
    parser.add_argument("--interval", type=float, default=10.0, help="采样间隔（秒）")
    parser.add_argument("--warmup", type=float, default=60.0, help="热身多少秒后开始统计，默认 60")
    parser.add_argument("--source", choices=("smtc", "mock"), default="smtc")
    parser.add_argument("--csv", metavar="PATH", help="把采样结果写成 csv")
    parser.add_argument("--extra", default="", help="额外传给主程序的参数（原样拼接）")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    python = sys.executable
    stats_path = Path(tempfile.gettempdir()) / f"musicglass_soak_{os.getpid()}.jsonl"
    stats_path.write_text("", encoding="utf-8")

    command = [python, str(root / "main.pyw"),
               "--silent", "--source", args.source,
               "--stats-file", str(stats_path),
               "--stats-interval", str(max(2.0, args.interval))]
    if args.extra:
        command.extend(args.extra.split())

    print("=" * 100)
    print(f"音璃挂机测试：{args.minutes:.1f} 分钟，每 {args.interval:.0f} 秒采一次"
          f"（数据源={args.source}）")
    print("提示：测试期间别关 QQ音乐；想验证重连，可以中途关掉 QQ音乐再打开")
    print("=" * 100)
    print(f"{'时间':>7} {'工作集':>8} {'私有':>8} {'句柄':>6} {'GDI':>5} {'USER':>5} "
          f"{'线程':>5} {'CPU%':>6}  内部状态")
    print("-" * 100)

    process = subprocess.Popen(command, cwd=str(root),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 【重要】Windows 上 venv 的 python.exe 只是个"转发器"：
    # 它自己几 MB 内存、几乎不吃 CPU，真正跑应用的是它启动的子进程（基解释器）。
    # 所以不能直接量 process.pid —— 必须等程序把真实 PID 写进统计文件。
    real_pid = 0
    for _ in range(60):
        time.sleep(0.5)
        if process.poll() is not None:
            print(f"[!] 主程序启动就退出了（退出码 {process.returncode}）")
            return 1
        internal = read_stats(stats_path)
        if internal.get("pid"):
            real_pid = int(internal["pid"])
            break
    print(f"启动器 PID={process.pid}   真正跑应用的 PID={real_pid or process.pid}")
    target_pid = real_pid or process.pid

    samples: list[dict] = []
    csv_rows: list[str] = ["elapsed,ws_mb,private_mb,handles,gdi,user,threads,cpu_pct,"
                           "snapshots,cover_cache,paint_errors,state"]
    started = time.monotonic()
    deadline = started + args.minutes * 60.0

    try:
        while time.monotonic() < deadline:
            time.sleep(args.interval)
            if process.poll() is not None:
                print(f"\n[!] 主程序提前退出了（退出码 {process.returncode}），测试中止")
                return 1
            data = sample(target_pid)
            if not data:
                # 真实进程没了（被杀了 / 崩了）
                print("\n[!] 目标进程已消失，测试中止")
                return 1
            elapsed = time.monotonic() - started
            internal = read_stats(stats_path)
            data["elapsed"] = elapsed
            data["snapshots"] = internal.get("snapshots", 0)
            data["cover_cache"] = internal.get("cover_cache", 0)
            data["paint_errors"] = internal.get("paint_errors", 0)
            data["state"] = internal.get("state", "?")
            samples.append(data)

            cpu_pct = 0.0
            if len(samples) > 1 and "cpu_s" in data and "cpu_s" in samples[0]:
                cpu_pct = ((data["cpu_s"] - samples[0]["cpu_s"]) / elapsed) * 100
            row = (f"{int(elapsed // 60):>3}分{int(elapsed % 60):>2}秒 "
                   f"{data.get('ws_mb', 0):>8.1f} {data.get('private_mb', 0):>8.1f} "
                   f"{data.get('handles', 0):>6} {data.get('gdi', 0):>5} {data.get('user', 0):>5} "
                   f"{data.get('threads', 0):>5} {cpu_pct:>6.2f}  "
                   f"快照={data['snapshots']} 封面={data['cover_cache']} "
                   f"绘制异常={data['paint_errors']} {data['state']}")
            print(row)
            csv_rows.append(
                f"{elapsed:.0f},{data.get('ws_mb', 0):.2f},{data.get('private_mb', 0):.2f},"
                f"{data.get('handles', 0)},{data.get('gdi', 0)},{data.get('user', 0)},"
                f"{data.get('threads', 0)},{cpu_pct:.2f},{data['snapshots']},"
                f"{data['cover_cache']},{data['paint_errors']},{data['state']}")
    except KeyboardInterrupt:
        print("\n[用户中断] 开始出报告")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    # ---------------- 报告 ----------------
    print("=" * 100)
    after_warmup = [s for s in samples if s["elapsed"] >= args.warmup]
    if len(after_warmup) < 4:
        print("采样点太少（把 --minutes 调大，或 --warmup 调小），无法判定")
        return 0

    span = after_warmup[-1]["elapsed"] - after_warmup[0]["elapsed"]
    print(f"统计区间：测试开始后第 {after_warmup[0]['elapsed']:.0f}s 到 "
          f"{after_warmup[-1]['elapsed']:.0f}s（共 {span / 60:.1f} 分钟）")
    print(f"内部计数器：快照 {after_warmup[-1]['snapshots']} 次"
          f"（约 {after_warmup[-1]['snapshots'] / max(1.0, after_warmup[-1]['elapsed']) * 60:.0f} 次/分钟）"
          f"  封面缓存 {after_warmup[-1]['cover_cache']} 张"
          f"  绘制异常 {after_warmup[-1]['paint_errors']} 次\n")

    problems = []
    for key, (label, limit) in THRESHOLDS.items():
        values = [s[key] for s in after_warmup if key in s]
        if not values:
            continue
        delta = growth(values)
        first, last = values[0], values[-1]
        # 只有"涨"才算泄漏。下降是好事（启动期的线程/句柄会陆续回收），
        # 早期版本这里用了 abs()，结果把线程数从 26 降到 17 也判成了异常。
        leaked = delta > limit
        flag = "⚠️ " if leaked else "✅"
        if leaked:
            problems.append(f"{label} 持续增长 {delta:+.1f}（阈值 {limit}）")
        print(f"  {flag} {label:<12} 起 {first:>8.1f} → 止 {last:>8.1f}   "
              f"后半段-前半段 = {delta:+.1f}")

    cpu_values = [s["cpu_s"] for s in after_warmup if "cpu_s" in s]
    if len(cpu_values) > 1:
        avg = (cpu_values[-1] - cpu_values[0]) / span * 100
        cores = os.cpu_count() or 1
        print(f"  ✅ 平均 CPU        {avg:.2f}% 单核 / {avg / cores:.2f}% 整机（整段平均，含刷新）")

    print("-" * 100)
    if problems:
        print("判定：⚠️ 有指标在增长，建议复查：")
        for item in problems:
            print("     - " + item)
    else:
        print("判定：✅ 所有指标稳定，没有发现泄漏迹象")

    if args.csv:
        Path(args.csv).write_text("\n".join(csv_rows) + "\n", encoding="utf-8")
        print(f"采样明细已写入 {args.csv}")
    try:
        stats_path.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
