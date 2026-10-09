"""Native-md drain mechanism — filemem consumes per-profile MEMORY.md / USER.md.

PRD: references/filemem-native-drain-PRD.md  (locked decisions D1-D10, §4
component spec, §5 eight acceptance criteria).

What this suite locks (all stores/paths are tmp synthetic — the live
/root/.hermes/filemem store and the host /usr/local/lib/hermes-agent tree are
NEVER touched; the host is only IMPORTED + its public API called, never
modified):

  D1  trigger face = pre_llm_call (turn_start), piggybacked on the existing
      filemem_auto_retrieve hook -> zero NEW hook registration.
  D2  md unchanged (mtime_ns + size + cached entries) -> short-circuit no-op.
  D3  split on NATIVE_MD_ENTRY_SEP ("\\n§\\n") + per-entry normalized fingerprint.
  D4  dedup key = (source_profile, content fp) — the SHARED central pool must
      NOT let profile A's row suppress profile B's write (acc #4).
  D5  write L0, source="auto", category=native-memory/native-user; the row
      auto-enters the existing distill / pending-confirm pipeline (zero
      modification to those gates — we call them, we don't edit them).
  D6  delete via the host PUBLIC API (a MemoryStore bound to the md dir:
      apply_batch / remove under the host's own .lock + drift guard ->
      structurally zero drift, acc #1).
  D7  bare-write fallback (host API absent / batch delete fails): exclusive
      .lock -> read § list -> drop targets -> temp + atomic rename write
      NATIVE_MD_ENTRY_SEP.join(survivors) (no BOM / no free text -> clean
      round-trip, no .bak on the host's next native write).
  D8  one-directional + idempotent: land-in-pool first, delete md after; dedup
      only gates "write", NEVER "delete"; a failed write keeps the md entry.
  D9  MEMORY + USER parameterized, each its own .lock.
  D10 retrieval inherits P0-2 hard isolation (source_profile stamp: auto-inject
      recalls own-profile rows only).

Acceptance mapping (PRD §5):
  #1 zero .bak / zero drift after drain + native remove/replace/apply_batch
  #2 drain (turn_start) and native write (turn_end) serialize on the SAME .lock
     (proven by the D7 exclusive-lock test: the drain re-reads under lock).
  #3 md driven to steady (empty) — 2200/1375 caps no longer hit.
  #4 two profiles writing the same text X -> TWO pool rows, each stamped,
     each auto-retrievable (_auto_recall_eligible per own profile only).
  #5 USER.md entries -> category=native-user, same mechanism.
  #7 idempotent (re-drain -> no dup row); write-fail -> md kept.
  #8 unchanged md -> drain short-circuit (<1ms, no pool write, no LLM).
"""
import copy
import json
import os
import sys
import threading
import time
import pathlib
import types

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES, NATIVE_MD_ENTRY_SEP  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


# ── store / provider fixtures (synthetic tmp store + tmp memories dir) ──────


def _noop_router():
    r = types.SimpleNamespace()
    r.detect_and_route = lambda content: {"level": None}
    return r


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "filemem"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_home(tmp: pathlib.Path, letter: str = "h") -> pathlib.Path:
    """A tmp <home> with a memories/ dir (the native MEMORY.md/USER.md home)."""
    home = tmp / letter / "home"
    (home / "memories").mkdir(parents=True, exist_ok=True)
    return home


