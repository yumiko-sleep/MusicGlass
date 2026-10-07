# -*- coding: utf-8 -*-
"""音璃 MusicGlass —— 一个常驻桌面的透明液态玻璃音乐小组件。

包结构：
    musicglass.core       数据层：只谈"歌是什么"，不谈怎么画
    musicglass.ui         表现层：只谈怎么画，不碰系统
    musicglass.platform   Windows 系统交互（注册表自启、托盘等）

依赖方向是单向的：ui -> core，platform -> 任意，但 core 不反向依赖 ui。
"""

__version__ = "0.5.0"
__app_name__ = "MusicGlass"
__display_name__ = "音璃"

__all__ = ["__version__", "__app_name__", "__display_name__"]
