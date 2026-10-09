"""4d LLM 面 (t_3b00b8c3): per-tick live main_runtime rebind + audit row.

Root cause (orchestrator-钉死, do NOT re-investigate): on a
gateway/api_server-driven profile (4d) register() runs OUTSIDE any host
turn, so the boot-time contextvar _RUNTIME_MAIN_CONTEXT read
(_normalize_main_runtime(None)) is empty -> _HOST_MAIN_RUNTIME stays None
forever -> every consolidation tick dead-skips the LLM faces
(`tick_skip: main_runtime missing`, live evidence in
/root/.hermes/filemem/consolidation_log.jsonl L1249+).

Fix (plugin-side self-sufficient, host ZERO changes):
FileMemProvider._rebind_main_runtime() runs at the TOP of the tick
daemon-thread body (after _refresh_provider_llm_ctx, before the LLM
segments) and re-resolves the host main_runtime LIVE:

  a. host helper `_normalize_main_runtime(None)` read on the tick thread
     -> `tick_live` (the daemon thread inherits its spawner's contextvars
     via spawn_context_thread -> ctx_bound — this is the 4d fix path);
  b. module-level _HOST_MAIN_RUNTIME captured at boot/lazy ->
     `module_fallback`;
  c. both empty -> keep None (the LLM faces' existing `main_runtime
     missing` skip semantics UNCHANGED); a rebind row with source=none is
     still written for self-provability, but it must NOT consume the
     shared throttle slot (the `main_runtime missing` tick_skip row
     written moments later by the LLM faces has to still land).

No-clobber: a current non-None _main_runtime is NEVER overwritten.
Audit: one `main_runtime_rebind` row per outcome, throttled to the same
window as tick_skip (_last_skip_audit_ts / consolidation_interval_min).
provider/model are NAMES ONLY: the host helper's api_key may be a
callable (Entra token provider) — it must never be referenced in the
row (zero disk落盘). Standalone (host helper unimportable) -> ladder
fully empty -> keep current value, no crash.

All tests use synthetic tmp stores; the live /root/.hermes filemem store
is never touched. Host behavior is asserted, never modified.
"""
import json
import pathlib
import threading
import types

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy


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
    fm._normalize_main_runtime = None


def _decay_copy(interval_min: int = 0) -> dict:
    d = copy.deepcopy(DECAY_DEFAULTS)
    d.setdefault("consolidation", {})
    d["consolidation"]["consolidation_interval_min"] = interval_min
    return d


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "store"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in ("preference", "fact", "decision", "task"):
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, store: pathlib.Path,
                   decay: dict, session_id: str = "s-rebind") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = "rebind-test"
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = decay
    p._llm_ctx = None
    p._main_runtime = None
    p._llm_consolidating = False
    p._supersede_confirming = False
    p._alias_mining_in_flight = False
    p._last_consolidation_ts = 0.0
    p._last_skip_audit_ts = 0.0
    p._last_recall_audit_ts = 0.0
    return p


def _cons_log_rows(store: pathlib.Path) -> list:
    p = store / "consolidation_log.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _rebind_rows(store: pathlib.Path) -> list:
    return [r for r in _cons_log_rows(store)
            if r.get("action") == "main_runtime_rebind"]


class _NoStartThread:
    """Test double honoring the host spawn_context_thread contract (an
    UNSTARTED thread the caller .start()s — the RC#1 fix): records the
    target so the suite can run it synchronously instead of racing a
    real daemon thread. .start()/.join() are no-ops by construction —
    the target runs ONCE, deterministically, on the calling thread."""
    def __init__(self, fn, name):
        self.fn = fn
        self.name = name
    def start(self):
        pass
    def join(self, timeout=None):
        pass
    def is_alive(self):
        return False


def _recorded_sct(monkeypatch):
    """Patch agent.memory_provider.spawn_context_thread with the
    no-start recording double (contract-preserving: unstarted thread,
    caller's .start() observed as no-op, target captured for a
    deterministic synchronous run)."""
    import agent.memory_provider as amp
    created = []
    def fake_sct(fn, **kw):
        th = _NoStartThread(fn, kw.get("name"))
        created.append(th)
        return th
    monkeypatch.setattr(amp, "spawn_context_thread", fake_sct)
    return created


