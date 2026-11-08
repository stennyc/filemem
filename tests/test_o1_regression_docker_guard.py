"""O1 regression suite (t_2c289725): docker-contradiction supersede + the P0-2
strict cross-profile write-face guard + the EP-2 candidate->confirmed flow.

This is the CROSS-CARD regression lane for the O1 feature: it re-asserts, in
one tmp-store-only file, the docker contradiction's end-to-end semantics, the
guard's EXACT rejection message (acceptance: specific error verified), and the
lineage state transition. It deliberately does NOT duplicate test_o1_supersede
or test_ep_2_confirm — it pins the user-visible contracts those lanes
implemented, so a later lane that edits __init__.py still gets a tripwire for
the three O1 behaviors the epic's acceptance criteria name.

Store hygiene (the task's hard constraint): every provider is a P0-2-shaped
partial instance on a tempfile tree; ZERO writes to the live
/root/.hermes/filemem store (the runner additionally snapshots the live store
byte-for-byte around the run — see the verification report).

Case map (task body):
  A. Docker contradiction — 'deploy with docker' vs 'no docker today', LOW
     Jaccard overlap (< 0.5, the gap EP-1's save door misses); explicit
     supersede; old row marked superseded + folded out of the AUTO-inject
     face, new row active and injected; the EXPLICIT search face keeps both
     readable (audit).
  B. Cross-profile guard — row stamped by profile A; supersede attempted from
     profile B; rejected BEFORE any write with the specific 'cross-profile'
     error naming BOTH profiles; zero shard/audit mutation.
  C. EP-2 flow — the O1 candidate walks the EXISTING
     _supersede_confirm_candidates face: candidate -> confirmed, the old row
     gains superseded_confirmed=True, host-LLM gate ran exactly once.
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    _content_overlap_score,
    _o1_fold_out_of_auto,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402
import pytest  # noqa: F401
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

_RECENT = "2026-10-01T00:00:00"  # recent L2 date -> no TTL expiry on any run date

# ── the docker contradiction pair (LOW Jaccard, proven below) ──────────────
DOCKER_OLD = "deployrail uses docker for deployment"
DOCKER_NEW = "deployrail has no docker today"
DOCKER_QUERY = "deployrail docker"


def _make_provider(tmp: pathlib.Path, source_profile: str = "default") -> FileMemProvider:
    """Same partial-provider shape as test_o1_supersede (enough fields for the
    REAL prefetch / search_memories / _supersede_confirm_candidates paths,
    without initialize())."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = None
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = source_profile
    p.session_id = "s-regress"
    p._turn_active_sid = "s-regress"
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


def _row(rid: str, content: str, source_profile="default", mem_type: str = "fact") -> dict:
    """A minimal L2 pool row stamped with the shared P0-2 origin channel."""
    return {
        "id": rid, "type": mem_type, "content": content, "level": "L2",
        "metadata": {
            "origin": {"kind": "user", "trust": "high"},
            "source_profile": source_profile,
        },
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


# ══ Case A — docker contradiction: low-Jaccard negation, explicit supersede ═

def test_a_docker_pair_is_below_ep1_jaccard_gate():
    """The docker negation is a LOW-overlap pair: EP-1's 0.5 Jaccard save-door
    stamp MISSES it — the exact gap O1's explicit face fills. This is the
    premise every Case A assertion builds on, pinned separately so a token-
    drift in the fixture pair is caught at the source, not downstream."""
    score = _content_overlap_score(DOCKER_NEW, DOCKER_OLD)
    assert score < 0.5, (
        f"docker pair no longer low-overlap (Jaccard={score:.3f}); the "
        f"counterexample fixture drifted — pin a pair the EP-1 gate misses")


def test_a_explicit_supersede_folds_old_row_out_of_auto_face(tmp_path):
    """End-to-end docker 反例: after the tool supersedes the old row, (1) the
    old row is marked superseded + back-linked and KEPT ON DISK (I1 无
    DELETE), (2) the AUTO-inject face folds it out entirely (no backfill, no
    tail note) while the NEW row is active and injected, and (3) the EXPLICIT
    search face keeps BOTH readable (audit semantics)."""
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_docker", "new_content": DOCKER_NEW,
        "reason": "today we do not use docker", "validity_scope": "day"}))
    assert out["success"] is True, out
    new_id = out["new_id"]
    assert out["supersede_state"] == "candidate"

    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    # old row: superseded + back-linked, still on disk
    assert rows["fact_docker"]["metadata"]["superseded"] is True
    assert rows["fact_docker"]["metadata"]["superseded_by"] == new_id
    # new row: explicit lineage link + P0-2 stamp + day validity horizon
    nm = rows[new_id]["metadata"]
    assert nm["supersedes"] == ["fact_docker"]
    assert nm["source_profile"] == "default"
    assert nm["validity_scope"] == "day" and nm["valid_until"]
    # the fold predicate agrees with the audit marker
    assert _o1_fold_out_of_auto(rows["fact_docker"]) is True
    assert _o1_fold_out_of_auto(rows[new_id]) is False

    # AUTO face (V2 confirmed gate): the candidate new row (supersede_state=
    # "candidate", not yet confirmed) is folded out of the auto face just like
    # the superseded old row. After the EP-2 confirm chain flips it to
    # confirmed=True, the new row enters the auto face.
    p._set_bringback_choice("s-regress", "clean_start")
    ctx = p.prefetch(DOCKER_QUERY, session_id="s-regress", confidence=0.5)
    assert DOCKER_OLD not in ctx, \
        f"superseded old row must be folded out of the auto face, got: {ctx!r}"
    assert DOCKER_NEW not in ctx, \
        f"unconfirmed candidate row must NOT inject on the auto face (V2 " \
        f"confirmed gate), got: {ctx!r}"
    # After EP-2 confirm: the new row (confirmed=True) enters the auto face.
    shard = tmp_path / "memories" / "fact.json"
    items = json.loads(shard.read_text(encoding="utf-8"))
    for item in items:
        if item.get("id") == new_id:
            item.setdefault("metadata", {})["confirmed"] = True
            item["metadata"]["supersede_state"] = "confirmed"
    shard.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    ctx_confirmed = p.prefetch(DOCKER_QUERY, session_id="s-regress", confidence=0.5)
    assert DOCKER_NEW in ctx_confirmed, \
        f"confirmed new row must inject after EP-2 confirm, got: {ctx_confirmed!r}"
    assert DOCKER_OLD not in ctx_confirmed

    # EXPLICIT face: both rows stay readable (EP-1 out-pool / audit)
    ids = {r.get("id") for r in p.search_memories(
        DOCKER_QUERY, limit=8, min_score=0.0, profile_scope=None)}
    assert "fact_docker" in ids and new_id in ids, \
        f"explicit face must keep the superseded row readable, got {ids}"


