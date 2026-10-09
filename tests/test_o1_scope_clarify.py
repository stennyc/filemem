"""Option C (t_de0df8b7) scope-clarify lane + validity-window boundary tests.

The 472 worker wrote C's five live components but stopped at the "三档出池测试
设计" (three-tier out-pool test design) — this file is that missing test face.
It locks C's live behavior against the pure functions, the write face, the
auto-inject fold, and the red lines, with ZERO writes to the live store
(tempfile-tree partial provider, the O1/EP-1/C3 shape).

C's five components (grep-verified wired in the live __init__.py):
  1. _o1_scope_stamp_clarify_candidates — the THIRD parallel supersedes-
     candidate lane at the save door. Fires ONLY when a TEMPORARY exception
     (short validity_scope in {session, day} OR a negation/premise-change
     signal) meets a STRICTLY-OLDER STANDING global row about the same topic
     (permanent scope / legacy, cross-profile OR neutral-global). It APPENDS
     to the existing supersedes links, stamps o1_scope_clarify=True, and leaves
     the terminal to the owner's clarify nod through the EXISTING C3/EP-2
     confirm gate. 只扩池、不自动作废旧行: it NEVER sets superseded=True.
  2. _o1_valid_until_for_scope — session/day -> end-of-creating-UTC-day
     (23:59:59); permanent/unknown/None -> None (never lapses / fail-safe).
  3. _row_is_lapsed — the shared read-side lapse predicate: a CONCRETE
     valid_until strictly in the past -> lapsed; permanent / no key / corrupt
     valid_until / legacy row -> NOT lapsed (fail-open, never drops recall).
  4. auto-inject fold — _o1_fold_out_of_auto is wired into the real auto face
     (the L3047 `results = [r for r in results if not _o1_fold_out_of_auto(r)]`
     line) so superseded + lapsed rows leave auto-inject; the EXPLICIT search
     face keeps both readable for audit.
  5. filemem_write validity_scope — the write face stamps validity_scope + a
     derived valid_until ONLY when the caller asks for one (the manual
     filemem_write / explicit-supersede faces); the auto / confirmed_draft /
     default path stamps NOTHING -> byte-identical to pre-Option-C.

Red lines pinned here (any breach = the card blocks with evidence):
  * 默认面字节级零变: a miss (no negation, no short scope) and the default
    write path mutate NOTHING new vs the pure-session behavior.
  * 升级/覆盖永远候选 + owner 点头: the lane extends the pool, never flips
    superseded=True; a candidate only lands via the owner-confirmed C3/EP-2
    gate.
  * 同 profile 隔离不破坏: a same-profile standing row is the owner's own
    fact, not a "global" conflict -> not a candidate (neutral / cross-profile
    only).
  * 宿主零写: tempfile trees, no live .db / live store / host code touched.
"""
import copy
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    _o1_scope_stamp_clarify_candidates,
    _o1_valid_until_for_scope,
    _row_is_lapsed,
    _o1_fold_out_of_auto,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import pytest  # noqa: F401
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_DECAY = copy.deepcopy(DECAY_DEFAULTS)

# Fixed, run-date-independent anchors (the C3/O1 convention: a recent L2 date
# with ttl None -> never TTL-expires on any run date).
_NEW_TS = "2026-10-08T00:00:00"
_OLD_TS = "2026-01-01T00:00:00"

# Negation new row (C negation signal) whose key concepts survive the strip and
# recall >= 0.5 against the STANDING docker row below (probed: recall 0.667).
_NEG_NEW = "today we do NOT use docker"
# Neutral standing (permanent) row the negation row conflicts with.
_STANDING_DOCKER = "we use docker for deployment"
# A negation row that shares NO key concept with the standing docker row
# (probed: recall 0.000) -> must MISS even though it carries a negation.
_NEG_UNRELATED = "today we do NOT run the monthly db backup"
# Plain (non-negation) content for the default / EP-1-adjacent paths.
_PLAIN_NEW = "the docker registry port is 5001"