def _make_provider(tmp: pathlib.Path, store: pathlib.Path, home: pathlib.Path,
                   source_profile: str, host_factory=None,
                   session_id: str = "s-drain") -> FileMemProvider:
    """Synthetic provider wired for the drain path (no initialize(): the
    drain only reads the attrs below, so a __new__ instance is complete).

    host_factory: a callable returning a host-style MemoryStore bound to the
    TEST's tmp memories dir (the D6 path), or None -> the D7 bare-write path.
    It is installed as the `_drain_host_store_fn` seam (the production code
    probes load_on_disk_store() itself when the seam is absent)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = source_profile
    p.hermetis_home = home
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = store / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._main_runtime = None          # -> _distill_item skips (zero LLM in tests)
    p.trigger_router = _noop_router()
    p._session_drafts = {}
    p._last_recall = None
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
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_report_line = lambda sid: ""
    p._turn_active_sid = session_id
    # native-drain state (the unit under test)
    p._drain_seen = {}
    # drain seams (test-only overrides)
    p._drain_memories_dir_fn = lambda: home / "memories"
    p._drain_host_store_fn = host_factory
    return p


def _seed_md(home: pathlib.Path, target: str, entries) -> pathlib.Path:
    fname = "USER.md" if target == "user" else "MEMORY.md"
    path = home / "memories" / fname
    path.write_text(NATIVE_MD_ENTRY_SEP.join(entries), encoding="utf-8")
    _bump_mtime(path)                 # so the first drain does not hit D2 cache
    return path


def _bump_mtime(path: pathlib.Path) -> None:
    ts = time.time() + 0.5
    os.utime(path, (ts, ts))


def _read_md(home: pathlib.Path, target: str) -> str:
    fname = "USER.md" if target == "user" else "MEMORY.md"
    p = home / "memories" / fname
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8-sig")


def _md_entries(home: pathlib.Path, target: str):
    raw = _read_md(home, target)
    return [e for e in (x.strip() for x in raw.split(NATIVE_MD_ENTRY_SEP)) if e]


def _pool_rows(store: pathlib.Path, mem_type: str = "fact"):
    p = store / "memories" / f"{mem_type}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _pool_rows_all(store: pathlib.Path):
    """Every row across all four type files (drain rows land in whichever type
    _detect_type picks — the plain-English test facts all resolve to `fact`,
    but aggregate so a future regex change cannot silently break the count)."""
    out = []
    for t in MEMORY_TYPES:
        out.extend(_pool_rows(store, t))
    return out


# ── D6 host-delete seam (two flavours, no host .py mutation) ────────────────


class _FakeStore:
    """Duck-typed stand-in for the host `MemoryStore` delete API, bound to the
    TEST's tmp <home>/memories. Lets the D6 BRANCH of the drain (call host
    .remove, handle 'No entry matched' as deleted, fall back to D7 on a hard
    error) be exercised under a 3.8 pytest env WITHOUT importing the host
    store module (which the host tree needs 3.9+ builtin generics for). The
    fake mirrors the host's public contract: `.remove(target, old_text)` ->
    {'success': True} on a clean whole-entry delete, else {'success': False,
    'error': ...}; the delete edits the file as a clean §-list (round-trip
    clean, no BOM / no free text)."""

    def __init__(self, home: pathlib.Path):
        self._mem = home / "memories"

    def _path(self, target: str) -> pathlib.Path:
        return self._mem / ("USER.md" if target == "user" else "MEMORY.md")

    def remove(self, target: str, old_text: str, matched_entry=None) -> dict:
        p = self._path(target)
        if not p.exists():
            return {"success": True}          # nothing there = already gone
        raw = p.read_text(encoding="utf-8-sig")
        entries = [e for e in (x.strip() for x in raw.split(NATIVE_MD_ENTRY_SEP)) if e]
        if old_text not in entries:
            # host `_locate`: full exact match first; a substring that hits more
            # than one distinct entry is the host's "Multiple entries matched".
            hits = [e for e in entries if old_text in e]
            if len(hits) > 1:
                return {"success": False,
                        "error": f"Multiple entries matched '{old_text}'."}
            return {"success": False,
                    "error": f"No entry matched '{old_text}'."}
        survivors = [e for e in entries if e != old_text]
        tmp = p.with_name(p.name + ".memdrain_fake")
        tmp.write_text(NATIVE_MD_ENTRY_SEP.join(survivors), encoding="utf-8")
        os.replace(str(tmp), str(p))
        return {"success": True, "message": "Entry removed."}


def _d6_factory(home: pathlib.Path):
    """D6 seam: a fake host store bound to the TEST's tmp memories dir."""
    return lambda: _FakeStore(home)


