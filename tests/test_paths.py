"""Tests for config path selection.

Where the config lands matters: writing to /root/.config under sudo looks like
it works, because the daemon is also root, but puts the setting somewhere a
daemon started with a clean environment would not read.
"""

from __future__ import annotations

from thorfan.core import config


def test_root_always_uses_the_system_path(monkeypatch):
    monkeypatch.setattr(config.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config.os.path, "exists", lambda p: False)
    assert config.default_config_path() == config.SYSTEM_CONFIG


def test_root_does_not_fall_back_to_home(monkeypatch, tmp_path):
    """The bug this guards: sudo wrote to /root/.config/thorfan/config.json."""
    monkeypatch.setattr(config.os, "geteuid", lambda: 0)
    monkeypatch.setattr(config.os.path, "exists", lambda p: False)
    monkeypatch.setenv("HOME", "/root")
    assert "/root" not in config.default_config_path()


def test_user_prefers_system_config_when_present(monkeypatch):
    """So `thorfan status` reflects what the daemon actually uses."""
    monkeypatch.setattr(config.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config.os.path, "exists",
                        lambda p: p == config.SYSTEM_CONFIG)
    assert config.default_config_path() == config.SYSTEM_CONFIG


def test_user_falls_back_to_own_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(config.os.path, "exists", lambda p: False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = config.default_config_path()
    assert path == str(tmp_path / "thorfan" / "config.json")


def test_xdg_config_home_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.user_config_path().startswith(str(tmp_path / "xdg"))
