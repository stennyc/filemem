#!/usr/bin/env python3
"""PLAN-type PRD — TDD red tests, slice 2: plan.json storage + filemem_plan
list/create verbs.

Vertical slice 2 (PRD §6 storage + create + list, 9 advance ops are slice 3):
  * `filemem_plan(action="create")` lands a plan row into plan.json with
    plan_state=in_progress / level=L2 / source_profile / plan_steps (auto-id)
    / done_when / upsert-by-plan_id semantics.
  * `filemem_plan(action="list")` returns unfinished plans (in_progress +
    blocked) cross-profile by default, state-filtered, with the three stamps
    (source_profile / last_touched_by / last_completed_by) + blocked rows
    expanded with blocked_reason + open_options.
  * anchor enforcement: a step marked done MUST carry an anchor
    (kind+value) or an explicit no_anchor — no ambiguity (PRD §1.5).

The provider is a REAL instance (with storage_dir/memories_dir) so create/list
touch the actual plan.json shard (same partial-instance pattern as
test_ep_1_lineage, but with a tempdir so no I/O leakage).
"""
import sys
import json
import tempfile
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from __init__ import FileMemProvider


def _provider(tmp: Path, profile: str = "coder"):
    """Real-ish provider: tempdir-backed storage + source_profile stamp."""
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.episodic_dir = tmp / "episodic"
    p.source_profile = profile
    p.session_id = "sess-1"
    # minimal attrs the tool face may touch
    p._last_save_time = 0.0
    p._rate_limited_queue = []
    return p


# ── create ────────────────────────────────────────────────────────────────────

def test_create_lands_plan_shard(tmp_path):
    p = _provider(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create",
        "title": "filemem plan 类型扩展",
        "steps": ["扩展 MEMORY_TYPES", "扩展 regex", "写门 clarify",
                  "llm_caller 面", "测试"],
        "done_when": "5 个步骤全部 done",
        "topic": "filemem",
    }))
    assert out.get("success") is True, out
    assert out.get("plan_id"), out
    # plan.json now exists and holds the row
    plan_file = p.memories_dir / "plan.json"
    assert plan_file.exists(), "plan.json shard not created"
    rows = json.loads(plan_file.read_text(encoding="utf-8"))
    assert len(rows) == 1
    row = rows[0]
    meta = row["metadata"]
    assert meta["plan_state"] == "in_progress"
    assert row["level"] == "L2"
    assert meta["source_profile"] == "coder"
    assert meta["plan_revision"] == 1
    # steps auto-id'd 1..N, all pending, no anchor (not done yet)
    steps = meta["plan_steps"]
    assert len(steps) == 5
    assert [s["id"] for s in steps] == [1, 2, 3, 4, 5]
    assert all(s["status"] == "pending" for s in steps)
    assert meta.get("done_when") == "5 个步骤全部 done"


def test_create_invalid_title_rejected(tmp_path):
    p = _provider(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": "", "steps": ["a"],
    }))
    assert out.get("error"), f"empty title must reject, got {out}"


def test_create_no_steps_rejected(tmp_path):
    p = _provider(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": "x", "steps": [],
    }))
    assert out.get("error"), f"zero steps must reject, got {out}"


# ── upsert-by-plan_id ────────────────────────────────────────────────────────

def test_upsert_same_plan_id_no_new_row(tmp_path):
    p = _provider(tmp_path)
    first = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": "t", "steps": ["a", "b"],
    }))
    pid = first["plan_id"]
    # advance the row upsert-style: re-create with the same plan_id must NOT
    # stack a second row (plan is a live record, not an immutable log).
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "advance", "plan_id": pid, "op": "revise",
        "new_steps": ["a", "b", "c"],
    }))
    assert out.get("success") is True, out
    rows = json.loads((p.memories_dir / "plan.json").read_text(encoding="utf-8"))
    assert len(rows) == 1, f"upsert must keep one row, got {len(rows)}"
    assert len(rows[0]["metadata"]["plan_steps"]) == 3
    assert rows[0]["metadata"]["plan_revision"] == 2


# ── list ──────────────────────────────────────────────────────────────────────

