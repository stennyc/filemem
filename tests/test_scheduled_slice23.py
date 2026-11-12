"""scheduled 门 Step 2 + Step 3 (owner 2026-11-12): 不衰减豁免 + 到期唤醒.

Step 2 — a PENDING scheduled row is exempt from the auto-inject / decay faces
so it stays "waiting" until its due point:
  * _auto_recall_filter folds PENDING scheduled rows out of the auto-inject
    pool (pre-due they are NOT live context; only the due-wake face surfaces
    them). Resolved (done/dismissed) rows are NOT folded — they may auto-
    inject like any settled memory.
  * salience-decay / sweep are structurally safe for scheduled (a pending row
    carries no superseded/stale flag, and L2 has auto_cleanup=False so the
    O7 sweep never physically removes it) — locked here by asserting a
    pending scheduled row survives both faces.

Step 3 — the due-wake face:
  * _scheduled_due_rows is a PURE code-side time compare (zero LLM): a row is
    due iff metadata.due_at <= now AND status == "pending".
  * _scheduled_due_block builds the user-visible [到期事项] notice; it is
    folded into the pre_llm_call notice face (turn_line) so a session with NO
    drafts but a due row still gets the reminder.
  * the LLM's job is limited to "what to do / report" AFTER the code has
    established due-ness (_SCHEDULED_WAKE_PROMPT); the user's verdict lands
    via filemem_scheduled(action="resolve").

Run:
    cd /root/.hermes/plugins/filemem
    /root/.hermes/plugin-venv/bin/python -m pytest tests/test_scheduled_slice23.py -q
"""
import json, sys, threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MEMORY_TYPES, MemoryTriggerRouter,
    _auto_recall_filter, _scheduled_pending_fold_out,
    _SCHEDULED_WAKE_PROMPT,
)


def _make_provider(tmp: Path, sid: str = "s1") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.episodic_dir = None
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.session_id = sid
    p._turn_active_sid = sid
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._last_save_time = 0
    p._save_interval = 0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._pending_drafts_notified = {}
    p._shared_mem = None
    p._append_consolidation_event = lambda ev: _noop_append(tmp, ev)
    return p


def _noop_append(tmp: Path, ev: dict) -> None:
    log = tmp / "consolidation_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _land_scheduled(p, *, content="9200 健康检查", due_at="2026-12-01T00:00:00",
                    action_kind="remind", on_due_do="检查 9200 存活",
                    expects="", status="pending", source_profile="test"):
    """Land a confirmed scheduled row directly into the shard (bypass the
    suggest->confirm chain; Step 1 already locks that chain)."""
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": f"sched_test_{abs(hash(content + on_due_do)) % 10**8:08d}",
        "type": "scheduled",
        "content": content,
        "level": "L2",
        "source": "confirmed_draft",
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "due_at": due_at,
            "action_kind": action_kind,
            "on_due_do": on_due_do,
            "expects": expects,
            "status": status,
            "source_profile": source_profile,
        },
    }
    rows = p._scheduled_load_rows()
    rows.append(row)
    p._scheduled_write_rows(rows)
    return row


@pytest.fixture
def p(tmp_path):
    return _make_provider(tmp_path)


# ── Step 2① : PENDING scheduled rows fold out of auto-inject ────────────────
def test_pending_scheduled_fold_out_of_auto_inject(p):
    row = _land_scheduled(p)  # status=pending
    pool = [dict(row), {"id": "fact_1", "type": "fact", "content": "x",
                          "metadata": {"source_profile": "test"}}]
    kept = _auto_recall_filter(pool, "test")
    kept_ids = [r.get("id") for r in kept]
    # the pending scheduled row is folded out; the plain fact row stays
    assert row["id"] not in kept_ids
    assert "fact_1" in kept_ids
    # predicate is pure + type-keyed (locked unit):
    assert _scheduled_pending_fold_out(row) is True
    assert _scheduled_pending_fold_out(
        {"type": "scheduled", "metadata": {"status": "done"}}) is False


def test_resolved_scheduled_not_folded_out(p):
    row = _land_scheduled(p, status="done")
    assert _scheduled_pending_fold_out(row) is False
    kept = _auto_recall_filter([dict(row)], "test")
    assert [r.get("id") for r in kept] == [row["id"]]


def test_scheduled_pending_fold_out_fail_open_on_garbage():
    assert _scheduled_pending_fold_out(None) is False
    assert _scheduled_pending_fold_out({"type": "fact"}) is False
    # a scheduled row with no metadata -> not "pending" -> NOT folded
    assert _scheduled_pending_fold_out({"type": "scheduled"}) is False


# ── Step 2② : a pending scheduled row survives the decay/sweep faces ────────
def _flagged_row(type_="fact", **meta_over):
    m = {"superseded": True, "stale": True}
    m.update(meta_over)
    return {
        "id": "x", "type": type_, "content": "old fact", "level": "L1",
        "source": "auto", "created_at": "2020-01-01T00:00:00+00:00",
        "last_referenced": "2020-01-02T00:00:00+00:00",
        "salience": 0.9, "metadata": m,
    }


def test_pending_scheduled_row_has_no_decay_flags(p):
    # the structural-safety claim, made concrete: a pending scheduled row
    # carries NO superseded/stale flag, so _salience_decay_downweight's
    # "only downweight flagged rows" gate never touches it.
    row = _land_scheduled(p)
    m = row["metadata"]
    assert m.get("superseded") in (None, False)
    assert m.get("stale") in (None, False)


