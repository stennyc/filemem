"""scheduled 门 Step 1 (owner 2026-11-12): 6th MEMORY_TYPE + dedicated tool.

Lands the read/write/confirm chain for the 'scheduled' memory type:
  - MEMORY_TYPES gains "scheduled" (5 -> 6)
  - a new memories/scheduled.json shard (zero migration to the other 5)
  - filemem_scheduled tool: suggest (LLM propose -> PENDING panel) / list /
    resolve (user verdict done|dismissed|postpone)
  - confirm via filemem_review_drafts(action="scheduled", verdict="confirm")
    lands the row into scheduled.json (the ONLY writer of that shard)

Locked invariants (owner rulings this session):
  (a) due_at is a STRICT ISO timestamp, compared purely by CODE at wake time
      (zero LLM on the due/detection face).
  auto rows require BOTH on_due_do AND expects (owner: "双必填").
  remind rows need on_due_do, may omit expects; they never auto-execute.
  (b) LLM suggests; the user confirms (due_at may be filled at confirm time).

Run:
    cd /root/.hermes/plugins/filemem
    /root/.hermes/plugin-venv/bin/python -m pytest tests/test_scheduled_slice1.py -q
"""
import json, sys, threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MEMORY_TYPES, MemoryTriggerRouter  # noqa: E402


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
    # _sm_notify fail-softs to a no-op when shared-memory is disabled.
    p._shared_mem = None
    # the confirm-consumption face writes audit rows via
    # _append_consolidation_event -> consolidation_log.jsonl under storage_dir.
    p._append_consolidation_event = lambda ev: _noop_append(tmp, ev)
    return p


def _noop_append(tmp: Path, ev: dict) -> None:
    log = tmp / "consolidation_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


@pytest.fixture
def p(tmp_path):
    return _make_provider(tmp_path)


def _suggest(p, **over) -> dict:
    """Drive the write face: filemem_scheduled(action='suggest')."""
    base = {
        "action": "suggest",
        "content": "9200 服务健康检查",
        "due_at": "2026-12-01T00:00:00",
        "action_kind": "remind",
        "on_due_do": "检查 9200 是否存活，未存活则告警",
        "expects": "HTTP 200 + body 含 ok",
    }
    base.update(over)
    out = json.loads(p.handle_tool_call("filemem_scheduled", base))
    return out


# ── ① type registration ──────────────────────────────────────────────────────
def test_memory_types_includes_scheduled():
    assert "scheduled" in MEMORY_TYPES
    assert len(MEMORY_TYPES) == 6


def test_scheduled_shard_path_is_new_file(p):
    assert p._scheduled_shard_path() == p.memories_dir / "scheduled.json"
    assert not p._scheduled_shard_path().exists()  # not pre-created
    assert p._scheduled_load_rows() == []  # missing file -> []


# ── ② suggest = LLM propose, lands PENDING, NOT the pool ────────────────────
def test_suggest_lands_pending_panel_not_confirmed(p):
    res = _suggest(p)
    assert res["success"] is True
    assert "建议已落待确认面板" in res["note"]
    # The confirmed pool must be EMPTY (suggest alone never lands a row).
    assert p._scheduled_load_rows() == []


def test_suggest_rejects_non_iso_due_at(p):
    res = _suggest(p, due_at="下周")
    assert res["success"] is False
    assert "ISO timestamp" in res["error"]


def test_auto_requires_expects(p):
    # (owner: auto 型 on_due_do + expects 双必填)
    res = _suggest(p, action_kind="auto", expects="")
    assert res["success"] is False
    assert "expects" in res["error"]


def test_remind_needs_on_due_do(p):
    res = _suggest(p, on_due_do="")
    assert res["success"] is False
    assert "on_due_do" in res["error"]


def test_remind_may_omit_expects(p):
    res = _suggest(p, action_kind="remind", on_due_do="提醒你手动更新证书",
                   expects="")
    assert res["success"] is True


def test_suggest_bad_action_kind_rejected(p):
    res = _suggest(p, action_kind="magical")
    assert res["success"] is False
    assert "action_kind" in res["error"]


# ── ③ confirm lands the row with all 5 fields + status=pending ───────────────
def _pending_sid(p, out):
    # the panel entry id is surfaced by _suggest's push; recover it from the
    # side-stamped marker table (single entry expected).
    tbl = p._scheduled_probe_markers
    sid = p._turn_active_sid or p.session_id or "default"
    return list(tbl[sid].keys())[0]


def test_confirm_moves_to_scheduled_shard(p):
    out = _suggest(p)
    probe_id = _pending_sid(p, out)
    res = json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm",
        "memory_id": probe_id}))
    assert res["success"] is True
    rows = p._scheduled_load_rows()
    assert len(rows) == 1
    r = rows[0]
    m = r["metadata"]
    assert r["type"] == "scheduled"
    assert r["level"] == "L2"
    assert r["source"] == "confirmed_draft"
    assert m["due_at"] == "2026-12-01T00:00:00"
    assert m["action_kind"] == "remind"
    assert m["on_due_do"].startswith("检查 9200")
    assert m["expects"] == "HTTP 200 + body 含 ok"
    assert m["status"] == "pending"