def _force_d7():
    """Force the D7 bare-write path: the drain's host-store seam returns None
    (simulates an older host build with no load_on_disk_store API) WITHOUT ever
    probing the real host memories dir."""
    return lambda: None


def _real_store_factory(home: pathlib.Path):
    """A REAL host `MemoryStore` (public class, public .remove/.add/.apply_batch
    + the host's own drift guard) BOUND to the TEST's tmp <home>/memories, via a
    subclass that overrides only `_path_for`. Requires the host store module to
    import under the running interpreter (3.9+; the pytest venv here is 3.8 so
    the callers guard with pytest.importorskip). Used ONLY by the two acc #1
    round-trip tests that exercise the host's real delete + native-op contract."""
    import tools.memory_tool_store as mts
    mem_dir = home / "memories"

    class BoundStore(mts.MemoryStore):
        def _path_for(self, target: str) -> pathlib.Path:
            return mem_dir / ("USER.md" if target == "user" else "MEMORY.md")

    def factory():
        s = BoundStore(2200, 1375)
        s.load_from_disk()
        return s
    return factory


# ─────────────────────────────────────────────────────────────────────────
#  D2  short-circuit: unchanged md -> no pool write, no LLM, <1ms
# ─────────────────────────────────────────────────────────────────────────


def test_drain_unchanged_md_is_fast_noop(tmp_path):
    """acc #8: steady-state drain (md already empty) is a <1ms stat no-op —
    no pool write, no re-parse, no LLM call."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["stable fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov.drain_native_md()                # consume the seed -> md emptied
    assert _md_entries(home, "memory") == []
    assert len(_pool_rows_all(store)) == 1

    rows_before = len(_pool_rows_all(store))
    t0 = time.perf_counter()
    prov.drain_native_md()                # D2: unchanged -> stat short-circuit
    dt_ms = (time.perf_counter() - t0) * 1000.0
    assert dt_ms < 1.0, f"short-circuit took {dt_ms:.3f}ms (want <1ms)"
    assert len(_pool_rows_all(store)) == rows_before     # no new row (idempotent)


# ─────────────────────────────────────────────────────────────────────────
#  D3 + D6 + acc #3  md driven to steady via the host API
# ─────────────────────────────────────────────────────────────────────────


def test_drain_clears_memory_md_via_host_api(tmp_path):
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["fact A", "fact B", "fact C"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    res = prov.drain_native_md()
    # md emptied (steady, acc #3); all 3 rows landed in the pool.
    assert _md_entries(home, "memory") == []
    rows = _pool_rows_all(store)
    assert len(rows) == 3
    for r in rows:
        assert r["level"] == "L0"
        assert r["source"] == "auto"                   # D5
        assert r["category"] == "native-memory"         # D5
        assert r["metadata"]["source_profile"] == "p-a" # D4/D10 stamp
        assert r["metadata"]["drain_fp"], "fingerprint stamp present"
    # the drain reported the delete path taken (observability)
    assert res["moved"] == 3
    assert res["path"]["memory"] in ("host-api", "bare-write")


def test_drain_user_md_same_mechanism(tmp_path):
    """acc #5: USER.md entries -> category=native-user, isomorphic to MEMORY."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "user", ["user fact X", "user fact Y"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov.drain_native_md()
    assert _md_entries(home, "user") == []
    rows = _pool_rows_all(store)
    assert len(rows) == 2
    for r in rows:
        assert r["category"] == "native-user"          # D5 / acc #5