def test_scheduled_l2_never_swept_expired(p):
    # O7 sweep only moves rows whose LEVEL has auto_cleanup=True. L2 is
    # permanent (auto_cleanup=False) -> a scheduled row (always L2) is never
    # physically swept, even past due_at.
    from __init__ import TTL_CONFIG
    assert TTL_CONFIG.get("L2", {}).get("auto_cleanup", False) is False
    row = _land_scheduled(p)
    assert row["level"] == "L2"


# ── Step 3① : the pure code-side due scanner ─────────────────────────────────
def test_due_rows_code_side_compare(p):
    past = "2026-01-01T00:00:00"
    far_future = "2999-01-01T00:00:00"
    r_due = _land_scheduled(p, content="due", due_at=past)
    r_future = _land_scheduled(p, content="future", due_at=far_future,
                               on_due_do="later")
    r_resolved = _land_scheduled(p, content="resolved", due_at=past,
                                 status="done", on_due_do="x")
    due = p._scheduled_due_rows(status="pending")
    due_ids = [r["id"] for r in due]
    assert r_due["id"] in due_ids          # past + pending -> due
    assert r_future["id"] not in due_ids  # future -> not due yet
    assert r_resolved["id"] not in due_ids  # resolved -> not re-surfaced


def test_due_rows_accepts_explicit_now(p):
    row = _land_scheduled(p, due_at="2030-01-01T00:00:00")
    # now=2035 -> due; now=2025 -> not due (same row, pure time compare)
    assert [r["id"] for r in p._scheduled_due_rows(now_iso="2035-01-01T00:00:00")] == [row["id"]]
    assert p._scheduled_due_rows(now_iso="2025-01-01T00:00:00") == []


def test_due_rows_fail_soft_on_unreadable_shard(p):
    p.memories_dir / "scheduled.json"  # ensure path
    open(p._scheduled_shard_path(), "w").write("{ not valid json ")
    # unparseable shard -> [] (advisory face, never blocks the turn)
    assert p._scheduled_due_rows() == []


# ── Step 3② : the user-visible due block rides the notice face ───────────────
def test_due_block_lists_only_due_pending_rows(p):
    _land_scheduled(p, content="due-now", due_at="2026-01-01T00:00:00")
    _land_scheduled(p, content="not-due", due_at="2999-01-01T00:00:00",
                    on_due_do="x")
    block = p._scheduled_due_block()
    assert "due-now" in block
    assert "not-due" not in block
    # user-visible instruction + the resolve verdict path are both in the block
    assert "处置规则" in block
    assert "resolve" in block
    # the LLM-facing field names are rendered as user-facing Chinese labels
    assert "到期动作" in block
    assert "on_due_do" not in block


def test_due_block_does_not_leak_internal_prompt(p):
    """reviewer 2026-11-12 nit + smoke: the pre_llm_call notice face must
    NOT splice the internal _SCHEDULED_WAKE_PROMPT template (with its
    '以下是 N 条' header + {each:...} placeholder) into the user-visible
    [到期事项] block. The block carries ONLY the per-row fields + a short
    user-facing instruction + the resolve verdict line."""
    _land_scheduled(p, content="due-now", due_at="2026-01-01T00:00:00")
    block = p._scheduled_due_block()
    assert block  # non-empty for a due row
    assert "以下是 N 条" not in block            # internal template header
    assert "{each:" not in block                # internal placeholder
    # the internal LLM prompt constant still EXISTS and is untouched (used by
    # a future LLM face), it just is not what this notice face renders.
    from __init__ import _SCHEDULED_WAKE_PROMPT
    assert _SCHEDULED_WAKE_PROMPT and "以下是 N 条" in _SCHEDULED_WAKE_PROMPT
    assert _SCHEDULED_WAKE_PROMPT not in block


def test_due_block_empty_when_nothing_due(p):
    _land_scheduled(p, content="future", due_at="2999-01-01T00:00:00",
                    on_due_do="x")
    assert p._scheduled_due_block() == ""


def test_due_block_rides_turn_line_notice_face(p):
    # a session with ZERO drafts but a DUE scheduled row still surfaces the
    # reminder via the pre_llm_call notice face (the owner's boundary: no
    # background push, reminder fires at next session start).
    _land_scheduled(p, content="due-now", due_at="2026-01-01T00:00:00")
    # no drafts files at all -> the draft notice is empty, but the due block
    # must still appear in the composed context.
    out = p._pending_draft_notice("", session_id=p.session_id)
    assert "[到期事项]" in out
    assert "due-now" in out


def test_wake_prompt_constant_is_present():
    # the read-side LLM prompt (owner's 'LLM 建议需不需要新提示词' ask) is a
    # locked module constant; it governs auto-vs-remind behavior.
    assert "auto" in _SCHEDULED_WAKE_PROMPT
    assert "remind" in _SCHEDULED_WAKE_PROMPT
    assert "on_due_do" in _SCHEDULED_WAKE_PROMPT or "到期动作" in _SCHEDULED_WAKE_PROMPT


# ── Step 3③ : end-to-end resolve closes the loop (due -> resolve) ────────────
def test_due_then_resolve_done_stops_resurfacing(p):
    row = _land_scheduled(p, content="due", due_at="2026-01-01T00:00:00")
    assert [r["id"] for r in p._scheduled_due_rows()] == [row["id"]]
    out = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": row["id"], "verdict": "done"}))
    assert out["success"] is True and out["status"] == "done"
    # resolved rows stop being surfaced as due
    assert p._scheduled_due_rows(status="pending") == []
    assert p._scheduled_due_block() == ""
