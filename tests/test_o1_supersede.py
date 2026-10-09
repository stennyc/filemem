"""O1 (t_d552a09f): explicit ``filemem_supersede`` LLM face + 谱系折叠 (①) + (b) validity.

Locked semantics (the docker counterexample + its regression gates):
  * docker 反例: a LOW-overlap negation ("deployment uses docker" ->
    "no docker today") that EP-1's 0.5-Jaccard save-door stamp MISSES is
    explicitly superseded by the tool — the old row is marked ``superseded``
    (out of the auto-inject pool, kept on disk for audit), the new correcting
    row carries the explicit ``supersedes`` link + a (b) validity window.
  * (b) validity: ``validity_scope`` in {session, day, permanent} + a concrete
    ``valid_until``; the read-side lapse check is ONE pure predicate; legacy
    rows (no validity keys) never lapse and never KeyError.
  * ① 谱系折叠: superseded + lapsed rows are folded OUT of the AUTO-inject
    face (prefetch) entirely — no backfill re-introduction, no tail note.
    The EXPLICIT search face keeps them readable (audit).
  * EP-2 flow: the O1 candidate confirms through the EXISTING
    ``_supersede_confirm_candidates`` face (candidate -> confirmed), reusing
    that gate — O1 feeds it, does not re-build it.

Portability: tempfile trees, no absolute paths, the partial provider shape the
sibling EP/P0-2 tests use. The EP-2 confirm test reuses the shared host-LLM
fake (``_llm_host_fake.install``). Zero live-store / zero .db writes.
"""
import json
import pathlib
import sys
import tempfile
import threading
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    MEMORY_TYPES,
    _stamp_lineage_candidates,
    _content_overlap_score,
    _row_is_lapsed,
    _o1_valid_until_for_scope,
    _o1_fold_out_of_auto,
    lineage_of,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402
import pytest  # noqa: F401
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]

# A recent L2 date (ttl None -> never expires on any run date).
_RECENT = "2026-10-01T00:00:00"