def _run_tick_body_once(p, monkeypatch):
    """Execute the _spawn_llm_consolidation _run body ONCE on the calling
    thread (record the no-start thread, run its target synchronously) —
    exercises the rebind wiring exactly as production would,
    deterministically."""
    created = _recorded_sct(monkeypatch)
    # Neutralize every LLM/deterministic downstream segment so the tick
    # body runs only through the rebind + refresh faces (zero host LLM
    # calls, zero fs beyond the tmp store).
    noop = lambda self=None: None
    for meth in ("_llm_consolidate", "_supersede_confirm_candidates",
                 "_mine_alias_candidates", "_observe_consolidate",
                 "_auto_archive_raw_pending", "_auto_prune_raw_pending_archive",
                 "sweep_memories_expired", "_rotate_consolidation_log",
                 "_cap_episodic_sessions"):
        monkeypatch.setattr(FileMemProvider, meth, noop)
    p._spawn_llm_consolidation()
    assert len(created) == 1
    created[0].fn()  # run the _run body synchronously (no .start())
    return created


# ── 1. ladder a hit: tick_live ─────────────────────────────────────────────


def test_tick_live_read_resolves_live_snapshot(tmp_path, monkeypatch):
    """Ladder a hit: the host helper read LIVE on the tick thread returns
    the published dict -> provider._main_runtime is the non-None dict
    after the tick + one rebind audit row with source=tick_live
    (provider/model names present, NO api_key field)."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    live = dict(HOST_SNAPSHOT)
    seen_args = []
    def fake_norm(mr):
        seen_args.append(mr)
        return live
    monkeypatch.setattr(fm, "_normalize_main_runtime", fake_norm)
    _run_tick_body_once(p, monkeypatch)
    assert p._main_runtime == live, "tick_live rebind must bind the live snapshot"
    assert seen_args == [None], "rebind must read the contextvar (arg None), got %r" % seen_args
    rows = _rebind_rows(store)
    assert len(rows) == 1, f"exactly one rebind audit row per rebind, got {rows!r}"
    r = rows[0]
    assert r["source"] == "tick_live"
    assert r["provider"] == "openai" and r["model"] == "host-model-1"
    assert "api_key" not in r, "api_key must never land in the audit row"
    assert r.get("source_profile") == "rebind-test"
    assert r.get("session_id") == "s-rebind"
    assert "ts" in r


def test_tick_live_read_empty_dict_keeps_none_goes_to_fallback_or_none(tmp_path,
                                                                        monkeypatch):
    """Host helper returns {} (empty contextvar — the host's actual
    return for a missing snapshot) -> rebind falls through the ladder:
    module value None -> source=none row, provider._main_runtime stays
    None, LLM faces' skip semantics intact (fail-soft, no crash)."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    _run_tick_body_once(p, monkeypatch)
    assert p._main_runtime is None, "empty ladder must keep None (no clobber to garbage)"
    rows = _rebind_rows(store)
    assert len(rows) == 1 and rows[0]["source"] == "none", rows
    assert "provider" not in rows[0] and "model" not in rows[0], \
        "none row must not carry provider/model names"
    assert "api_key" not in rows[0]


# ── 2. a empty -> b hit: module_fallback ───────────────────────────────────


def test_module_fallback_when_tick_read_empty(tmp_path, monkeypatch):
    """a empty + b hit: the helper returns {} on the tick thread but the
    module-level _HOST_MAIN_RUNTIME (boot/lazy capture) is bound ->
    rebind binds it via its OWN ladder b, audit row
    source=module_fallback.

    Note: the pre-existing top-of-run _refresh_provider_llm_ctx also
    propagates the module value (module inheritance, S1b F3) — it is
    no-op'd here on purpose: this test exercises the rebind's ladder b
    itself (the face this card adds), not the refresh's."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    fallback = dict(HOST_SNAPSHOT)
    monkeypatch.setattr(fm, "_HOST_MAIN_RUNTIME", fallback)
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    monkeypatch.setattr(fm, "_refresh_provider_llm_ctx", lambda prov: None)
    created = _recorded_sct(monkeypatch)
    for meth in ("_llm_consolidate", "_supersede_confirm_candidates",
                 "_mine_alias_candidates", "_observe_consolidate",
                 "_auto_archive_raw_pending", "_auto_prune_raw_pending_archive",
                 "sweep_memories_expired", "_rotate_consolidation_log",
                 "_cap_episodic_sessions"):
        monkeypatch.setattr(FileMemProvider, meth, lambda self=None: None)
    p._spawn_llm_consolidation()
    created[0].fn()
    assert p._main_runtime == fallback, "module fallback must reach the instance"
    rows = _rebind_rows(store)
    assert len(rows) == 1 and rows[0]["source"] == "module_fallback", rows
    assert rows[0]["provider"] == "openai" and rows[0]["model"] == "host-model-1"
    assert "api_key" not in rows[0]


# ── 3. all empty -> keep None + skip + source=none audit row ──────────────


def test_all_empty_keeps_none_and_writes_none_audit_row(tmp_path, monkeypatch):
    """c: both ladder steps empty -> provider._main_runtime stays None
    (the LLM faces keep today's exact skip semantics — the existing
    `main_runtime missing` tick_skip row still lands because the none
    rebind row does NOT consume the throttle slot) + one rebind row
    with source=none."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    # Let the REAL _llm_consolidate run (its `main_runtime missing` gate
    # writes the tick_skip row) — neutralize only the later segments.
    for meth in ("_supersede_confirm_candidates", "_mine_alias_candidates",
                 "_observe_consolidate", "_auto_archive_raw_pending",
                 "_auto_prune_raw_pending_archive", "sweep_memories_expired",
                 "_rotate_consolidation_log", "_cap_episodic_sessions"):
        monkeypatch.setattr(FileMemProvider, meth, lambda self=None: None)
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()
    created[0].fn()  # run the _run body synchronously
    assert p._main_runtime is None
    rows = _rebind_rows(store)
    assert len(rows) == 1 and rows[0]["source"] == "none", rows
    skips = [r for r in _cons_log_rows(store)
             if r.get("action") == "tick_skip" and r.get("reason") == "main_runtime missing"]
    assert skips, "the none rebind must not starve the main_runtime-missing tick_skip row"