def _make_provider(tmp: pathlib.Path, source_profile: str = "default") -> FileMemProvider:
    """Same partial-provider shape as test_c3_negation_channel / test_o1_supersede
    (enough fields for the REAL _save_memory / _pool_rows_all /
    _supersede_confirm_candidates paths, without initialize())."""
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
    p.session_id = "s-c"
    p._turn_active_sid = "s-c"
    p.trigger_router = fm.MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = copy.deepcopy(_DECAY)
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


def _new(rid, content, *, negation=True, vs=None, sp=None, ts=_NEW_TS):
    """A TEMPORARY-exception (or plain) new row for the pure lane.

    negation=True marks it a negation/premise-change trigger (condition a).
    vs (validity_scope in {session, day, permanent, None}) is the short-scope
    trigger; both are OR'd. sp stamps metadata.source_profile (None -> omit,
    i.e. neutral)."""
    meta = {"validity_scope": vs} if vs is not None else {}
    if sp is not None:
        meta["source_profile"] = sp
    row = {
        "id": rid, "type": "fact", "content": content, "level": "L0",
        "metadata": meta,
        "created_at": ts, "updated_at": ts, "occurred_at": None,
        "source": "manual", "reference_count": 0, "salience": 0.5,
        "last_referenced": "", "ttl_days": None,
    }
    # the lane keys its trigger on negation text (proven), so mirror it: a
    # "plain" row keeps its content verbatim; the caller passes content that
    # does / does not carry a negation marker.
    return row


def _old(rid, content, *, scope=None, sp=None, ts=_OLD_TS):
    """A STRICTLY-OLDER standing row. scope in {None (legacy), 'permanent',
    'day', 'session'}. sp in {None (neutral), 'coder', 'orchestrator', ...}."""
    meta = {}
    if scope is not None:
        meta["validity_scope"] = scope
    if sp is not None:
        meta["source_profile"] = sp
    row = {
        "id": rid, "type": "fact", "content": content, "level": "L2",
        "metadata": meta,
        "created_at": ts, "updated_at": ts, "occurred_at": None,
        "source": "manual", "reference_count": 0, "salience": 0.5,
        "last_referenced": "", "ttl_days": None,
    }
    return row


# ═══════════════════════════════════════════════════════════════════════════
# GROUP 1 — pure clarify-lane hit / miss (零 I/O, deterministic)
# ═══════════════════════════════════════════════════════════════════════════

def test_clarify_hit_negation_meets_neutral_standing():
    """Condition (a) negation + (b) neutral (unstamped) permanent standing row
    about the same topic -> the lane APPENDS the standing id to supersedes,
    stamps o1_scope_clarify=True + supersede_state=candidate, and returns the
    unmutated standing row dict. The OLD row is NEVER flipped superseded."""
    new = _new("newrow", _NEG_NEW, sp="coder")
    old = _old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)
    snapshot = copy.deepcopy(old)
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert [r["id"] for r in out] == ["old_stand"], "neutral standing row is a candidate"
    # APPEND to the new row's links + clarify stamp
    assert "old_stand" in (new["metadata"].get("supersedes") or [])
    assert new["metadata"].get("o1_scope_clarify") is True
    assert new["metadata"].get("supersede_state") == "candidate"
    # RED LINE: the standing row is byte-identical (no superseded flip here)
    assert old == snapshot, "clarify lane must never mutate the standing row"
    assert not old["metadata"].get("superseded")


def test_clarify_hit_cross_profile_standing_is_candidate():
    """A standing row stamped by ANOTHER profile (cross-profile global fact)
    IS a legitimate conflict target — the owner's session-level exception may
    need to promote/override it, pending the owner's clarify nod."""
    new = _new("newrow", _NEG_NEW, sp="coder")
    old = _old("old_cross", _STANDING_DOCKER, scope="permanent", sp="orchestrator")
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert [r["id"] for r in out] == ["old_cross"], \
        "a cross-profile standing row is a clarify candidate"
    assert new["metadata"].get("o1_scope_clarify") is True


def test_clarify_hit_short_scope_trigger_no_negation():
    """Condition (a) via a SHORT validity_scope (no negation word): a plain
    session-scope row about the same topic as a permanent standing row still
    fires — the scope itself is the 'temporary exception'."""
    new = _new("newrow", _PLAIN_NEW, negation=False, vs="session", sp="coder")
    old = _old("old_stand", "the docker registry port is 5000",
               scope="permanent", sp=None)
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert [r["id"] for r in out] == ["old_stand"], \
        "a short-scope row is a temporary exception even without negation"
    assert new["metadata"].get("o1_scope_clarify") is True


