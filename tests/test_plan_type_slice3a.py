#!/usr/bin/env python3
"""PLAN-type PRD — TDD red tests, slice 3a: the six pure state-machine ops
(continue / block / resume / complete_all / abandon / reopen) + level
migration + cross-profile stamps + decision_log.

Reconcile, select_option, and the write-door clarify hook are later slices
(3c / 3b). Slice 3a tests what PRD §6.2 defines for the six ops:
  * continue: step -> done, anchor REQUIRED (enforced by _plan_validate_anchor),
    refresh last_touched_by + updated_at
  * block: state -> blocked, store blocked_at_step/reason/options (outcome=pending)
  * resume: blocked -> in_progress, no scheme change
  * complete_all: force ALL pending/in_progress steps to done (each with an
    anchor or no_anchor), state -> done, settled=True, level stays L2,
    last_completed_by stamped
  * abandon (no supersede_with): state -> abandoned, superseded=True,
    level -> L0, decision_log appended
  * abandon (with supersede_with): state -> superseded, superseded_by set,
    level -> L1, decision_log appended
  * reopen: terminal -> in_progress, clear settled/superseded flags,
    level back to L2, revision+1, decision_log appended
  * cross-profile: data-engineer can advance a coder-created plan,
    last_touched_by updates, source_profile stays coder (PRD §7)
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


def _create(p, title="t", steps=("a", "b", "c"), profile="coder"):
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": title, "steps": list(steps),
    }))
    assert out.get("success"), out
    return out["plan_id"]


def _get_rows(tmp: Path):
    f = tmp / "memories" / "plan.json"
    return json.loads(f.read_text(encoding="utf-8"))


def _find(pid, tmp):
    for r in _get_rows(tmp):
        if r["id"] == pid:
            return r
    raise AssertionError(f"plan {pid} not found")


def _advance(p, pid, op, **kw):
    args = {"action": "advance", "plan_id": pid, "op": op, **kw}
    return json.loads(p.handle_tool_call("filemem_plan", args))


# ── continue + anchor enforcement ─────────────────────────────────────────────

def test_continue_marks_step_done_with_anchor(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("s1", "s2"))
    out = _advance(p, pid, "continue", step_done=1,
                   anchor={"kind": "test_pass", "value": "tests/test_x.py::t1"})
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    s1 = meta["plan_steps"][0]
    assert s1["status"] == "done", s1
    assert s1["anchor"]["kind"] == "test_pass"
    assert meta["last_touched_by"] == "coder"
    assert meta["plan_state"] == "in_progress"  # not all done yet


def test_continue_requires_anchor(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("s1",))
    out = _advance(p, pid, "continue", step_done=1)  # no anchor
    assert out.get("error"), f"missing anchor must reject, got {out}"
    row = _find(pid, tmp_path)
    assert row["metadata"]["plan_steps"][0]["status"] == "pending"


def test_continue_rejects_invalid_anchor_kind(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("s1",))
    out = _advance(p, pid, "continue", step_done=1,
                   anchor={"kind": "session_var", "value": "x"})
    assert out.get("error"), f"non-whitelist anchor kind must reject, got {out}"


def test_continue_no_anchor_explicit(tmp_path):
    """A design-only step may be marked done with an explicit no_anchor."""
    p = _provider(tmp_path)
    pid = _create(p, steps=("design decision",))
    out = _advance(p, pid, "continue", step_done=1,
                   anchor={"kind": "no_anchor"})
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    assert row["metadata"]["plan_steps"][0]["status"] == "done"


# ── block ─────────────────────────────────────────────────────────────────────

def test_block_sets_state_and_stores_options(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("a", "b", "c"))
    out = _advance(p, pid, "block", step=2, reason="接口不可达",
                   options=[{"id": "b1", "text": "备用"},
                            {"id": "b2", "text": "mock"},
                            {"id": "b3", "text": "跳过"}])
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "blocked"
    assert meta["blocked_at_step"] == 2
    assert meta["blocked_reason"] == "接口不可达"
    opts = meta["open_options"]
    assert len(opts) == 3
    assert all(o.get("outcome") == "pending" for o in opts), opts


# ── resume ────────────────────────────────────────────────────────────────────

def test_resume_returns_blocked_to_in_progress(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    _advance(p, pid, "block", step=1, reason="stuck", options=[{"id": "x", "text": "x"}])
    out = _advance(p, pid, "resume", reason="user said keep going")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    assert row["metadata"]["plan_state"] == "in_progress"


def test_resume_requires_blocked_state(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    out = _advance(p, pid, "resume")  # still in_progress, not blocked
    assert out.get("error"), f"resume on non-blocked must reject, got {out}"


# ── complete_all (force all pending -> done) ─────────────────────────────────

def test_complete_all_forces_all_steps_done(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("a", "b", "c", "d"))
    # only step 1 was advanced; steps 2-4 still pending
    _advance(p, pid, "continue", step_done=1,
             anchor={"kind": "git_hash", "value": "d87aa17"})
    out = _advance(p, pid, "complete_all")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "done"
    assert meta.get("settled") is True
    assert row["level"] == "L2"  # done stays L2
    for s in meta["plan_steps"]:
        assert s["status"] == "done", s
    assert meta.get("last_completed_by") == "coder"


def test_complete_all_stamps_no_anchor_for_unanchored(tmp_path):
    """Steps never advanced (no anchor) get explicit no_anchor on complete_all,
    never left ambiguous (PRD §1.5 no-ambiguity invariant)."""
    p = _provider(tmp_path)
    pid = _create(p, steps=("a", "b"))
    out = _advance(p, pid, "complete_all")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    for s in row["metadata"]["plan_steps"]:
        assert s["status"] == "done"
        # each step now has an anchor dict (either real or no_anchor)
        assert s["anchor"] is not None, s
        assert s["anchor"]["kind"] == "no_anchor"  # never advanced -> no_anchor


# ── abandon (two flavors) ─────────────────────────────────────────────────────

def test_abandon_without_supersede_sets_L0(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    out = _advance(p, pid, "abandon", reason="user said drop it")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "abandoned"
    assert meta.get("superseded") is True
    assert row["level"] == "L0"  # no reference value -> L0 (30d cleanup)
    assert meta.get("superseded_by") is None
    assert len(meta["decision_log"]) >= 1
    assert meta["decision_log"][-1]["op"] == "abandon"


def test_abandon_with_supersede_sets_superseded_L1(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    out = _advance(p, pid, "abandon", reason="route changed",
                   supersede_with="plan_new_42")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "superseded"
    assert meta.get("superseded") is True
    assert meta.get("superseded_by") == "plan_new_42"
    assert row["level"] == "L1"  # decision-record value -> L1 (90d)
    assert meta["decision_log"][-1]["op"] == "abandon"
    assert meta["decision_log"][-1].get("supersede_with") == "plan_new_42"


# ── reopen (terminal -> in_progress) ─────────────────────────────────────────

def test_reopen_from_done(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    _advance(p, pid, "complete_all")  # -> done
    out = _advance(p, pid, "reopen", reason="user changed mind")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "in_progress"
    assert meta.get("settled") in (None, False), "settled flag must clear"
    assert row["level"] == "L2"
    assert meta.get("plan_revision", 1) > 1  # reopen bumps revision


def test_reopen_from_abandoned(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    _advance(p, pid, "abandon", reason="drop")
    out = _advance(p, pid, "reopen", reason="actually keep it")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["plan_state"] == "in_progress"
    assert meta.get("superseded") in (None, False)
    assert row["level"] == "L2"


def test_reopen_rejects_in_progress(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p)
    out = _advance(p, pid, "reopen")  # not a terminal state
    assert out.get("error"), f"reopen on in_progress must reject, got {out}"


# ── cross-profile (PRD §7: no owner lock, stamps only) ────────────────────────

def test_cross_profile_continue_updates_touched_by(tmp_path):
    p_coder = _provider(tmp_path, profile="coder")
    pid = _create(p_coder, profile="coder")
    # data-engineer advances the coder's plan
    p_de = _provider(tmp_path, profile="data-engineer")
    out = _advance(p_de, pid, "continue", step_done=1,
                   anchor={"kind": "file_path", "value": "src/fix.py"})
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    assert meta["source_profile"] == "coder"       # provenance untouched
    assert meta["last_touched_by"] == "data-engineer"  # who advanced it
    assert row["level"] == "L2"


def test_cross_profile_abandon_stamps_decision_log_with_profile(tmp_path):
    p_coder = _provider(tmp_path, profile="coder")
    pid = _create(p_coder, profile="coder")
    p_de = _provider(tmp_path, profile="data-engineer")
    out = _advance(p_de, pid, "abandon", reason="de decided to drop",
                   supersede_with="plan_new")
    assert out.get("success") is True, out
    row = _find(pid, tmp_path)
    meta = row["metadata"]
    entry = meta["decision_log"][-1]
    assert entry["by"] == "data-engineer"
    assert entry["op"] == "abandon"
    assert meta["source_profile"] == "coder"  # never stolen


# ── I1 no-DELETE: abandoned/superseded/done rows stay on disk ────────────────

def test_terminal_rows_stay_on_disk(tmp_path):
    p = _provider(tmp_path)
    pid1 = _create(p, title="abandoned one")
    pid2 = _create(p, title="done one")
    _advance(p, pid1, "abandon", reason="drop")
    _advance(p, pid2, "complete_all")
    rows = _get_rows(tmp_path)
    ids = {r["id"] for r in rows}
    assert pid1 in ids and pid2 in ids, "terminal rows must stay (I1 no-DELETE)"
    assert len(rows) == 2  # no deletion, no stacking


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
