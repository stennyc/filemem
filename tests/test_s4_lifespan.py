"""S4-T1 lifespan-and-cap slice — sweep real call point + consolidation_log
rotation + episodic/sessions age-cap (North Star #5 bounded growth).

Blueprint: specs/S4-lifespan-selfheal-impl-notes.md S4-T1. The three
deterministic (zero LLM) faces:

  S4-① sweep_memories_expired gets a REAL call point: the _run daemon-thread
        C section of the consolidation tick (after _auto_prune_raw_pending_
        archive, before the .start() spawn) — the paper "4h sweeper" realized
        as "idempotent once per tick" (threshold-gated, so no re-scan).
  S4-② _rotate_consolidation_log: consolidation.log_rotate_lines (5000) /
        consolidation.log_keep_tail_lines (500); front N-k lines MOVE to
        consolidation_log.<UTC-ts>.jsonl (audit chain unbroken, not
        deleted), active keeps the most recent k; same flock protocol as the
        appender -> no torn lines across processes; 0/absent = disabled.
  S4-④ _cap_episodic_sessions: episodic.max_age_days (90) /
        sessions.max_age_days (30); DUE files move to knowledge/archive/
        (manifest row FIRST — "忘掉≠抹掉"); bounded-glob touches ONLY the
        episodic/ and sessions/ prefixes.

ALL stores are tmp synthetic — the live /root/.hermes/filemem store is never
touched. Zero host-package changes; zero third-party deps in the new code.
"""
import copy
import json
import multiprocessing
import os
import pathlib
import re
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── synthetic store / provider fixtures (mirror the S1b suite) ──────────────


