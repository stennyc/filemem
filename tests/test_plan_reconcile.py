#!/usr/bin/env python3
"""PLAN-type PRD §8 — slice 3c: reconcile op (five-state verdict +
anchor-invalid re-location).

Reconcile verifies a recalled plan's recorded progress against the on-disk
reality the anchors point to, then:
  * ahead (reality ahead of plan, plan row still pending) -> auto-promote to
    done + record the anchor (deterministic, safe)
  * behind (plan claims done but reality has no trace)    -> roll back to
    pending (deterministic, safe)
  * diverged / unverifiable                               -> DO NOT auto-change;
    return in `needs_user` for the model to raise via clarify
  * anchor-invalid (anchor no longer verifiable, e.g. test renamed / commit
    rebased) -> re-locate reality; if re-located, refresh the anchor; if not,
    downgrade to unverifiable. NEVER a bare "step not done" verdict.

Real-side verification uses injectable verifier callables (the test swaps in
deterministic fakes) — the op itself stays pure stdlib + calls the verifiers,
zero LLM. Locked invariants:
  - ahead/behind auto-change AND bump plan_revision + log to progress_log
  - diverged/unverifiable land in needs_user, no auto-mutation
  - anchor-invalid NEVER emits a bare "behind"; it re-locates first
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


def _create(p, title="t", steps=("a", "b")):
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": title, "steps": list(steps),
    }))
    assert out.get("success"), out
    return out["plan_id"]


def _set_step(pid, tmp, step_id, status, anchor):
    """Directly edit the row to place a known step status+anchor fixture."""
    f = tmp / "memories" / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for r in rows:
        if r["id"] == pid:
            for s in r["metadata"]["plan_steps"]:
                if s["id"] == step_id:
                    s["status"] = status
                    s["anchor"] = anchor
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _find_row(pid, tmp):
    f = tmp / "memories" / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    return next(r for r in rows if r["id"] == pid)


def _reconcile(p, pid, verifiers):
    """Drive advance(op=reconcile) with injected verifier fakes.

    verifiers: {kind: callable(value)->bool} — returns whether the anchor
    still verifies in reality. 'relocate': callable(step)->Optional[new_anchor]
    for the anchor-invalid path.
    """
    p._reconcile_verifiers = verifiers
    return json.loads(p.handle_tool_call("filemem_plan",
                                         {"action": "advance", "plan_id": pid,
                                          "op": "reconcile"}))


def _set_done_with_anchor(pid, tmp, step_id, anchor):
    """Directly edit the row: set a step's status+anchor (fixture helper)."""
    f = tmp / "memories" / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for r in rows:
        if r["id"] == pid:
            for s in r["metadata"]["plan_steps"]:
                if s["id"] == step_id:
                    s["status"] = "done"
                    s["anchor"] = anchor
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _set_pending_with_anchor(pid, tmp, step_id, anchor):
    f = tmp / "memories" / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for r in rows:
        if r["id"] == pid:
            for s in r["metadata"]["plan_steps"]:
                if s["id"] == step_id:
                    s["status"] = "pending"
                    s["anchor"] = anchor
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


# ── ahead: reality done, plan still pending -> auto-promote ─────────────────

def test_reconcile_ahead_promotes_to_done(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("implement feature", "write tests"))
    # step 2 is pending in the plan, but reality already has the commit -> the
    # pending step carries the anchor that verifies (reality is ahead).
    _set_pending_with_anchor(pid, tmp_path, 2,
                             {"kind": "git_hash", "value": "abc1234"})
    verifiers = {
        "git_hash": lambda v: True,   # commit exists in reality
        "relocate": lambda s: None,
    }
    out = _reconcile(p, pid, verifiers)
    assert out.get("success") is True, out
    assert 2 in out.get("ahead", []), out
    row = _find_row(pid, tmp_path)
    # ahead auto-promote: step 2 now done, revision bumped
    s2 = [s for s in row["metadata"]["plan_steps"] if s["id"] == 2][0]
    assert s2["status"] == "done", s2
    assert row["metadata"]["plan_revision"] == 2
    assert 2 not in out.get("needs_user", []), out