# ── 4. no-clobber ───────────────────────────────────────────────────────────


def test_bound_runtime_never_clobbered_by_rebind(tmp_path, monkeypatch):
    """no-clobber: an already-bound non-None _main_runtime survives the
    rebind even when the ladder reads are empty — the rebind is a pure
    'fill when empty' face."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    bound = dict(HOST_SNAPSHOT)
    p._main_runtime = bound
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    _run_tick_body_once(p, monkeypatch)
    assert p._main_runtime is bound, "rebind must never clobber a bound runtime"
    assert _rebind_rows(store) == [], "no-clobber path writes no rebind row"


def test_none_ladder_read_never_clobbers_bound_runtime(tmp_path, monkeypatch):
    """no-clobber (explicit): even a LADDER that 'resolves' to None (a
    empty tick read with no module fallback) must not overwrite an
    already-bound runtime — the current non-None value wins."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    bound = dict(HOST_SNAPSHOT)
    p._main_runtime = bound
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: {})
    p._rebind_main_runtime()
    assert p._main_runtime is bound


# ── 5. api_key (callable) never lands in the audit row ────────────────────


def test_callable_api_key_never_lands_in_audit_row(tmp_path, monkeypatch):
    """The host _normalize_main_runtime may return a body whose api_key is
    a callable (Entra token provider). The audit row records NAMES only —
    the callable must be neither referenced nor serialized into the row
    (zero disk落盘; and serializing a callable would blow up json.dumps)."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    def _token_fn():
        return "CALLABLE-NEVER-LANDS"
    live = dict(HOST_SNAPSHOT)
    live["api_key"] = _token_fn
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: dict(live))
    _run_tick_body_once(p, monkeypatch)
    assert p._main_runtime == live, "the full dict (callable included) binds in memory"
    rows = _rebind_rows(store)
    assert len(rows) == 1 and rows[0]["source"] == "tick_live"
    assert "api_key" not in rows[0], "audit row must not reference the api_key"
    raw = (store / "consolidation_log.jsonl").read_text(encoding="utf-8")
    assert "CALLABLE-NEVER-LANDS" not in raw, "callable key value must never be disk-written"
    assert "openai" in raw and "host-model-1" in raw, "names still land"


# ── 6. throttle: one row per window (same window as tick_skip) ────────────


def test_rebind_rows_throttled_to_skip_window(tmp_path, monkeypatch):
    """interval 30: two ticks within the window -> one rebind row total
    (the second read-only re-hit lands no row). Success rows stamp the
    shared slot; the none row stays read-only (covered by test 3)."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(30))
    live = dict(HOST_SNAPSHOT)
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: live)
    _run_tick_body_once(p, monkeypatch)
    _run_tick_body_once(p, monkeypatch)
    assert len(_rebind_rows(store)) == 1, "one rebind row per window, not per tick"


def test_rebind_success_stamps_shared_slot(tmp_path, monkeypatch):
    """A successful rebind stamps _last_skip_audit_ts (the shared slot)
    so a busy tick loop can't alternate rebind/tick_skip rows every
    second — the flood bound is the tick_skip one."""
    _reset_state()
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(30))
    live = dict(HOST_SNAPSHOT)
    monkeypatch.setattr(fm, "_normalize_main_runtime", lambda mr: live)
    p._rebind_main_runtime()
    assert p._last_skip_audit_ts > 0.0, "successful rebind must stamp the throttle slot"
    p._rebind_main_runtime()
    assert len(_rebind_rows(store)) == 1