def test_clarify_miss_plain_permanent_new_row():
    """RED LINE 默认面字节级零变: a PLAIN permanent/standing new row (no
    negation, no short scope) is NOT a temporary exception -> [] and the new
    row's metadata is untouched (no o1_scope_clarify, no supersede_state)."""
    new = _new("newrow", _PLAIN_NEW, negation=False, vs="permanent", sp="coder")
    old = _old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert out == [], "a plain standing row never triggers the clarify lane"
    assert "o1_scope_clarify" not in new["metadata"]
    assert "supersede_state" not in new["metadata"]
    assert "supersedes" not in new["metadata"]


def test_clarify_miss_negation_unrelated_topic():
    """A negation row whose key concepts share nothing with the standing row
    (recall 0.000, probed) carries a negation but is about a DIFFERENT topic
    -> no candidate. Negation alone is not enough; the same-topic gate holds."""
    new = _new("newrow", _NEG_UNRELATED, sp="coder")
    old = _old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert out == []
    assert "o1_scope_clarify" not in new["metadata"]


def test_clarify_miss_same_profile_standing_not_candidate():
    """RED LINE 同 profile 隔离: a STRICTLY-OLDER row stamped by the SAME
    profile as the new row is the owner's own fact, not a 'global' conflict ->
    NOT a candidate (only neutral / cross-profile standing rows qualify)."""
    new = _new("newrow", _NEG_NEW, sp="coder")
    old = _old("old_mine", _STANDING_DOCKER, scope="permanent", sp="coder")
    out = _o1_scope_stamp_clarify_candidates(new, [old], dict(_DECAY))
    assert out == [], "same-profile standing row is the owner's own fact"
    assert "o1_scope_clarify" not in new["metadata"]


def test_clarify_caps_candidates_deterministic_ranking():
    """Two qualifying standing rows with DIFFERENT recalls (neutral 1.0,
    cross-profile 0.667, both probed and both >= the 0.5 gate) -> the default
    supersede_max_candidates=2 cap + deterministic ranking (recall desc, id
    asc) yields the higher-recall row first. The ids are chosen so that an
    id-asc sort would put the LOWER-recall row first — so passing proves
    recall is the primary sort key, not the id tiebreak."""
    new = _new("newrow", _NEG_NEW, sp="coder")
    # neutral, recall 1.0, deliberately a LARGER id so an id-asc sort would lose
    old_high_recall = _old("old_z", "do we use docker", scope="permanent", sp=None)
    # cross-profile, recall 0.667, a SMALLER id so an id-asc sort would wrongly win
    old_low_recall = _old("old_a", "we use docker for deployment",
                          scope="permanent", sp="orchestrator")
    out = _o1_scope_stamp_clarify_candidates(
        new, [old_high_recall, old_low_recall], dict(_DECAY))
    ids = [r["id"] for r in out]
    assert ids == ["old_z", "old_a"], \
        f"recall desc must rank the 1.0 row first, got {ids}"


# ═══════════════════════════════════════════════════════════════════════════
# GROUP 2 — three-tier out-pool boundary (valid_until / lapse / fold)
# ═══════════════════════════════════════════════════════════════════════════

def test_valid_until_session_day_permanent_boundaries():
    """The 三档 (three-tier) valid_until resolution: session AND day both
    resolve to the end-of-creating-UTC-day (23:59:59); permanent -> None
    (never lapses); an unknown/None scope fails SAFE to a finite day horizon
    (never infinite)."""
    created = "2026-01-01T05:00:00"
    assert _o1_valid_until_for_scope("session", created) == "2026-01-01T23:59:59"
    assert _o1_valid_until_for_scope("day", created) == "2026-01-01T23:59:59"
    assert _o1_valid_until_for_scope("permanent", created) is None
    # fail-safe: unknown scope -> finite day horizon, never None (never infinite)
    assert _o1_valid_until_for_scope("weird", created) == "2026-01-01T23:59:59"
    # None scope = permanent
    assert _o1_valid_until_for_scope(None, created) is None
    # a corrupt / missing created_at falls back to today's horizon, still finite
    assert _o1_valid_until_for_scope("day", "") is not None
    assert _o1_valid_until_for_scope("day", "garbage") is not None


