"""S1b root-cause fix slice — LTM-fix-epic S1 (root-cause diagnostics -> coder fix).

Blueprint: specs/LTM-fix-epic.md S1 + S1a root-cause adjudication. The three
documented root causes, and the 5 fixes this suite locks:

  RC#1 TRIGGER-FACE BREAK   _spawn_llm_consolidation only reached via the RARE
        on_pre_compress face + the `if not drafts: return None`-gated
        on_session_end face => 3 production days, 0 ticks, C 段 never ran,
        archive/ empty.
        -> F1 interval-throttled post_llm_call tick spawn
           (_maybe_spawn_consolidation_tick, decay key consolidation_interval_min
           default 30, instance _last_consolidation_ts, reused in-flight guard).
           + the deepest fix: host spawn_context_thread returns an UNSTARTED
           thread and the legacy line never called .start() => the tick body
           (all 7 LLM faces + C auto-archive + prune) NEVER ran. .start() added.
  RC#2 LLM-BINDING BREAK    _llm_ctx was written in exactly 2 places (init None
        / register -> only the 'default' instance). Per-session instances built
        by _get_cached_provider kept _llm_ctx None forever => even a triggered
        tick silently skipped all 7 LLM faces.
        -> F3 module-level _HOST_LLM_CTX inheritance (register records it;
           _get_cached_provider propagates it to new instances; the hooks refresh
           their instance's _llm_ctx from it WITHOUT clobbering when None).
  RC#3 REINFORCE 0 SUCCESS  prefetch early-exits before reinforce, and production
        short prompts trivial-gate / 0-hit => _reinforce_recall never truly bumped
        reference_count, and there was no skip reason to query.
        -> F5 low-frequency recall_audit event (hits / reinforced / reason_trivial)
           via decay key recall_audit_interval_min (default 60).

  Plus: F2 on_session_end spawn relocation to BEFORE the drafts gate (D4(a)
        semantic change: tick is now an UNCONDITIONAL boundary trigger; the
        episodic/draft flow stays drafts-gated and untouched).
        F4 tick_skip audit events at every skip point (in-flight / interval /
           llm_ctx None), throttled by the SAME interval key (30min -> 1 row).

North Star #1 self-proof face: every skip now leaves a bounded, reasoned audit
trail instead of a silent 3-day consolidation_log silence.

ALL stores are tmp synthetic (or __new__ partial providers + monkeypatch). The
live /root/.hermes/filemem store is never touched. Zero host-package changes:
the only host-adjacent behavior exercised is the pre-existing
spawn_context_thread contract (unstarted thread; caller starts) — asserted,
not modified.
"""
import json
import copy
import pathlib
import threading
import types
import time
from datetime import datetime, timezone

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES, MemoryItem  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))


# ── store / provider fixtures (synthetic tmp store, full attrs) ─────────────


def _row(rid, content, mem_type="fact", **extra):
    d = {"id": rid, "type": mem_type, "content": content, "level": "L0",
         "salience": 0.5, "reference_count": 0,
         "created_at": "2026-09-15T00:00:00", "updated_at": "2026-09-15T00:00:00"}
    d.update(extra)
    return d


def _noop_router():
    """Lightweight trigger-router stand-in: detects no tier (returns no level)
    so sync_turn's trigger scan does nothing (no L0 fallback save is fired)."""
    r = types.SimpleNamespace()
    r.detect_and_route = lambda content: {"level": None}
    return r


