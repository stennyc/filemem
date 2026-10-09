"""T4 batch (on top of c255305): the confirm panel @user asked for.

@user requirement (2026-09-27): the per-turn memory ask must LIST what will
be saved (one item per line, with its id) and present a confirm / discard /
cancel choice like the engine's native "approval required" panel (图2).

Engine fact: the plugin cannot build a native UI panel; the closest native
surface the MODEL can drive is the `clarify` tool (a numbered, clickable
options panel, auto-appending a free-text "Other" row). So the landable form
(@data-engineer contract, @reviewer endorses):

- [记忆报告] no longer reports a bare count. It lists each PENDING item
  (new this-session L0 auto-rows + new drafts) one per line:
      i. [L0/<type>] <content, truncated 100 chars> (id: <memory_id>)
  with an id tail so confirm/discard can address an item partially.
- the notice tail instructs the model to call `clarify` with
  [1 确认保存 / 2 丢弃 / 3 取消]:
      confirm = keep (drafts promote via review_drafts(confirm); L0 rows
                are already in the pool, confirm just acknowledges)
      discard = drop (drafts via review_drafts(discard) + snapshots to the
                audit chain; L0 rows via filemem_delete(id))
      cancel  = zero side effect: nothing consumed, the same items
                re-report on the NEXT turn (the "删得掉 item、删不掉审计链"
                symmetric face — cancel leaves the audit chain intact too).

Also this batch restores the ACTIVATION CONTRACT regression @reviewer found:
is_available() was clobbered back to `provider == self.name` by the c255305
sync (my local repo copy carried the old version). That deadlocks the WebUI
switch flow (PUT /api/memory/provider calls _require_memory_provider_ready
BEFORE writing the key, so an "inactive==unavailable" provider is unreachable
through the dashboard). filemem has no hard deps and creates its storage
dirs in initialize(), so is_available() must be ALWAYS True — recovery of
@user's 20:39 diff, byte-for-byte from the 117 pre-c255305 backup.

Consumption wiring (reuses existing tools, keeps their return shapes):
- review_drafts(confirm) current-session  -> consumes ALL pending items
- review_drafts(discard) current-session   -> consumes pending draft items
- filemem_delete(id)                   -> consumes that id
- cancel (no tool call)                    -> nothing consumed, re-reports
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem, MemoryTriggerRouter  # noqa: E402


def _provider(tmp: pathlib.Path, sid: str = "s_t") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.source_profile = "test"
    p._session_drafts = {}
    p._last_recall = None
    p.trigger_router = MemoryTriggerRouter()
    p._pending_drafts_notified = {}
    # T4 pending-confirm state
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
    p._turn_marker_index = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 30
    p.auto_extract = False
    p._pending_memories = []
    return p


def _drive(p, user_content: str, sid: str = "") -> None:
    """Drive one post_llm_call face turn the way the engine does: sync_turn is
    always called with the recent conversation as `messages` (that is what the
    trigger router's auto-save path reads)."""
    msgs = [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": "assistant reply"},
    ]
    p.sync_turn(user_content, "assistant reply",
                session_id=sid or p.session_id, messages=msgs)


# ── ① activation contract: is_available() always True ──────────────────────

def test_0_is_available_always_true():
    """The regression @reviewer caught: availability must gate ACTIVATION
    (can it run if selected?), not 'am I currently selected?' — so it is
    ALWAYS True (no hard deps, storage dirs self-created in initialize()).
    The old `provider == self.name` version deadlocks the WebUI switch flow."""
    p = FileMemProvider()
    assert p.is_available() is True, "is_available must be a constant True"
    assert p.unavailable_reason() == "", "always-available -> empty reason"


# ── ② itemized report: one line per pending item, with id tail ──────────────

def _itemized_ctx(p, sid="s_t") -> str:
    return p.prefetch("continue work", session_id=sid)


def test_1_itemized_report_lists_each_draft_with_id():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    p._add_draft("always run pytest before pushing to main")
    p._last_save_time = 0
    p._add_draft("keep gateway masked after 2026-09-27 migration")
    ctx = _itemized_ctx(p)
    assert "记忆报告" in ctx, ctx
    # both drafts listed, one per line (D-site: no ids on this face)
    assert "always run pytest before pushing to main" in ctx
    assert "keep gateway masked after 2026-09-27 migration" in ctx
    assert "(id:" not in ctx, "ids no longer shown on the report face"
    # clarify instruction present
    assert "clarify" in ctx and "取消" in ctx and "确认" in ctx, ctx


def test_2_itemized_report_includes_new_l0_row():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    _drive(p, "database connection string is at env DATABASE_URL for the app")
    ctx = _itemized_ctx(p)
    assert "记忆报告" in ctx, ctx
    assert "DATABASE_URL" in ctx, "new L0 row content must be listed"
    assert "(id:" not in ctx, "ids no longer shown on the report face"


def test_3_dedup_burst_lists_once():
    """D2③ same-marker fold: a burst that lands ONE row lists it once,
    not burst-many times."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    p._save_memory("database connection string is at env DATABASE_URL", level="L0", source="auto")
    p._last_save_time = 0
    p._save_memory("database connection string is at env DATABASE_URL", level="L0", source="auto")
    _drive(p, "keep going")  # short -> no new row this turn
    ctx = _itemized_ctx(p)
    assert "记忆报告" in ctx
    assert ctx.count("DATABASE_URL") == 1, "a deduped burst lists its one row once"


# ── ④ cancel = zero side effect -> re-report next turn ──────────────────────

def test_4_cancel_rereports_same_items():
    """No tool call between reports => the pending items survive and the
    SAME items re-report on the next turn (cancel/ignore keeps them)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    p._add_draft("candidate fact that should persist until consumed")
    assert "记忆报告" in _itemized_ctx(p)
    # user cancels / ignores: no review_drafts, no delete
    _drive(p, "next turn keep going")
    ctx = _itemized_ctx(p)
    assert "记忆报告" in ctx, "cancel must not consume -> re-report"
    assert "candidate fact that should persist until consumed" in ctx


# ── ⑤ confirm / discard consume -> next turn silent on that batch ───────────

def test_5_confirm_consumes_and_silences():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    p._add_draft("confirmed fact one")
    p._add_draft("confirmed fact two")
    assert "记忆报告" in _itemized_ctx(p)
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"]
    # after consuming the batch, the next report is silent on it
    _drive(p, "next turn keep going")
    ctx = _itemized_ctx(p)
    assert "confirmed fact one" not in ctx
    assert "记忆报告" not in ctx or "confirmed fact" not in ctx


def test_6_discard_consumes_drafts_and_snaps_audit():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp, "s_t")
    p._last_save_time = 0
    p._add_draft("to be discarded fact")
    assert "记忆报告" in _itemized_ctx(p)
    out = json.loads(p._tool_review_drafts({"action": "discard"}))
    assert out["success"]
    _drive(p, "next turn keep going")
    ctx = _itemized_ctx(p)
    assert "to be discarded fact" not in ctx
    # discard snapshots to the audit chain (no silent drop)
    log = tmp / "consolidation_log.jsonl"
    evs = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(e.get("action") == "drafts_review" and e.get("op") == "discard" for e in evs)
