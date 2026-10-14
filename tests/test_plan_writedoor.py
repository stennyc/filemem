#!/usr/bin/env python3
"""PLAN-type PRD §2 — write-door commit-signal predicate (pure, zero LLM).

`_plan_commit_signal(content)` decides whether a plan-typed save should fire
the "store this plan?" clarify (PRD §2). Fires ONLY when the content is
plan-typed AND carries a commit signal. A plan still under discussion must NOT
fire (it lands as an L0 draft, not a plan shard). Non-plan content with a
commit phrase must NOT fire (the write-door is plan-specific).

Zero LLM, deterministic. Locked invariants:
  - plan strong-signal + commit phrase -> True
  - plan strong-signal, no commit (still discussing) -> False
  - commit phrase but NOT plan-typed -> False
"""
import sys
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from __init__ import FileMemProvider


def _provider():
    p = FileMemProvider.__new__(FileMemProvider)
    p.session_id = "s1"
    return p


def test_plan_commit_signal_true_for_final_plan():
    p = _provider()
    for text in (
        "最终版实施计划：步骤1 加正则表，步骤2 加 LLM 面，方案已定稿",
        "就按这个执行计划落地：第1步、第2步、第3步",
        "Here's the final plan: Step 1 add table, Step 2 add face, Step 3 test",
        "落地方案已定稿，分3阶段实施",
    ):
        assert p._plan_commit_signal(text) is True, f"should fire: {text!r}"


def test_plan_commit_signal_false_when_discussing():
    p = _provider()
    for text in (
        "我想出一个实施计划：步骤1、步骤2，待讨论",
        "roadmap 草案，3 个阶段，还没定",
        "计划还在讨论中，第1步可能改成别的",
    ):
        assert p._plan_commit_signal(text) is False, f"should NOT fire: {text!r}"


def test_plan_commit_signal_false_for_non_plan():
    p = _provider()
    # commit phrase but NOT plan-typed (no step/roadmap/PRD strong-signal)
    assert p._plan_commit_signal("最终版的配置项是 42") is False
    assert p._plan_commit_signal("定稿了，缓存用 redis") is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
