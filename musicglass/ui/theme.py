# -*- coding: utf-8 -*-
"""视觉主题：颜色、尺寸、字体、动画时长，全部集中在这里。

关键设计：**所有几何尺寸都是"设计基准值"，单位是逻辑像素**，
基准面板是 400x120。运行时用 u = 面板高度 / 120 当作缩放单位，
所以窗口放大缩小时，圆角、字号、间距、图标粗细会整体等比变化，
不会出现"窗口变大了但字还是那么小"的割裂感。
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QFont

# ---------------------------------------------------------------------------
# 设计基准（不要改这两个值，改了所有布局都要重算）
# ---------------------------------------------------------------------------
BASE_PANEL_W = 400
BASE_PANEL_H = 120

# 面板之外的透明留白：给阴影和折射留出空间，不属于可见面板
MARGIN = 56
# 面板圆角
RADIUS = 26

# 面板内部的排版基准（都要乘以 u）
PAD = 12          # 内边距
GAP = 10          # 元素间距
BOTTOM_H = 14     # 底部行（进度条 + 时间 + 频谱）高度
V_GAP = 6         # 上下两行之间的间距
BUTTON = 30       # 控制按钮的命中框边长
PROGRESS_H = 3.5  # 进度条高度

# ---------------------------------------------------------------------------
# 配色：薄荷绿 -> 青蓝
# ---------------------------------------------------------------------------
MINT = QColor(0x3D, 0xDC, 0x97)
CYAN = QColor(0x22, 0xC1, 0xDC)

TEXT_PRIMARY = QColor(255, 255, 255, 245)     # 歌名
TEXT_SECONDARY = QColor(255, 255, 255, 155)   # 歌手
TEXT_TERTIARY = QColor(255, 255, 255, 115)    # 时间

ICON_IDLE = QColor(255, 255, 255, 205)
ICON_HOVER = QColor(255, 255, 255, 255)
ICON_ACTIVE = MINT                            # 循环模式被激活时用薄荷绿

BUTTON_HOVER_BG = QColor(255, 255, 255, 38)   # 按钮悬停时的圆形底
PROGRESS_TRACK = QColor(255, 255, 255, 52)    # 进度条底槽
PROGRESS_FILL_FROM = MINT                     # 进度条已播部分：渐变
PROGRESS_FILL_TO = CYAN
PROGRESS_KNOB = QColor(255, 255, 255, 240)

# 氛围光（组件四周的浅绿柔光）
AMBIENT = QColor(0x3D, 0xDC, 0x97)
AMBIENT_LAYERS = 12
# 每层向外扩散多少像素。
# 【为什么是 MARGIN / 层数】光晕必须在这个范围内衰减到看不见：
# 窗口只有“面板 + MARGIN”这么大，画到窗口外面的部分会被直接裁掉 ——
# 亮度一高，那道又直又硬的裁切线就会露出来（像给组件套了一圈矩形光边）。
# 铺满整个 MARGIN 后，最外层刚好落在窗口边缘、alpha 已经衰减到 0，不会硬切。
# （之前是 11，也就是撑到 132px，而窗口只有 56px：7 层纯属白画，
#   而且因为能看到的那几层叠加起来很均匀，衰减曲线是平的，一调亮就硬切）
AMBIENT_SPREAD = MARGIN / AMBIENT_LAYERS
# 每层的透明度（叠加起来形成柔光）。
# 【这个值不要拍脑袋】实测 7 在深色桌面上只有 6/255 的亮度差，肉眼基本看不出，
# 呼吸幅度更是只有 2/255。用 tools/verify_glow.py 能量化并自动调到看得见。
AMBIENT_ALPHA = 114
# 衰减指数：alpha = AMBIENT_ALPHA * (1 - 已外扩距离 / MARGIN) ** 这个值。
# 【为什么要调它】想提亮度，直接加 AMBIENT_ALPHA 会让最外层也变亮，
# 而窗口边缘是硬裁的 —— 于是四周会冒出一道矩形光边。
# 指数调大 = 靠内层亮、到边缘迅速归零：既能提亮又不会硬切（实测 3.0 时边缘残亮 <1/255）。
AMBIENT_FALLOFF = 3.0

# 频谱条
EQ_BARS = 5
EQ_BAR_W = 2.6
EQ_BAR_GAP = 1.6
EQ_ACTIVE = QColor(0x3D, 0xDC, 0x97, 235)
EQ_IDLE = QColor(255, 255, 255, 70)

# ---------------------------------------------------------------------------
# 动画
# ---------------------------------------------------------------------------
FADE_MS = 170        # 鼠标移入/移出的透明度过渡时长
WHEEL_STEP = 0.05    # 每格滚轮缩放多少

# ---- 阶段4：帧动画（全部由 ui/animation.py 的共享时钟驱动）----
ANIMATION_FPS = 30            # 共享动画时钟的帧率（60 会明显更耗 CPU，收益很小）

DISC_SPEED_DEG_S = 18.0       # 封面角速度（度/秒）：18 度/秒 -> 20 秒转一圈
                              # （真黑胶 33 转 = 200 度/秒，在这个尺寸上会晃眼）

EQ_PHASE_SPEED = 2.6          # 频谱相位推进速度（弧度/秒），越大跳得越快

GLOW_BREATH_PERIOD_S = 4.0    # 氛围光呼吸一个来回的秒数（4 秒比较「活着」）
GLOW_BREATH_SPEED = 2.0 * 3.141592653589793 / GLOW_BREATH_PERIOD_S
GLOW_BREATH_BASE = 0.80       # 呼吸的基准亮度（1.0 = 满亮）
GLOW_BREATH_AMP = 0.20        # 呼吸幅度 -> 亮度在 0.60 ~ 1.00 之间起伏（40% 摆幅）
# 氛围光的更新间隔（秒）。它是四个动画里单帧最贵的（12 层柔光描边，
# 实测 ~2.3ms/次），而亮度变化又慢 —— 降到 10fps 完全看不出差别，
# 重绘开销直接少 2/3。
GLOW_BREATH_MIN_STEP_S = 0.1

TRANSITION_OUT_S = 0.15       # 切歌：旧内容淡出时长
TRANSITION_IN_S = 0.28        # 切歌：新内容淡入时长（淡入比淡出慢，观感更舒服）

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
FONT_FAMILY = "Microsoft YaHei UI"
FONT_FALLBACKS = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "sans-serif")

FONT_TITLE = 16.0
FONT_ARTIST = 11.0
FONT_TIME = 9.5
FONT_TOAST = 9.5      # 临时提示条（toast）


def font(size: float, *, bold: bool = False, pixel: bool = True) -> QFont:
    """按"逻辑像素"字号造字体。

    用像素而不是磅（point）是刻意的：磅会随系统 DPI 变化，
    而我们的布局全是按像素算的，两者混用会导致不同机器上排版不一致。
    """
    f = QFont(FONT_FAMILY)
    if pixel:
        f.setPixelSize(max(1, int(round(size))))
    else:
        f.setPointSizeF(size)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    # 轻微的字距收紧，让中文标题更紧凑好看
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 99.5)
    return f