def _make_provider(tmpdir: pathlib.Path, source_profile: str = "default") -> FileMemProvider:
    """P0-2-shaped partial provider: enough fields for the REAL
    search_memories + prefetch + _supersede_confirm_candidates to run without
    initialize(). L2 rows so no TTL expiry on any run date."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = None
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = source_profile
    p.session_id = "s-o1"
    p._turn_active_sid = "s-o1"
    p.trigger_router = fm.MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._session_drafts = {}
    p._pending_memories = []
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._last_recall = None
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {}
    p._pending_drafts_notified = {}
    p._main_runtime = _MAIN_RUNTIME
    p._supersede_confirming = False
    p._fail_open_count = 0
    return p


def _row(rid: str, content: str, source_profile="default", mem_type: str = "fact",
         extra_meta=None) -> dict:
    """A minimal L2 pool row. extra_meta is merged into metadata (for
    superseded / valid_until / source_profile stamping in fixtures)."""
    meta = {
        "origin": {"kind": "user", "trust": "high"},
        "source_profile": source_profile,
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "id": rid, "type": mem_type, "content": content, "level": "L2",
        "metadata": meta,
        "created_at": _RECENT, "updated_at": _RECENT, "occurred_at": None,
        "source": "manual", "reference_count": 0, "salience": 0.5,
        "last_referenced": "", "ttl_days": None,
    }


def _seed(tmp: pathlib.Path, items, mem_type: str = "fact") -> None:
    (tmp / "memories" / f"{mem_type}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_shard(tmp: pathlib.Path, mem_type: str = "fact") -> list:
    with open(tmp / "memories" / f"{mem_type}.json", encoding="utf-8") as fh:
        return json.load(fh)


# ── 0. tool schema + dispatch registration (the O1 LLM face) ───────────────

def test_supersede_tool_registered_and_dispatched():
    """LLM face = 1 schema + 1 dispatch: get_tool_schemas exposes
    filemem_supersede and handle_tool_call routes it (zero new model/API/key —
    it rides the existing host tool channel)."""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    names = {s["name"] for s in p.get_tool_schemas()}
    assert "filemem_supersede" in names
    # dispatch routes to the handler (unknown-tool error proves the elif fires,
    # since the handler's own arg-validation error is what comes back):
    out = json.loads(p.handle_tool_call("filemem_supersede", {}))
    assert out.get("error") == "old_id is required"
    # the 5 pre-existing tools are untouched
    for t in ("filemem_read", "filemem_write", "filemem_search",
              "filemem_delete", "filemem_review_drafts"):
        assert t in names


def test_supersede_requires_all_args():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    assert "error" in json.loads(p.handle_tool_call(
        "filemem_supersede", {"old_id": "fact_0"}))
    assert "error" in json.loads(p.handle_tool_call(
        "filemem_supersede", {"old_id": "fact_0", "new_content": "x"}))
    assert "error" in json.loads(p.handle_tool_call(
        "filemem_supersede", {"old_id": "fact_0", "reason": "r"}))


# ── 1. docker counterexample: low-overlap negation, explicit supersede ─────

def _docker_overlap_is_below_ep1_gate() -> tuple:
    """The docker negation is LOW token-overlap: EP-1's 0.5 Jaccard gate
    MISSES it (the exact gap O1 fills). 'deployrail' is a distinctive shared
    anchor so the auto/explicit recall tests co-match both rows; the rest of
    the token sets differ enough that Jaccard < 0.5. Returns the two
    contents + the shared anchor query."""
    old = "deployrail uses docker for deployment"
    new = "deployrail has no docker today"
    query = "deployrail docker"
    return old, new, query


def test_docker_counterexample_explicit_supersede(tmp_path=None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    old_content, new_content, _query = _docker_overlap_is_below_ep1_gate()
    # GAP PROOF: EP-1's deterministic save-door stamp would NOT link these
    # (overlap < 0.5) — the automatic gate misses the negation correction.
    assert _content_overlap_score(new_content, old_content) < 0.5
    auto_new = {"id": "probe", "type": "fact", "content": new_content,
                "created_at": _RECENT}
    _stamp_lineage_candidates(auto_new, [_row("fact_0", old_content)], dict(DECAY_DEFAULTS))
    assert not auto_new.get("metadata", {}).get("supersedes"), \
        "premise: EP-1 auto gate misses the low-overlap docker negation"

    # Seed the live old row (still active, injectable).
    _seed(tmp, [_row("fact_0", old_content)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_0", "new_content": new_content,
        "reason": "today we do not use docker", "validity_scope": "day"}))
    assert out["success"] is True, out
    assert out["old_id"] == "fact_0" and out["supersede_state"] == "candidate"
    new_id = out["new_id"]
    assert new_id != "fact_0"

    rows = {r["id"]: r for r in _read_shard(tmp)}
    # old row: superseded + back-linked, KEPT ON DISK (I1 无 DELETE)
    assert rows["fact_0"]["metadata"]["superseded"] is True
    assert rows["fact_0"]["metadata"]["superseded_by"] == new_id
    # new row: explicit lineage link + candidate + P0-2 profile stamp + (b) window
    nm = rows[new_id]["metadata"]
    assert nm["supersedes"] == ["fact_0"]
    assert nm["supersede_state"] == "candidate"
    assert nm["source_profile"] == "default"          # P0-2 hard isolation intact
    assert nm["origin"]["trust"] == "high"           # injectable (user/high)
    assert nm["supersede_reason"] == "today we do not use docker"
    assert nm["validity_scope"] == "day"
    assert nm["valid_until"]  # a day scope has a concrete horizon
    # audit row on the SAME append-only channel as EP-1/EP-2
    aud = [json.loads(l) for l in
           (tmp / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()
           if l.strip()]
    assert any(a["action"] == "supersede_explicit" and a["old_id"] == "fact_0"
               and a["new_id"] == new_id for a in aud)


def test_supersede_not_found_returns_guarded_error(tmp_path=None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [_row("fact_0", "some fact")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "no_such_row", "new_content": "new", "reason": "r"}))
    assert out.get("success") is not True
    assert "not found" in out["error"]


def test_supersede_cross_shard_locates_by_id(tmp_path=None):
    """old row lives in the `preference` shard, new row is written to `fact`:
    the tool locates the old row across shards and stamps it, appending the
    new row to its target shard (cross-file RMW, no nested lock)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [_row("pref_0", "prefer docker builds", mem_type="preference")],
          mem_type="preference")
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "pref_0", "new_content": "prefer systemd builds",
        "reason": "migrated off docker", "type": "fact"}))
    assert out["success"] is True and out["type"] == "fact"
    pref = {r["id"]: r for r in _read_shard(tmp, "preference")}
    assert pref["pref_0"]["metadata"]["superseded"] is True
    assert pref["pref_0"]["metadata"]["superseded_by"] == out["new_id"]
    # new row appended to the fact shard, linked back to the old pref row
    fact_ids = {r["id"] for r in _read_shard(tmp, "fact")}
    assert out["new_id"] in fact_ids


