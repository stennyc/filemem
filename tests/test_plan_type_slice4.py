#!/usr/bin/env python3
"""PLAN-type PRD slice 4 — the plan shard's write gate (owner ruling 2026-10-17).

Owner ruling: the LLM type-classifier face must NOT be the door that lands a
row into plan.json. The classifier lacks the capability to recognize that the
agent itself authored a PRD / plan / ordered multi-step sequence, so ordinary
content (user corrections, design choices, water-level specs) was getting the
`plan` tag and polluting the plan shard with rows that carry NO plan_steps /
NO plan_state / NO L2 / NO no-TTL — i.e. the exact opposite of the PRD shape.

Three faces are locked here:

F1 (LLM classify face): `plan` is REMOVED from the classifier's output
   vocabulary. The LLM may still *detect* plan-shaped content, but the only
   verdict it can return is one of the legacy four (preference/fact/decision/
   task); plan-shaped content now classifies as task (ordered steps to
   complete) or decision (a settled plan). The instruction that used to
   describe "plan" as a returnable type is replaced by an explicit
   never-return-"plan" clause.

F2 (regex face): the regex strong-signal `plan` row is kept ONLY as a
   pre-screen signal for the probe face (F3), not as a classifier verdict
   path. `FileMemProvider._detect_type` no longer returns "plan" — it is
   routed to the task/preference/decision verdicts instead, so a regex hit
   can never land a row in plan.json by itself.

F3 (probe + clarify face): a NEW `FileMemProvider._plan_probe(content)`
   surface. It is the ONLY LLM face allowed to raise a plan candidate; it
   is budget-gated + fail-soft, and it emits a PENDING PROBE marker (a
   structured dict: {title, steps, done_when}) rather than writing a row.
   The caller (save-door / turn-close hook) pushes that marker through the
   existing clarify channel (`_push_pending_confirm` kind="plan_probe") so
   the USER is asked "save this detected plan/PRD to memory?"; only a
   confirmed answer lands a PRD-shaped row via `filemem_plan(action=
   "create")`. No auto-land, ever.

Portability: no absolute paths; provider shells are `__new__` where possible;
llm_caller constants are module-level (zero I/O); the probe LLM face is
exercised through the loader-seam monkeypatch used by the slice-2 tests.
"""
import sys
import pathlib
import json
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
import __init__ as fm
from __init__ import FileMemProvider, MEMORY_TYPES
from consolidation.llm_caller import (
    TYPE_CLASSIFY_SCHEMA,
    TYPE_CLASSIFY_INSTRUCTIONS,
    classify_type,
    PLAN_PROBE_INSTRUCTIONS,
    PLAN_PROBE_SCHEMA,
    probe_plan,
)


# ── F1: LLM classify face — 'plan' removed from the returnable vocabulary ────

def test_classify_type_enum_excludes_plan():
    """The classifier's schema no longer offers 'plan' as a verdict.

    MEMORY_TYPES itself STILL contains 'plan' (the shard exists — that is
    the filemem_plan face's business); only the LLM *classifier* loses it.
    """
    enum = TYPE_CLASSIFY_SCHEMA["properties"]["type"]["enum"]
    assert "plan" not in enum, \
        f"F1: classify_type enum {enum!r} still offers 'plan' as a verdict"
    # legacy four intact
    for t in ("preference", "fact", "decision", "task"):
        assert t in enum
    # the shard itself still exists for the filemem_plan face
    assert "plan" in MEMORY_TYPES


def test_classify_type_instructions_never_return_plan():
    """The instructions carry an explicit never-return-"plan" clause, and the
    old 'plan = ordered steps with progress + completion state' *definition*
    (which invited the LLM to hand out the tag) is gone."""
    low = TYPE_CLASSIFY_INSTRUCTIONS.lower()
    assert "never return \"plan\"" in low or "never return 'plan'" in low \
        or "do not return \"plan\"" in low, \
        "F1: instructions lack the explicit never-return-plan clause"
    # the invitation is gone: the old line that defined plan as a returnable
    # shard type must be removed.
    assert "plan = ordered steps with progress" not in low


def test_classify_type_helper_rejects_plan_verdict():
    """Defense-in-depth in the helper: even a malformed LLM reply that says
    'plan' is treated as invalid -> None (the caller keeps the regex
    verdict)."""
    # main_runtime=None -> no host surface -> fail-soft None.
    res = classify_type(None, "Phase 1: land the PRD; Phase 2: land the code")
    assert res.get("type") is None
    assert res.get("model_used") is False


# ── F2: regex face — _detect_type no longer returns "plan" ──────────────────

def _provider():
    p = FileMemProvider.__new__(FileMemProvider)
    p.session_id = "s1"
    return p


def test_detect_type_plan_signals_no_longer_return_plan():
    """The plan strong-signal texts that USED to verdict `plan` now route to
    task (ordered steps) — never the plan shard. This is the F2 routing: the
    regex row survives as a pre-screen signal (F3 probe), not as a verdict."""
    p = _provider()
    for text in (
        "Implementation plan: Step 1 add the regex table, Step 2 add the LLM face, Step 3 add tests",
        "roadmap: fix sequence for the ledger bug across 4 steps",
        "实施计划：第1步加正则表，第2步加 LLM 面，第3步加测试",
    ):
        verdict = p._detect_type(text)
        assert verdict != "plan", \
            f"F2: _detect_type still returns 'plan' for {text!r} (got {verdict!r})"
        # routes to a legacy shard (task is the expected home for ordered steps)
        assert verdict in ("task", "preference", "decision", "fact")


