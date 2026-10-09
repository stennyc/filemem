"""LLM 显式化◎ (t_32980f71): register-time host main_runtime capture.

The filemem plugin calls the host LLM via ctx.llm (PluginLlm facade ->
agent.auxiliary_client.call_llm) WITHOUT a main_runtime argument; the host
resolves the main-model route implicitly through the contextvar
_RUNTIME_MAIN_CONTEXT. This card makes that EXPLICIT — the supercompress
three-line pattern (import host call surface + capture main_runtime +
pass it at call time), step 1 of 2:

  - import the host helper on the plugin side only (zero host edits):
    ``from agent.auxiliary_client import _normalize_main_runtime``
  - at register() time (inside a host turn, main runtime already set in
    the contextvar) read the snapshot: ``_normalize_main_runtime(None)``
    -> dict{model, provider, base_url, api_key, api_mode} (or None)
  - store it at the same level as _HOST_LLM_CTX (module + provider face)
    so epic card 2's 5 LLM call sites consume it

Hard constraints (host zero-touch):
  - the module-level value is _HOST_MAIN_RUNTIME (None = standalone /
    non-host / empty snapshot); instances keep _main_runtime None and the
    LLM faces keep today's exact skip semantics (fail-soft, no crash).
  - a broken/unimportable host module must never crash register().

All tests use synthetic tmp stores; the live /root/.hermes filemem store
is never touched. Host behavior is asserted, never modified.
"""
import pathlib
import sys
import types

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402

HOST_SNAPSHOT = {
    "model": "host-model-1",
    "provider": "openai",
    "base_url": "https://example.invalid/v1",
    "api_key": "k-test-main",
    "api_mode": "openai",
}


def _reset_state():
    with fm._cache_lock:
        fm._provider_cache.clear()
    fm._HOST_LLM_CTX = None
    fm._HOST_MAIN_RUNTIME = None


def _fake_ctx():
    return types.SimpleNamespace(
        register_memory_provider=lambda prov: None,
        register_hook=lambda name, fn: None,
    )


def _isolate(tmp_path, monkeypatch):
    """Route provider construction to a tmp store (never the live root)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))


# ── host-present path ───────────────────────────────────────────────────────


def test_register_captures_host_main_runtime_snapshot(tmp_path, monkeypatch):
    """Host path acceptance: after register() inside a host turn the
    module-level _HOST_MAIN_RUNTIME is the 5-key dict (non-None), and a
    freshly built per-session instance inherits it (same level as
    _HOST_LLM_CTX)."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(fm, "_normalize_main_runtime",
                        lambda mr: dict(HOST_SNAPSHOT))
    fm.register(_fake_ctx())

    assert isinstance(fm._HOST_MAIN_RUNTIME, dict), \
        "register must snapshot the host main runtime"
    for key in ("model", "provider", "base_url", "api_key", "api_mode"):
        assert key in fm._HOST_MAIN_RUNTIME, f"missing key {key}"
    assert fm._HOST_MAIN_RUNTIME == HOST_SNAPSHOT

    default_p = fm._get_cached_provider("default")
    assert default_p._main_runtime == HOST_SNAPSHOT
    sess_p = fm._get_cached_provider("session-mm")
    assert sess_p is not default_p
    assert sess_p._main_runtime == HOST_SNAPSHOT, \
        "new session instance must inherit the host main runtime"


def test_register_snapshot_is_none_arg_contextvar_read(tmp_path, monkeypatch):
    """The snapshot must read the contextvar at register time (call with
    None = 'resolve from _RUNTIME_MAIN_CONTEXT / compat mirror'), NOT pass
    an invented route — a non-None argument would be a hardcoded route
    (forbidden by the hard constraints)."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(
        fm, "_normalize_main_runtime",
        lambda mr: (seen.append(mr), dict(HOST_SNAPSHOT))[1])
    fm.register(_fake_ctx())
    assert seen == [None], \
        f"snapshot must read the contextvar (arg None), got {seen!r}"


def test_refresh_propagates_main_runtime_to_pre_register_instance(tmp_path,
                                                                   monkeypatch):
    """An instance created/cached BEFORE register() (or built standalone)
    gets the snapshot via _refresh_provider_llm_ctx — same no-clobber-
    when-None level as _llm_ctx (P2 boundary-face window, closed for the
    runtime face too)."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    p = FileMemProvider.__new__(FileMemProvider)
    p._llm_ctx = None
    p._main_runtime = None

    monkeypatch.setattr(fm, "_HOST_MAIN_RUNTIME", dict(HOST_SNAPSHOT))
    fm._refresh_provider_llm_ctx(p)
    assert p._main_runtime == HOST_SNAPSHOT, \
        "a bound _HOST_MAIN_RUNTIME must reach the instance"


# ── standalone / non-host path (fail-soft) ──────────────────────────────────


def test_standalone_empty_snapshot_stays_none(tmp_path, monkeypatch):
    """Standalone acceptance: the host helper returns {} (no main runtime
    in the contextvar, fresh compat mirror) -> register() raises nothing,
    _HOST_MAIN_RUNTIME stays None, instances keep _main_runtime None —
    today's exact LLM-face skip semantics, not a crash."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    fm.register(_fake_ctx())
    assert fm._HOST_MAIN_RUNTIME is None
    p = fm._get_cached_provider("standalone-x")
    assert p._main_runtime is None


def test_standalone_none_return_stays_none(tmp_path, monkeypatch):
    """Same fail-soft path when the helper returns None outright."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: None)
    fm.register(_fake_ctx())
    assert fm._HOST_MAIN_RUNTIME is None


def test_none_module_value_never_clobbers_instance_runtime(tmp_path, monkeypatch):
    """Refresh with a None module value (standalone / pre-register) must
    NOT clobber an already-bound instance runtime — the standalone
    regression gate for the runtime face (mirrors the _llm_ctx rule)."""
    _reset_state()
    p = FileMemProvider.__new__(FileMemProvider)
    p._llm_ctx = None
    bound = dict(HOST_SNAPSHOT)
    p._main_runtime = bound
    fm._HOST_MAIN_RUNTIME = None
    fm._refresh_provider_llm_ctx(p)
    assert p._main_runtime is bound, \
        "None _HOST_MAIN_RUNTIME must not clobber an instance runtime"


# ── import-surface greps (host zero-touch) ─────────────────────────────────


def test_imports_host_normalize_main_runtime_plugin_side_only():
    """The host helper is imported on the plugin side (plugin tree only);
    no host file is touched by construction — the grep proves the import
    surface exists."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "from agent.auxiliary_client import _normalize_main_runtime" in src


def test_broken_host_module_never_crashes_register(tmp_path, monkeypatch):
    """If the host helper is absent (standalone interpreter, older host
    build — the module-level import fallback) register() still succeeds
    and _HOST_MAIN_RUNTIME stays None — fail-soft, zero crash."""
    _reset_state()
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(fm, "_normalize_main_runtime", None, raising=False)
    fm.register(_fake_ctx())
    assert fm._HOST_MAIN_RUNTIME is None
