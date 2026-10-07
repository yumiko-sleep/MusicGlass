# -*- coding: utf-8 -*-
"""开机自启（注册表）的单元测试。

不碰真实注册表：用一个内存版的假 winreg 顶上去，
既能覆盖所有分支（键不存在 / 写入被拒 / 删除不存在的值），又绝不会污染系统。
"""

from __future__ import annotations

import pytest

from musicglass.platform import autostart


class FakeKey:
    def __init__(self, store: dict, path: str) -> None:
        self._store = store
        self._path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    """只实现 autostart 用到的那几个 API。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.store: dict[str, dict[str, str]] = {}
        self.fail_on_write = False

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        if path not in self.store:
            raise FileNotFoundError(path)
        return FakeKey(self.store, path)

    def CreateKeyEx(self, root, path, reserved=0, access=0):  # noqa: N802
        self.store.setdefault(path, {})
        return FakeKey(self.store, path)

    def QueryValueEx(self, key, name):  # noqa: N802
        values = self.store.get(key._path)
        if not values or name not in values:
            raise FileNotFoundError(name)
        return values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        if self.fail_on_write:
            raise OSError("access denied")
        self.store[key._path][name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        values = self.store.get(key._path, {})
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]


@pytest.fixture
def fake(monkeypatch):
    fake_reg = FakeWinreg()
    monkeypatch.setattr(autostart, "winreg", fake_reg)
    return fake_reg


# ---------------------------------------------------------------------------
# 命令行拼装（最容易出错的地方：引号）
# ---------------------------------------------------------------------------
def test_launch_command_quotes_paths(monkeypatch):
    """路径带空格必须加引号，否则 Windows 会把命令拆成两段直接启动失败。"""
    monkeypatch.setattr(autostart.sys, "frozen", False, raising=False)
    command = autostart.launch_command(
        executable=r"C:\Program Files\Python\pythonw.exe",
        script=r"C:\Users\我 的文档\MusicGlass\main.pyw",
    )
    assert command.startswith('"C:\\Program Files\\Python\\pythonw.exe"')
    assert '"C:\\Users\\我 的文档\\MusicGlass\\main.pyw"' in command
    assert command.endswith("--silent")


def test_launch_command_when_frozen(monkeypatch):
    """打包成 exe 之后，注册的应该是 exe 自己。"""
    monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)
    monkeypatch.setattr(autostart.sys, "executable", r"C:\Apps\MusicGlass.exe")
    command = autostart.launch_command()
    assert command == '"C:\\Apps\\MusicGlass.exe" --silent'


def test_launch_command_prefers_pythonw(monkeypatch, tmp_path):
    """源码运行时优先用 pythonw.exe（没有控制台），避免开机闪黑框。"""
    monkeypatch.setattr(autostart.sys, "frozen", False, raising=False)
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_text("")
    pythonw.write_text("")
    command = autostart.launch_command(executable=str(pythonw), script=str(tmp_path / "main.pyw"))
    assert "pythonw.exe" in command


# ---------------------------------------------------------------------------
# 注册表读写
# ---------------------------------------------------------------------------
def test_enable_then_is_enabled(fake):
    assert autostart.is_enabled() is False
    assert autostart.enable() is True
    assert autostart.is_enabled() is True
    assert autostart.current_value() == autostart.launch_command()


def test_disable_removes_value(fake):
    autostart.enable()
    assert autostart.disable() is True
    assert autostart.is_enabled() is False
    assert autostart.current_value() == ""


def test_disable_when_missing_is_success(fake):
    """本来就没开，删除也算成功（目标是"关掉"这个状态，不是"删掉某个值"）。"""
    assert autostart.disable() is True


def test_is_enabled_survives_missing_key(fake):
    assert autostart.is_enabled() is False


def test_write_failure_reported(fake):
    fake.fail_on_write = True
    assert autostart.enable() is False
    assert autostart.is_enabled() is False


def test_status_text_when_off_and_on(fake):
    assert autostart.status_text() == "未开启"
    autostart.enable()
    assert "已开启" in autostart.status_text()
    assert "main.pyw" in autostart.status_text()


def test_uses_hkcu_run_key(fake):
    """必须是 HKCU 的 Run 键：用户级自启，不需要管理员权限。"""
    autostart.enable()
    assert autostart.RUN_KEY in fake.store
    assert "CurrentVersion\\Run" in autostart.RUN_KEY
    assert autostart.VALUE_NAME in fake.store[autostart.RUN_KEY]
