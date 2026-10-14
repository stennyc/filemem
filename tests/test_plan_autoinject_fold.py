#!/usr/bin/env python3
"""PLAN-type PRD §10 — auto-inject face must never include plan rows.

Locked invariants (the "task != context" red line, owner-钉):
  * `_auto_inject_eligible(plan_row, ...) is False` for EVERY plan_state and
    for session_sp=None (fail-open) — a plan row NEVER enters the auto-inject
    face, even confirmed.
  * a confirmed NON-plan row still auto-injects (regression gate: the plan
    fold must not clobber the ordinary auto-inject behavior for other types).
  * `_auto_recall_filter` (the other auto-face pool filter) also folds plan
    rows in BOTH the session_sp=None fail-open path and the isolated path.
"""
import sys
import json
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from __init__ import _auto_inject_eligible, _auto_recall_filter


def _plan_row(state="in_progress", confirmed=True):
    return {
        "id": "plan_20261014_abc",
        "type": "plan",
        "content": "filemem plan 扩展",
        "level": "L2",
        "source": "manual",
        "metadata": {
            "plan_state": state,
            "plan_id": "plan_20261014_abc",
            "confirmed": confirmed,
        },
    }


def test_plan_row_never_auto_inject_when_confirmed():
    assert _auto_inject_eligible(_plan_row(), session_sp="coder",
                                 cur_session_id="sess") is False


def test_plan_row_never_auto_inject_fail_open():
    # session_sp=None is the fail-open path — plan fold must STILL return False
    assert _auto_inject_eligible(_plan_row(), session_sp=None,
                                 cur_session_id=None) is False


def test_plan_row_never_auto_inject_any_state():
    for state in ("in_progress", "blocked", "done", "abandoned", "superseded"):
        assert _auto_inject_eligible(_plan_row(state=state), session_sp="coder",
                                     cur_session_id=None) is False, \
            f"plan row in {state} must never auto-inject"


def test_confirmed_fact_still_auto_injects():
    """Regression gate: the plan fold must not clobber ordinary auto-inject."""
    fact = {"id": "f1", "type": "fact", "content": "缓存用 redis",
            "level": "L0",
            "metadata": {"confirmed": True, "source_profile": "coder"}}
    assert _auto_inject_eligible(fact, session_sp="coder",
                                 cur_session_id=None) is True


def test_auto_recall_filter_folds_plan_fail_open():
    pool = [_plan_row(),
            {"id": "f1", "type": "fact", "content": "x", "level": "L0",
             "metadata": {}}]
    kept = _auto_recall_filter(pool, session_sp=None)
    assert all(r.get("type") != "plan" for r in kept), \
        f"plan row must be folded even in the fail-open path: {kept}"
    assert any(r.get("id") == "f1" for r in kept)


def test_auto_recall_filter_folds_plan_isolated():
    pool = [_plan_row(),
            {"id": "f1", "type": "fact", "content": "x", "level": "L0",
             "metadata": {"source_profile": "coder"}}]
    kept = _auto_recall_filter(pool, session_sp="coder")
    assert all(r.get("type") != "plan" for r in kept), \
        f"plan row must be folded in the isolated path: {kept}"
    assert any(r.get("id") == "f1" for r in kept)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
