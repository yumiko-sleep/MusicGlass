# -*- coding: utf-8 -*-
"""配置读写。

设计要点：
1. 配置文件放在 %APPDATA%\\MusicGlass\\config.json —— 不放程序目录，
   因为打包成 exe 后程序目录可能只读（Program Files），而 APPDATA 永远可写。
2. 读配置必须"永不抛异常"：文件损坏、字段缺失、类型不对、值越界，
   一律降级到默认值。桌面小组件因为一个坏配置文件起不来是不可接受的。
3. 只认识的字段才写回，未知字段会被忽略（老版本配置升上来不会炸）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
APP_DIR_NAME = "MusicGlass"

# 配置格式版本。改动默认值、删改字段时都要 +1，并在 migrate() 里写清楚怎么升。
# 为什么需要它：老用户配置文件里存的是"当时的默认值"，
# 而我们改了默认值（比如 always_on_top 从 true 改成 false）
# 后，光改代码是没用的 —— 文件里的旧值会一直生效。只能靠版本号识别并升级。
CONFIG_VERSION = 3


def config_dir() -> Path:
    """返回配置目录（不存在则创建）。"""
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return config_dir() / "config.json"


# ---------------------------------------------------------------------------
# 配置项
# ---------------------------------------------------------------------------
def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Config:
    """全部可调参数。

    注意：这里刻意用"基本类型"（int/float/str/bool），
    这样 JSON 序列化不需要任何自定义编码器。
    """

    # ---- 窗口位置与尺寸 ----
    # 配置格式版本（不要手动改，交给 migrate()）
    config_version: int = CONFIG_VERSION
    # 位置存的是"面板左上角的屏幕坐标"；None 表示还没被用户拖过，启动时自动放到右下角
    pos_x: int | None = None
    pos_y: int | None = None
    # 缩放系数：1.0 = 400x120 的设计基准尺寸
    scale: float = 1.0

    # ---- 透明度（鼠标移出 / 移入）----
    opacity_idle: float = 0.62
    opacity_hover: float = 1.0

    # ---- 玻璃 ----
    glass_backend: str = "pyglass"     # pyglass（真折射）| qt（半透明+模糊，备选）
    # 抓屏方式（只影响 pyglass 后端）：
    #   "auto" —— 用 Windows 放大镜 API（默认，画质最好，组件也能被录屏/截图看到）
    #   "gdi"  —— 禁用放大镜，改用 BitBlt 抓屏 + SetWindowDisplayAffinity
    #             （组件对所有截图/录屏隐形，但能避开与放大镜 API 相关的任何异常）
    # 实测：放大镜初始化会抛访问违例（faulthandler 能看到，pyglass 自己兜住了，
    # 目前不影响使用）。留着这个开关，万一以后遇到放大镜相关的怪问题可以一键绕开。
    capture_method: str = "auto"
    glass_thickness: float = 0.55      # pyglass 旋钮：玻璃厚度
    glass_frost: float = 0.15          # pyglass 旋钮：霜化
    # 抓屏间隔（毫秒）—— 实测这是整个程序最贵的操作，而且**耗时与面积无关**：
    # 屏幕抓取有个 ~21ms 的固定延迟，32x32 和 978x492 花的时间差不多。
    # 所以唯一有效的优化就是"少抓"，并且跟着用户操作走（见下面的 idle 值）：
    #   用户正在操作 / 鼠标悬停在组件上 -> 用这个快档（看起来跟得紧）
    capture_interval_ms: int = 250
    #   用户已经静下来（超过几秒没输入）-> 用慢档。桌面上没人在动，背景本来就不会变。
    capture_interval_idle_ms: int = 3000
    # 抓屏时面板四周多抓多少像素。
    # 【注意】别想着"抓小一点省 CPU"——实测面积不影响耗时；
    # 而且抓得太小反而让 pyglass 的变化检测变敏感（折射次数 11 -> 25），更贵。
    # 保持 pyglass 默认的 150 就好。
    capture_margin: int = 150
    # 折射时跳过霜化散射（fast 路径）。实测这是折射耗时的大头之一。
    # 代价是玻璃少一点"磨砂"感（默认霜化本来就只有 0.15，差别很小）。
    refract_fast: bool = True

    # ---- 行为 ----
    # 是否启用系统托盘（托盘是最可靠的入口：贴桌面模式下快捷键失效）
    tray_enabled: bool = True
    # 数据源：smtc = 真实读取 QQ音乐；mock = 假数据（开发 UI 时用）
    media_source: str = "smtc"
    # SMTC 轮询间隔（毫秒）。实测 QQ音乐 不推送事件，只能轮询；
    # 每次轮询 3 次 COM 调用，500ms 完全够用，调小也不会更实时。
    smtc_poll_ms: int = 500
    # 贴桌面层：窗口只贴在桌面上（像壁纸），别的程序会把它盖住，Win+D 又能看到它。
    desktop_layer: bool = True
    # 贴桌面的具体做法：
    #   "zorder"  —— 插到桌面层之上、所有普通窗口之下（默认，可点可拖）
    #   "workerw" —— SetParent 到桌面 WorkerW 当子窗口（更"融进桌面"，
    #                但会被桌面图标层 SysListView32 挡住鼠标，变成只能看不能点）
    desktop_layer_mode: str = "zorder"
    # 置顶：浮在所有窗口之上。默认关 —— 开着会挡住微信/浏览器，很烦。
    # 置顶和贴桌面是互斥的：置顶优先。
    always_on_top: bool = False
    autostart: bool = False            # 阶段5：开机自启

    # ---- 阶段4：动画 ----
    # 总开关：关掉之后界面回到阶段3 的静态样子（封面不转、频谱不动、光晕不呼吸、
    # 切歌直接硬切）。嫌 CPU 高 / 想安静一点时可以随时关（启动参数 --no-anim，
    # 或在右键菜单 / 托盘菜单里切换）。
    animations: bool = True
    # 共享动画时钟的帧率。30 够用；60 更顺但 CPU 几乎翻倍（实测见 README）。
    animation_fps: int = 30
    # 抓屏自适应降级（问题2）：动画被抓屏卡住时（比如开着录屏软件），
    # 自动先降抓屏频率、再彻底暂停折射（只保留动画）。嫌它多事可以关掉。
    adaptive_capture: bool = True

    # ---- 缩放范围 ----
    MIN_SCALE: float = 0.55
    MAX_SCALE: float = 2.20

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        # 范围常量不必落盘
        for key in ("MIN_SCALE", "MAX_SCALE"):
            data.pop(key, None)
        data["config_version"] = CONFIG_VERSION
        return data

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        """从 dict 构造，未知字段忽略、错误字段回退默认值。"""
        known = {f.name for f in fields(cls)}
        instance = cls()
        for key, value in (raw or {}).items():
            if key not in known or key in ("MIN_SCALE", "MAX_SCALE"):
                continue
            default = getattr(instance, key)
            try:
                if default is None:
                    setattr(instance, key, value if isinstance(value, int) else None)
                elif isinstance(default, bool):
                    setattr(instance, key, bool(value))
                elif isinstance(default, int) and not isinstance(default, bool):
                    setattr(instance, key, int(value))
                elif isinstance(default, float):
                    setattr(instance, key, float(value))
                elif isinstance(default, str):
                    setattr(instance, key, str(value))
            except (TypeError, ValueError):
                continue  # 坏值 -> 保留默认值
        instance.normalize()
        return instance

    def normalize(self) -> None:
        """把越界/非法值拉回合法区间。"""
        self.scale = _clamp(float(self.scale or 1.0), self.MIN_SCALE, self.MAX_SCALE)
        self.opacity_idle = _clamp(float(self.opacity_idle), 0.15, 1.0)
        self.opacity_hover = _clamp(float(self.opacity_hover), 0.15, 1.0)
        self.glass_thickness = _clamp(float(self.glass_thickness), 0.0, 1.0)
        self.glass_frost = _clamp(float(self.glass_frost), 0.0, 1.0)
        if self.glass_backend not in ("pyglass", "qt"):
            self.glass_backend = "pyglass"
        if self.desktop_layer_mode not in ("zorder", "workerw"):
            self.desktop_layer_mode = "zorder"
        if self.media_source not in ("smtc", "mock"):
            self.media_source = "smtc"
        if self.capture_method not in ("auto", "gdi"):
            self.capture_method = "auto"
        self.capture_margin = int(_clamp(int(self.capture_margin), 8, 400))
        self.capture_interval_idle_ms = int(
            _clamp(int(self.capture_interval_idle_ms), self.capture_interval_ms, 10000))
        self.smtc_poll_ms = int(_clamp(int(self.smtc_poll_ms), 120, 5000))
        self.animation_fps = int(_clamp(int(self.animation_fps), 10, 60))
        self.capture_interval_ms = int(_clamp(int(self.capture_interval_ms), 16, 2000))

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """读配置；任何异常都回退到默认值，绝不抛给调用方。"""
        target = path or config_path()
        try:
            with open(target, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                return cls()
            config = cls.from_dict(raw)
            raw_version = int(raw.get("config_version", 0) or 0)
            if raw_version < CONFIG_VERSION:
                config = migrate(config, raw_version)
                config.save(target)          # 升级后的配置立刻落盘
            return config
        except FileNotFoundError:
            return cls()
        except Exception:
            # 文件坏了：改名保留现场，方便排查，然后从默认值开始
            try:
                target.replace(target.with_suffix(".json.broken"))
            except Exception:
                pass
            return cls()

    def save(self, path: Path | None = None) -> bool:
        """原子写：先写临时文件再替换，避免写一半掉电留下半个坏文件。"""
        target = path or config_path()
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            os.replace(tmp, target)
            return True
        except Exception:
            return False


def migrate(config: "Config", from_version: int) -> "Config":
    """把老版本配置升级到当前版本。

    每一条规则都要写清楚"为什么要改"，否则以后没人敢删。
    """
    if from_version < 1:
        # v0 -> v1：阶段1 之前的配置没有版本号，字段跟现在一致，不用动。
        pass
    if from_version < 2:
        # v1 -> v2：窗口层级策略变了。
        # 旧版默认为 always_on_top=True（浮在所有窗口之上），会挡住微信/浏览器；
        # 新版默认"贴桌面层"。而老配置文件里存着 always_on_top=true，
        # 光改代码不会生效，必须在这里强制改掉。
        config.always_on_top = False
        config.desktop_layer = True
    if from_version < 3:
        # v2 -> v3：性能优化（都是实测调出来的值）。
        # 关键发现：屏幕抓取的耗时**与面积无关**（固定 ~21ms 延迟），
        # 所以优化手段是"少抓"，而不是"抓小"。
        config.capture_interval_ms = 250
        config.capture_interval_idle_ms = 3000
        config.capture_margin = 150
    config.config_version = CONFIG_VERSION
    return config
