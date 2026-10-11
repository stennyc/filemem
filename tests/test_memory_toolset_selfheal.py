"""Memory-toolset self-heal (2026-10-11): register() ensures the built-in
``memory`` toolset is present in every SAVED ``platform_toolsets.<platform>``
list of the active profile, because the host gates external-provider tool
injection on it (``memory_provider_tools_enabled`` checks for ``"memory"``).

Stubbed host modules — the live config.yaml is NEVER touched:
  - hermes_cli.config.load_config / save_config
  - hermes_cli.toolset_validation.parse_platform_toolsets_value

Covered faces:
  1. saved list missing 'memory' -> appended, save_config called once
  2. list already has 'memory'  -> no write (idempotent)
  3. platform without a saved list (non-list value) -> untouched
  4. no platform_toolsets section at all -> no write
  5. host config API unavailable -> fail-soft, register() never crashes
  6. string-list (legacy hermes config set) -> parsed, appended, re-saved as list
"""
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402


class _HostModules:
    """Context manager stubbing the two host modules the self-heal imports."""

    def __init__(self, load_return=None, save_side_effect=None, parse=None):
        self._load_return = load_return if load_return is not None else {}
        self._save_side_effect = save_side_effect
        self._parse = parse
        self.save_calls = []
        self._mods = {}

    def _make_parse(self):
        if self._parse is not None:
            return self._parse

        def _default_parse(value):
            # Mirror host semantics: list -> as-is; '[...]' string -> literal; else None
            if isinstance(value, list):
                return value
            if isinstance(value, str) and value.strip().startswith("["):
                import ast
                try:
                    parsed = ast.literal_eval(value.strip())
                except (ValueError, SyntaxError):
                    return None
                return [str(x) for x in parsed] if isinstance(parsed, list) else None
            return None

        return _default_parse

    def __enter__(self):
        cfg_mod = types.ModuleType("hermes_cli.config")
        cfg_mod.load_config = MagicMock(return_value=self._load_return)
        def _save(c):
            self.save_calls.append(c)
            if self._save_side_effect:
                self._save_side_effect(c)
        cfg_mod.save_config = _save

        tv_mod = types.ModuleType("hermes_cli.toolset_validation")
        tv_mod.parse_platform_toolsets_value = self._make_parse()

        pkg = types.ModuleType("hermes_cli")
        pkg.config = cfg_mod
        pkg.toolset_validation = tv_mod

        for name, mod in (("hermes_cli", pkg), ("hermes_cli.config", cfg_mod),
                          ("hermes_cli.toolset_validation", tv_mod)):
            self._mods[name] = sys.modules.get(name)
            sys.modules[name] = mod
        return self

    def __exit__(self, *exc):
        for name, old in self._mods.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old
        return False


def test_missing_memory_is_appended(tmp_path, monkeypatch):
    """Saved cli list without 'memory' -> appended + exactly one save."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"platform_toolsets": {"cli": ["terminal", "file"]}}
    with _HostModules(load_return=cfg) as host:
        fm._ensure_memory_toolset()
    assert "memory" in cfg["platform_toolsets"]["cli"]
    assert len(host.save_calls) == 1


def test_present_memory_is_not_written(tmp_path, monkeypatch):
    """Idempotent: 'memory' already in the list -> zero saves."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"platform_toolsets": {"cli": ["terminal", "memory"]}}
    with _HostModules(load_return=cfg) as host:
        fm._ensure_memory_toolset()
    assert cfg["platform_toolsets"]["cli"] == ["terminal", "memory"]
    assert len(host.save_calls) == 0


def test_no_saved_platforms_no_write(tmp_path, monkeypatch):
    """No platform_toolsets section (default composite) -> nothing touched."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"plugins": {"enabled": ["filemem"]}}
    with _HostModules(load_return=cfg) as host:
        fm._ensure_memory_toolset()
    assert len(host.save_calls) == 0


def test_unsaved_platform_not_seeded(tmp_path, monkeypatch):
    """A platform whose value is NOT a saved list (e.g. scalar string) must
    be left alone — seeding it would replace the core composite (host guard)."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"platform_toolsets": {"tui": "some-platform-bundle", "cli": ["file"]}}
    with _HostModules(load_return=cfg) as host:
        fm._ensure_memory_toolset()
    assert cfg["platform_toolsets"]["tui"] == "some-platform-bundle"  # untouched
    assert "memory" in cfg["platform_toolsets"]["cli"]
    assert len(host.save_calls) == 1


def test_legacy_string_list_is_parsed_and_resaved(tmp_path, monkeypatch):
    """Older `hermes config set` stored the list as a '[...]' string: the host
    parser materialises it; the healed value must be re-saved as a proper list."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"platform_toolsets": {"cli": '["terminal", "file"]'}}
    with _HostModules(load_return=cfg) as host:
        fm._ensure_memory_toolset()
    assert cfg["platform_toolsets"]["cli"] == ["terminal", "file", "memory"]
    assert isinstance(cfg["platform_toolsets"]["cli"], list)
    assert len(host.save_calls) == 1


def test_host_api_absent_is_fail_soft(tmp_path, monkeypatch):
    """Standalone interpreter (no host modules): warning only, no exception,
    register() must still complete."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    with _HostModules() as host:
        # Drop the stubs to simulate a bare interpreter:
        for name in ("hermes_cli", "hermes_cli.config", "hermes_cli.toolset_validation"):
            sys.modules.pop(name, None)
        # re-point: our __exit__ already restored; here we ensure absence
        fm._ensure_memory_toolset()  # must not raise
    assert len(host.save_calls) == 0


def test_register_invokes_self_heal(tmp_path, monkeypatch):
    """register() calls _ensure_memory_toolset exactly once (wired in
    register, not left orphaned)."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    with _HostModules() as host:
        spy = MagicMock(side_effect=None)
        monkeypatch.setattr(fm, "_ensure_memory_toolset", spy)
        ctx = types.SimpleNamespace(
            register_memory_provider=lambda prov: None,
            register_hook=lambda name, fn: None,
        )
        fm.register(ctx)
        assert spy.call_count == 1


def test_save_failure_is_fail_soft(tmp_path, monkeypatch):
    """Managed-install / admin-locked config: save_config raises -> the
    in-memory dict is ROLLED BACK to the loaded state (disk never had it,
    memory must not claim it), no exception escapes."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    cfg = {"platform_toolsets": {"cli": ["file"]}}

    def _boom(c):
        raise PermissionError("admin-managed")

    with _HostModules(load_return=cfg, save_side_effect=_boom) as host:
        fm._ensure_memory_toolset()  # must not raise
    assert cfg["platform_toolsets"]["cli"] == ["file"]  # rolled back
    assert len(host.save_calls) == 1  # attempted once, no retry
