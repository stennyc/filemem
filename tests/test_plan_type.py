#!/usr/bin/env python3
"""PLAN-type PRD (specs/PLAN-type-PRD.md) — TDD red tests, slice 1.

Vertical slice 1 (the minimal runnable face, per TDD):
  * MEMORY_TYPES gains "plan"
  * _MEMORY_TYPE_REGEX gains a "plan" entry (en + zh strong signals)
  * SLICE-4 (owner ruling 2026-10-17) flip: _detect_type no longer VERDICTS
    "plan" — a regex plan-signal routes to "task" (the ordered-steps home)
    and the plan regex row survives only as a pre-screen signal for the
    probe face. The LLM classify_type face likewise no longer accepts a
    "plan" verdict (enum + instructions + helper valid-set + caller
    whitelist all steer clear of "plan"). The ONLY writer of the plan
    shard is the probe + clarify confirm chain (test_plan_type_slice4.py).
  * the existing tool schemas (filemem_search / filemem_write) expose "plan"
    in their type enums (read-compat)

Slice 1 deliberately does NOT touch the plan.json storage / filemem_plan tool /
reconcile / multi-profile faces — those are later slices (PRD §6/§7/§8).

Portability: no absolute paths; the provider shell is __new__ (zero I/O) for
the regex layer; the LLM face + tool schema are module-level (zero provider).
"""
import sys
import pathlib
import json
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
import __init__ as fm
from __init__ import (
    FileMemProvider,
    MEMORY_TYPES,
    _MEMORY_TYPE_REGEX,
)
from consolidation.llm_caller import (
    TYPE_CLASSIFY_SCHEMA,
    TYPE_CLASSIFY_INSTRUCTIONS,
)


# ── 1.1 type registration ────────────────────────────────────────────────────

def test_plan_in_memory_types():
    assert "plan" in MEMORY_TYPES, f"MEMORY_TYPES={MEMORY_TYPES!r} missing 'plan'"
    # other four untouched (no regression)
    for t in ("preference", "fact", "decision", "task"):
        assert t in MEMORY_TYPES


def test_plan_regex_table_entry_present():
    assert "plan" in _MEMORY_TYPE_REGEX, "plan regex table entry missing"
    pats = _MEMORY_TYPE_REGEX["plan"]
    assert isinstance(pats, (list, tuple)) and len(pats) >= 1, "plan regex entry must be a non-empty pattern list"


# ── 1.3 regex layer: _detect_type (zero LLM, deterministic) ──────────────────

def _provider():
    p = FileMemProvider.__new__(FileMemProvider)
    p.session_id = "s1"
    return p


def test_detect_type_plan_english():
    """Slice 4 flip (owner ruling 2026-10-17): plan-shaped content no longer
    verdicts `plan`. The regex plan row is now a pre-screen signal for the
    probe face; a plan-hit routes to `task` (ordered steps to complete).
    The LLM probe face (not the regex layer) is the only path that can land
    a confirmed plan row in the plan shard.
    """
    p = _provider()
    for text in (
        "Implementation plan: Step 1 add the regex table, Step 2 add the LLM face, Step 3 add tests",
        "Phase 1 lands the PRD, Phase 2 lands the code, Phase 3 lands the review",
        "roadmap: fix sequence for the ledger bug across 4 steps",
        "execution plan with multi-step repair, each step test-gated",
    ):
        verdict = p._detect_type(text)
        assert verdict != "plan", \
            f"F2: _detect_type still verdicts 'plan' for {text!r} (got {verdict!r})"
        assert verdict in ("task", "preference", "decision", "fact"), \
            f"plan-shape {text!r} should route to a legacy shard, got {verdict!r}"


def test_detect_type_plan_chinese():
    p = _provider()
    for text in (
        "实施计划：第1步加正则表，第2步加 LLM 面，第3步加测试",
        "修复步骤：步骤1 定位存根，步骤2 修账本，步骤3 验证",
        "落地方案已定稿：分3阶段执行",
        "路线图：多步骤实施",
    ):
        verdict = p._detect_type(text)
        assert verdict != "plan", \
            f"F2: _detect_type still verdicts 'plan' for {text!r} (got {verdict!r})"
        assert verdict in ("task", "preference", "decision", "fact"), \
            f"plan-shape {text!r} should route to a legacy shard, got {verdict!r}"


def test_detect_type_task_not_regressed():
    """Pure task strong-signal must still land task (plan must NOT steal it)."""
    p = _provider()
    # these match task regex rows, not plan
    assert p._detect_type("记得在周五前跑一遍回归测试") == "task"
    assert p._detect_type("todo: remember to bump the version") == "task"


def test_detect_type_fact_not_regressed():
    p = _provider()
    # a neutral statement with no step/plan/strong-signal -> fact
    assert p._detect_type("缓存命中率在压测后从 78% 升到 91%") == "fact"


def test_detect_type_preference_decision_not_regressed():
    p = _provider()
    assert p._detect_type("我更喜欢用 vim 而不是 vscode") == "preference"
    assert p._detect_type("我决定选用 redis 作为缓存") == "decision"


# ── 1.4 LLM face: classify_type (slice 4) no longer accepts "plan" ──────────

def test_llm_type_classify_enum_includes_plan():
    """Slice 4 flip: the LLM upgrade face no longer advertises 'plan'.

    Owner ruling (2026-10-17): plan rows enter plan.json ONLY via the
    confirmed filemem_plan face (probe + clarify). The t_090d6f3d
    type-classify face (enum + instructions + helper valid-set + caller
    whitelist) must steer clear of 'plan' entirely. The legacy four remain.
    """
    enum = TYPE_CLASSIFY_SCHEMA["properties"]["type"]["enum"]
    assert "plan" not in enum, f"I1: TYPE_CLASSIFY_SCHEMA enum {enum!r} still lists 'plan'"
    # the legacy four must remain (no regression)
    for t in ("preference", "fact", "decision", "task"):
        assert t in enum


def test_llm_type_classify_instructions_mention_plan():
    """The instructions must steer the LLM AWAY from 'plan' (a reservation
    clause): plan-shaped content classifies as task/decision here, never plan."""
    low = TYPE_CLASSIFY_INSTRUCTIONS.lower()
    assert "plan" in low, \
        "instructions must mention plan (to tell the LLM NOT to return it)"


# ── read-compat: existing tool schemas expose "plan" ─────────────────────────

def _tool_schema(name: str):
    p = _provider()
    # full provider needed for get_tool_schemas only if it touches storage;
    # build a minimal one (the schema face is pure data, no I/O).
    for schema in p.get_tool_schemas():
        if schema.get("name") == name:
            return schema
    raise AssertionError(f"tool schema {name!r} not found")


def test_search_tool_schema_type_enum_includes_plan():
    s = _tool_schema("filemem_search")
    tprop = s["parameters"]["properties"]["type"]
    assert "plan" in tprop["enum"], f"filemem_search type enum {tprop['enum']!r} missing 'plan'"


def test_write_tool_schema_type_enum_includes_plan():
    s = _tool_schema("filemem_write")
    tprop = s["parameters"]["properties"]["type"]
    assert "plan" in tprop["enum"], f"filemem_write type enum {tprop['enum']!r} missing 'plan'"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
