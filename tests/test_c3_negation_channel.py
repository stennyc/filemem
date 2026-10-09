"""C3 (t_03a0c213): 写时否定候选扩池（EP-1 negation 通道 + EP-2 确认门兜底）.

The docker contradiction is a NEGATION that EP-1's deterministic 0.5-Jaccard
save-door stamp MISSES ("部署时使用 docker" vs "今天不使用 docker" share too few
raw tokens; the negation words 不/没/no/stop themselves are the delta). C3
adds a PARALLEL second channel at the save door: when a NEW row's content
carries a negation / premise-change signal, recall key concepts against the
same shard's strictly-older rows (same profile) — and record the strong
overlaps as supersedes CANDIDATES in the EP-1 pool (扩展池). The safety red
line is honoured by construction: the negation channel only ENLARGES the
candidate pool; it NEVER auto-invalidates an old row. Final verdict belongs to
the EXISTING EP-2 LLM confirm gate (host shared main-model channel,
budget-gated, fail-soft). EP-2 hang/timeout/budget-out = candidate stays in
the audit surface; no stamp, no old-row touch, no new path.

Case map (acceptance, task body 验收 #1):
  a. docker 反例自动线: seed "部署时使用 docker" (an old fact row); the save
     door writes "今天不使用 docker" (low raw-overlap, EP-1 gate MUST miss it)
     -> the negation channel records the old row as a supersedes candidate
     (扩展池) WITHOUT marking it superseded -> EP-2 confirms (mock yes) ->
     the old row flips superseded=True (出 auto-inject 主池, 不进 top-N), the
     new row ranks normally (渲染谱系折叠沿用 O1).
  b. 误报例: a negation word appears but the concept is UNRELATED to the old
     row ("今天不部署到生产环境" vs "部署时使用 docker") -> the channel stays
     quiet (no candidate), AND a genuine over-fire ("no docker in staging")
     where the concept IS shared is recorded but EP-2 judges NO -> old row
     NOT stamped, candidate stays in the audit surface.
  c. fail-soft 例: the EP-2 channel is unavailable (host main_runtime None)
     -> zero superseded stamps, zero exceptions, and the 0.5 channel's
     behavior is byte-unchanged.

Store hygiene: tempfile-tree partial provider (the O1/EP-1 shape), ZERO
writes to the live store. EP-2 is driven through the REAL
_supersede_confirm_candidates face with the shared host-LLM fake
(_llm_host_fake.install) — O1 feeds the flow, C3 reuses it.
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
    _c3_has_negation_signal,
    _c3_key_concepts,
    _c3_concept_recall,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402
import pytest  # noqa: F401
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

_RECENT = "2026-10-01T00:00:00"  # recent -> no TTL expiry on any run date

# ── the docker contradiction pair (LOW raw-Jaccard, proven below) ─────────
# A true negation correction: EP-1's 0.5 gate misses it, the negation
# channel catches it. The 'docker' key concept is shared.
DOCKER_OLD = "部署时使用 docker"
DOCKER_NEW = "今天不使用 docker"
DOCKER_QUERY = "docker 部署"

# A premature / unrelated negation: a negation word ('不') with NO key concept
# shared with the docker old row -> the negation channel stays quiet.
FP_PREMATURE = "今天不部署到生产环境"

# An OVER-FIRE: the negation word IS present and 'docker' IS shared, but the
# new row is about a DIFFERENT stage (staging) — EP-2 judges NO.
FP_OVERFIRE = "no docker in staging today"


def _make_provider(tmp: pathlib.Path, source_profile: str = "default") -> FileMemProvider:
    """Same partial-provider shape as test_o1_supersede (enough fields for the
    REAL _save_memory / search_memories / _supersede_confirm_candidates paths,
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
    p.session_id = "s-c3"
    p._turn_active_sid = "s-c3"
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
         created_at: str = _RECENT) -> dict:
    """A minimal L2 pool row stamped with the shared P0-2 origin channel."""
    return {
        "id": rid, "type": mem_type, "content": content, "level": "L2",
        "metadata": {
            "origin": {"kind": "user", "trust": "high"},
            "source_profile": source_profile,
        },
        "created_at": created_at, "updated_at": created_at, "occurred_at": None,
        "source": "manual", "reference_count": 0, "salience": 0.5,
        "last_referenced": "", "ttl_days": None,
    }