# ══ Case B — cross-profile guard: specific error, zero writes ═════════════

def test_b_cross_profile_supersede_rejected_with_specific_error(tmp_path):
    """A row stamped by profile A may never be superseded from profile B:
    the P0-2 write-face guard rejects with the SPECIFIC 'cross-profile' error
    naming both profiles, and rejects BEFORE any write — the shard is
    byte-identical afterwards and no supersede audit event was appended."""
    p_a = _make_provider(tmp_path, source_profile="profile_a")
    _seed(tmp_path, [_row("fact_a", "profile A deployment note",
                          source_profile="profile_a")])
    shard_before = (tmp_path / "memories" / "fact.json").read_bytes()
    log_before = ((tmp_path / "consolidation_log.jsonl").read_bytes()
                  if (tmp_path / "consolidation_log.jsonl").exists() else b"")

    p_b = _make_provider(tmp_path, source_profile="profile_b")
    out = json.loads(p_b.handle_tool_call("filemem_supersede", {
        "old_id": "fact_a", "new_content": "profile B correction",
        "reason": "r"}))
    assert out.get("success") is not True
    err = out["error"]
    # the specific guard message (acceptance: verified literally) — the row's
    # owning profile is quoted via repr; the session's own identity renders
    # via str() (unquoted, None->'unresolved')
    assert "cross-profile supersede is rejected" in err
    assert "'profile_a'" in err and "(profile_b)" in err
    assert err.endswith("(unstamped) row"), f"guard error text drifted: {err!r}"

    # zero writes: shard byte-identical, old row untouched, no audit event
    assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before
    log_after = ((tmp_path / "consolidation_log.jsonl").read_bytes()
                 if (tmp_path / "consolidation_log.jsonl").exists() else b"")
    assert log_after == log_before
    assert "superseded" not in _read_shard(tmp_path)[0]["metadata"]


def test_b_unresolved_identity_tightens_to_neutral_only(tmp_path):
    """The guard's P1-4 tightening: an UNRESOLVED session identity (a provider
    with no source_profile) rejects stamped rows with the same cross-profile
    error naming the 'unresolved' session, while a neutral (unstamped) row
    still passes — don't know who I am -> never claim someone else's row."""
    p = _make_provider(tmp_path)
    p.source_profile = None
    neutral = _row("fact_z", "neutral note")
    del neutral["metadata"]["source_profile"]
    _seed(tmp_path, [_row("fact_s", "stamped note", source_profile="profile_a"),
                     neutral])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_s", "new_content": "x", "reason": "r"}))
    assert out.get("success") is not True
    assert "cross-profile supersede is rejected" in out["error"]
    assert "(unresolved)" in out["error"]
    out2 = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_z", "new_content": "y", "reason": "r"}))
    assert out2["success"] is True, out2


# ══ Case C — EP-2 flow: candidate -> confirmed ════════════════════════════

def test_c_candidate_confirms_via_ep2_face(tmp_path, monkeypatch):
    """The O1-stamped candidate flips candidate -> confirmed through the
    EXISTING _supersede_confirm_candidates face (host-LLM gate) — O1 feeds
    the flow, does not re-build it; the old row gains superseded_confirmed."""
    fake, state = install(monkeypatch,
                         replies=[{"confirmed": True, "reason": "genuine update"}])
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_docker", "new_content": DOCKER_NEW,
        "reason": "today we do not use docker"}))
    new_id = out["new_id"]
    assert _read_shard(tmp_path)[1] is not None
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "candidate"

    p._supersede_confirm_candidates()

    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_docker"]["metadata"]["superseded_confirmed"] is True
    assert state["calls"] == 1  # the host-LLM confirm gate ran exactly once


def test_c_rejected_verdict_keeps_candidate(tmp_path, monkeypatch):
    """confirmed=False is a VERDICT, not a failure: the candidate stays
    candidate (retries next boundary tick), the old row gains NO
    superseded_confirmed marker (不吞脏)."""
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": False, "reason": "not a real correction"}])
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_docker", "new_content": DOCKER_NEW, "reason": "maybe"}))
    new_id = out["new_id"]
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows[new_id]["metadata"]["supersede_state"] == "candidate"
    assert "superseded_confirmed" not in rows["fact_docker"]["metadata"]
    assert state["calls"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