# ── 2. EP-2 candidate -> confirmed flow (reuse the existing confirm gate) ──

def test_o1_candidate_confirms_via_ep2_face(tmp_path, monkeypatch):
    """The O1-stamped candidate flips candidate->confirmed through the EXISTING
    _supersede_confirm_candidates face (host-LLM confirm gate) — O1 feeds it,
    does not re-build it; the old row gains superseded_confirmed=True."""
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": True, "reason": "genuine update"}])
    p = _make_provider(tmp_path)
    _seed(tmp_path, [_row("fact_0", "deployment uses docker on host 117")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_0", "new_content": "no docker today on host 117",
        "reason": "today we do not use docker"}))
    new_id = out["new_id"]
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "candidate"
    # now run the EXISTING EP-2 confirm face
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_0"]["metadata"]["superseded_confirmed"] is True
    assert state["calls"] == 1  # the host-LLM gate ran exactly once


def test_o1_candidate_not_confirmed_stays_candidate(tmp_path, monkeypatch):
    """A confirm verdict of False leaves the O1 candidate unconfirmed (不吞脏):
    the pair stays candidate, the old row gains no superseded_confirmed."""
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": False, "reason": "not a real correction"}])
    p = _make_provider(tmp_path)
    _seed(tmp_path, [_row("fact_0", "deployment uses docker on host 117")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_0", "new_content": "no docker today on host 117",
        "reason": "maybe"}))
    new_id = out["new_id"]
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "candidate"
    assert "superseded_confirmed" not in rows["fact_0"]["metadata"]


# ── 3. (b) validity window: scope resolution + read-side lapse predicate ──

def test_validity_scope_to_valid_until(tmp_path=None):
    """session/day -> end-of-creating-UTC-day; permanent -> None; unknown
    scope degrades to a finite day (never infinite). Deterministic across runs
    because it is derived from the row's created_at."""
    created = "2026-10-01T08:15:30"
    assert _o1_valid_until_for_scope("permanent", created) is None
    assert _o1_valid_until_for_scope("day", created) == "2026-10-01T23:59:59"
    assert _o1_valid_until_for_scope("session", created) == "2026-10-01T23:59:59"
    # unknown scope -> fail-safe to 'day' (finite horizon)
    assert _o1_valid_until_for_scope("weird", created) == "2026-10-01T23:59:59"
    # corrupt/absent created_at -> falls back to the current UTC day
    assert _o1_valid_until_for_scope("day", "garbage")  # not None, no raise
    assert _o1_valid_until_for_scope(None, "x") is None  # None scope = permanent


def test_row_is_lapsed_predicate(tmp_path=None):
    """lapsed iff a CONCRETE valid_until is already past. permanent / no keys /
    corrupt valid_until / legacy rows -> not lapsed, never KeyError."""
    old_ts = "2020-01-01T00:00:00"
    far_ts = "9999-01-01T00:00:00"
    assert _row_is_lapsed({"metadata": {"valid_until": old_ts}},
                          datetime(2021, 1, 1)) is True
    assert _row_is_lapsed({"metadata": {"valid_until": far_ts}},
                          datetime(2021, 1, 1)) is False
    # permanent scope: never lapses even with a stray valid_until
    assert _row_is_lapsed({"metadata": {"validity_scope": "permanent",
                                        "valid_until": old_ts}},
                          datetime(2021, 1, 1)) is False
    # legacy row: no validity keys at all -> not lapsed
    assert _row_is_lapsed({"metadata": {"superseded": False}}) is False
    assert _row_is_lapsed({}) is False
    assert _row_is_lapsed({"metadata": "garbage-not-a-dict"}) is False
    assert _row_is_lapsed({"metadata": {"valid_until": "not-a-date"}}) is False
    # a non-dict row -> not lapsed
    assert _row_is_lapsed("not-a-dict") is False
    # a FUTURE valid_until as of a fixed now -> not lapsed
    assert _row_is_lapsed({"metadata": {"valid_until": far_ts}},
                          datetime(2021, 1, 1)) is False