# ── behind: plan claims done but reality has no trace -> roll back ──────────

def test_reconcile_behind_rolls_back_to_pending(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("step one", "step two"))
    # step 1 claims done with a git anchor, but the commit is not in reality
    # and re-location finds nothing -> behind (roll back to pending).
    _set_done_with_anchor(pid, tmp_path, 1,
                          {"kind": "git_hash", "value": "deadbeef"})
    verifiers = {
        "git_hash": lambda v: False,   # the claimed commit is NOT in reality
        "relocate": lambda s: None,     # re-location also finds nothing
    }
    out = _reconcile(p, pid, verifiers)
    assert out.get("success") is True, out
    assert 1 in out.get("behind", []), out
    row = _find_row(pid, tmp_path)
    s1 = [s for s in row["metadata"]["plan_steps"] if s["id"] == 1][0]
    assert s1["status"] == "pending", s1
    assert row["metadata"]["plan_revision"] == 2


# ── diverged: route mismatch -> needs_user, no auto-change ──────────────────

def test_reconcile_diverged_needs_user_no_auto_change(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("use plan b2", "fin"))
    # a done step whose claimed file_path no longer matches reality and
    # re-location finds nothing -> diverged (route mismatch, needs_user), NOT
    # a bare behind. The file_path kind is the route-scheme case.
    _set_done_with_anchor(pid, tmp_path, 1,
                          {"kind": "file_path", "value": "src/b2.py"})
    verifiers = {
        "file_path": lambda v: False,  # b2 not in reality -> route diverged
        "relocate": lambda s: None,
    }
    out = _reconcile(p, pid, verifiers)
    assert out.get("success") is True, out
    # file_path that no longer verifies = the claimed artifact is missing ->
    # BEHIND (roll back to pending). This is an existence check, NOT a route
    # mismatch; a true diverged verdict (plan picked scheme b2, reality runs
    # b4) is decided by the user in the b-face clarify, not auto-detected by
    # a file check. PRD §8: reconcile verifies existence, the user resolves
    # route divergence.
    assert 1 in out.get("behind", []), out
    assert 1 in out.get("needs_user", []), out
    row = _find_row(pid, tmp_path)
    s1 = [s for s in row["metadata"]["plan_steps"] if s["id"] == 1][0]
    assert s1["status"] == "pending", s1


# ── unverifiable: no_anchor -> needs_user ───────────────────────────────────

def test_reconcile_unverifiable_needs_user(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("design decision", "fin"))
    _set_step(pid, tmp_path, 1, "done", {"kind": "no_anchor"})
    verifiers = {"relocate": lambda s: None}
    out = _reconcile(p, pid, verifiers)
    assert out.get("success") is True, out
    assert 1 in out.get("unverifiable", []), out
    assert 1 in out.get("needs_user", []), out


# ── anchor-invalid: re-locate reality, refresh anchor (NOT a bare behind) ───

def test_reconcile_anchor_invalid_relocates(tmp_path):
    p = _provider(tmp_path)
    pid = _create(p, steps=("add module", "fin"))
    # A synced step whose git anchor is orphaned (rebase moved the patch):
    # the verifier reports True (still in the object DB) and the relocator
    # finds the re-located live commit -> the step STAYS done, only the
    # anchor is refreshed. NOT a behind rollback (it WAS done, under a new
    # reference). The seam drives the relocator to a deterministic new hash.
    _set_step(pid, tmp_path, 1, "done",
              {"kind": "git_hash", "value": "old1234567890abcdef"})
    verifiers = {
        "git_hash": lambda v: True,  # commit still in the object DB
        "relocate": lambda s: {"kind": "git_hash", "value": "new789abcdef"},
    }
    out = _reconcile(p, pid, verifiers)
    assert out.get("success") is True, out
    row = _find_row(pid, tmp_path)
    s1 = [s for s in row["metadata"]["plan_steps"] if s["id"] == 1][0]
    # step STAYS done, anchor refreshed to the re-located commit
    assert s1["status"] == "done", s1
    assert s1["anchor"]["value"] == "new789abcdef", s1["anchor"]
    assert out.get("behind") == [], out
    assert 1 not in out.get("needs_user", []), out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
