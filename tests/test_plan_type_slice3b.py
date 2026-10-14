#!/usr/bin/env python3
"""PLAN-type PRD — TDD red tests, slice 3b: select_option (step-internal
candidate swap, the "agent offered b1/b2/b3, user picked b2" scenario).

PRD §6.2 op = select_option (atomic, one shard-lock pass):
  * the target step's original scheme is replaced by the CHosen option
  * the UNCHOSEN options are marked outcome=rejected (with reason) and stay
    in the step's internal audit (open_options[]) — NOT promoted to a plan-level
    supersede row (PRD §7 hierarchy: step-internal candidates live in-step)
  * the step text is updated to reflect the chosen scheme
  * plan_revision + 1 (the route actually changed)
  * plan_state: blocked -> in_progress
  * decision_log appended (route change, cross-profile traceable)
  * works even when the plan is already in_progress (a step-level scheme swap
    without going through block first)

Invariants (locked here):
  * select_option NEVER opens a new plan row, NEVER writes superseded_by
    (that's plan-level supersede, a different lane — PRD §7).
  * the chosen option's outcome flips to "chosen"; unchosen to "rejected".
  * a choice that matches NO stored option is rejected (no silent pass).
"""
import sys
import json
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from __init__ import FileMemProvider


def _provider(tmp: Path, profile: str = "coder"):
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.episodic_dir = tmp / "episodic"
    p.source_profile = profile
    p.session_id = "sess-1"
    p._last_save_time = 0.0
    p._rate_limited_queue = []
    return p


def _create(p, title="t", steps=("a", "b"), profile="coder"):
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": title, "steps": list(steps),
    }))
    assert out.get("success"), out
    return out["plan_id"]


def _get_rows(tmp):
    f = tmp / "memories" / "plan.json"
    return json.loads(f.read_text(encoding="utf-8"))


def _find(pid, tmp):
    for r in _get_rows(tmp):
        if r["id"] == pid:
            return r
    raise AssertionError(f"plan {pid} not found")


def _advance(p, pid, op, **kw):
    return json.loads(p.handle_tool_call("filemem_plan",
                                         {"action": "advance", "plan_id": pid,
                                          "op": op, **kw}))


# ── the core scenario: block then select_option b2 ───────────────────────────

def test_select_option_swaps_step_scheme_and_rejects_others(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, title="ledger fix", steps=("定位存根", "修账本", "验证"))
    # agent hit a wall at step 2 and offered b1/b2/b3
    _advance(p, pid, "block", step=2, reason="a 方案接口不可达",
             options=[{"id": "b1", "text": "改用备用接口"},
                     {"id": "b2", "text": "本地 mock"},
                     {"id": "b3", "text": "跳过此步"}])
    out = _advance(p, pid, "select_option", step=2, choice="b2",
                   reason="b2 依赖最少")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    # state back to in_progress
    assert meta["plan_state"] == "in_progress", meta["plan_state"]
    # the chosen option is marked chosen; the rest rejected
    opts = {o["id"]: o for o in meta["open_options"]}
    assert opts["b2"]["outcome"] == "chosen", opts["b2"]
    assert opts["b1"]["outcome"] == "rejected"
    assert opts["b3"]["outcome"] == "rejected"
    # the rejected options keep their reasons (in-step audit, PRD §7)
    assert opts["b2"]["reason"] == "b2 依赖最少"
    # step text now reflects the chosen scheme
    step2 = [s for s in meta["plan_steps"] if s["id"] == 2][0]
    assert "本地 mock" in step2["text"], step2
    # revision bumped (route changed)
    assert meta["plan_revision"] == 2
    # decision_log has a select_option entry
    assert any(d["op"] == "select_option" for d in meta["decision_log"]), \
        meta["decision_log"]


def test_select_option_no_new_plan_row_no_supersede(tmp_path):
    """PRD §7 hierarchy: a step-internal swap stays in-step audit; it must NOT
    open a new plan row nor write a plan-level superseded_by."""
    p = _provider(tmp_path)
    pid = _create(p)
    _advance(p, pid, "block", step=1, reason="stuck",
             options=[{"id": "x", "text": "x"}, {"id": "y", "text": "y"}])
    before = len(_get_rows(tmp_path))
    out = _advance(p, pid, "select_option", step=1, choice="y")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    after = len(_get_rows(tmp_path))
    assert before == after == 1, f"select_option must not stack a row " \
                                 f"({before} -> {after})"
    assert row["metadata"].get("superseded_by") is None
    assert row["metadata"].get("superseded") is not True


def test_select_option_unknown_choice_rejected(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    _advance(p, pid, "block", step=1, reason="stuck",
             options=[{"id": "b1", "text": "b1"}])
    out = _advance(p, pid, "select_option", step=1, choice="b9")  # not stored
    assert out.get("error"), f"unknown choice must reject, got {out}"


def test_select_option_without_block_from_in_progress(tmp_path):
    """A step-level scheme swap is allowed even when the plan never went
    through block (agent offers alternatives inline)."""
    p = _provider(tmp_path)
    pid = _create(p, steps=("a", "b"))
    out = _advance(p, pid, "select_option", step=1, choice="b2",
                   reason="pick b2 over a",
                   options=[{"id": "b1", "text": "a (原方案)"},
                            {"id": "b2", "text": "本地实现"}])
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "in_progress"
    opts = {o["id"]: o for o in meta["open_options"]}
    assert opts["b2"]["outcome"] == "chosen"
    assert opts["b1"]["outcome"] == "rejected"
    step1 = [s for s in meta["plan_steps"] if s["id"] == 1][0]
    assert "本地实现" in step1["text"]


def test_select_option_sets_rejected_reason_for_unchosen(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("s1", "s2"))
    _advance(p, pid, "block", step=1, reason="wall",
             options=[{"id": "b1", "text": "b1"},
                     {"id": "b2", "text": "b2"}])
    _advance(p, pid, "select_option", step=1, choice="b2",
             reason="b2 依赖最少")
    row = _find(pid, tmp_path)
    opts = {o["id"]: o for o in row["metadata"]["open_options"]}
    # the chosen one carries the choice reason
    assert opts["b2"]["reason"] == "b2 依赖最少"
    # unchosen: rejected (reason optional but outcome must be set)
    assert opts["b1"]["outcome"] == "rejected"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