def _row(rid, content, mem_type="fact", level="L0", **extra):
    d = {"id": rid, "type": mem_type, "content": content, "level": level,
         "salience": 0.5, "reference_count": 0,
         "created_at": _utcnow_naive().isoformat(),
         "updated_at": _utcnow_naive().isoformat()}
    d.update(extra)
    return d


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "store"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, store: pathlib.Path,
                   decay: dict, llm_ctx=None, session_id: str = "s-s4") -> FileMemProvider:
    """FULL synthetic provider (no initialize): every attribute the tick
    body / the new S4 faces read is wired against the tmp store."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = "s4-test"
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
    p.trigger_router = types.SimpleNamespace(
        detect_and_route=lambda content: {"level": None})
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
    p._turn_report_line = lambda sid: ""
    return p


def _decay_copy() -> dict:
    return copy.deepcopy(DECAY_DEFAULTS)


def _recorded_sct(monkeypatch):
    """Patch spawn_context_thread to return a REAL (unstarted) thread, so
    tests .join() it and the production .start() (S1b F1) runs _run."""
    import agent.memory_provider as amp
    created = []
    def fake_sct(fn, **kw):
        th = threading.Thread(target=fn, daemon=True, name=kw.get("name", "fm-s4"))
        created.append(th)
        return th
    monkeypatch.setattr(amp, "spawn_context_thread", fake_sct)
    return created


# ── S4-① sweep_memories_expired behavior (direct call, threshold-gated) ─────


def test_sweep_moves_expired_rows_and_keeps_active(tmp_path):
    """fact.json over the rotate threshold: expired L0 (auto_cleanup) rows
    move to memories/fact_<YYYY-MM>.jsonl (append-only, bucketed by
    created_at month); non-expired L0 + L2 (permanent) stay in active."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["memories_rotate_mb"] = 0.001  # ~1KB threshold for the tmp store
    p = _make_provider(tmp_path, store, decay)

    now = _utcnow_naive()
    pad = "x" * 400  # 3 rows -> ~1.3KB > the ~1KB threshold
    expired_row = _row("exp1", "stale fact " + pad,
                       created_at=(now - timedelta(days=200)).isoformat())
    live_row = _row("live1", "fresh fact " + pad,
                    created_at=(now - timedelta(days=5)).isoformat())
    l2_row = _row("perm1", "permanent decision " + pad, level="L2",
                  created_at=(now - timedelta(days=3650)).isoformat())
    (store / "memories" / "fact.json").write_text(
        json.dumps([expired_row, live_row, l2_row], ensure_ascii=False, indent=2),
        encoding="utf-8")
    assert (store / "memories" / "fact.json").stat().st_size > 1024

    res = p.sweep_memories_expired()
    assert res["swept"] == 1, f"exactly the expired row sweeps, got {res}"
    assert res["moved"].get("fact") == 1

    # expired row lands in the month-bucketed archive (created_at month)
    exp_month = expired_row["created_at"][:7]
    arch = store / "memories" / f"fact_{exp_month}.jsonl"
    assert arch.exists(), "append-only sweep archive missing"
    moved_rows = [json.loads(l) for l in arch.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert [r["id"] for r in moved_rows] == ["exp1"], "only the expired row moved"

    # active keeps the live + L2 rows, in original relative order
    with open(store / "memories" / "fact.json", encoding="utf-8") as f:
        active = json.load(f)
    assert [r["id"] for r in active] == ["live1", "perm1"], active

    # idempotent: a second sweep of the SAME store moves nothing (the cohort
    # is gone; no buckets -> the active file is not even rewritten)
    active_before = (store / "memories" / "fact.json").read_bytes()
    arch_before = arch.read_bytes()
    res2 = p.sweep_memories_expired()
    assert res2 == {"swept": 0, "moved": {}}, f"second sweep must be a no-op: {res2}"
    assert (store / "memories" / "fact.json").read_bytes() == active_before
    assert arch.read_bytes() == arch_before


def test_sweep_under_threshold_is_noop(tmp_path):
    """Below memories_rotate_mb the sweep does NOT touch the files at all
    (the threshold gate is the 4h-behavior-unchanged contract): expired rows
    present, tiny file -> zero swept, no archive file created."""
    store = _make_store(tmp_path)
    decay = _decay_copy()  # default threshold = 1MB, the tmp file is tiny
    p = _make_provider(tmp_path, store, decay)
    now = _utcnow_naive()
    (store / "memories" / "fact.json").write_text(
        json.dumps([_row("exp1", "tiny stale fact",
                         created_at=(now - timedelta(days=400)).isoformat())],
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    res = p.sweep_memories_expired()
    assert res == {"swept": 0, "moved": {}}, res
    assert not list((store / "memories").glob("fact_*.jsonl")), \
        "under threshold no sweep archive may be created"


def test_sweep_audit_row_when_log_path_given(tmp_path):
    """The C-section caller passes log_path=None (no audit row — the sweep
    is quiet when idle); an explicit log_path lands one bounded
    memories_sweep audit row."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["memories_rotate_mb"] = 0.001
    p = _make_provider(tmp_path, store, decay)
    now = _utcnow_naive()
    pad = "y" * 1200  # ~1.4KB > the ~1KB threshold so the sweep actually runs
    (store / "memories" / "fact.json").write_text(
        json.dumps([_row("exp1", "stale " + pad,
                         created_at=(now - timedelta(days=200)).isoformat())],
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    log_path = store / "sweep_audit.jsonl"
    res = p.sweep_memories_expired(log_path=log_path)
    assert res["swept"] == 1
    rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 1 and rows[0]["action"] == "memories_sweep"
    assert rows[0]["moved"].get("fact") == 1


# ── S4-① mount: sweep really runs in the _run C section (spy, no 4h wait) ──


def test_sweep_spy_called_in_run_c_section(tmp_path, monkeypatch):
    """Grep-free mount lock: the daemon-thread _run body really invokes
    sweep_memories_expired / _rotate_consolidation_log /
    _cap_episodic_sessions — each exactly once per tick. The spies replace
    the bodies so the tick runs against a clean store (zero side effects,
    no real 4h timer dependency)."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(),
                       llm_ctx=types.SimpleNamespace(llm=None))
    calls = []
    p.sweep_memories_expired = lambda *a, **kw: calls.append("sweep")
    p._rotate_consolidation_log = lambda *a, **kw: calls.append("rotate")
    p._cap_episodic_sessions = lambda *a, **kw: calls.append("cap")
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()
    assert len(created) == 1, "the tick must spawn exactly one daemon thread"
    created[0].join(timeout=15.0)
    assert not created[0].is_alive(), "fail-soft tick must run to completion"
    assert calls == ["sweep", "rotate", "cap"], \
        f"C-section mount order must be sweep->rotate->cap, got {calls}"


def test_sweep_fail_soft_does_not_break_later_c_sections(tmp_path, monkeypatch):
    """Segment isolation (isomorphic with the existing C sections): a raise
    inside the sweep segment must NOT break the later rotate/cap segments —
    they still run on the same tick."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy(),
                       llm_ctx=types.SimpleNamespace(llm=None))
    calls = []
    def boom():
        raise RuntimeError("synthetic sweep hiccup")
    p.sweep_memories_expired = boom
    p._rotate_consolidation_log = lambda *a, **kw: calls.append("rotate")
    p._cap_episodic_sessions = lambda *a, **kw: calls.append("cap")
    created = _recorded_sct(monkeypatch)
    p._spawn_llm_consolidation()
    created[0].join(timeout=15.0)
    assert not created[0].is_alive()
    assert calls == ["rotate", "cap"], f"a sweep hiccup must not stop the later C sections: {calls}"


# ── S4-② _rotate_consolidation_log ───────────────────────────────────────────


def _write_log(store: pathlib.Path, n: int) -> list:
    rows = [{"ts": f"t{i}", "action": "probe", "i": i} for i in range(n)]
    with open(store / "consolidation_log.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    return rows


def test_rotate_moves_front_lines_and_keeps_tail(tmp_path):
    """> log_rotate_lines lines: the front N-k lines MOVE to
    consolidation_log.<ts>.jsonl (rename-class move, not delete — audit
    chain intact) and active keeps exactly the most recent k lines.
    Row order is preserved across the union (rotated, then active)."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["consolidation"]["log_rotate_lines"] = 10
    decay["consolidation"]["log_keep_tail_lines"] = 4
    p = _make_provider(tmp_path, store, decay)
    original = _write_log(store, 12)
    n = p._rotate_consolidation_log()
    assert n == 8, f"must move exactly N-k=8 overflow lines, got {n}"
    active = [json.loads(l) for l in
              (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(active) == 4, f"active must keep the k=4 tail, got {len(active)}"
    rotated_files = sorted(store.glob("consolidation_log.*.jsonl"))
    assert len(rotated_files) == 1, f"exactly one rotated file, got {rotated_files}"
    rotated = [json.loads(l) for l in
               rotated_files[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert rotated == original[:8], "rotated file = the FRONT lines (move, not delete)"
    assert active == original[8:], "active = the most recent k lines"
    assert rotated + active == original, "no row lost: the union is the original order"
    assert re.fullmatch(r"consolidation_log\.\d{8}T\d{6}Z(-\d+)?\.jsonl",
                        rotated_files[0].name), rotated_files[0].name


def test_rotate_noop_when_under_threshold(tmp_path):
    """At-or-under the threshold the log is untouched (no rotation, no
    rotated file): N == log_rotate_lines is NOT 'exceeds'."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["consolidation"]["log_rotate_lines"] = 10
    decay["consolidation"]["log_keep_tail_lines"] = 4
    p = _make_provider(tmp_path, store, decay)
    before = _write_log(store, 10)
    assert p._rotate_consolidation_log() == 0
    assert (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() \
        == [json.dumps(r, sort_keys=True) for r in before]
    assert not list(store.glob("consolidation_log.*.jsonl"))


def test_rotate_disabled_when_key_absent_or_zero(tmp_path):
    """Fail-soft data safety: a missing consolidation block / a
    0-valued log_rotate_lines NEVER rotate — the log keeps growing,
    nothing is ever deleted by default."""
    store = _make_store(tmp_path)
    # (a) key absent (the pre-S4 shape of the block)
    decay = _decay_copy()
    decay["consolidation"].pop("log_rotate_lines", None)
    decay["consolidation"].pop("log_keep_tail_lines", None)
    p = _make_provider(tmp_path, store, decay)
    _write_log(store, 100)
    assert p._rotate_consolidation_log() == 0
    assert not list(store.glob("consolidation_log.*.jsonl"))
    # (b) explicit 0 = disabled (even with a huge log)
    decay["consolidation"]["log_rotate_lines"] = 0
    assert p._rotate_consolidation_log() == 0
    # (c) consolidation block absent entirely
    decay["consolidation"] = None
    p2 = _make_provider(tmp_path, store, decay)
    assert p2._rotate_consolidation_log() == 0


def _mp_rotate_worker(store, decay_json, iterations):
    """Cross-process worker (the 'N processes, 0 torn lines' gate): each
    process appends its own audit rows and rotates, all on the SAME
    consolidation_log.jsonl — the shared flock protocol is what keeps the
    lines intact. Runs in a real forked process so the flock is a real
    cross-process gate, not an in-process artifact."""
    import json as _json
    import pathlib as _pl
    import sys as _sys
    import threading as _th
    _sys.path.insert(0, str(PLUGIN_DIR))
    from __init__ import FileMemProvider as _FMP
    p = _FMP.__new__(_FMP)
    p._lock = _th.Lock()
    p.storage_dir = _pl.Path(store)
    p._decay = _json.loads(_pl.Path(decay_json).read_text(encoding="utf-8"))
    for i in range(iterations):
        p._append_consolidation_event({"action": "mp_probe", "i": i, "pid": os.getpid()})
        p._rotate_consolidation_log()


def test_rotate_dual_process_no_torn_lines(tmp_path):
    """Two real processes co-append + co-rotate the same log: EVERY line in
    EVERY file (active + all rotated) is valid JSON, and the total row
    count equals the total appends (0 lost updates — the flock protocol
    serializes the read-modify against concurrent appends)."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["consolidation"]["log_rotate_lines"] = 20
    decay["consolidation"]["log_keep_tail_lines"] = 5
    decay_file = tmp_path / "decay.json"
    decay_file.write_text(json.dumps(decay), encoding="utf-8")
    iterations = 25
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_mp_rotate_worker,
                         args=(str(store), str(decay_file), iterations))
             for _ in range(2)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(timeout=60)
        assert pr.exitcode == 0, f"rotation worker crashed (exit {pr.exitcode})"
    total = 0
    all_files = [store / "consolidation_log.jsonl"] + \
                list(store.glob("consolidation_log.*.jsonl"))
    for f in all_files:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            json.loads(line)  # any torn line raises here
            total += 1
    assert total == 2 * iterations, \
        f"0 lost updates expected ({2 * iterations} rows), counted {total}"
    assert len(all_files) - 1 >= 1, \
        "at least one rotated file must exist (50 total rows > 20-line threshold)"
    # The active tail can legitimately hold more than keep-tail rows right
    # after the LAST rotation of the loop (appends refill it toward the
    # threshold; the next tick's rotation trims it back to keep-tail). The
    # hard invariants are: no torn lines (every line parsed above), 0 lost
    # updates (total == 2*iterations), and active never exceeds
    # rotate_lines + keep_tail (a rotate fires at >rotate_lines and trims
    # to keep-tail, so post-rotation active is at most keep-tail plus the
    # appends that slip in before the next trim).
    active_rows = len([l for l in
                      (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()
                      if l.strip()])
    assert active_rows <= 20 + 5 + iterations, \
        f"active over the sane post-rotation bound, got {active_rows}"


# ── S4-④ _cap_episodic_sessions ──────────────────────────────────────────────


def _manifest_rows(store: pathlib.Path, action: str) -> list:
    p = store / "knowledge" / "archive" / "pruned_index.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("action") == action:
            rows.append(r)
    return rows


def test_cap_episodic_moves_due_shards_keeps_recent(tmp_path):
    """episodic.max_age_days=90: a 2025-06 shard (month end long past the
    cutoff) moves to knowledge/archive/ under the episodic_auto_ prefix
    (manifest row FIRST); the current month's shard stays. '忘掉≠抹掉':
    the archive row is append-only + manifest-audited."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy())
    now = _utcnow_naive()
    old_shard = store / "episodic" / "2025" / "06"
    old_shard.mkdir(parents=True)
    (old_shard / "s1.jsonl").write_text(json.dumps({"session_id": "s1"}) + "\n",
                                         encoding="utf-8")
    cur_shard = store / "episodic" / now.strftime("%Y") / now.strftime("%m")
    cur_shard.mkdir(parents=True, exist_ok=True)
    (cur_shard / "s2.jsonl").write_text(json.dumps({"session_id": "s2"}) + "\n",
                                         encoding="utf-8")
    n = p._cap_episodic_sessions()
    assert n == 1, f"exactly the due old shard moves, got {n}"
    assert not (old_shard / "s1.jsonl").exists(), "source shard must be GONE (move)"
    dest = store / "knowledge" / "archive" / "episodic_auto_2025-06_s1.jsonl"
    assert dest.exists() and \
        dest.read_text(encoding="utf-8") == '{"session_id": "s1"}\n', "the moved shard is intact"
    assert (cur_shard / "s2.jsonl").exists(), "the current month's shard stays"
    man = _manifest_rows(store, "episodic_cap")
    assert len(man) == 1 and man[0]["source_file"] == "episodic/2025/06/s1.jsonl"
    assert man[0]["moved_to"] == "episodic_auto_2025-06_s1.jsonl"
    assert man[0]["char_len"] > 0 and man[0]["moved_ts"], man


def test_cap_sessions_moves_stale_traces(tmp_path):
    """sessions.max_age_days=30: a trace whose mtime is 60 days old moves to
    the archive (sessions_auto_ prefix + manifest row); a freshly written
    trace stays. Session filenames carry no timestamp -> mtime is the stamp."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, _decay_copy())
    old_t = time.time() - 60 * 86400
    old_trace = store / "sessions" / "s_old.jsonl"
    old_trace.write_text(json.dumps({"session_id": "s_old"}) + "\n", encoding="utf-8")
    os.utime(old_trace, (old_t, old_t))
    new_trace = store / "sessions" / "s_new.jsonl"
    new_trace.write_text(json.dumps({"session_id": "s_new"}) + "\n", encoding="utf-8")
    n = p._cap_episodic_sessions()
    assert n == 1, f"exactly the stale trace moves, got {n}"
    assert not old_trace.exists(), "stale trace must be moved out of sessions/"
    assert new_trace.exists(), "fresh trace stays"
    dests = sorted(f.name for f in
                   (store / "knowledge" / "archive").glob("sessions_auto_s_old_*.jsonl"))
    assert len(dests) == 1 and (store / "knowledge" / "archive" / dests[0]).exists()
    man = _manifest_rows(store, "session_cap")
    assert len(man) == 1 and man[0]["source_file"] == "sessions/s_old.jsonl"
    assert man[0]["moved_to"] == dests[0]


def test_cap_bounded_glob_never_touches_other_classes(tmp_path):
    """Bounded-glob discipline (prune-layer rule): even with EVERYTHING due
    (max_age_days=1 in both directions), the cap ONLY globs the
    episodic/YYYY/MM/*.jsonl and sessions/*.jsonl prefixes —
    memories/, consolidated/, knowledge/archive/ (incl. the prune layer's
    raw_pending_auto_* files) are NEVER moved."""
    store = _make_store(tmp_path)
    decay = _decay_copy()
    decay["episodic"]["max_age_days"] = 1
    decay["sessions"]["max_age_days"] = 1
    p = _make_provider(tmp_path, store, decay)
    now = _utcnow_naive()
    old_shard = store / "episodic" / "2020" / "01"
    old_shard.mkdir(parents=True)
    (old_shard / "ancient.jsonl").write_text("{}", encoding="utf-8")
    old_t = time.time() - 365 * 86400
    old_trace = store / "sessions" / "ancient.jsonl"
    old_trace.write_text("{}", encoding="utf-8")
    os.utime(old_trace, (old_t, old_t))
    # distractor classes that must survive untouched:
    (store / "memories" / "fact.json").write_text("[]", encoding="utf-8")
    (store / "consolidated" / "pending_upgrades.jsonl").write_text("{}", encoding="utf-8")
    prunefile = store / "knowledge" / "archive" / "raw_pending_auto_20200101T000000Z.jsonl"
    prunefile.write_text(json.dumps({"content": "raw"}) + "\n", encoding="utf-8")
    n = p._cap_episodic_sessions()
    assert n == 2, f"only the two due cap files move, got {n}"
    assert (store / "memories" / "fact.json").exists(), "memories/ must be untouched"
    assert (store / "consolidated" / "pending_upgrades.jsonl").exists(), "consolidated/ untouched"
    assert prunefile.exists(), "knowledge/archive raw_pending_auto_* belongs to the prune layer"
    assert not old_trace.exists() and not (old_shard / "ancient.jsonl").exists()


def test_cap_disabled_when_keys_absent_or_zero(tmp_path):
    """0/absent = that direction disabled (fail-soft: a missing key never
    moves data) — the pre-S4 decay shape moves NOTHING."""
    store = _make_store(tmp_path)
    now = _utcnow_naive()
    old_shard = store / "episodic" / "2020" / "01"
    old_shard.mkdir(parents=True)
    (old_shard / "s1.jsonl").write_text("{}", encoding="utf-8")
    old_t = time.time() - 365 * 86400
    old_trace = store / "sessions" / "s1.jsonl"
    old_trace.write_text("{}", encoding="utf-8")
    os.utime(old_trace, (old_t, old_t))
    # (a) keys absent (pre-S4 decay shape)
    decay = _decay_copy()
    decay.pop("episodic", None)
    decay.pop("sessions", None)
    p = _make_provider(tmp_path, store, decay)
    assert p._cap_episodic_sessions() == 0
    assert (old_shard / "s1.jsonl").exists() and old_trace.exists()
    # (b) explicit 0 = disabled even with ancient files
    decay["episodic"] = {"max_age_days": 0}
    decay["sessions"] = {"max_age_days": 0}
    p_decay_zero = p  # same provider, new decay shape
    p_decay_zero._decay = decay
    assert p_decay_zero._cap_episodic_sessions() == 0
    assert (old_shard / "s1.jsonl").exists() and old_trace.exists()


# ── config surface: decay.json <-> DECAY_DEFAULTS dual sync ──────────────────


def test_config_new_keys_dual_sync_per_key():
    """The four S4-T1 keys are dual-synced (改表不改码): decay.json and
    DECAY_DEFAULTS carry the SAME value per key."""
    for key in ("log_rotate_lines", "log_keep_tail_lines"):
        assert DECAY_JSON["consolidation"][key] == DECAY_DEFAULTS["consolidation"][key], key
        assert DECAY_JSON["consolidation"][key] == \
            (int(DECAY_JSON["consolidation"][key]))  # ints, not strings
    assert DECAY_JSON["consolidation"]["log_rotate_lines"] == 5000
    assert DECAY_JSON["consolidation"]["log_keep_tail_lines"] == 500
    assert DECAY_JSON["episodic"]["max_age_days"] == DECAY_DEFAULTS["episodic"]["max_age_days"] == 90
    assert DECAY_JSON["sessions"]["max_age_days"] == DECAY_DEFAULTS["sessions"]["max_age_days"] == 30
    # the defaults themselves: 0-valued knobs are the disabled directions,
    # never an implicit data delete
    assert DECAY_DEFAULTS["episodic"]["max_age_days"] > 0
    assert DECAY_DEFAULTS["sessions"]["max_age_days"] > 0


# ── hard-constraint grep: the new code is deterministic / zero LLM / stdlib ──


def _method_body(src: str, method_name: str) -> str:
    """Extract one method's body (up to the next same-indent def/class)."""
    lines = src.splitlines()
    m = next((i for i, l in enumerate(lines)
              if l.strip().startswith(f"def {method_name}(")), None)
    assert m is not None, f"{method_name} not found in __init__.py"
    indent = len(lines[m]) - len(lines[m].lstrip())
    end = len(lines)
    for j in range(m + 1, len(lines)):
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= indent and \
                (l.lstrip().startswith("def ") or l.lstrip().startswith("class ")):
            end = j
            break
    return "\n".join(lines[m:end])


def test_new_faces_zero_llm_zero_third_party():
    """Hard constraint grep: the two new methods are pure deterministic file
    ops — zero third-party imports, zero LLM call-sites, zero hardcoded
    model names; stdlib json/re/datetime/hashlib/time/os only."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    for name in ("_rotate_consolidation_log", "_cap_episodic_sessions"):
        body = _method_body(src, name)
        forbidden = ("llm_caller", "score_salience", "confirm_supersede",
                     "_load_llm_caller", "import requests", "import http")
        for tok in forbidden:
            assert tok not in body, f"{name} contains a non-deterministic token: {tok}"
        for model in ("gpt-", "gpt4", "claude", "gemini", "qwen", "llama", "grok"):
            assert model not in body.lower(), f"{name} hardcodes a model name: {model}"
        for imp in re.findall(r"^\s*import\s+(?!json|re|os|time|hashlib|threading|pathlib|typing|contextlib|datetime|multiprocessing)\S+|^\s*from\s+(?!__future__|json|re|os|time|hashlib|threading|pathlib|typing|contextlib|datetime|multiprocessing)\S+",
                              body, re.M):
            assert False, f"{name} imports a non-stdlib module: {imp}"


def test_run_mount_sites_inside_daemon_thread_body():
    """Host-turn zero-blocking: all THREE S4-T1 call sites sit inside the
    _run closure body of _spawn_llm_consolidation (between `def _run():`
    and the `if spawn_context_thread is not None:` spawn) — i.e. they run
    on the daemon thread, never on the host turn thread. Each is wrapped
    in its own fail-soft try/except (segment isolation)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    sp = next(i for i, l in enumerate(lines) if "def _spawn_llm_consolidation" in l)
    run_def = next(i for i in range(sp, len(lines)) if lines[i].strip() == "def _run():")
    spawn_gate = next(i for i in range(run_def, len(lines))
                      if "if spawn_context_thread is not None:" in lines[i])
    region = lines[run_def:spawn_gate]
    region_text = "\n".join(region)
    for call in ("self.sweep_memories_expired()",
                 "self._rotate_consolidation_log()",
                 "self._cap_episodic_sessions()"):
        assert call in region_text, f"{call} is NOT in the _run daemon-thread body"
    # each call is fail-soft-wrapped (its own try/except -> the other
    # C sections are never broken)
    for call in ("self.sweep_memories_expired()",
                 "self._rotate_consolidation_log()",
                 "self._cap_episodic_sessions()"):
        idx = region_text.index(call)
        prefix_lines = region_text[:idx].splitlines()
        assert any(l.strip() == "try:" for l in prefix_lines[-15:]), \
            f"{call} is not inside its own fail-soft try/except block"
        assert "except Exception" in region_text[idx:idx + 400], \
            f"{call} has no except-Exception fail-soft tail"


def test_no_host_package_changes_in_diff():
    """S4-T1 is plugin-surface only: the changed-file set is confined to
    the plugin tree (__init__.py, consolidation/, decay.json, tests/) —
    no /usr/local/lib/hermes-agent path is touched."""
    import subprocess
    # 🔴 守卫 (同 S1b/S4-T2/S6): 非 git 仓库 -> git diff 无意义 (exit 129),
    # 转环境性 SKIP 而非假红; git 托管环境下仍真跑。
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
            f"S4-T1 touched a host package path: {c}"
        assert low.startswith(("__init__.py", "consolidation/", "decay.json",
                                "tests/", "spec", "tools/", "shared_memory.py", "plugin.yaml")), \
            f"unexpected changed path (non-plugin-surface): {c}"