def _lapsed_row(scope, valid_until):
    m = {}
    if scope is not None:
        m["validity_scope"] = scope
    if valid_until is not None:
        m["valid_until"] = valid_until
    return {"id": "r", "metadata": m}


def test_row_is_lapsed_strict_past_boundaries():
    """_row_is_lapsed lapsed iff a CONCRETE valid_until is STRICTLY in the
    past. Equal to now -> not lapsed; one second past -> lapsed. permanent
    scope never lapses even with a stray valid_until; a corrupt / missing
    valid_until and legacy rows fail open (never drop recall, never KeyError)."""
    from datetime import datetime
    at_day_end = datetime(2026, 1, 1, 23, 59, 59)
    at_midnight = datetime(2026, 1, 2, 0, 0, 0)
    day_vu = "2026-01-01T23:59:59"
    # equal to the horizon -> not yet lapsed
    assert _row_is_lapsed(_lapsed_row("day", day_vu), at_day_end) is False
    # strictly past the horizon -> lapsed
    assert _row_is_lapsed(_lapsed_row("day", day_vu), at_midnight) is True
    # permanent: never lapses even with a stray past valid_until
    assert _row_is_lapsed(_lapsed_row("permanent", "2020-01-01T00:00:00"),
                          at_midnight) is False
    # no concrete horizon (legacy / no key) -> not lapsed
    assert _row_is_lapsed(_lapsed_row(None, None), at_midnight) is False
    # corrupt valid_until -> fail open (never drop a row)
    assert _row_is_lapsed(_lapsed_row("session", "not-a-date"), at_midnight) is False
    # a row with no metadata / not a dict -> fail open
    assert _row_is_lapsed({"id": "r"}, at_midnight) is False
    assert _row_is_lapsed(None, at_midnight) is False


def test_fold_out_of_auto_superseded_or_lapsed():
    """_o1_fold_out_of_auto (wired into the auto-inject face) folds a row OUT
    iff it is superseded OR its validity window has lapsed; a clean legacy row
    (no superseded / no valid_until key) is NEVER folded -> byte-identical
    auto-inject behavior (the EP-1 regression gate)."""
    from datetime import datetime
    at_midnight = datetime(2026, 1, 2, 0, 0, 0)
    assert _o1_fold_out_of_auto(
        {"id": "r", "metadata": {"superseded": True}}, at_midnight) is True
    assert _o1_fold_out_of_auto(
        {"id": "r", "metadata": {"validity_scope": "day",
                                  "valid_until": "2020-01-01T00:00:00"}},
        at_midnight) is True
    # clean legacy row: not folded
    assert _o1_fold_out_of_auto({"id": "r", "metadata": {}}, at_midnight) is False
    assert _o1_fold_out_of_auto({"id": "r"}, at_midnight) is False


# ═══════════════════════════════════════════════════════════════════════════
# GROUP 3 — save-door integration (real _save_memory + _pool_rows_all)
# ═══════════════════════════════════════════════════════════════════════════

def test_save_door_clarify_hit_neutral_standing(tmp_path):
    """Drive the REAL save door: seed a neutral permanent standing docker row,
    then save a day-scoped negation row from profile 'coder'. The new row gains
    the o1_scope_clarify stamp + a validity window; the standing row stays
    byte-identical (NOT auto-invaliaded — the EP-2/owner-confirm gate is the
    terminal)."""
    p = _make_provider(tmp_path, source_profile="coder")
    standing = _old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)
    _seed(tmp_path, [standing])
    new_id = p._save_memory(_NEG_NEW, level="L0", source="manual",
                            validity_scope="day")
    assert new_id, "save door wrote the new row"
    rows = _by_id(_read_shard(tmp_path))
    assert new_id != "old_stand"
    nm = rows[new_id]["metadata"]
    assert nm.get("validity_scope") == "day"
    assert nm.get("valid_until"), "a day scope derives a concrete horizon"
    assert nm.get("o1_scope_clarify") is True, "clarify lane fired on the hit"
    assert "old_stand" in (nm.get("supersedes") or [])
    # RED LINE: the standing row is NOT auto-invaliided
    old = rows["old_stand"]
    assert not old["metadata"].get("superseded"), \
        "clarify lane must not auto-invalidate the standing row"