def _make_store(tmp: pathlib.Path, rows_by_type: dict, raw_pending_rows: list = None) -> pathlib.Path:
    store = tmp / "store"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export", "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps(rows_by_type.get(t, []), ensure_ascii=False, indent=2),
            encoding="utf-8")
    if raw_pending_rows is not None:
        (store / "knowledge" / "raw_pending.json").write_text(
            json.dumps(raw_pending_rows, ensure_ascii=False), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, store: pathlib.Path,
                   decay: dict, llm_ctx=None, session_id: str = "s-test") -> FileMemProvider:
    """FULL synthetic provider (no initialize): every attribute sync_turn /
    on_session_end / the tick gate / the audit helpers read is wired so the
    REAL production paths run against a tmp store — no host, no live data."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = "s1b-test"
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = store / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = decay
    p._llm_ctx = llm_ctx
    p._llm_consolidating = False
    p._supersede_confirming = False
    p._alias_mining_in_flight = False
    p.trigger_router = _noop_router()
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p._turn_marker_index = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._last_consolidation_ts = 0.0
    p._last_skip_audit_ts = 0.0
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {"reinforced": 0, "durable_hits": 0}
    p.min_search_score = 0.3
    p.auto_extract = False
    p._turn_self_test = False
    p._turn_canary = False
    # T4 turn-report line stub (prefetch's _pending_draft_notice composes it;
    # a no-op keeps the recall context pure without pulling in the panel face).
    p._turn_report_line = lambda sid: ""
    return p


def _decay_copy(interval_min: int = 30, recall_min: int = 60) -> dict:
    """A fresh deep copy of DECAY_DEFAULTS (suite-order immune) with the two
    S1b interval keys explicitly set."""
    d = copy.deepcopy(DECAY_DEFAULTS)
    d.setdefault("consolidation", {})
    d["consolidation"]["consolidation_interval_min"] = interval_min
    d["consolidation"]["recall_audit_interval_min"] = recall_min
    # F4 C-段 test knobs (bounded archive) are applied per-test, not baked.
    return d


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


def _spawn_capture(p) -> list:
    """Return a list that records EVERY _spawn_llm_consolidation call so the
    tests can assert spawn vs skip (the North Star self-proof face)."""
    cap = []
    real = p._spawn_llm_consolidation

    def rec():
        cap.append(time.time())
        return real()

    p._spawn_llm_consolidation = rec
    return cap


def _recorded_sct(monkeypatch):
    """Patch agent.memory_provider.spawn_context_thread to return a REAL
    (unstarted) threading.Thread and record it, so tests can .join() it.
    Mirrors the host contract: the fake returns an unstarted thread and the
    production .start() (the fix under test) is what actually runs _run."""
    import agent.memory_provider as amp
    created = []
    def fake_sct(fn, **kw):
        th = threading.Thread(target=fn, daemon=True, name=kw.get("name", "fm-s1b"))
        created.append(th)
        return th
    monkeypatch.setattr(amp, "spawn_context_thread", fake_sct)
    return created


# ── F1: interval-throttled tick spawn ────────────────────────────────────────


def test_f1_sync_turn_interval_zero_spawns(tmp_path):
    """interval_min=0 => every turn's post_llm_call face spawns a tick
    (in-flight guard is the only bounder). Two sync_turns -> two spawns."""
    store = _make_store(tmp_path, {"fact": [_row("f1", "server runs on port 8443")]})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=0, recall_min=0))
    cap = _spawn_capture(p)
    p.sync_turn("note: server runs on port 8443 in staging", "got it",
                 session_id="s1b", messages=[])
    p.sync_turn("note: deploy window moved to friday 09:00 utc", "ok",
                 session_id="s1b", messages=[])
    assert len(cap) == 2, f"interval 0 must spawn every turn, got {len(cap)} spawns"
    # interval stamp is made by the spawn itself -> advances both times
    assert p._last_consolidation_ts > 0


def test_f1_sync_turn_large_interval_no_second_spawn(tmp_path, monkeypatch):
    """interval_min=30: first turn spawns (stamp 0.0 -> eligible), the
    in-window second turn does NOT spawn and instead leaves a throttled
    tick_skip(interval) audit row — one row, not one-per-turn.

    F4 throttle note: the first spawn's thread writes its OWN
    tick_skip(llm_ctx None) row AND stamps the shared _last_skip_audit_ts
    window, which would throttle the gate's interval-skip row. Join that
    thread first, then clear the window so the interval row is auditable
    (throttle semantics are kept intact, only the shared window is reset
    between the two distinct skip classes)."""
    store = _make_store(tmp_path, {"fact": [_row("f1", "server runs on port 8443")]})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=30))
    cap = _spawn_capture(p)
    created = _recorded_sct(monkeypatch)
    p.sync_turn("note: server runs on port 8443 in staging", "got it",
                 session_id="s1b", messages=[])
    assert len(cap) == 1, "first turn (stamp 0.0) must spawn"
    assert len(created) == 1
    created[0].join(timeout=10.0)
    # The thread's llm_ctx-None skip row consumed the 30-min skip-audit
    # window; clear it so the gate's interval-skip row lands (F4 kept).
    p._last_skip_audit_ts = 0.0
    p.sync_turn("note: deploy window moved to friday 09:00 utc", "ok",
                 session_id="s1b", messages=[])
    assert len(cap) == 1, "in-window second turn must NOT spawn"
    skips = [r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"]
    assert [r["reason"] for r in skips] == ["main_runtime missing", "interval"], skips


def test_f1_in_flight_guard_no_respawn(tmp_path):
    """Reused in-flight guard: a tick already running (_llm_consolidating)
    wins — the gate audits tick_skip(in_flight) and never respawns."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=0))
    cap = _spawn_capture(p)
    p._llm_consolidating = True
    p.sync_turn("note: server runs on port 8443 in staging", "got it",
                 session_id="s1b", messages=[])
    assert len(cap) == 0, "in-flight tick must not be duplicated"
    skips = [r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"]
    assert len(skips) == 1 and skips[0]["reason"] == "in_flight", skips


def test_f1_decay_json_defaults_key_parity():
    """decay.json <-> DECAY_DEFAULTS key parity for the two S1b throttle keys
    (both files carry the same default: interval 30, recall_audit 60)."""
    dj = DECAY_JSON.get("consolidation", {})
    dd = DECAY_DEFAULTS.get("consolidation", {})
    assert dj.get("consolidation_interval_min") == dd.get("consolidation_interval_min") == 30
    assert dj.get("recall_audit_interval_min") == dd.get("recall_audit_interval_min") == 60


# ── F1+RC#1 deepest fix: spawn_context_thread .start() contract ─────────────


def test_f1_spawn_thread_is_started(monkeypatch, tmp_path):
    """The engine's spawn_context_thread returns an UNSTARTED thread (the
    caller must .start() it — host contract, see agent/credits_tracker.py
    `.start()` and memory_manager). The legacy line created-but-never-started
    the thread => the tick body (7 LLM faces + C/prune) NEVER ran in
    production. This locks the .start() fix on the REAL host path: the
    spawned thread actually runs the consolidation _run body to completion
    (C auto-archive fires over a synthetic over-cap pool)."""
    store = _make_store(
        tmp_path,
        {"fact": [_row(f"f{i}", f"fact number {i} content") for i in range(5)]},
        raw_pending_rows=[{"topic": f"t{i}", "content": f"raw {i}", "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}
                          for i in range(5)],
    )
    decay = _decay_copy(interval_min=0)
    decay["knowledge"]["raw_pending_max_rows"] = 3
    p = _make_provider(tmp_path, store, decay, llm_ctx=None, session_id="s-start")
    created = _recorded_sct(monkeypatch)
    p._maybe_spawn_consolidation_tick()
    assert len(created) == 1, "interval 0 + not in-flight => the gate must spawn"
    th = created[0]
    # The gate's spawn .start()ed the host-created thread (the fix under test).
    # Wait for the daemon body to finish the 7-segment tick.
    th.join(timeout=10.0)
    assert not th.is_alive(), "consolidation tick thread must run to completion"
    # C 段 real face: 5 raw rows, max 3 => the overflow rows auto-archived.
    arch_files = list((store / "knowledge" / "archive").glob("raw_pending_auto_*.jsonl"))
    assert arch_files, "C 段 auto-archive must have fired inside the REAL tick body"
    kept = json.loads((store / "knowledge" / "raw_pending.json").read_text(encoding="utf-8"))
    assert len(kept) == 3, "pool bounded to raw_pending_max_rows after the tick"


# ── F2: on_session_end spawn relocation (before the drafts gate) ────────────


def test_f2_no_drafts_tick_still_spawns(tmp_path):
    """RC#1 face: a session ending with ZERO pending drafts (the common
    production case) now still fires the consolidation tick — D4(a) semantic
    change: tick is an UNCONDITIONAL boundary trigger. The episodic write
    stays drafts-gated (a draft-less session has nothing to summarize)."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy())
    cap = _spawn_capture(p)
    ep_calls = []
    p._write_episodic_entry = lambda *a, **kw: ep_calls.append(a)
    p._session_drafts["s-none"] = []  # empty draft list -> the old early exit
    ret = p.on_session_end([])
    assert ret is None, "no drafts -> no auto-confirm prompt (draft flow unchanged)"
    assert len(cap) == 1, "the tick MUST spawn even with zero drafts"
    assert len(ep_calls) == 0, "episodic write stays drafts-gated (unchanged)"


def test_f2_drafts_flow_unchanged(tmp_path):
    """With drafts present the ENTIRE draft flow is untouched: prompt
    generated, episodic entry written — and the tick now spawns BEFORE the
    gate (not inside the drafts branch). Order locked: spawn, then episodic."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(), session_id="s-drafts")
    p._session_drafts["s-drafts"] = [
        MemoryItem(id="d1", type="fact", content="deploy note: use the staging endpoint",
                   level="L0", salience=0.8),
    ]
    order = []
    real_spawn = p._spawn_llm_consolidation
    p._spawn_llm_consolidation = lambda: (order.append("spawn"), real_spawn())

    def ep_spy(*a, **kw):
        order.append("episodic")
    p._write_episodic_entry = ep_spy
    ret = p.on_session_end([])
    assert isinstance(ret, str) and "Session Summary" in ret, "drafts -> prompt still built"
    # spawn fires BEFORE the episodic write on the draft path (F2 semantics):
    assert order == ["spawn", "episodic"], f"order must be spawn-then-episodic, got {order}"


def test_f2_spawn_moved_before_gate_in_source():
    """Grep acceptance: the on_session_end spawn call sits BEFORE the
    `if not drafts: return None` gate, and the OLD in-branch spawn is gone
    (exactly one _spawn_llm_consolidation call inside on_session_end, in
    the pre-gate block)."""
    lines = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8").splitlines()
    m = next((i for i, l in enumerate(lines) if "def on_session_end" in l), None)
    assert m is not None, "on_session_end not found"
    # Bound the scan to the on_session_end METHOD BODY (up to the next
    # dedented def) so later methods' spawn calls cannot overwrite spawn_line.
    def_indent = len(lines[m]) - len(lines[m].lstrip())
    end = len(lines)
    for j in range(m + 1, len(lines)):
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= def_indent and l.lstrip().startswith("def "):
            end = j
            break
    body = lines[m:end]
    spawn_line = None
    gate_line = None
    for j, line in enumerate(body):
        if "_spawn_llm_consolidation()" in line and "def " not in line:
            spawn_line = j
        if gate_line is None and "if not drafts:" in line:
            gate_line = j
    assert spawn_line is not None, "the on_session_end tick spawn call is missing"
    assert gate_line is not None, "the drafts gate is missing"
    assert spawn_line < gate_line, f"spawn (offset {spawn_line}) must precede the gate (offset {gate_line})"
    # exactly ONE spawn call remains inside on_session_end (the old in-branch one removed)
    assert sum("_spawn_llm_consolidation()" in l and "def " not in l for l in body) == 1


# ── F3: _HOST_LLM_CTX module-level inheritance ──────────────────────────────


def _reset_provider_cache():
    with fm._cache_lock:
        fm._provider_cache.clear()
        fm._HOST_LLM_CTX_BY_HOME.clear()
        fm._HOST_MAIN_RUNTIME_BY_HOME.clear()
    fm._HOST_LLM_CTX = None
    fm._HOST_MAIN_RUNTIME = None


def test_f3_register_new_session_instance_gets_ctx(tmp_path, monkeypatch):
    """After register(ctx), a NEW per-session instance built by
    _get_cached_provider inherits the module-level _HOST_LLM_CTX — the RC#2
    binding break is closed (pre-S1b every non-'default' instance kept
    _llm_ctx None forever)."""
    _reset_provider_cache()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    # isolate _resolve_storage_root to a tmp store (never touch the live root)
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                        lambda self: str(tmp_path / "store"))
    fake_ctx = types.SimpleNamespace(
        register_memory_provider=lambda prov: None,
        register_hook=lambda name, fn: None,
        register_system_prompt_section=lambda id, content: None,
    )
    fm.register(fake_ctx)
    # The 'default' instance got the ctx directly...
    default_p = fm._get_cached_provider("default")
    assert default_p._llm_ctx is fake_ctx
    # ...and a freshly created per-session instance inherits it (the RC#2 fix):
    sess_p = fm._get_cached_provider("session-42")
    assert sess_p is not default_p
    assert sess_p._llm_ctx is fake_ctx, "new session instance must inherit _HOST_LLM_CTX"
    assert fm._HOST_LLM_CTX is fake_ctx


def test_f3_standalone_pre_register_instance_unchanged(tmp_path, monkeypatch):
    """Pre-register (no _HOST_LLM_CTX) a standalone instance keeps
    _llm_ctx None — today's exact behavior: the 7 LLM segments SKIP, they
    never error (RC#2 standalone regression gate)."""
    _reset_provider_cache()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: str(tmp_path / "store"))
    p = fm._get_cached_provider("standalone-1")
    assert p._llm_ctx is None, "no register() => _llm_ctx stays None (standalone unchanged)"
    # The 7-segment skip behavior: ctx-None head LLM section is a clean skip
    # (not an error) — it now ALSO leaves a throttled tick_skip audit row.
    store = tmp_path / "store"
    p._decay = _decay_copy()
    p._llm_consolidate()  # must NOT raise (standalone skip is not an error)
    skips = [r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"]
    assert any(r["reason"] == "main_runtime missing" for r in skips), skips


def test_f3_hook_refresh_does_not_clobber_existing_binding(tmp_path, monkeypatch):
    """_refresh_provider_llm_ctx only OVERWRITES _llm_ctx when the module
    value is non-None (a real host ctx bound at register()). When
    _HOST_LLM_CTX is None (standalone / pre-register) it is a no-op — an
    already-bound per-instance ctx is never clobbered by a None module value."""
    _reset_provider_cache()
    p = _make_provider(tmp_path, _make_store(tmp_path, {}), _decay_copy())
    bound = types.SimpleNamespace(marker="instance-bound")
    p._llm_ctx = bound
    # module value None => refresh must NOT clobber the instance binding
    fm._refresh_provider_llm_ctx(p)
    assert p._llm_ctx is bound, "None _HOST_LLM_CTX must not clobber an instance binding"
    # a real module value DOES propagate (the post-register refresh path)
    host = types.SimpleNamespace(marker="host")
    monkeypatch.setattr(fm, "_HOST_LLM_CTX", host)
    fm._refresh_provider_llm_ctx(p)
    assert p._llm_ctx is host, "a bound _HOST_LLM_CTX must reach the instance"
    fm._HOST_LLM_CTX = None


# ── F4: tick_skip audit events (self-proof face) ────────────────────────────


def test_f4_ctx_none_llm_skip_but_c_section_runs(tmp_path):
    """North Star #1 + F4: ctx-None skips the LLM face but the SAME tick's
    C/prune (zero-LLM) section still runs — archive rows move. The skip is
    also SELF-PROVING: a tick_skip(llm_ctx None) audit row is written.
    Assert the deterministic archive face, not just the audit row."""
    store = _make_store(
        tmp_path,
        {},
        raw_pending_rows=[{"topic": f"t{i}", "content": f"raw {i}", "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}
                          for i in range(5)],
    )
    decay = _decay_copy(interval_min=0)
    decay["knowledge"]["raw_pending_max_rows"] = 3
    p = _make_provider(tmp_path, store, decay, llm_ctx=None, session_id="s-c")
    # Run the full tick body synchronously (what the daemon thread would run):
    p._llm_consolidate()  # ctx None -> clean LLM-face skip + tick_skip audit
    p._auto_archive_raw_pending()  # C 段 (zero LLM) — MUST run regardless
    kept = json.loads((store / "knowledge" / "raw_pending.json").read_text(encoding="utf-8"))
    assert len(kept) == 3, "C 段 archive must bound the pool even when ctx is None"
    assert list((store / "knowledge" / "archive").glob("raw_pending_auto_*.jsonl"))
    skips = [r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"]
    assert any(r["reason"] == "main_runtime missing" for r in skips), skips


def test_f4_tick_skip_throttle_one_per_window(tmp_path):
    """F4 throttle: the tick_skip AUDIT row itself is bounded to ONE per
    consolidation_interval_min window (shares _last_skip_audit_ts) — a
    busy 30-minute loop must not flood consolidation_log. interval_min=30,
    two in-window skips => exactly 1 row."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=30))
    cap = _spawn_capture(p)
    p._llm_consolidating = True  # force the in_flight skip path, every turn
    p.sync_turn("note: first turn in window", "ok", session_id="s-thr", messages=[])
    n1 = len([r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"])
    p.sync_turn("note: second turn in window", "ok", session_id="s-thr", messages=[])
    n2 = len([r for r in _cons_log_rows(store) if r.get("action") == "tick_skip"])
    assert n1 == 1, "first in-window skip writes exactly one tick_skip row"
    assert n2 == 1, "second in-window skip must be throttled (no 2nd row)"
    assert len(cap) == 0, "in-flight guard must block both spawns"


def test_f4_skip_events_distinct_reasons(tmp_path):
    """F4 self-proof: each skip CLASS emits a DISTINCT, queryable reason
    (in_flight / interval / llm_ctx None) — the audit face that was missing.
    The 30min cap: at most 1 tick_skip row within one interval window."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=0))  # 0 = no audit throttle
    # in_flight skip
    p._llm_consolidating = True
    p._maybe_spawn_consolidation_tick()
    # interval skip
    p._llm_consolidating = False
    p._last_consolidation_ts = time.time()
    p._decay["consolidation"]["consolidation_interval_min"] = 30
    p._last_skip_audit_ts = 0.0  # reset the skip-audit throttle so 'interval' can fire
    p._maybe_spawn_consolidation_tick()
    # llm_ctx None skip (head LLM section)
    p._decay["consolidation"]["consolidation_interval_min"] = 0
    p._last_consolidation_ts = 0.0
    p._llm_consolidate()
    reasons = sorted(r["reason"] for r in _cons_log_rows(store) if r.get("action") == "tick_skip")
    assert reasons == ["in_flight", "interval", "main_runtime missing"], reasons
    assert all("ts" in r and "source_profile" in r for r in _cons_log_rows(store)
               if r.get("action") == "tick_skip")


# ── F5: recall_audit events ──────────────────────────────────────────────────


def test_f5_recall_audit_success_shape(tmp_path):
    """F5 shape on the SUCCESS path (hits>0): one recall_audit row carries
    hits / reinforced / reason_trivial / durable_hits, reinforced reflecting
    the REAL _reinforce_recall bump count (North Star #3 self-proof:
    reference_count moving)."""
    store = _make_store(tmp_path, {"fact": [_row("f1", "server runs on port 8443")],
                                    "preference": [_row("p1", "prefer concise answers",
                                                         mem_type="preference")]})
    p = _make_provider(tmp_path, store, _decay_copy(recall_min=0))
    hits = [{"id": "f1", "type": "fact", "content": "server runs on port 8443"},
            {"id": "p1", "type": "preference", "content": "prefer concise answers"}]
    p.search_memories = lambda *a, **kw: list(hits)
    # Call the REAL _reinforce_recall to exercise the counter (it bumps both
    # durable rows on disk -> reinforced=2, durable_hits=2):
    real = p._reinforce_recall
    real(hits)
    assert p._last_recall_stats["reinforced"] == 2, p._last_recall_stats
    p._audit_recall(len(hits), reason_trivial=False)  # what prefetch does post-reinforce
    ra = [r for r in _cons_log_rows(store) if r.get("action") == "recall_audit"]
    assert len(ra) == 1, ra
    row = ra[0]
    assert row["hits"] == 2 and row["reinforced"] == 2 and row["reason_trivial"] is False
    assert row["durable_hits"] == 2 and row["session_id"] == "s-test" and "ts" in row


def test_f5_recall_audit_trivial_and_zero_hit(tmp_path):
    """F5 branches: a trivial prompt (reason_trivial=True) and a 0-hit
    early-exit (reason_trivial=False) each leave one recall_audit row
    (interval_min=0 => no throttle, both visible); forced 0/0 counters on
    the 0-hit paths (no stale inherited stats)."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(recall_min=0))
    p.search_memories = lambda *a, **kw: []
    p._reinforce_recall = lambda results: None
    # seed stale stats that the 0-hit path must NOT inherit:
    p._last_recall_stats = {"reinforced": 7, "durable_hits": 7}
    p.prefetch("ok", session_id="s-test")              # trivial gate
    p.prefetch("kubernetes ingress 502", session_id="s-test")  # 0-hit early exit
    ra = [r for r in _cons_log_rows(store) if r.get("action") == "recall_audit"]
    assert len(ra) == 2, ra
    trivial = [r for r in ra if r["reason_trivial"] is True]
    zero = [r for r in ra if r["reason_trivial"] is False]
    assert len(trivial) == 1 and trivial[0]["hits"] == 0 and trivial[0]["reinforced"] == 0
    assert len(zero) == 1 and zero[0]["hits"] == 0 and zero[0]["reinforced"] == 0


def test_f5_recall_audit_throttle(tmp_path):
    """F5 throttle: recall_audit is bounded to ONE row per
    recall_audit_interval_min window (default 60) — a busy recall loop
    never floods consolidation_log. Two in-window audits => 1 row."""
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(recall_min=60))
    p.search_memories = lambda *a, **kw: []
    p._reinforce_recall = lambda results: None
    p.prefetch("ok", session_id="s-test")
    p.prefetch("ok", session_id="s-test")
    ra = [r for r in _cons_log_rows(store) if r.get("action") == "recall_audit"]
    assert len(ra) == 1, f"60min throttle must cap recall_audit to 1 row, got {len(ra)}"


# ── regression: host tick 7-segment fail-soft (the faces S1b must not break) ─


def test_reg_tick_body_fail_soft_all_green(tmp_path, monkeypatch):
    """Regression gate: with the LLM ctx present but every LLM section
    degrading (no episodic pool / no supersede candidates / no alias topics /
    no knowledge rows), the 7-segment tick body still runs clean end-to-end
    on the REAL host spawn path — no segment raises and the C/prune face
    still does its zero-LLM work. S1b must not regress the fail-soft
    contract the S2 baseline locked."""
    store = _make_store(
        tmp_path,
        {},  # empty memory pool -> no episodic / no candidates
        raw_pending_rows=[{"topic": "t", "content": "raw",
                           "created_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")}],
    )
    decay = _decay_copy(interval_min=0)
    fake_ctx = types.SimpleNamespace(llm=None)  # ctx present but LLM unavailable
    p = _make_provider(tmp_path, store, decay, llm_ctx=fake_ctx, session_id="s-reg")
    created = _recorded_sct(monkeypatch)
    p._maybe_spawn_consolidation_tick()
    assert len(created) == 1
    th = created[0]
    th.join(timeout=10.0)
    assert not th.is_alive(), "fail-soft tick must run to completion (no segment raised)"
    # ctx present but llm=None -> the LLM face is a clean no-op; C face ran:
    assert (store / "knowledge" / "archive").exists()


def test_reg_prefetch_reinforce_regression(tmp_path):
    """Regression: prefetch still returns context (recall unchanged) even
    with the new F5 audit wiring active; a hit recall reinforces the real
    durable row on disk (reference_count bump) AND the recall context is
    returned. The F5 audit row must never break the recall outcome."""
    store = _make_store(tmp_path, {"fact": [_row("f9", "the api base url is https://api.internal")]})
    p = _make_provider(tmp_path, store, _decay_copy(recall_min=0))
    # Force a deterministic hit: patch the search to return the row, keep the
    # REAL _reinforce_recall (the S1b counter must bump the on-disk row).
    # V2 confirmed gate: the auto face requires metadata.confirmed=True (or a
    # current-session episodic row), so the stubbed hit row carries confirmed.
    row = {"id": "f9", "type": "fact",
           "content": "the api base url is https://api.internal",
           "metadata": {"source_profile": "s1b-test", "confirmed": True}}
    p.search_memories = lambda *a, **kw: [row]
    p._set_bringback_choice("s-test", "clean_start")
    ctx = p.prefetch("api base url internal", session_id="s-test")
    assert "the api base url is https://api.internal" in ctx, ctx
    with open(store / "memories" / "fact.json", encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted[0]["reference_count"] == 1, "F5 must not regress the on-disk bump"


# ── host-package purity: S1b adds zero host-package changes ─────────────────


def test_no_host_package_changes_in_diff():
    """Hard constraint: S1b is plugin-surface only. The changed-file set is
    confined to the plugin tree (__init__.py, consolidation/__init__.py,
    decay.json, tests/); no /usr/local/lib/hermes-agent (or engine workspace)
    path is touched by these edits. (The .start() fix is asserted on the
    PLUGIN call site, not the host function — the host contract is read-only.)"""
    import subprocess
    # 🔴 守卫: 插件目录非 git 仓库时 git diff 无意义 (exit 129) — 之前 check=True
    # 直接抛 CalledProcessError -> 环境性假红 (非 S3/源码缺陷)。skip 转环境性 SKIP,
    # 在 git 托管环境下仍真跑 (宿主包路径守卫不丢)。
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--git-dir"], cwd=str(PLUGIN_DIR),
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.skip("plugin dir is not a git repository (git-diff guard N/A)")
    out = subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=str(PLUGIN_DIR), text=True)
    changed = [l for l in out.splitlines() if l.strip()]
    for c in changed:
        low = c.lower()
        assert "hermes-agent" not in low and "hermes_agent" not in low, \
            f"S1b touched a host package path: {c}"
        assert low.startswith(("__init__.py", "consolidation/", "decay.json",
                                "tests/", "spec", "tools/", "shared_memory.py", "plugin.yaml")), \
            f"unexpected changed path (non-plugin-surface): {c}"


def test_start_fix_present_on_spawn_path():
    """Grep acceptance: the host spawn_context_thread call now .start()s the
    returned (previously-never-started) thread on the consolidation path."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "spawn_context_thread(_run" in src
    idx = src.index("spawn_context_thread(_run")
    # the .start() must follow the call (the RC#1 deepest fix)
    tail = src[idx: idx + 120]
    assert ".start()" in tail, f".start() missing after spawn_context_thread: {tail!r}"


# ── P2: boundary-face tick ctx refresh (S-table functional gap, post-review) ─

def test_p2_boundary_face_tick_refreshes_ctx_from_host(tmp_path, monkeypatch):
    """P2: a provider instance created/cached BEFORE register() has
    _llm_ctx None. When it reaches a BOUNDARY face (on_pre_compress /
    on_session_end -> _spawn_llm_consolidation), the _run body's top
    _refresh_provider_llm_ctx(self) must propagate the module-level
    _HOST_LLM_CTX before the 4 LLM sections run — closing the window
    where the whole tick self-skipped the LLM faces on a stale/None
    ctx (the reviewer S-func-review P2 gap)."""
    _reset_provider_cache()
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=0),
                       llm_ctx=None)
    host_ctx = types.SimpleNamespace(llm=None)
    monkeypatch.setattr(fm, "_HOST_LLM_CTX", host_ctx)
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()  # boundary-face spawn (no interval gate)
    assert len(created) == 1
    th = created[0]
    th.join(timeout=10.0)
    assert not th.is_alive(), "P2 tick body must run to completion (fail-soft)"
    # P2 fix under test: the boundary-face instance now holds the host ctx
    assert p._llm_ctx is host_ctx, "top-of-_run refresh must reach the instance"
    # and the LLM face did NOT self-skip on a stale/None ctx
    skips = [r for r in _cons_log_rows(store)
             if r.get("action") == "tick_skip" and r.get("reason") == "llm_ctx None"]
    assert not skips, f"boundary-face tick still self-skipped the LLM face: {skips}"


def test_p2_standalone_no_host_ctx_still_skips_clean(tmp_path, monkeypatch):
    """P2 control: with _HOST_LLM_CTX None (standalone / pre-register) the
    top-of-_run refresh is a no-op (never clobbers) — the LLM face
    self-skips exactly as before the fix: clean skip + one throttled
    tick_skip audit row, no raise, C sections still run."""
    _reset_provider_cache()
    store = _make_store(tmp_path, {})
    p = _make_provider(tmp_path, store, _decay_copy(interval_min=0),
                       llm_ctx=None)
    monkeypatch.setattr(fm, "_HOST_LLM_CTX", None)
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()
    assert len(created) == 1
    th = created[0]
    th.join(timeout=10.0)
    assert not th.is_alive()
    # standalone behavior unchanged: ctx stays None, LLM face records a clean skip
    assert p._llm_ctx is None
    skips = [r for r in _cons_log_rows(store)
             if r.get("action") == "tick_skip" and r.get("reason") == "main_runtime missing"]
    assert skips, "standalone tick must still record the main_runtime-missing skip"


def test_p2_refresh_present_in_run_body():
    """Grep acceptance: the _run body refreshes the instance ctx from
    _HOST_LLM_CTX at the top, BEFORE the 4 LLM sections run (P2
    boundary-face gap closure — one O(1) module-level call, daemon
    thread, host turn stays zero-blocking)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    k = src.index("def _spawn_llm_consolidation")
    i = src.index("def _run():", k)
    j = src.index("self._llm_consolidate()", i)
    seg = src[i:j]
    assert "_refresh_provider_llm_ctx(self)" in seg, \
        "P2: _run body must refresh the instance ctx before the LLM sections"