# ── 7. standalone: host helper absent -> ladder fully empty, no crash ────


def test_standalone_helper_absent_keeps_current_semantics(tmp_path, monkeypatch):
    """Host helper NOT importable (standalone interpreter / older host
    build — the L51 fallback): the ladder is fully empty -> rebind is a
    clean no-op (value stays None, no crash, no row beyond the none row).
    today's exact semantics."""
    _reset_state()
    monkeypatch.setattr(fm, "_normalize_main_runtime", None)
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    p._rebind_main_runtime()
    assert p._main_runtime is None
    rows = _rebind_rows(store)
    assert len(rows) == 1 and rows[0]["source"] == "none", rows


def test_standalone_none_module_value_stays_none_and_skips(tmp_path, monkeypatch):
    """Standalone end-to-end: helper None + module value None -> tick body
    completes fail-soft, provider._main_runtime stays None, and the
    LLM faces' `main_runtime missing` tick_skip still lands (P2 control)."""
    _reset_state()
    monkeypatch.setattr(fm, "_normalize_main_runtime", None)
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(0))
    for meth in ("_supersede_confirm_candidates", "_mine_alias_candidates",
                 "_observe_consolidate", "_auto_archive_raw_pending",
                 "_auto_prune_raw_pending_archive", "sweep_memories_expired",
                 "_rotate_consolidation_log", "_cap_episodic_sessions"):
        monkeypatch.setattr(FileMemProvider, meth, lambda self=None: None)
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()
    created[0].fn()  # run the _run body synchronously
    assert p._main_runtime is None
    skips = [r for r in _cons_log_rows(store)
             if r.get("action") == "tick_skip" and r.get("reason") == "main_runtime missing"]
    assert skips, "standalone tick must still record the main_runtime-missing skip"


# ── 8. grep acceptance: wiring in the tick body ───────────────────────────


def test_rebind_wired_into_tick_body_before_llm_sections():
    """The _run body must call _rebind_main_runtime() on the daemon
    thread, AFTER the ctx refresh and BEFORE _llm_consolidate (the LLM
    sections read the rebind result)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    k = src.index("def _spawn_llm_consolidation")
    i = src.index("def _run():", k)
    j = src.index("self._llm_consolidate()", i)
    seg = src[i:j]
    assert "_refresh_provider_llm_ctx(self)" in seg
    assert "_rebind_main_runtime()" in seg
    assert seg.index("_rebind_main_runtime()") > seg.index("_refresh_provider_llm_ctx(self)"), \
        "rebind must run after the ctx refresh"


def test_rebind_method_uses_host_helper_and_module_fallback():
    """Host zero-touch surface: the method reads through the L51-imported
    host helper (zero hardcoded model names) AND falls back to the
    module-level _HOST_MAIN_RUNTIME (the boot/lazy capture)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    k = src.index("def _rebind_main_runtime")
    j = src.index("def _maybe_spawn_consolidation_tick", k)
    seg = src[k:j]
    assert "_normalize_main_runtime(None)" in seg
    assert "_HOST_MAIN_RUNTIME" in seg
    assert '"tick_live"' in seg and '"module_fallback"' in seg and '"none"' in seg
    assert '"main_runtime_rebind"' in seg


def test_audit_row_has_no_api_key_reference():
    """The rebind audit row builder must never READ the api_key field
    (zero disk落盘 hard constraint — the host helper may return a
    callable key, and json.dumps of a callable would raise). The row
    dict is built from NAMES only; assert no dict/attribute access of
    the api_key field in the method body (prose mentioning the field in
    a comment is allowed — the guarantee is on the CODE)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    k = src.index("def _rebind_main_runtime")
    j = src.index("def _maybe_spawn_consolidation_tick", k)
    seg = src[k:j]
    for pattern in ('["api_key"]', "['api_key']",
                    '.get("api_key"', ".get('api_key'",
                    "row[\"api_key\"]"):
        assert pattern not in seg, \
            f"rebind row builder reads the api_key field via {pattern!r}"


def test_py38_no_bare_pep604_in_rebind():
    """py3.8 compat: the rebind method body must not use bare PEP604
    unions (X | Y) or other 3.10+ syntax (the file already carries
    `from __future__ import annotations`)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    k = src.index("def _rebind_main_runtime")
    j = src.index("def _maybe_spawn_consolidation_tick", k)
    seg = src[k:j]
    assert not ("None |" in seg or "| None" in seg), "py3.8: no bare PEP604 unions"