def test_save_door_clarify_miss_unrelated_no_stamp(tmp_path):
    """A day-scoped negation row about an UNRELATED topic: the clarify lane
    must MISS (no o1_scope_clarify stamp) even though the write face still
    stamps the requested validity window."""
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)])
    new_id = p._save_memory(_NEG_UNRELATED, level="L0", source="manual",
                            validity_scope="day")
    rows = _by_id(_read_shard(tmp_path))
    nm = rows[new_id]["metadata"]
    assert nm.get("validity_scope") == "day"
    assert nm.get("o1_scope_clarify") is not True, \
        "no same-topic standing conflict -> lane stays quiet"


def test_save_door_default_zero_change_no_validity_stamps(tmp_path):
    """RED LINE 默认面字节级零变: the default write path (validity_scope=None,
    the auto / confirmed_draft / flusher re-entry callers) stamps NO
    validity_scope and NO valid_until — byte-identical to the pre-Option-C
    save door. A standing docker row coexists with the plain new row with no
    clarify interference."""
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_old("old_stand", _STANDING_DOCKER, scope="permanent", sp=None)])
    new_id = p._save_memory(_PLAIN_NEW, level="L0", source="manual")
    assert new_id
    nm = _by_id(_read_shard(tmp_path))[new_id]["metadata"]
    assert "validity_scope" not in nm, "default path must NOT stamp a scope"
    assert "valid_until" not in nm, "default path must NOT stamp a horizon"
    assert nm.get("o1_scope_clarify") is not True


# ═══════════════════════════════════════════════════════════════════════════
# GROUP 4 — red-line invariant: 只扩池、不自动作废旧行 (no auto supersede)
# ═══════════════════════════════════════════════════════════════════════════

def test_clarify_lane_never_sets_superseded_true():
    """The terminal of a clarify candidate is the owner's nod through the
    EXISTING C3/EP-2 gate — the lane itself NEVER sets superseded=True on any
    standing row. A hit + a no-hit + a short-scope run all leave every old row
    byte-identical (content / level / metadata snapshot stable)."""
    standing = [_old("old_a", _STANDING_DOCKER, scope="permanent", sp=None),
                _old("old_b", "we use docker", scope="permanent", sp="orchestrator"),
                _old("old_c", "the db backup runs nightly", scope="permanent", sp=None)]
    snapshot = copy.deepcopy(standing)
    # (i) a hit
    new_hit = _new("n1", _NEG_NEW, sp="coder")
    _o1_scope_stamp_clarify_candidates(new_hit, list(standing), dict(_DECAY))
    # (ii) a short-scope trigger on the same pool
    new_short = _new("n2", "the docker registry port is 5001",
                     negation=False, vs="session", sp="coder")
    _o1_scope_stamp_clarify_candidates(new_short, list(standing), dict(_DECAY))
    # (iii) a miss (unrelated negation)
    new_miss = _new("n3", _NEG_UNRELATED, sp="coder")
    _o1_scope_stamp_clarify_candidates(new_miss, list(standing), dict(_DECAY))
    # EVERY standing row is byte-identical: no superseded flip, no content/level
    # mutation anywhere in the pool
    assert standing == snapshot, "the clarify lane mutates ONLY the new row"
    for r in standing:
        assert not r["metadata"].get("superseded"), \
            "lane must never auto-invalidate a standing row"


# ═══════════════════════════════════════════════════════════════════════════
# shared store-hygiene helpers (the C3/O1/EP-1 convention: tempfile, zero
# writes to the live store)
# ═══════════════════════════════════════════════════════════════════════════

def _seed(tmp: pathlib.Path, items, mem_type: str = "fact") -> None:
    (tmp / "memories" / f"{mem_type}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_shard(tmp: pathlib.Path, mem_type: str = "fact") -> list:
    with open(tmp / "memories" / f"{mem_type}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
