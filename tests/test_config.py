# -*- coding: utf-8 -*-
"""config.py 的单元测试：默认值、坏值容错、越界收敛、读写往返。

这些测试存在的意义：桌面小组件最怕"配置文件坏掉就起不来"，
所以每个容错分支都要有测试兜着。
"""

from __future__ import annotations

import json

from musicglass.config import CONFIG_VERSION, Config


def test_defaults():
    config = Config()
    assert config.scale == 1.0
    assert config.glass_backend == "pyglass"
    assert 0.0 < config.opacity_idle <= 1.0
    assert config.to_dict()["glass_backend"] == "pyglass"
    # 范围常量不应该被写进配置文件
    assert "MIN_SCALE" not in config.to_dict()


def test_capture_throttle_defaults():
    """抓屏节流的两个默认值。

    实测依据：屏幕抓取耗时与面积无关（固定 ~21ms 延迟），
    所以只能靠"少抓"降 CPU；而空闲档必须明显大于活跃档。
    """
    config = Config()
    assert config.capture_interval_ms == 250          # 用户在操作时的快档
    assert config.capture_interval_idle_ms == 3000    # 用户静静的时候的慢档
    assert config.capture_interval_idle_ms > config.capture_interval_ms
    assert config.capture_margin == 150               # 保持 pyglass 默认（不是越小越好）


def test_capture_intervals_are_clamped():
    """空闲档不能小于活跃档，否则会出现"越空闲抓得越勤"的荒唐情况。"""
    config = Config.from_dict({"capture_interval_ms": 800, "capture_interval_idle_ms": 100})
    assert config.capture_interval_idle_ms >= config.capture_interval_ms


def test_migration_v2_to_v3_tunes_capture(tmp_path):
    """v2 的老配置里存着 capture_interval_ms=120（当时的默认值），必须升到新值。

    这两个值是实测调出来的：屏幕抓取有 ~21ms 固定延迟、与面积无关，
    所以只能靠"少抓"降 CPU，而且得区分活跃/空闲两档。
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"config_version": 2, "capture_interval_ms": 120}),
                    encoding="utf-8")

    config = Config.load(path)
    assert config.config_version == CONFIG_VERSION
    assert config.capture_interval_ms == 250
    assert config.capture_interval_idle_ms == 3000
    assert config.capture_margin == 150


def test_default_layer_is_desktop_not_on_top():
    """默认必须是"贴桌面"，不能是置顶——置顶会挡住微信/浏览器。"""
    config = Config()
    assert config.desktop_layer is True
    assert config.always_on_top is False
    assert config.desktop_layer_mode == "zorder"


def test_bad_layer_mode_falls_back():
    assert Config.from_dict({"desktop_layer_mode": "魔法"}).desktop_layer_mode == "zorder"
    assert Config.from_dict({"desktop_layer_mode": "workerw"}).desktop_layer_mode == "workerw"


def test_from_dict_ignores_unknown_keys():
    config = Config.from_dict({"scale": 1.5, "外星字段": 42})
    assert config.scale == 1.5


def test_from_dict_bad_types_fall_back_to_default():
    config = Config.from_dict({"scale": "不是数字", "glass_backend": 123, "always_on_top": "yes"})
    assert config.scale == 1.0                     # 坏值回退默认
    assert config.glass_backend == "pyglass"
    assert config.always_on_top is True            # 非空字符串按真处理


def test_normalize_clamps_scale():
    assert Config.from_dict({"scale": 99}).scale == Config.MAX_SCALE
    assert Config.from_dict({"scale": 0.01}).scale == Config.MIN_SCALE


def test_normalize_clamps_opacity_and_interval():
    config = Config.from_dict({"opacity_idle": 5.0, "opacity_hover": -3, "capture_interval_ms": 1})
    assert config.opacity_idle == 1.0
    assert config.opacity_hover == 0.15            # 下限，避免调到完全看不见
    assert config.capture_interval_ms == 16        # 上限 60fps，别把 CPU 烧掉


def test_unknown_backend_falls_back():
    assert Config.from_dict({"glass_backend": "魔法"}).glass_backend == "pyglass"


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    original = Config(pos_x=120, pos_y=340, scale=1.4, glass_backend="qt",
                      opacity_idle=0.5, capture_interval_ms=200)
    assert original.save(path) is True

    loaded = Config.load(path)
    assert loaded.pos_x == 120
    assert loaded.pos_y == 340
    assert loaded.scale == 1.4
    assert loaded.glass_backend == "qt"
    assert loaded.opacity_idle == 0.5
    assert loaded.capture_interval_ms == 200


def test_load_missing_file_returns_defaults(tmp_path):
    config = Config.load(tmp_path / "不存在.json")
    assert config.scale == 1.0


def test_load_corrupt_file_returns_defaults_and_keeps_evidence(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ 这不是合法的 json", encoding="utf-8")

    config = Config.load(path)
    assert config.scale == 1.0
    # 坏文件应该被改名保留，方便排查，而不是直接丢掉
    assert (tmp_path / "config.json.broken").exists()


def test_load_json_array_returns_defaults(tmp_path):
    """根节点是数组而不是对象时也要能容错。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert Config.load(path).scale == 1.0


def test_save_is_atomic_no_leftover_tmp(tmp_path):
    path = tmp_path / "config.json"
    Config(scale=1.2).save(path)
    assert path.exists()
    assert not (tmp_path / "config.json.tmp").exists()


def test_migrate_v1_config_turns_off_always_on_top(tmp_path):
    """老版配置（无 config_version，里面存着旧默认 always_on_top=true）必须被升级掉。

    这是实打实踩过的坑：光改代码里的默认值没用，
    因为用户配置文件里那个 true 会一直覆盖新默认值。
    """
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"always_on_top": True, "scale": 1.3}), encoding="utf-8")

    config = Config.load(path)
    assert config.always_on_top is False
    assert config.desktop_layer is True
    assert config.scale == 1.3                 # 用户自己的设置要保留
    # 升级后的配置要立刻写回磁盘
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION


def test_latest_config_is_not_rewritten(tmp_path):
    path = tmp_path / "config.json"
    Config(always_on_top=True, scale=1.5).save(path)
    config = Config.load(path)
    # 已经是最新版本：用户显式打开的置顶不能被迁移逻辑改掉
    assert config.always_on_top is True
    assert config.scale == 1.5