def test_o1_fold_out_of_auto_predicate(tmp_path=None):
    """The ① fold-out predicate: superseded OR lapsed -> True (fold out of the
    auto face); legacy rows (no keys) and non-dict rows -> False (never drop
    recall, byte-identical auto-inject for legacy corpora)."""
    assert _o1_fold_out_of_auto({"metadata": {"superseded": True}}) is True
    assert _o1_fold_out_of_auto({"metadata": {"superseded": False}}) is False
    # lapsed via validity window (no superseded key)
    assert _o1_fold_out_of_auto({"metadata": {"valid_until": "2020-01-01T00:00:00"}},
                                datetime(2021, 1, 1)) is True
    # legacy: neither key -> False
    assert _o1_fold_out_of_auto({"metadata": {"origin": {"kind": "user", "trust": "high"}}}) is False
    assert _o1_fold_out_of_auto({}) is False
    assert _o1_fold_out_of_auto("nope") is False


# ── 4. ① 谱系折叠: superseded + lapsed rows leave the AUTO-inject face ──────

def test_prefetch_fold_superseded_row(tmp_path=None):
    """End-to-end docker 反例 through the AUTO face: after the tool supersedes
    the old row, prefetch's injection context shows ONLY the new (active)
    row — the superseded old row is folded out entirely (no backfill, no tail
    note). The new row is injectable (user/high + same profile)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    old_content, new_content, query = _docker_overlap_is_below_ep1_gate()
    _seed(tmp, [_row("fact_0", old_content)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_0", "new_content": new_content,
        "reason": "today we do not use docker"}))
    new_id = out["new_id"]
    # drive the AUTO-inject face (the pre_llm_call pipeline). The query carries
    # the distinctive shared anchor "deployrail" so BOTH rows would match the
    # keyword gate; the fold is what keeps the superseded old row out of the
    # injected context.
    ctx = p.prefetch(query, session_id="s-o1", confidence=0.5)
    assert new_content in ctx, f"new correcting row must be injected, got: {ctx!r}"
    assert old_content not in ctx, \
        f"superseded old row must be folded out of the auto face, got: {ctx!r}"


def test_prefetch_fold_lapsed_row(tmp_path=None):
    """(b) a permanent row is injectable; a SAME row carrying a PAST
    valid_until (lapsed) is folded out of the auto face — the validity window
    closes the contradiction of a short-term override surfacing forever."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    far_future = "9999-01-01T00:00:00"
    lapsed = "2020-01-01T00:00:00"
    _seed(tmp, [
        _row("fact_permanent", "persistent deploy note sharedtoken",
             extra_meta={"validity_scope": "permanent"}),
        _row("fact_day_now", "day-scope deploy note sharedtoken",
             extra_meta={"validity_scope": "day", "valid_until": far_future}),
        _row("fact_day_lapsed", "lapsed deploy note sharedtoken",
             extra_meta={"validity_scope": "day", "valid_until": lapsed}),
        _row("fact_legacy", "legacy deploy note sharedtoken"),  # no validity keys
    ])
    ctx = p.prefetch("sharedtoken deploy note", session_id="s-o1", confidence=0.5)
    assert "persistent deploy note sharedtoken" in ctx   # permanent stays
    assert "day-scope deploy note sharedtoken" in ctx     # future valid_until stays
    assert "legacy deploy note sharedtoken" in ctx        # legacy stays
    assert "lapsed deploy note sharedtoken" not in ctx    # lapsed folded out


