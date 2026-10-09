"""T3 batch (on top of 4919de8): turn-level memory-confirmation surface.

@user design requirement (2026-09-27): "ask whether to SAVE memory at TURN
END, not session_end". Engine fact (reviewer-verified): the engine drops
post_llm_call return values (same fate as on_session_end), so the only
user-visible face is the NEXT turn's pre_llm_call injection. Landable form
(@data-engineer contract, @reviewer endorses the wording):

- every turn-end (sync_turn) takes a cumulative snapshot of this session:
  L0 auto-saves written this turn + current draft count
- the next turn's prefetch leads with a single [记忆报告] incremental line
  ("last turn added N new L0 auto-saves / M drafts - ask the user keep or
  discard") ONLY when the delta since the last reported point is > 0
- session-end / historical [未决草稿] batch notice stays untouched (one-shot
  per session, first turn)

Plus the two non-blocking items pinned for this batch:
- empty-shell draft files: a session-drafts write that empties the current
  session's file now UNLINKS it instead of leaving a `[]` shell (186 shells
  on 117 = this exact path); historical target path already unlinks
- audit chain: drafts_review (review_drafts confirm/discard, both scopes)
  and memory_delete append a snapshot row to consolidation_log.jsonl -
  "items can be deleted, the audit chain cannot be broken": the deleted
  content itself lands in the audit row.
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem, MemoryTriggerRouter  # noqa: E402


def _make_provider(tmp: pathlib.Path, session_id: str = "s_cur") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p._session_drafts = {}
    p._last_recall = None
    p.trigger_router = MemoryTriggerRouter()
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_active_sid = None
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p._turn_marker_index = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 30
    p.sessions_dir = None
    p.auto_extract = False
    p._pending_memories = []
    return p


def _drive_turn(p, user_content: str, sid: str = "") -> None:
    """Drive one post_llm_call face turn (sync_turn) the way the hook does.
    The engine always passes the recent conversation as `messages` — that is
    what the trigger router's L0 auto-save path reads, so a fact-candidate
    turn only auto-saves when `messages` is supplied."""
    msgs = [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": "assistant reply"},
    ]
    p.sync_turn(user_content, "assistant reply",
                session_id=sid or p.session_id, messages=msgs)


def _audit_events(tmp: pathlib.Path) -> list:
    p = tmp / "consolidation_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# -- 1. same instance, two consecutive turns: incremental report, then silence --

def test_1_turn_report_then_rereport_until_consumed():
    """T4 supersedes T3's per-turn *delta count*: pending items stay listed
    and RE-REPORT every turn until consumed (confirm/discard/delete). Here
    turn 1 writes 1 L0 + 1 draft -> turn 2 lists both -> turn 3 (no
    consumption) STILL lists them (cancel/ignore = zero side effect)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_t")

    # turn 1: one substantive fact (L0 auto-save) + one draft
    p._last_save_time = 0  # allow the aperture-widen fallback to really write
    _drive_turn(p, "always run pytest before pushing to main branch in this repo")
    p._session_drafts["s_t"] = []
    p._last_save_time = 0
    p._add_draft("decided: keep gateway masked after 2026-09-27 migration")

    # turn 2: the NEXT pre_llm_call must lead with the itemized report
    ctx2 = p.prefetch("continue work", session_id="s_t")
    assert "记忆报告" in ctx2, ctx2
    assert "always run pytest" in ctx2, ctx2
    assert "keep gateway masked" in ctx2, ctx2

    # turn 3: no consumption -> the SAME items re-report (not silent)
    _drive_turn(p, "keep going")
    ctx3 = p.prefetch("more", session_id="s_t")
    assert "记忆报告" in ctx3, ctx3
    assert "keep gateway masked" in ctx3, "cancel/ignore must not consume"


