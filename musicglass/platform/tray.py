# -*- coding: utf-8 -*-
"""系统托盘图标 + 菜单。

为什么托盘在阶段3 优先级最高：
    贴上桌面之后，组件已经拿不到键盘焦点（WS_EX_NOACTIVATE，否则点一下就会浮到
    微信前面去），ESC 之类的快捷键就失效了。托盘是最自然的"总入口"：
    隐藏/显示、切换置顶、开机自启、退出，全都能在这里找到。

菜单只在"该更新"的时候更新勾选状态（aboutToShow），
而不是每次打开都重建菜单对象 —— 重建会让 Windows 上的托盘菜单偶尔闪一下。
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from .. import __display_name__, __version__
from ..core.models import PlaybackState
from ..ui.tray_icon import build_icon
from . import autostart


class TrayIcon(QObject):
    """托盘图标。持有窗口和媒体源的引用，负责把菜单动作转成窗口操作。"""

    def __init__(self, window, config, source, parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._config = config
        self._source = source

        self._tray = QSystemTrayIcon(build_icon(playing=False), self)
        self._tray.setToolTip(f"{__display_name__} MusicGlass")

        self._menu = QMenu()
        self._act_toggle = self._menu.addAction("隐藏组件")
        self._menu.addSeparator()

        self._act_desktop = self._menu.addAction("只贴桌面（会被其它窗口盖住）")
        self._act_desktop.setCheckable(True)
        self._act_top = self._menu.addAction("置顶显示（浮在其它窗口之上）")
        self._act_top.setCheckable(True)
        self._menu.addSeparator()

        self._act_autostart = self._menu.addAction("开机自启")
        self._act_autostart.setCheckable(True)
        # 动画总开关放在托盘里：贴桌面模式下右键菜单得点得到组件才行，
        # 而 CPU 高低是随时想调的事，托盘是最好的入口
        self._act_anim = self._menu.addAction("动画效果（旋转封面 / 频谱 / 呼吸光）")
        self._act_anim.setCheckable(True)
        self._menu.addSeparator()

        self._act_reset = self._menu.addAction("回到屏幕右下角")
        self._act_about = self._menu.addAction(f"关于 音璃 v{__version__}")
        self._menu.addSeparator()
        self._act_quit = self._menu.addAction("退出音璃")

        self._menu.aboutToShow.connect(self._sync_menu)
        self._act_toggle.triggered.connect(self._toggle_visibility)
        self._act_desktop.triggered.connect(lambda: self._set_layer(desktop=True))
        self._act_top.triggered.connect(lambda: self._set_layer(desktop=False))
        self._act_autostart.triggered.connect(self._toggle_autostart)
        self._act_anim.triggered.connect(self._window.set_animations)
        self._act_reset.triggered.connect(self._window.move_to_default_position)
        self._act_about.triggered.connect(self._show_about)
        self._act_quit.triggered.connect(self._window.quit_application)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

        # 数据源变化时更新提示文案和图标（托盘区就能看到在放什么歌）
        source.track_changed.connect(self._on_track_changed)
        source.state_changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def show(self) -> bool:
        """显示托盘图标。系统没有托盘（极少数情况）时返回 False。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("[音璃] 系统托盘不可用，跳过托盘图标")
            return False
        self._tray.show()
        self._sync_menu()
        self._refresh_tooltip()
        return True

    def hide(self) -> None:
        self._tray.hide()

    @property
    def is_visible(self) -> bool:
        return self._tray.isVisible()

    def menu_titles(self) -> list[str]:
        """菜单里所有条目的文字（--debug 用它做自检）。"""
        return [action.text() for action in self._menu.actions() if not action.isSeparator()]

    # ------------------------------------------------------------------
    # 菜单状态同步
    # ------------------------------------------------------------------
    def _sync_menu(self) -> None:
        visible = self._window.isVisible()
        self._act_toggle.setText("隐藏组件" if visible else "显示组件")
        # 置顶和贴桌面是互斥的两个单选项（用勾选状态表达"当前是哪种"）
        on_top = bool(self._config.always_on_top)
        self._act_top.setChecked(on_top)
        self._act_desktop.setChecked(not on_top)
        self._act_autostart.setChecked(autostart.is_enabled())
        self._act_anim.setChecked(bool(self._config.animations))

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def _toggle_visibility(self) -> None:
        self._window.toggle_visibility()

    def _set_layer(self, *, desktop: bool) -> None:
        if desktop:
            self._config.always_on_top = False
            self._window.set_always_on_top(False)
            self._config.desktop_layer = True
            self._window.set_desktop_layer(True)
        else:
            self._window.set_always_on_top(True)
        self._sync_menu()

    def _toggle_autostart(self, checked: bool) -> None:
        if not autostart.is_supported():
            self._act_autostart.setChecked(False)
            self._notify("当前系统不支持开机自启")
            return
        if autostart.set_enabled(checked):
            self._config.autostart = checked
            self._config.save()
            detail = autostart.current_value() if checked else "已从注册表移除"
            self._notify(f"开机自启{'已开启' if checked else '已关闭'}\n{detail}")
        else:
            # 失败一定要回到真实状态，别让勾选状态骗人
            self._act_autostart.setChecked(autostart.is_enabled())
            self._notify("开机自启设置失败（注册表写入被拒绝）")

    def _show_about(self) -> None:
        self._notify(f"音璃 MusicGlass v{__version__}\n"
                     f"数据源：{self._source.status_text() if hasattr(self._source, 'status_text') else '—'}\n"
                     f"开机自启：{autostart.status_text()}")

    def _notify(self, message: str) -> None:
        """用系统气泡提示（比弹对话框温和，不会打断用户）。"""
        self._tray.showMessage(f"{__display_name__} MusicGlass", message,
                               QSystemTrayIcon.MessageIcon.Information, 4000)

    # ------------------------------------------------------------------
    # 托盘交互
    # ------------------------------------------------------------------
    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            # 左键单击：显示/隐藏（最常见的心智模型）
            self._window.toggle_visibility()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            # 中键：播放/暂停（手不用离开鼠标就能换歌）
            self._source.play_pause()

    # ------------------------------------------------------------------
    # 数据源回调
    # ------------------------------------------------------------------
    def _on_track_changed(self, track) -> None:
        self._refresh_tooltip()

    def _on_state_changed(self, state) -> None:
        playing = state is PlaybackState.PLAYING
        self._tray.setIcon(build_icon(playing=playing))
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        track = self._source.track
        title = getattr(track, "title", "") or ""
        artist = getattr(track, "artist", "") or ""
        if not self._source.is_available or not title:
            self._tray.setToolTip(f"{__display_name__} MusicGlass")
            return
        suffix = "" if self._source.state is PlaybackState.PLAYING else "（已暂停）"
        self._tray.setToolTip(f"{title}{suffix}\n{artist}")