def _mk(p, title, state="in_progress", profile="coder", steps=("a", "b"),
        blocked_reason=None, options=None):
    """Create a plan row, then flip its state/stamps in place (by plan_id,
    NOT rows[0] — a profile can hold many rows). Returns the row's plan_id."""
    created = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": title, "steps": list(steps),
    }))
    pid = created["plan_id"]
    # locate THIS row by plan_id, flip its state, rewrite the whole shard
    plan_file = p.memories_dir / "plan.json"
    rows = json.loads(plan_file.read_text(encoding="utf-8"))
    target = next(r for r in rows if r["id"] == pid)
    meta = target["metadata"]
    meta["plan_state"] = state
    meta["source_profile"] = profile
    if state == "blocked":
        meta["blocked_reason"] = blocked_reason or "step1 撞墙"
        meta["open_options"] = options or [
            {"id": "b1", "text": "改用备用接口", "outcome": "rejected",
             "reason": "外部服务不可控"},
            {"id": "b2", "text": "本地 mock", "outcome": "chosen",
             "reason": "依赖最少"},
            {"id": "b3", "text": "跳过", "outcome": "pending"},
        ]
    plan_file.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return pid


def test_list_default_cross_profile_in_progress_plus_blocked(tmp_path):
    p = _provider(tmp_path, profile="coder")
    _mk(p, "coder in-progress", state="in_progress", profile="coder")
    _mk(p, "coder blocked", state="blocked", profile="coder")
    # a SECOND profile's plan must ALSO be visible (cross-profile, PRD §6.3)
    p2 = _provider(tmp_path, profile="data-engineer")
    _mk(p2, "de in-progress", state="in_progress", profile="data-engineer")
    p3 = _provider(tmp_path, profile="orchestrator")
    _mk(p3, "orch done", state="done", profile="orchestrator")

    out = json.loads(p.handle_tool_call("filemem_plan", {"action": "list"}))
    titles = {r["title"] for r in out.get("plans", out.get("plans_list", []))} \
        if isinstance(out, dict) else set(out)
    # unfinished = in_progress + blocked; the done one is excluded by default
    assert "coder in-progress" in titles
    assert "coder blocked" in titles
    assert "de in-progress" in titles, "cross-profile plan must be visible by default"
    assert "orch done" not in titles, "done plan must not show by default"


def test_list_state_filter_blocked_only(tmp_path):
    p = _provider(tmp_path)
    _mk(p, "in-progress one", state="in_progress")
    _mk(p, "blocked one", state="blocked")
    out = json.loads(p.handle_tool_call("filemem_plan",
                                        {"action": "list", "state": "blocked"}))
    rows = out.get("plans", [])
    assert all(r["plan_state"] == "blocked" for r in rows), rows
    # blocked rows expand blocked_reason + open_options
    blk = [r for r in rows if r["title"] == "blocked one"][0]
    assert blk.get("blocked_reason")
    assert isinstance(blk.get("open_options"), list) and blk["open_options"]
    # each option carries an outcome
    assert all("outcome" in o for o in blk["open_options"])


def test_list_state_done_explicit(tmp_path):
    p = _provider(tmp_path)
    _mk(p, "a done plan", state="done")
    out = json.loads(p.handle_tool_call("filemem_plan",
                                        {"action": "list", "state": "done"}))
    rows = out.get("plans", [])
    assert any(r["title"] == "a done plan" for r in rows), rows


def test_list_carries_three_stamps(tmp_path):
    p = _provider(tmp_path, profile="coder")
    _mk(p, "stamped", state="in_progress", profile="coder")
    # simulate data-engineer touching it
    plan_file = p.memories_dir / "plan.json"
    rows = json.loads(plan_file.read_text(encoding="utf-8"))
    rows[0]["metadata"]["last_touched_by"] = "data-engineer"
    (plan_file).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    out = json.loads(p.handle_tool_call("filemem_plan", {"action": "list"}))
    row = [r for r in out.get("plans", []) if r["title"] == "stamped"][0]
    assert row.get("source_profile") == "coder"
    assert row.get("last_touched_by") == "data-engineer"
    assert row.get("last_completed_by") is None  # not yet complete_all'd


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