def test_confirm_requires_due_at_b_rule(p):
    # (b): due_at may be left blank at suggest, but is REQUIRED at confirm.
    out = _suggest(p, due_at="")
    probe_id = _pending_sid(p, out)
    res = json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm", "memory_id": probe_id}))
    assert res["success"] is False
    assert "due_at" in res["error"]
    # supply it at confirm time -> lands
    res2 = json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm",
        "memory_id": probe_id, "due_at": "2026-12-15T09:00:00"}))
    assert res2["success"] is True
    assert p._scheduled_load_rows()[0]["metadata"]["due_at"] == "2026-12-15T09:00:00"


def test_dismiss_writes_nothing(p):
    out = _suggest(p)
    probe_id = _pending_sid(p, out)
    res = json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "dismiss", "memory_id": probe_id}))
    assert res["success"] is True
    assert p._scheduled_load_rows() == []


# ── ④ resolve: user verdicts (done / dismissed / postpone) ──────────────────
def test_resolve_done_and_dismissed(p):
    _suggest(p)
    probe_id = _pending_sid(p, None)
    json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm", "memory_id": probe_id}))
    rows = p._scheduled_load_rows()
    sid1, sid2 = rows[0]["id"], rows[0]["id"]
    r1 = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": sid1, "verdict": "done"}))
    assert r1["success"] is True and r1["status"] == "done"
    r2 = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": sid2, "verdict": "dismissed"}))
    assert r2["success"] is True and r2["status"] == "dismissed"
    # resolve is a status UPDATE on the same row, not a new row (I1 no-DELETE)
    assert len(p._scheduled_load_rows()) == 1


def test_resolve_postpone_requires_new_due_at(p):
    _suggest(p)
    probe_id = _pending_sid(p, None)
    json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm", "memory_id": probe_id}))
    sid = p._scheduled_load_rows()[0]["id"]
    bad = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": sid, "verdict": "postpone"}))
    assert bad["success"] is False
    ok = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": sid, "verdict": "postpone",
        "new_due_at": "2026-12-20T00:00:00"}))
    assert ok["success"] is True
    row = p._scheduled_load_rows()[0]
    assert row["metadata"]["due_at"] == "2026-12-20T00:00:00"
    assert row["metadata"]["status"] == "pending"


def test_resolve_unknown_id_rejected(p):
    res = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": "nope", "verdict": "done"}))
    assert res["success"] is False
    assert "no scheduled row" in res["error"]


# ── ⑤ list / search-visible, 5 existing shards byte-unchanged ────────────────
def test_list_pending_after_confirm(p):
    _suggest(p)
    probe_id = _pending_sid(p, None)
    json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm", "memory_id": probe_id}))
    res = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "list", "status": "pending"}))
    assert res["count"] == 1 and res["rows"][0]["metadata"]["status"] == "pending"
    # terminal rows are excluded from the default pending view
    json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve", "sched_id": res["rows"][0]["id"],
        "verdict": "done"}))
    res2 = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "list", "status": "pending"}))
    assert res2["count"] == 0
    res3 = json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "list", "status": "all"}))
    assert res3["count"] == 1


def test_existing_5_shards_byte_unchanged(tmp_path):
    p = _make_provider(tmp_path)
    # seed the 5 pre-existing shards with distinct content
    for t, payload in {
        "preference": "pref row", "fact": "fact row",
        "decision": "decision row", "task": "task row", "plan": "plan row",
    }.items():
        f = p.memories_dir / f"{t}.json"
        with open(f, "w", encoding="utf-8") as fh:
            json.dump([{"id": f"{t}_1", "type": t, "content": payload}],
                      fh, ensure_ascii=False)
    before = {t: (p.memories_dir / f"{t}.json").read_bytes()
              for t in ("preference", "fact", "decision", "task", "plan")}
    # exercise the whole scheduled chain
    out = _suggest(p, action_kind="auto")
    probe_id = _pending_sid(p, out)
    json.loads(p.handle_tool_call("filemem_review_drafts", {
        "action": "scheduled", "verdict": "confirm", "memory_id": probe_id}))
    json.loads(p.handle_tool_call("filemem_scheduled", {
        "action": "resolve",
        "sched_id": p._scheduled_load_rows()[0]["id"], "verdict": "done"}))
    after = {t: (p.memories_dir / f"{t}.json").read_bytes()
             for t in ("preference", "fact", "decision", "task", "plan")}
    for t in before:
        assert before[t] == after[t], f"{t}.json was mutated by the scheduled chain"
    # scheduled.json is the only new file
    assert (p.memories_dir / "scheduled.json").exists()