def _seed(tmp: pathlib.Path, items, mem_type: str = "fact") -> None:
    (tmp / "memories" / f"{mem_type}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_shard(tmp: pathlib.Path, mem_type: str = "fact") -> list:
    with open(tmp / "memories" / f"{mem_type}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _by_id(rows) -> dict:
    return {r["id"]: r for r in rows if isinstance(r, dict) and "id" in r}


def _supersede_confirm_audit(tmp: pathlib.Path) -> list:
    """Only the C3-relevant audit rows: the EP-2 supersede_confirm verdicts.
    The auto save door ALSO writes llm_call (classify/distill) and
    no_popup_discard telemetry rows to the SAME append-only channel — those
    are a separate concern and are filtered out here, so a test asserts on
    the supersession decision surface, not the raw host-call log."""
    log = tmp / "consolidation_log.jsonl"
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("action") == "supersede_confirm":
            out.append(row)
    return out


# ══ A — docker 反例自动线（写时扩池 + EP-2 确认 -> 旧行出池）═════════════

def test_a_ep1_gate_misses_the_docker_negation():
    """GAP PROOF: EP-1's deterministic 0.5-Jaccard channel MISSES the docker
    negation pair (the exact known gap the O1 memo pinned). The negation
    channel is what C3 adds — its detectors are the premise of every
    assertion below, pinned separately so a tokenizer / wordlist drift is
    caught at the source, not downstream."""
    # premise: raw Jaccard of the pair is below the 0.5 gate
    assert _content_overlap_score(DOCKER_NEW, DOCKER_OLD) < 0.5, (
        "docker pair no longer low-overlap — pin a pair the EP-1 gate misses")
    # the NEW row carries a negation signal (不 / 使用 docker)
    assert _c3_has_negation_signal(DOCKER_NEW) is True
    # after negation/temporal/stopword stripping, the docker KEY concepts are
    # recalled against the old row with strong overlap
    assert _c3_concept_recall(DOCKER_NEW, DOCKER_OLD) >= 0.5, (
        f"negation key-concept recall drifted below the gate: "
        f"{_c3_key_concepts(DOCKER_NEW)} vs {_c3_key_concepts(DOCKER_OLD)}")


def test_a_save_door_records_negation_candidate_not_superseded(tmp_path):
    """Case a (write side): seeding the docker old row then driving the REAL
    save door with the negation new row makes the negation channel EXTEND the
    candidate pool — the new row gains the supersedes link + the c3 mark —
    while the OLD row is NOT yet marked superseded (只扩池、不自动作废)."""
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    out_id = p._save_memory(DOCKER_NEW, level="L0", source="auto")
    assert out_id, "save door wrote the new row"
    rows = _by_id(_read_shard(tmp_path))
    new_id = out_id
    assert new_id != "fact_docker"
    nm = rows[new_id]["metadata"]
    # the new row recorded the docker old row as a supersedes CANDIDATE, and
    # is itself an EP-2-confirmable candidate (the negation channel extends the
    # EP-1 candidate pool, so the existing confirm face picks it up)
    assert "fact_docker" in (nm.get("supersedes") or [])
    assert nm.get("supersede_state") == "candidate"
    # marked as a C3 negation-channel candidate (EP-2 / audit distinguish it)
    assert nm.get("c3_negation") is True
    # RED LINE: the old row is NOT auto-invalidated (no superseded flip yet —
    # only EP-2's verdict may flip it). A lineage back-link hint is allowed.
    old = rows["fact_docker"]
    assert not old["metadata"].get("superseded"), \
        "negation channel must NOT auto-invalidate the old row at write time"


def test_a_ep2_yes_confirms_and_flips_old_row(tmp_path, monkeypatch):
    """Case a (confirm side): the candidate from the negation channel walks the
    EXISTING EP-2 confirm face. A host-LLM verdict of yes flips the old row
    to superseded=True (出 auto-inject 主池 / 不进 top-N; 渲染谱系折叠沿用
    O1) and the new row's candidate is confirmed. The new row ranks normally
    in the explicit search face."""
    fake, state = install(monkeypatch,
                         replies=[{"confirmed": True, "reason": "genuine negation correction"}])
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    new_id = p._save_memory(DOCKER_NEW, level="L0", source="auto")
    p._supersede_confirm_candidates()
    rows = _by_id(_read_shard(tmp_path))
    # new row: confirmed
    assert rows[new_id]["metadata"].get("supersede_state") == "confirmed"
    # old row: flipped superseded + confirmed (the one EP-2 verdict)
    old = rows["fact_docker"]
    assert old["metadata"].get("superseded") is True
    assert old["metadata"].get("superseded_confirmed") is True
    assert old["metadata"].get("superseded_by") == new_id
    # exactly ONE EP-2 confirm verdict was recorded for this pair (the
    # save-door's own classify/distill host calls are a separate concern and
    # are NOT counted here — this is the C3-relevant audit surface)
    aud = _supersede_confirm_audit(tmp_path)
    assert len(aud) == 1, f"exactly one confirm verdict, got {aud}"
    assert aud[0]["new_id"] == new_id and aud[0]["target_id"] == "fact_docker"
    assert aud[0]["confirmed"] is True
    # O1 fold predicate agrees: the old row is now folded OUT of auto-inject,
    # the new row stays active
    assert _o1_fold_out_of_auto(old) is True
    assert _o1_fold_out_of_auto(rows[new_id]) is False
    # the new row ranks normally in the EXPLICIT face (both readable; the old
    # row is the lineage-expansion backfill, the new row is in the main pool)
    ids = [r["id"] for r in p.search_memories(DOCKER_QUERY, limit=8, min_score=0.0,
                                               profile_scope=None)]
    assert new_id in ids, f"new row must rank normally, got {ids}"


# ══ B — 误报例（概念无关 -> 通道静默；概念有交 -> 记录但 EP-2 判 no）══════

def test_b_premature_negation_channel_stays_quiet(tmp_path):
    """A negation word with NO key concept shared with the docker old row
    (今天不部署到生产环境 vs 部署时使用 docker) must NOT extend the candidate
    pool: the new row carries no supersedes link and no c3 mark, the old row
    is untouched. The channel only fires on a real concept recall."""
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    new_id = p._save_memory(FP_PREMATURE, level="L0", source="auto")
    assert new_id
    rows = _by_id(_read_shard(tmp_path))
    nm = rows[new_id]["metadata"]
    assert not nm.get("supersedes"), "unrelated negation must not link candidates"
    assert nm.get("c3_negation") is not True, \
        "unrelated negation must not carry the c3 mark"
    # the docker old row is untouched (no back-link, no superseded flip)
    old = rows["fact_docker"]
    assert old["metadata"].get("superseded_by") != new_id
    assert not old["metadata"].get("superseded")


def test_b_overfire_recorded_but_ep2_no_keeps_old(tmp_path, monkeypatch):
    """An over-fire (the negation word IS present and 'docker' IS shared, but
    the new row is about a DIFFERENT stage) is recorded as a candidate, then
    the EP-2 confirm gate judges NO -> the old row is NOT stamped superseded
    (只扩池、不自动作废; 候选留审计面). This is the safety red line
    end-to-end: a recorded candidate that EP-2 vetoes leaves zero invalidation
    on the old row."""
    fake, state = install(monkeypatch,
                         replies=[{"confirmed": False, "reason": "different deployment stage"}])
    p = _make_provider(tmp_path, source_profile="default")
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    new_id = p._save_memory(FP_OVERFIRE, level="L0", source="auto")
    p._supersede_confirm_candidates()
    rows = _by_id(_read_shard(tmp_path))
    # the negation channel DID record the candidate (concept shared)
    nm = rows[new_id]["metadata"]
    assert "fact_docker" in (nm.get("supersedes") or []), \
        "shared-concept negation should extend the pool"
    # EP-2 judged no: the new row STAYS a candidate (不吞脏, retries later)
    assert nm.get("supersede_state") == "candidate"
    # and the old row is NOT invalidated (the red line)
    old = rows["fact_docker"]
    assert not old["metadata"].get("superseded"), \
        "a vetoed EP-2 verdict must NOT stamp the old row"
    assert "superseded_confirmed" not in old["metadata"]
    # audit surface: the veto is recorded (the candidate lives on for review)
    aud = _supersede_confirm_audit(tmp_path)
    assert any(a.get("confirmed") is False for a in aud), \
        f"veto must be audited, got {aud}"
    assert len(aud) == 1, f"exactly one confirm verdict, got {aud}"


# ══ C — fail-soft（EP-2 通道不可用 -> 零 stamp、零异常、0.5 通道行为不变）═

def test_c_ep2_unavailable_fail_soft_no_stamp(tmp_path):
    """With the host main_runtime unavailable (a standalone / budget-out
    posture), the negation channel still EXTENDS the pool at write time, but
    the EP-2 confirm face is a no-op (skip, not an error): zero superseded
    stamps, zero exceptions, zero audit rows — the candidate stays in the
    audit surface for a later tick, and the 0.5 channel's behavior is
    untouched."""
    p = _make_provider(tmp_path, source_profile="default")
    p._main_runtime = None  # host shared main-model channel unavailable
    _seed(tmp_path, [_row("fact_docker", DOCKER_OLD)])
    new_id = p._save_memory(DOCKER_NEW, level="L0", source="auto")
    p._supersede_confirm_candidates()  # must not raise (skip, not error)
    rows = _by_id(_read_shard(tmp_path))
    nm = rows[new_id]["metadata"]
    # write-side pool extension happened regardless of EP-2 availability
    assert "fact_docker" in (nm.get("supersedes") or [])
    # the candidate is still PENDING EP-2 (not confirmed) — it stays in the
    # audit surface for a later tick; zero auto-invalidation happened
    assert nm.get("supersede_state") == "candidate"
    assert not rows["fact_docker"]["metadata"].get("superseded")
    assert not rows["fact_docker"]["metadata"].get("superseded_confirmed")
    # no EP-2 VERDICT was recorded: the save door's own llm_call telemetry rows
    # (classify/distill) may still be in the log, but there is NO
    # supersede_confirm row — the confirm face skipped (not a verdict).
    assert _supersede_confirm_audit(tmp_path) == [], \
        "no EP-2 verdict -> no supersede_confirm audit row"


def test_c_zero_negative_channel_no_c3_marks(tmp_path):
    """A plain (no-negation) save with a strong-overlap pair still goes
    through the EXISTING 0.5 channel byte-unchanged (C3 adds nothing to it):
    the new row carries no c3 mark, and the 0.5 channel's stamp behavior is
    exactly what test_ep_1_lineage pins."""
    p = _make_provider(tmp_path, source_profile="default")
    old = "部署时使用 docker"
    new = "部署时使用 docker 加上 k8s 缓存"  # strong overlap, NO negation
    _seed(tmp_path, [_row("fact_x", old)])
    new_id = p._save_memory(new, level="L0", source="auto")
    rows = _by_id(_read_shard(tmp_path))
    assert rows[new_id]["metadata"].get("c3_negation") is not True, \
        "the negation channel must not fire on a no-negation save"


# ══ D — 同 profile 内检索（O1 1D 默认隔离；不跨 profile 扩池）══════════

def test_d_cross_profile_never_a_negation_candidate(tmp_path):
    """Design pin #5: the negation channel respects O1 1D's default
    same-profile isolation. A row stamped by ANOTHER profile is NEVER a
    candidate for this profile's negation correction (the shared pool must
    not let profile A's '今天不用 docker' clobber profile B's docker fact); a
    neutral (unstamped) row is claimable, as with the 0.5 channel. Verified
    directly on the C3 stamp function — the same guard the save door runs."""
    from __init__ import _c3_stamp_negation_candidates
    new_ts = "2026-10-07T00:00:00"
    old_ts = "2026-10-01T00:00:00"

    def _c3_new(sp):
        return {"id": "newrow", "type": "fact", "content": "今天不使用 docker",
                "level": "L0", "metadata": {"source_profile": sp},
                "created_at": new_ts, "updated_at": new_ts}

    def _c3_old(rid, sp):
        r = _row(rid, "部署时使用 docker", source_profile=sp, created_at=old_ts)
        if sp is None:
            del r["metadata"]["source_profile"]
        return r

    # cross-profile: orchestrator old row, coder new row -> NO candidate
    out = _c3_stamp_negation_candidates(
        _c3_new("coder"), [_c3_old("old_x", "orchestrator")], dict(DECAY_DEFAULTS))
    assert out == [], "a cross-profile row must never be a negation candidate"

    # same-profile: coder old row, coder new row -> candidate fires
    out = _c3_stamp_negation_candidates(
        _c3_new("coder"), [_c3_old("old_same", "coder")], dict(DECAY_DEFAULTS))
    assert [r["id"] for r in out] == ["old_same"]

    # neutral (unstamped) old row, coder new row -> claimable
    out = _c3_stamp_negation_candidates(
        _c3_new("coder"), [_c3_old("old_neutral", None)], dict(DECAY_DEFAULTS))
    assert [r["id"] for r in out] == ["old_neutral"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
