# -*- coding: utf-8 -*-
"""氛围光自检：用像素差判定「氛围光到底画没画、肉眼能不能看见」，并能自动调亮。

为什么要有它
    "氛围光太淡，看不出来"这种事不该靠肉眼猜，也不该靠改参数碰运气。
    这里把组件渲染两遍（氛围光关 / 开），**合成到指定底色上**（组件本身是透明的，
    只有合成之后才是人眼看到的样子），再逐像素算差值，最后给出明确判定：

        * 差值 == 0            -> 【没画】逐层打印诊断：哪一层的 alpha 被取整成 0、
                                  有多少层完全落在窗口外面、更新区域有没有算上 glow、
                                  绘制有没有抛异常
        * 0 < 差值 < 阈值(5)    -> 【太淡，肉眼不可见】：算出要放大多少倍
        * 阈值 <= 差值 < 目标(20) -> 【偏淡，勉强能看出但不明显】
        * 差值 >= 目标          -> 【看得见】
        另外单独量 **呼吸幅度**（亮度 1.00 与波谷 0.72 的差）和 **衰减剖面**
        （光晕在窗口边缘有没有衰减到 0 —— 没衰减完的话，一调亮就会出现一道
          又直又硬的矩形光边）。

    --apply 会自动搜一个让「静置态」达标的最小 AMBIENT_ALPHA，搜完才写 theme.py。

用法（项目根目录）
    .\\.venv\\Scripts\\python.exe tools\\verify_glow.py                  # 只测 + 判定 + 给建议
    .\\.venv\\Scripts\\python.exe tools\\verify_glow.py --apply           # 太淡就自动调亮并复测
    .\\.venv\\Scripts\\python.exe tools\\verify_glow.py --bg 0,0,0 --target 24
    .\\.venv\\Scripts\\python.exe tools\\verify_glow.py --json            # 附带一行机器可读结果

参数
    --bg     桌面底色：一个数（灰度）或 R,G,B，默认 32（深色桌面，光晕最容易看清）。
    --min    判定阈值，默认 5/255（小于它就算「太淡，肉眼不可见」）。
    --target 调亮目标，默认 20/255（明显看得见，又不刺眼）。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="音璃 氛围光自检")
    parser.add_argument("--bg", default="32", help="桌面底色：一个数（灰）或 R,G,B，默认 32")
    parser.add_argument("--min", type=float, default=5.0,
                        help="判定阈值：最大通道差小于它就算「太淡，肉眼不可见」，默认 5")
    parser.add_argument("--target", type=float, default=20.0,
                        help="自动调亮的目标最大通道差，默认 20")
    parser.add_argument("--apply", action="store_true",
                        help="偏淡/太淡时自动调亮，并把新值写进 musicglass/ui/theme.py")
    parser.add_argument("--save", action="store_true", help="保存前后对比图到 build/glow_check/")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="面板缩放（默认 1.0；你平时用的可能是 1.4 之类）")
    parser.add_argument("--json", action="store_true", help="额外输出一行 JSON 结果")
    return parser.parse_args()


ARGS = parse_args()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402
from PyQt6.QtCore import QBuffer  # noqa: E402
from PyQt6.QtGui import QColor, QPainter, QPainterPath  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import musicglass.ui.glass_window as glass_window  # noqa: E402
from musicglass.config import Config  # noqa: E402
from musicglass.core.mock_source import MockSource  # noqa: E402
from musicglass.ui import theme  # noqa: E402
from musicglass.ui.glass_backend import GlassBackend  # noqa: E402
from musicglass.ui.widgets.ambient_glow import AmbientGlow  # noqa: E402

THEME_PATH = Path(__file__).resolve().parent.parent / "musicglass" / "ui" / "theme.py"
OUT_DIR = Path(__file__).resolve().parent.parent / "build" / "glow_check"

# 呼吸幅度阈值：亮度在 0.60~1.00 之间起伏，最大通道差不到这个数就说不上「在呼吸」
# （用户要求 15~25/255 才算“明显能感觉到”）
BREATH_MIN = 15.0
# 窗口边缘允许的残余亮度：超过它说明光晕还没衰减完，调亮后会看到一道硬切光边
EDGE_MAX = 4.0
# 衰减剖面采样距离（像素，从面板边缘往外）
PROFILE_STEPS = (6, 12, 18, 24, 30, 36, 42, 48, 52)


def parse_bg(text: str) -> tuple[int, int, int]:
    parts = [int(value) for value in re.split(r"[,\s]+", text.strip()) if value]
    if len(parts) == 1:
        parts *= 3
    if len(parts) != 3:
        raise SystemExit(f"--bg 只接受「灰度」或「R,G,B」，给的是 {text!r}")
    return tuple(max(0, min(255, value)) for value in parts)  # type: ignore[return-value]


BG = parse_bg(ARGS.bg)
ISSUES: list[str] = []


# ---------------------------------------------------------------------------
# 只读诊断用的小包装
# ---------------------------------------------------------------------------
class NullBackend(GlassBackend):
    """把玻璃换成一块纯色面板：这样组件外圈里**只剩氛围光**，像素差就是氛围光本身。"""

    def paint(self, painter: QPainter, panel, radius, alpha, interior: bool = False) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(panel, radius, radius)
        painter.setOpacity(alpha)
        painter.fillPath(path, QColor(20, 24, 32))
        painter.restore()


def build_window(app: QApplication):
    config = Config()
    config.scale = ARGS.scale
    config.animations = False          # 手动设 glow_strength，不要让呼吸自己动
    config.opacity_idle = 1.0
    config.opacity_hover = 1.0
    config.always_on_top = False
    config.desktop_layer = False       # 不走贴桌面，免得被「组件被盖住」的逻辑挂起
    config.tray_enabled = False
    glass_window.create_backend = lambda widget, cfg: NullBackend(widget)
    source = MockSource()
    window = glass_window.GlassWindow(config, source)
    window.set_persist_enabled(False)
    window.show()
    source.start()
    for _ in range(20):
        app.processEvents()
    window._fade.stop()
    return window


def snapshot(window, app, glow_strength: float, alpha: float) -> tuple[np.ndarray, Image.Image]:
    """渲染一帧：返回 (合成到底色后的 RGB 数组, 带 alpha 的原始图)。"""
    window._fade.stop()
    window._glow_strength = float(glow_strength)
    window._alpha = float(alpha)
    window.repaint()
    for _ in range(4):
        app.processEvents()

    pixmap = window.grab()
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    raw = Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")
    buffer.close()

    composed = Image.alpha_composite(Image.new("RGBA", raw.size, (*BG, 255)), raw).convert("RGB")
    return np.asarray(composed).astype(np.int16), raw


def band_mask(window) -> np.ndarray:
    """外圈掩码：窗口里、面板之外的那一圈（氛围光只可能出现在这里）。"""
    mask = np.ones((window.height(), window.width()), dtype=bool)
    panel = window.panel_rect().toAlignedRect()
    mask[panel.top():panel.bottom(), panel.left():panel.right()] = False
    return mask


def diff_metrics(on_rgb: np.ndarray, off_rgb: np.ndarray, mask: np.ndarray) -> dict:
    delta = np.abs(on_rgb - off_rgb)
    peak_map = delta.max(axis=2)
    peak = peak_map[mask]
    channels = [delta[:, :, index][mask] for index in range(3)]
    # 最亮那个像素在“开”的图里到底是什么颜色 —— 用它判断“柔和 vs 刺眼/霓虹”
    masked = np.where(mask, peak_map, -1)
    index = np.unravel_index(int(masked.argmax()), masked.shape)
    return {
        "max": float(peak.max()) if peak.size else 0.0,
        "p99": float(np.percentile(peak, 99)) if peak.size else 0.0,
        "mean": float(peak.mean()) if peak.size else 0.0,
        "pixels_gt1": int((peak > 1).sum()),
        "pixels": int(peak.size),
        "max_rgb": tuple(float(channel.max()) for channel in channels),
        "halo_rgb": tuple(int(value) for value in on_rgb[index]),
    }


def falloff_profile(on_rgb: np.ndarray, off_rgb: np.ndarray,
                    window, steps=PROFILE_STEPS) -> list[tuple[int, float]]:
    """从面板上边缘往外量一条衰减剖面：每个距离上的平均最大通道差。"""
    panel = window.panel_rect().toAlignedRect()
    delta = np.abs(on_rgb - off_rgb).max(axis=2)
    columns = slice(panel.left() + 40, panel.right() - 40)
    rows: list[tuple[int, float]] = []
    for distance in steps:
        top = panel.top() - distance
        if top < 2:
            break
        rows.append((distance, float(delta[top:top + 3, columns].mean())))
    return rows


def crop_band(image: Image.Image, window) -> Image.Image:
    """把外圈裁出来（上边 + 左右各留 40px），方便肉眼对比。"""
    panel = window.panel_rect().toAlignedRect()
    return image.crop((max(0, panel.left() - 40), 0,
                       min(image.width, panel.right() + 40), max(1, panel.top() + 6)))


def layer_summary(window) -> str:
    """算一下「多少层完全落在窗口外面」（几何合不合理，看数字就知道）。

    用 AmbientGlow.layer_plan：绘制和自检必须共用同一份计算，否则两边会说得不一样。
    """
    info = window.layout_info()
    step, plan = AmbientGlow.layer_plan(info.radius)
    outside = sum(1 for spread, _ in plan if spread - step * 0.9 >= theme.MARGIN)
    nonzero_outside = sum(1 for spread, alpha in plan
                          if alpha > 0 and spread - step * 0.9 >= theme.MARGIN)
    return (f"光晕几何：{theme.AMBIENT_LAYERS} 层，外扩 {step:.1f}~"
            f"{step * theme.AMBIENT_LAYERS:.1f}px，窗口只有 {theme.MARGIN}px 余量"
            f" -> {outside} 层完全在窗口外（其中 {nonzero_outside} 层本来是能看见的，被白裁）")


def diagnose(window) -> None:
    """判定「没画/太淡」时逐条排查原因。"""
    print("\n  —— 逐条诊断 ——")
    info = window.layout_info()
    region = window._glow_band(info)
    parts = window._dirty_parts(region, info)
    print(f"  1) 外圈重绘区域算出来的部件：{sorted(parts)}"
          f"  -> {'OK' if 'glow' in parts else '异常：外圈没被识别成氛围光，重绘时根本不会调它'}")
    print(f"  2) 当前状态：_glow_strength={window._glow_strength}  _alpha={window._alpha}"
          f"  _occluded={window._occluded}  绘制异常={window._paint_errors} 次")
    if window._paint_errors:
        print("     -> 绘制抛过异常，先看控制台里的栈")
    if window._glow_strength * window._alpha <= 0.01:
        print("     -> 强度太小：AmbientGlow.paint 会直接 return（strength <= 0.01）")

    print(f"  3) 逐层检查（LAYERS={theme.AMBIENT_LAYERS}  ALPHA={theme.AMBIENT_ALPHA}"
          f"  SPREAD={theme.AMBIENT_SPREAD:.2f}）")
    step, plan = AmbientGlow.layer_plan(info.radius)
    print(f"     层间距 {step:.2f}px  笔画宽 {step * 1.8:.1f}px  窗口余量 {theme.MARGIN}px")
    visible = nonzero = 0
    for spread, alpha in plan:
        inner = spread - step * 0.9
        inside = inner < theme.MARGIN
        nonzero += 1 if alpha > 0 else 0
        visible += 1 if (alpha > 0 and inside) else 0
        print(f"     外扩 {spread:6.1f}px  笔画区 [{inner:6.1f},{spread + step * 0.9:6.1f}]"
              f"  alpha={alpha:3d}  {'窗口内' if inside else '整层在窗口外(被裁掉)'}")
    print(f"  4) 小结：alpha>0 的层 {nonzero} 个，其中真正落在窗口里的只有 {visible} 个")
    if visible == 0:
        print("     -> 全部层都落在外圈之外：AMBIENT_SPREAD 太大，光晕被推到窗口外面去了")
    elif nonzero == 0:
        print("     -> 所有层的 alpha 都被取整成 0：AMBIENT_ALPHA 太小")
    else:
        print("     -> 层数和 alpha 都正常，那问题在绘制/上色环节（看第 1、2 条）")


# ---------------------------------------------------------------------------
def report(label: str, metrics: dict) -> None:
    print(f"  {label}：最大通道差 {metrics['max']:5.1f}/255  p99 {metrics['p99']:5.1f}"
          f"  均值 {metrics['mean']:4.2f}  变化像素 {metrics['pixels_gt1']}/{metrics['pixels']}"
          f"  RGB峰值 {tuple(round(v) for v in metrics['max_rgb'])}")
    print(f"      最亮处颜色 RGB{metrics['halo_rgb']}（底色 RGB{BG}）"
          f" -> 单通道最大改变 {metrics['max']:.0f}/255")


def judge(metrics_idle: dict, metrics_breath: dict, edge: float) -> tuple[str, list[str]]:
    """给出结论标签 + 待处理清单（清单里的是硬问题，决定退出码）。"""
    idle = metrics_idle["max"]
    problems: list[str] = []
    if idle <= 0.0:
        return "没画", ["没画"]
    if idle < ARGS.min:
        label = "太淡"
        problems.append("太淡")
    elif idle < ARGS.target:
        label = "偏淡"
        problems.append("偏淡")          # 能看出一点，但没到“看得清”
    else:
        label = "看得见"
    if metrics_breath["max"] < BREATH_MIN:
        problems.append("呼吸看不出")
    if edge > EDGE_MAX:
        problems.append("光晕被窗口切硬边")
    return label, problems


def apply_alpha(value: int) -> bool:
    """把新的 AMBIENT_ALPHA 写进 theme.py（只改那一行）。"""
    text = THEME_PATH.read_text(encoding="utf-8")
    new_text, count = re.subn(r"(?m)^AMBIENT_ALPHA = \d+", f"AMBIENT_ALPHA = {value}", text)
    if count != 1:
        print(f"  [X] theme.py 里没找到唯一的 AMBIENT_ALPHA 行（找到 {count} 处），没有改动")
        return False
    THEME_PATH.write_text(new_text, encoding="utf-8")
    theme.AMBIENT_ALPHA = value
    print(f"  已写入 theme.py：AMBIENT_ALPHA = {value}")
    return True


# ---------------------------------------------------------------------------
def main() -> int:
    app = QApplication(sys.argv)
    window = build_window(app)
    mask = band_mask(window)
    idle_alpha = Config().opacity_idle

    def measure() -> dict:
        """量一整套：悬停态开关、静置态开关、呼吸幅度，外加两张原始图。"""
        off_full, off_img = snapshot(window, app, 0.0, 1.0)
        on_full, on_img = snapshot(window, app, 1.0, 1.0)
        off_idle, _ = snapshot(window, app, 0.0, idle_alpha)
        on_idle, _ = snapshot(window, app, 1.0, idle_alpha)
        low_idle, _ = snapshot(window, app,
                               theme.GLOW_BREATH_BASE - theme.GLOW_BREATH_AMP, idle_alpha)
        rgba_on = Image.alpha_composite(Image.new("RGBA", on_img.size, (*BG, 255)), on_img)
        rgba_off = Image.alpha_composite(Image.new("RGBA", off_img.size, (*BG, 255)), off_img)
        on_rgb = np.asarray(rgba_on.convert("RGB")).astype(np.int16)
        off_rgb = np.asarray(rgba_off.convert("RGB")).astype(np.int16)
        return {
            "full": diff_metrics(on_full, off_full, mask),
            "idle": diff_metrics(on_idle, off_idle, mask),
            "breath": diff_metrics(on_idle, low_idle, mask),
            "profile": falloff_profile(on_rgb, off_rgb, window),
            "rgba_on": rgba_on,
            "rgba_off": rgba_off,
        }

    print("=" * 92)
    print(f"音璃 氛围光自检   底色 RGB{BG}   阈值 {ARGS.min:.0f}/255   调亮目标 {ARGS.target:.0f}/255")
    print("=" * 92)
    print(f"窗口 {window.width()}x{window.height()}"
          f"  面板 {window.panel_rect().toAlignedRect().width()}x"
          f"{window.panel_rect().toAlignedRect().height()}  MARGIN={theme.MARGIN}")
    print("  " + layer_summary(window))

    state = measure()
    edge_now = state["profile"][-1][1] if state["profile"] else 0.0

    print(f"\n[1] 悬停态（整体不透明）：氛围光 开 vs 关")
    report("悬停态可见度", state["full"])
    print(f"\n[2] 静置态（整体 {idle_alpha:.2f} 不透明）—— 用户平时看到的就是这个")
    report("静置态可见度", state["idle"])
    print(f"\n[3] 呼吸幅度：亮度 1.00 与波谷 0.72 差多少（太小就说不上「呼吸」）")
    report("呼吸幅度", state["breath"])

    print("\n[4] 判定（以静置态为准）")
    label, problems = judge(state["idle"], state["breath"], edge_now)
    tag = {"没画": "[FAIL]", "太淡": "[FAIL]", "偏淡": "[WARN]", "看得见": "[OK]"}[label]
    print(f"  {tag} 结论：{label}"
          f"（静置时最大通道差 {state['idle']['max']:.1f}/255，"
          f"目标 {ARGS.target:.0f}，肉眼阈值 {ARGS.min:.0f}）")
    for name in problems:
        extra = {"呼吸看不出": f"呼吸幅度 {state['breath']['max']:.1f}/255 < {BREATH_MIN:.0f}",
                 "光晕被窗口切硬边": f"窗口边缘残余亮度 {edge_now:.1f}/255 > {EDGE_MAX:.0f}",
                 "没画": "开/关两张图一个像素都没差",
                 "太淡": f"小于肉眼阈值 {ARGS.min:.0f}，基本看不出来",
                 "偏淡": f"在 {ARGS.min:.0f}~{ARGS.target:.0f} 之间：能看出一点，"
                         f"但没到「看得清」，建议调亮"}.get(name, "")
        print(f"  [{'WARN' if name == '偏淡' else 'FAIL'}] {name}：{extra}")
    if label in ("没画", "太淡"):
        diagnose(window)

    # ---- 5. 自动调亮 ----
    print("\n[5] 自动调亮")
    need = ARGS.target / max(state["idle"]["max"], 1e-6)
    if label == "没画":
        print("  没画的时候调参没有意义，先按上面的诊断修绘制代码")
    elif label == "看得见" and "呼吸看不出" not in problems:
        print(f"  不用调：静置 {state['idle']['max']:.1f}/255、"
              f"呼吸 {state['breath']['max']:.1f}/255 都已达标")
    else:
        print(f"  需要把亮度提大约 {need:.1f} 倍：AMBIENT_ALPHA "
              f"{theme.AMBIENT_ALPHA} -> {max(1, round(theme.AMBIENT_ALPHA * need))}")
        if not ARGS.apply:
            print("  （加 --apply 就会真的调亮 + 写进 theme.py + 复测）")
        else:
            # 【先在内存里搜，最后才写文件】否则一次一次试写，万一半途失败，
            # theme.py 里会留下一个没验证过的参数。
            # 【另外】氛围光是烘成位图的：改完参数必须让缓存失效，否则量到的还是旧图
            # （这个坑真的踩过：改了 theme 却一点变化都没有）。
            start_alpha = value = theme.AMBIENT_ALPHA
            best: int | None = None
            for _ in range(6):
                value = max(1, min(400,
                                   round(value * (ARGS.target / max(state["idle"]["max"], 1e-6)))))
                theme.AMBIENT_ALPHA = value
                window._glow.invalidate()
                state = measure()
                edge_now = state["profile"][-1][1] if state["profile"] else 0.0
                print(f"  试 AMBIENT_ALPHA={value:3d} -> 静置 {state['idle']['max']:5.1f}/255"
                      f"  悬停 {state['full']['max']:5.1f}/255"
                      f"  呼吸 {state['breath']['max']:.1f}/255"
                      f"  边缘残亮 {edge_now:.1f}/255")
                if state["idle"]["max"] >= ARGS.target and state["breath"]["max"] >= BREATH_MIN:
                    best = value
                    break
                if state["idle"]["max"] >= ARGS.target and best is None:
                    best = value          # 亮度够了、呼吸还差一点：记下继续找更大的
            if best is None:
                theme.AMBIENT_ALPHA = start_alpha
                window._glow.invalidate()
                print("  [X] 搜了几轮都不达标：这不是一个系数能解决的问题，"
                      "看上面的逐层诊断 / 衰减剖面")
            else:
                theme.AMBIENT_ALPHA = best
                window._glow.invalidate()
                if apply_alpha(best):
                    state = measure()
                    edge_now = state["profile"][-1][1] if state["profile"] else 0.0
                    print(f"  [OK] 已调亮：AMBIENT_ALPHA = {best}"
                          f"（静置 {state['idle']['max']:.1f}/255，"
                          f"悬停 {state['full']['max']:.1f}/255）")

    # ---- 6. 衰减剖面 ----
    print("\n[6] 衰减剖面（面板上边缘往外采样；看它是不是在窗口内衰减到 0）")
    print("  " + "  ".join(f"{distance}px:{value:4.1f}"
                           for distance, value in state["profile"]))
    print(f"  窗口边缘（{theme.MARGIN}px 处）残余亮度 {edge_now:.1f}/255 -> "
          + ("[FAIL] 还没衰减完，调亮后会看到一道硬切口（矩形光边）"
             if edge_now > EDGE_MAX else "[PASS] 已衰减到看不见，不会有硬切边"))

    # ---- 7. 最终结论 ----
    label, problems = judge(state["idle"], state["breath"], edge_now)
    ISSUES[:] = problems
    print("\n[7] 最终数据")
    report("悬停态 开/关", state["full"])
    report("静置态 开/关", state["idle"])
    report("呼吸幅度", state["breath"])

    if ARGS.save or ARGS.apply:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        top = crop_band(state["rgba_off"].convert("RGB"), window)
        bottom = crop_band(state["rgba_on"].convert("RGB"), window)
        canvas = Image.new("RGB", (top.width, top.height * 2 + 6), (90, 90, 90))
        canvas.paste(top, (0, 0))
        canvas.paste(bottom, (0, top.height + 6))
        canvas = canvas.resize((canvas.width * 2, canvas.height * 2), Image.NEAREST)
        out = OUT_DIR / "glow_before_after.png"
        canvas.save(out)
        print(f"\n  对比图（上=氛围光关，下=氛围光开，2 倍放大）：{out}")

    print("\n" + "=" * 92)
    if ISSUES:
        print(f"结果：{len(ISSUES)} 项需要处理 -> {ISSUES}")
    else:
        print(f"结果：全部通过（结论：{label}，AMBIENT_ALPHA={theme.AMBIENT_ALPHA}）")
    print("=" * 92)
    if ARGS.json:
        print("JSON " + str({
            "verdict": label, "issues": ISSUES,
            "idle_max": round(state["idle"]["max"], 2),
            "hover_max": round(state["full"]["max"], 2),
            "breath_max": round(state["breath"]["max"], 2),
            "edge_residual": round(edge_now, 2),
            "ambient_alpha": theme.AMBIENT_ALPHA,
            "ambient_spread": round(theme.AMBIENT_SPREAD, 2),
        }))
    return 1 if ISSUES else 0


if __name__ == "__main__":
    raise SystemExit(main())