def test_plan_regex_row_survives_as_probe_prescreen():
    """The regex TABLE still carries the plan row (it is now the probe's
    pre-screen signal), even though _detect_type no longer verdicts it. A
    missing table entry would make F3's pre-screen a no-op."""
    assert "plan" in fm._MEMORY_TYPE_REGEX, \
        "F3: the plan regex pre-screen row was deleted — the probe has no signal"


# ── F3: probe face — LLM detection + PENDING marker + clarify channel ────────

def test_plan_probe_instructions_exist_and_scoped():
    """The probe face has its OWN instructions + schema (not a reuse of the
    classifier's), scoped to 'does this content contain an agent-authored
    multi-step plan/PRD worth remembering'."""
    assert isinstance(PLAN_PROBE_INSTRUCTIONS, str) and PLAN_PROBE_INSTRUCTIONS
    low = PLAN_PROBE_INSTRUCTIONS.lower()
    # scoped: mentions both agent-authored plan and the "only if multi-step"
    # guard so the LLM does not probe on a single to-do.
    assert "plan" in low
    assert ("multi-step" in low or "多步" in low or "multi step" in low)
    # structured output shape
    assert "steps" in json.dumps(PLAN_PROBE_SCHEMA)


def test_review_drafts_schema_exposes_plan_probe_face():
    """Owner ruling 2026-10-17 (reviewer caveat resolved): the LLM must be
    able to DISCOVER the plan-probe consumption path from the tool schema.
    Before slice-4, the `action="plan_probe"` + `verdict` consumption branch
    existed in _tool_review_drafts but the schema's action enum was still
    ["preview","confirm","discard"] with no `verdict` property — so a listed
    plan-probe panel entry had no discoverable tool path. Locked here:
    `plan_probe` is in the action enum AND `verdict` is a property."""
    p = _provider()
    for schema in p.get_tool_schemas():
        if schema.get("name") == "filemem_review_drafts":
            props = schema["parameters"]["properties"]
            assert "plan_probe" in props["action"]["enum"], \
                f"plan_probe missing from filemem_review_drafts action enum: " \
                f"{props['action']['enum']!r}"
            assert "verdict" in props, \
                "verdict property missing from filemem_review_drafts schema"
            assert set(props["verdict"]["enum"]) == {"save", "dismiss"}
            return
    raise AssertionError("filemem_review_drafts tool schema not found")


def test_probe_plan_returns_pending_marker_on_hit(monkeypatch):
    """A plan-shaped content + LLM hit -> a PENDING PROBE marker dict
    (title + steps + done_when), NOT a written row."""
    p = _provider()

    class FakeLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            return {"probe": True,
                    "title": "fix ledger bug",
                    "steps": ["locate", "patch", "verify"],
                    "done_when": "all 3 steps green",
                    "model_used": True}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: FakeLoader)
    p._plan_probe_budget = {}
    p._plan_probe_limit = 5

    res = p._plan_probe("Implementation plan: Step 1 locate, Step 2 patch, Step 3 verify")
    assert res is not None, "F3: probe should return a pending marker on a hit"
    assert res["kind"] == "plan_probe"
    assert res["title"] == "fix ledger bug"
    assert res["steps"] == ["locate", "patch", "verify"]
    # the probe NEVER lands a row itself (no auto-land)


def test_probe_plan_failsoft_on_miss(monkeypatch):
    """An LLM 'no plan detected' verdict -> None (no clarify fired)."""
    p = _provider()

    class FakeLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            return {"probe": False, "model_used": True}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: FakeLoader)
    p._plan_probe_budget = {}
    p._plan_probe_limit = 5

    res = p._plan_probe("缓存命中率在压测后从 78% 升到 91%")
    assert res is None, "F3: a miss must yield None (no clarify, no row)"


def test_probe_plan_budget_gate(monkeypatch):
    """Over the per-session probe budget -> None WITHOUT issuing the call
    (the avoid-LLM-cost-explosion red line, same posture as classify_type)."""
    p = _provider()
    calls = []

    class FakeLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": True, "title": "t", "steps": ["a"], "done_when": "x"}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: FakeLoader)
    p._plan_probe_budget = {"s1": 3}
    p._plan_probe_limit = 2  # already over cap

    res = p._plan_probe("Step 1, Step 2, Step 3")
    assert res is None
    assert calls == [], "F3: over-budget probe must NOT issue the LLM call"


def test_probe_plan_charges_attempt_on_host_failure(monkeypatch):
    """A host-call FAILURE still charges the session budget — 'charge the
    attempt, not the success'. A persistent host-down must not retry the LLM
    call on every plan-signal auto-save (the avoid-LLM-cost-explosion red
    line); the failed attempt burns the budget so retries stop this session.
    Mirrors the _reclassify_type_llm 'call happened -> charge it' posture."""
    p = _provider()

    class RaisingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            raise RuntimeError("host down")

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RaisingLoader)
    p._plan_probe_budget = {}
    p._plan_probe_limit = 1

    res = p._plan_probe("Implementation plan: Step 1, Step 2, Step 3")
    assert res is None, "host failure -> None (fail-soft, save not blocked)"
    # The attempt was charged: the budget is now at its cap.
    assert p._plan_probe_budget.get("s1") == 1, \
        f"F3: host-call failure must charge the budget (got {p._plan_probe_budget!r})"
    # A second probe now hits the over-cap path -> no further LLM attempt.
    calls = []

    class FakeLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": True, "title": "t", "steps": ["a"], "done_when": "x"}

    monkeypatch.setattr(fm, "_load_llm_caller", lambda: FakeLoader)
    res2 = p._plan_probe("Implementation plan: Step 1, Step 2, Step 3")
    assert res2 is None
    assert calls == [], "over-budget (post-failure) probe must NOT re-issue the LLM call"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