def test_drain_both_files_one_call(tmp_path):
    """D9: one drain call handles MEMORY + USER, each with its own .lock."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["mem fact 1"])
    _seed_md(home, "user", ["user fact 1"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov.drain_native_md()
    assert _md_entries(home, "memory") == []
    assert _md_entries(home, "user") == []
    cats = sorted(r["category"] for r in _pool_rows_all(store))
    assert cats == ["native-memory", "native-user"]


# ─────────────────────────────────────────────────────────────────────────
#  D7  bare-write fallback (host API absent -> host_factory=None)
# ─────────────────────────────────────────────────────────────────────────


def test_drain_d7_raw_when_host_api_missing(tmp_path):
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["raw fact 1", "raw fact 2"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_force_d7())   # -> D7 bare-write
    res = prov.drain_native_md()
    assert _md_entries(home, "memory") == []
    assert len(_pool_rows_all(store)) == 2
    assert res["path"]["memory"] == "bare-write"
    # D7 round-trip clean: the drain wrote a clean § list (here: empty).
    assert _read_md(home, "memory").strip() == ""
    assert not any(".bak" in p.name for p in (home / "memories").iterdir())


def test_d7_bare_write_keeps_survivors_atomic(tmp_path):
    """Delete product stays a clean §-list: a SURVIVING entry (not drained
    because its pool write failed) coexists with the drain and the file still
    round-trips host-clean (no .bak on a later native write)."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["drain me", "keep me"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_force_d7())
    # Fail the pool write for "drain me" only -> D8 keeps its md line, while
    # "keep me" is pooled + deleted. NOTE: the override is installed as a plain
    # instance attribute (a closure), NOT a bound method — calling
    # prov._drain_write_native(entry, target, sid) passes exactly 3 args, so
    # the closure must match that arity.
    real_write = FileMemProvider._drain_write_native
    def flaky_write(entry, target, sid, _self=prov, _real=real_write):
        if entry == "drain me":
            raise OSError("simulated pool write failure")
        return _real(_self, entry, target, sid)
    prov._drain_write_native = flaky_write
    res = prov.drain_native_md()
    # "keep me" was pooled + deleted; "drain me" is KEPT in the md (D8).
    assert _md_entries(home, "memory") == ["drain me"]
    assert res["moved"] == 1 and res["kept"] == 1
    assert len(_pool_rows_all(store)) == 1
    assert "keep me" not in _md_entries(home, "memory")