def test_2_dedup_turn_lists_once():
    """Same-marker fold (D2③): a burst that lands ONE row lists it ONCE in
    the panel (content-dedup), not the burst size."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_t")
    p._last_save_time = 0  # allow a real write
    p._save_memory("database connection string is at env DATABASE_URL", level="L0", source="auto")
    # second identical fact: dedup update, NOT a new row -> not re-listed
    p._last_save_time = 0
    p._save_memory("database connection string is at env DATABASE_URL", level="L0", source="auto")
    p.sync_turn("user turn three", "a", session_id="s_t")
    ctx = p.prefetch("next", session_id="s_t")
    assert "记忆报告" in ctx
    # one row, listed once
    assert ctx.count("(id:") == 1, ctx
    assert "DATABASE_URL" in ctx


# -- 3. empty-shell draft unlink ------------------------------------------------

def test_3_empty_session_draft_write_unlinks_shell():
    """The 186-shell path: session clears its drafts and persists - the file
    is UNLINKED, not rewritten as a `[]` shell."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_x")
    p._session_drafts["s_x"] = [MemoryItem(id="d1", type="fact", content="x", level="L1")]
    p._save_session_drafts()
    shell = tmp / "drafts_s_x.json"
    assert shell.exists()
    p._session_drafts["s_x"] = []
    p._save_session_drafts()
    assert not shell.exists(), "empty write must unlink the shell"


def test_3b_empty_save_without_file_is_noop():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_y")
    p._session_drafts["s_y"] = []
    p._save_session_drafts()  # no prior file -> no crash, no shell created
    assert not (tmp / "drafts_s_y.json").exists()


def test_4_review_drafts_confirm_unlinks_current_session_file():
    """Current-session confirm clears + persists -> shell gone, audit row in."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_z")
    p._session_drafts["s_z"] = [MemoryItem(id="d9", type="fact", content="z", level="L1")]
    p._save_session_drafts()
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"] and out["count"] == 1
    assert not (tmp / "drafts_s_z.json").exists(), "confirm-clear must not leave a shell"
    ev = [e for e in _audit_events(tmp) if e.get("action") == "drafts_review"]
    assert ev, "drafts_review audit row missing"
    assert ev[-1].get("target_session") == "s_z" and ev[-1].get("op") == "confirm"


# -- 5. audit chain: historical discard snapshots content, delete snapshots too --

def test_5_discard_snapshot_in_audit_row():
    """discard of a historical file appends a drafts_review row that SNAPSHOTs
    the discarded content - item deletable, audit chain unbreakable."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_a")
    item = MemoryItem(id="dh1", type="fact", content="historical fact alpha", level="L1")
    (tmp / "drafts_s_old.json").write_text(
        json.dumps([item.to_dict()]), encoding="utf-8")
    out = json.loads(p._tool_review_drafts(
        {"action": "discard", "target": "drafts_s_old.json"}))
    assert out["success"] and out["discarded_count"] == 1
    assert not (tmp / "drafts_s_old.json").exists()
    ev = [e for e in _audit_events(tmp) if e.get("action") == "drafts_review"]
    assert ev and ev[-1]["op"] == "discard"
    snap = ev[-1]["snapshot"]
    assert any("historical fact alpha" in (s.get("content") or "") for s in snap), snap


def test_6_memory_delete_appends_audit_row_with_content():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_b")
    p._last_save_time = 0
    p._save_memory("unique fact beta for delete test", level="L0", source="auto")
    fact_file = p.memories_dir / "fact.json"
    rows = json.loads(fact_file.read_text(encoding="utf-8"))
    mid = rows[0]["id"]
    out = json.loads(p._tool_delete({"id": mid}))
    assert out["success"]
    assert not (json.loads(fact_file.read_text(encoding="utf-8")) or [])
    ev = [e for e in _audit_events(tmp) if e.get("action") == "memory_delete"]
    assert ev and ev[-1]["id"] == mid
    assert ev[-1]["snapshot"][0]["content"] == "unique fact beta for delete test"


# TESTS REMOVED: test_7_memory_update_appends_audit_row_with_snapshot
# and test_8_memory_update_preserves_p1_1b_idempotent_upsert
# were deleted as part of filemem_update tool rollback (unapproved change)