def test_explicit_face_keeps_superseded_readable(tmp_path=None):
    """The EXPLICIT filemem_search face (profile_scope=None) does NOT run the
    fold: the superseded old row stays retrievable for audit, alongside the
    new row. (Contrast with the auto face above.)"""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    old_content, new_content, query = _docker_overlap_is_below_ep1_gate()
    _seed(tmp, [_row("fact_0", old_content)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_0", "new_content": new_content, "reason": "r"}))
    rows = p.search_memories(query, limit=8, min_score=0.0,
                             profile_scope=None)
    ids = {r.get("id") for r in rows}
    # the explicit face keeps the old row readable (EP-1 out-pool backfill or
    # main pool) — audit semantics unchanged by the auto-face fold.
    assert "fact_0" in ids, f"explicit face must still surface the old row, got {ids}"
    assert out["new_id"] in ids


# ── 5. legacy read path: lineage_of / no KeyError ─────────────────────────

def test_legacy_row_lineage_view_no_keyerror(tmp_path=None):
    """Legacy rows (no lineage / no validity keys) read cleanly: lineage_of
    yields empty lists / un-superseded, and the O1 fold predicate is a no-op
    for them (the EP-1 regression gate)."""
    legacy = _row("leg_0", "a legacy note")
    del legacy["metadata"]["source_profile"]  # strip a modern key
    lv = lineage_of(legacy)
    assert lv["supersedes"] == [] and lv["superseded"] is False
    assert _o1_fold_out_of_auto(legacy) is False
    assert _row_is_lapsed(legacy) is False


# ── 6. P0-2 strict cross-profile write-face guard (spec item 2) ───────────

def test_supersede_cross_profile_row_rejected(tmp_path=None):
    """A row stamped by ANOTHER profile may never be superseded from this
    session: the guard rejects BEFORE any write — no new row appended, no
    old-row marker, no supersede_explicit audit event."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="coder")
    _seed(tmp, [_row("fact_x", "other profile's note",
                     source_profile="orchestrator")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "corrected", "reason": "r"}))
    assert out.get("success") is not True
    assert "cross-profile" in out["error"]
    # zero writes: the old row is untouched, nothing was appended
    rows = _read_shard(tmp)
    assert [r["id"] for r in rows] == ["fact_x"]
    assert "superseded" not in rows[0]["metadata"]
    log_file = tmp / "consolidation_log.jsonl"
    aud = ([json.loads(l) for l in log_file.read_text(encoding="utf-8").splitlines()
            if l.strip()] if log_file.exists() else [])
    assert not any(a.get("action") == "supersede_explicit" for a in aud)


def test_supersede_neutral_row_allowed_from_any_profile(tmp_path=None):
    """The guard's exception clause: a NEUTRAL (unstamped) row is claimable by
    ANY profile — an unstamped legacy row belongs to no one, so a session may
    supersede it and the new correcting row carries the session's stamp."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="coder")
    legacy = _row("fact_n", "neutral legacy note")
    del legacy["metadata"]["source_profile"]
    _seed(tmp, [legacy])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_n", "new_content": "corrected", "reason": "r"}))
    assert out["success"] is True, out
    rows = {r["id"]: r for r in _read_shard(tmp)}
    assert rows["fact_n"]["metadata"]["superseded"] is True
    assert out["new_id"] in rows
    assert rows[out["new_id"]]["metadata"]["source_profile"] == "coder"


def test_supersede_same_profile_row_allowed(tmp_path=None):
    """Own-stamp row -> allowed: the guard must not break the happy path."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="coder")
    _seed(tmp, [_row("fact_m", "my note", source_profile="coder")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_m", "new_content": "corrected", "reason": "r"}))
    assert out["success"] is True, out


def test_supersede_unresolved_session_rejects_stamped_row(tmp_path=None):
    """Unresolved provider identity (source_profile None) -> the write face
    tightens to NEUTRAL-ROWS-ONLY (the P1-4 convention: don't know who I am,
    so never claim someone else's stamped row). A stamped row is rejected, a
    neutral row still passes."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.source_profile = None
    neutral = _row("fact_z", "neutral")
    del neutral["metadata"]["source_profile"]
    _seed(tmp, [_row("fact_s", "stamped", source_profile="orchestrator"),
                neutral])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_s", "new_content": "x", "reason": "r"}))
    assert out.get("success") is not True
    assert "cross-profile" in out["error"]
    out2 = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_z", "new_content": "y", "reason": "r"}))
    assert out2["success"] is True, out2


def test_supersede_draft_id_rejected_not_found(tmp_path=None):
    """Spec item 7: a draft row lives in drafts_<sid>.json, NOT in the pool —
    it has no linkable pool id, so supersede rejects with the guarded
    not-found error (the draft's content is never touched)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    drafts = tmp / "drafts_s-o1.json"
    drafts.write_text(
        json.dumps([{"id": "draft_candidate", "content": "draft"}]),
        encoding="utf-8")
    _seed(tmp, [_row("fact_0", "some fact")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "draft_candidate", "new_content": "x", "reason": "r"}))
    assert out.get("success") is not True
    assert "not found" in out["error"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