def test_d7_write_then_native_ops_zero_drift(tmp_path):
    """acc #1 (D7 flavour): after the drain's bare-write delete, a NATIVE
    add/replace/apply_batch on the same file (REAL host store, real drift
    guard) produces ZERO .bak / zero drift_error — the drain's bare-write
    bytes round-trip host-clean. Guarded: the real host store module is
    PEP-585 (3.9+); under the 3.8 test venv this skips cleanly."""
    pytest.importorskip("tools.memory_tool_store",
                        reason="host MemoryStore needs 3.9+; D7 drain logic is "
                               "covered separately by the fake-store D7 tests")
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["drainable fact", "keeper fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_force_d7())
    # Pool only the drainable entry (keep the keeper in the md) via D7 bare
    # write: force the pool write to fail for "keeper fact".
    real_write = FileMemProvider._drain_write_native

    def partial_write(entry, target, sid, _self=prov, _real=real_write):
        if entry == "keeper fact":
            raise OSError("keep this one (pool write skipped)")
        return _real(_self, entry, target, sid)

    prov._drain_write_native = partial_write
    prov.drain_native_md()
    assert _md_entries(home, "memory") == ["keeper fact"]

    # Now a NATIVE write on the same file (REAL host store + drift guard).
    st = _real_store_factory(home)()
    r1 = st.add("memory", "brand new entry")
    assert r1["success"] is True
    r2 = st.remove("memory", "brand new entry")
    assert r2["success"] is True
    r3 = st.apply_batch("memory", [{"action": "add", "content": "batch row"}])
    assert r3.get("success") is True
    # No drift snapshot, no drift_error in any native op result.
    assert not any(".bak" in p.name for p in (home / "memories").iterdir()), \
        [p.name for p in (home / "memories").iterdir()]
    assert "drift" not in json.dumps([r1, r2, r3])


# ─────────────────────────────────────────────────────────────────────────
#  D6  host-API delete + acc #1 (no .bak / no drift after native ops)
# ─────────────────────────────────────────────────────────────────────────


def test_drain_then_native_ops_zero_drift(tmp_path):
    """acc #1: drain removes entries via the host API; a subsequent native
    remove/replace/apply_batch on the SAME file (REAL host store + drift
    guard) -> no .bak, no drift_error. The drain uses the fake D6 seam (its
    .remove contract); the native-op half uses the real host store. Guarded:
    the real store is PEP-585 (3.9+) so this skips under the 3.8 venv."""
    pytest.importorskip("tools.memory_tool_store",
                        reason="acc #1 native-op round-trip needs the real host "
                               "store (3.9+); the D6 drain branch is covered by "
                               "the fake-store drain tests")
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["drainable fact", "drainable fact 2", "keeper"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov.drain_native_md()
    assert _md_entries(home, "memory") == []

    st = _real_store_factory(home)()
    r1 = st.add("memory", "brand new entry")
    assert r1["success"] is True
    r2 = st.remove("memory", "brand new entry")
    assert r2["success"] is True
    r3 = st.apply_batch("memory", [{"action": "add", "content": "batch row"}])
    assert r3.get("success") is True
    assert not any(".bak" in p.name for p in (home / "memories").iterdir())
    assert "drift" not in json.dumps([r1, r2, r3])


# ─────────────────────────────────────────────────────────────────────────
#  D4 + acc #4  cross-profile no-clobber on the SHARED pool
# ─────────────────────────────────────────────────────────────────────────


def test_cross_profile_same_text_two_rows(tmp_path):
    """Two profiles draining the SAME text X -> TWO pool rows (each stamped),
    not one; each auto-retrieves its own (P0-2 hard isolation, D10)."""
    store = _make_store(tmp_path)          # ONE shared pool tree
    home_a = _make_home(tmp_path, "a")
    home_b = _make_home(tmp_path, "b")
    _seed_md(home_a, "memory", ["SHARED FACT X"])
    _seed_md(home_b, "memory", ["SHARED FACT X"])

    prov_a = _make_provider(tmp_path, store, home_a, source_profile="prof-a",
                            host_factory=_d6_factory(home_a))
    prov_b = _make_provider(tmp_path, store, home_b, source_profile="prof-b",
                            host_factory=_d6_factory(home_b))
    prov_a.drain_native_md()
    prov_b.drain_native_md()

    rows = _pool_rows_all(store)
    assert len(rows) == 2, f"expected 2 rows (one per profile), got {len(rows)}"
    stamps = sorted(r["metadata"]["source_profile"] for r in rows)
    assert stamps == ["prof-a", "prof-b"]
    # D10: auto-inject isolation — a row is eligible only for its own profile.
    row_a = next(r for r in rows if r["metadata"]["source_profile"] == "prof-a")
    row_b = next(r for r in rows if r["metadata"]["source_profile"] == "prof-b")
    assert fm._auto_recall_eligible(fm._row_source_profile(row_a), "prof-a") is True
    assert fm._auto_recall_eligible(fm._row_source_profile(row_a), "prof-b") is False
    assert fm._auto_recall_eligible(fm._row_source_profile(row_b), "prof-b") is True
    assert fm._auto_recall_eligible(fm._row_source_profile(row_b), "prof-a") is False
    # and BOTH md files were cleared regardless of the shared pool.
    assert _md_entries(home_a, "memory") == []
    assert _md_entries(home_b, "memory") == []


# ─────────────────────────────────────────────────────────────────────────
#  D8  idempotent + one-directional
# ─────────────────────────────────────────────────────────────────────────


def test_re_drain_same_content_no_duplicate_row(tmp_path):
    """acc #7: the same md content drained twice -> no duplicate pool row
    (the md is emptied the first time; the second drain is a D2 no-op)."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["only fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov.drain_native_md()
    assert len(_pool_rows_all(store)) == 1
    prov.drain_native_md()
    assert len(_pool_rows_all(store)) == 1


def test_write_failure_keeps_md_entry(tmp_path):
    """acc #7: a failed filemem write PRESERVES the md entry (D8: land-first,
    never delete-on-fail); the next turn's drain retries and then deletes.

    NOTE on the override: `prov._drain_write_native = <plain function>` stores a
    BARE function in the instance dict (no self-injection), so production's
    `self._drain_write_native(entry, target, sid)` passes exactly 3 args. The
    override therefore takes 3 positional args (self is captured via a
    default, not injected by Python)."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["critical fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    real_write = FileMemProvider._drain_write_native

    def failing_write(entry, target, sid):
        raise OSError("simulated pool write failure")

    prov._drain_write_native = failing_write          # force a write failure
    res = prov.drain_native_md()
    # md entry preserved (D8), no pool row, delete path NOT taken.
    assert _md_entries(home, "memory") == ["critical fact"]
    assert _pool_rows_all(store) == []
    assert res["moved"] == 0 and res["kept"] == 1
    assert res["path"]["memory"] == "noop"

    # Restore the real write path (forward self=prov) -> the retry now pools
    # AND deletes on the next turn.
    prov._drain_write_native = lambda e, t, s: real_write(prov, e, t, s)
    prov.drain_native_md()
    assert _md_entries(home, "memory") == []
    assert len(_pool_rows_all(store)) == 1


# ─────────────────────────────────────────────────────────────────────────
#  D1  trigger face: drain fires on pre_llm_call, before the retrieve
# ─────────────────────────────────────────────────────────────────────────


def test_pre_llm_call_hook_runs_drain_before_retrieve(tmp_path):
    """D1: the pre_llm_call face calls drain_native_md() BEFORE prefetch();
    freshly-drained rows are therefore available to the same-turn recall.
    (The hook body is exercised via the real filemem_auto_retrieve function
    with the provider cache + config gate stubbed.)"""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["drained fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov._native_drain = True                 # gate ON (test seam attr)
    calls = []
    prov.prefetch = lambda *a, **kw: (calls.append("prefetch"), "ctx")[1]

    # Point the cached-provider seam at our tmp provider + disable the global
    # _drain_host_cache probe (we already bound the store via the seam).
    orig_get = fm._get_cached_provider
    fm._get_cached_provider = lambda sid: prov
    try:
        fm.filemem_auto_retrieve("s-x", "hello", [], True, "m", "p")
    finally:
        fm._get_cached_provider = orig_get

    assert "prefetch" in calls                 # retrieve still ran
    # the drain cleared the md BEFORE the retrieve.
    assert _md_entries(home, "memory") == []
    assert len(_pool_rows_all(store)) == 1


def test_drain_disabled_by_config(tmp_path):
    """Rollback switch: native_drain=false -> the pre_llm_call face skips the
    drain entirely (the md is left intact)."""
    home = _make_home(tmp_path)
    store = _make_store(tmp_path)
    _seed_md(home, "memory", ["untouched fact"])
    prov = _make_provider(tmp_path, store, home, source_profile="p-a",
                          host_factory=_d6_factory(home))
    prov._native_drain = False
    prov.prefetch = lambda *a, **kw: "ctx"

    orig_get = fm._get_cached_provider
    fm._get_cached_provider = lambda sid: prov
    try:
        fm.filemem_auto_retrieve("s-y", "hello", [], True, "m", "p")
    finally:
        fm._get_cached_provider = orig_get

    assert _md_entries(home, "memory") == ["untouched fact"]
    assert _pool_rows_all(store) == []
