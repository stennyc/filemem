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
import copy
import re
import sys
import threading
import pathlib
import json
import types
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
import __init__ as fm
from __init__ import FileMemProvider, MEMORY_TYPES
from consolidation import DECAY_DEFAULTS


def _noop_router():
    r = types.SimpleNamespace()
    r.detect_and_route = lambda content: {"level": None}
    return r


def _make_store(tmp, sid):
    store = tmp / "filemem" / sid
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_home(tmp, letter="h"):
    home = tmp / letter / "home"
    (home / "memories").mkdir(parents=True, exist_ok=True)
    return home
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


# ── F4: live-path face — agent reports a spec/PRD landing (owner 2026-10-17) ─
# Known-gap fix: _save_memory's probe trigger never sees the specs/ file write
# (agent writes PRD to disk directly, not through the memory store). The live
# path is this turn's ASSISTANT text reporting the landing. A joint pre-screen
# (plan-signal word AND a spec/PRD-landing structure word) routes the text into
# the SAME budget-gated _plan_probe face — no new LLM face, no auto-land.

def test_plan_land_signals_table_exists():
    """The structure-word table is module-level and non-empty (F4 pre-screen
    layer A: a spec-landing report carries these words; the table is matched
    as case-insensitive SUBSTRINGS, not regexes)."""
    assert isinstance(fm._PLAN_LAND_SIGNALS, tuple) and len(fm._PLAN_LAND_SIGNALS) >= 3, \
        "F4: _PLAN_LAND_SIGNALS table missing or too thin"
    # the owner's own PRD vocabulary must be covered (the 2026-10-17 supercompress
    # PRD used all of these; substring match, lower-cased):
    for word in ("PRD 落地", "独立失败面", "逃生门", "TDD", "RED 锚", "specs/"):
        assert any(word.lower() in s.lower() for s in fm._PLAN_LAND_SIGNALS), \
            f"F4: _PLAN_LAND_SIGNALS does not cover {word!r}"


def test_plan_probe_prescreen_passes_spec_landing_report():
    """A PRD-landing assistant report passes the two-layer pre-screen
    (structure word hit OR plan-signal word hit), so _plan_probe actually
    issues the LLM call for it."""
    p = _provider()
    report = ("PRD 落地了。\n文件：/usr/local/lib/hermes-agent/plugins/context_engine/"
              "supercompress/specs/水位递进带PRD-降档序90到55加90逃生门两面.md（新建 specs/ 目录、"
              "18KB）。两条面彻底分开：A 面 sc 窗口膨胀，B 面 474k 宿主 refuse 卡死。"
              "8 条 TDD RED 锚（T-A1/A2/A3 降档序 + T-B1/B2/B3 两面叠省）。"
              "改动的 6 个点全在 sc 侧。")
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    p._plan_probe_last_decision = None
    p._plan_probe_last_seen = None

    calls = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": False, "model_used": True}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    try:
        p._plan_probe(report)
    finally:
        monkeypatch.undo()
    assert calls, \
        ("F4: a spec/PRD-landing report with plan-signal words must PASS the "
         "pre-screen and reach the LLM probe face (joint signal, not just "
         "the auto task/decision rows _save_memory sees)")


def test_plan_probe_prescreen_blocks_plain_prose():
    """Negative control: ordinary assistant prose without the joint signal
    (no plan-signal word, no structure word) is NOT probed — zero LLM call."""
    p = _provider()
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    p._plan_probe_last_decision = None
    p._plan_probe_last_seen = None

    calls = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": True, "title": "t", "steps": ["a"], "done_when": "x"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    try:
        res = p._plan_probe("缓存命中率在压测后从 78% 升到 91%")
    finally:
        monkeypatch.undo()
    assert res is None and calls == [], \
        "F4: plain prose without the joint plan-signal+structure signal must NOT reach the LLM probe"


def _make_provider(tmp, store, home, source_profile="coder", host_factory=None,
                   session_id="s-e2e", auto_extract=False,
                   decay=None):
    """A synthetic provider wired for the sync_turn live path (no initialize();
    the attrs below are what sync_turn touches). Mirrors the production
    __new__ wiring the drain-path tests use, plus the plan-probe seam."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = source_profile
    p.hermetis_home = home
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = store / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = copy.deepcopy(decay) if decay is not None else copy.deepcopy(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._main_runtime = None
    p.trigger_router = _noop_router()
    p._session_drafts = {}
    p._last_recall = None
    p._turn_marker_index = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_active_sid = session_id
    p._session_hwm = {}
    p._turn_report_line = lambda sid: ""
    p._drain_seen = {}
    p._drain_host_store_fn = host_factory
    p.auto_extract = auto_extract
    return p


def test_plan_probe_prescreen_blocks_when_both_layers_miss(monkeypatch):
    """Negative control (cost red line, '宁漏勿炸'): assistant prose that
    misses BOTH pre-screen layers — no _PLAN_LAND_SIGNALS structure word AND
    no _MEMORY_TYPE_REGEX["plan"] plan-signal word — is NOT probed, zero LLM
    call. This is the both-miss case only; a hit on EITHER layer passes."""
    p = _provider()
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    calls = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": True, "title": "t", "steps": ["a"], "done_when": "x"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    try:
        res = p._plan_probe("查了，缓存命中率在压测后从 78% 升到 91%，不用改")
    finally:
        monkeypatch.undo()
    assert res is None and calls == [], \
        ("F4: prose with no structure word and no plan-signal word must NOT "
         "reach the LLM probe (both layers miss = skip)")


def test_plan_probe_prescreen_structure_word_sufficient_alone(monkeypatch):
    """Layer A alone is sufficient: a structure word hit (e.g. '两条面') with
    NO plan-signal word still passes the pre-screen and reaches the LLM probe
    face — the LLM decides whether it is a genuine multi-step plan (the
    pre-screen is a cost gate, not the verdict)."""
    p = _provider()
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    calls = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": False, "model_used": True}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    try:
        p._plan_probe("两条面分开处理，A 面 sc 窗口，B 面宿主。")
    finally:
        monkeypatch.undo()
    assert calls, \
        "F4: a structure-word hit alone (layer A) must pass the pre-screen"


def test_sync_turn_probes_spec_landing_assistant_report(monkeypatch, tmp_path):
    """End-to-end live path: sync_turn whose assistant text reports a specs/
    PRD landing (joint signal present) issues the probe face once for the
    report, and a no-signal assistant turn issues zero probes. This is the
    face the 2026-10-17 supercompress PRD turn was supposed to hit and missed
    (owner catch: 'filemem 还是没有触发 LLM 询问用户是否保存')."""
    store = _make_store(tmp_path, "s-e2e")
    home = _make_home(tmp_path, "h-e2e")
    p = _make_provider(tmp_path, store, home, session_id="s-e2e")
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    p._plan_probe_last_decision = None
    p._plan_probe_last_seen = None
    p._plan_probe_marker_pushed = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            return {"probe": False, "model_used": True}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    monkeypatch.setattr(p, "_plan_probe_push", lambda m: p._plan_probe_marker_pushed.append(m))

    report = ("PRD 落地了。文件：/root/specs/水位递进带PRD-降档序.md（新建 specs/ 目录）。"
              "两条面彻底分开：A 面 sc 窗口膨胀，B 面 474k 宿主 refuse。"
              "8 条 TDD RED 锚 + 6 个改动点全在 sc 侧。")
    p.sync_turn("改 supercompress 水位", report)
    assert p._plan_probe_last_seen == report, \
        "F4: sync_turn must feed the spec-landing assistant report into the plan probe"
    assert p._plan_probe_last_decision == "prescreen_pass", \
        f"F4: joint-signal report must pass the pre-screen (got {p._plan_probe_last_decision!r})"

    # negative control: a turn whose assistant text has NO joint signal -> skipped
    p._plan_probe_last_decision = None
    p._plan_probe_last_seen = None
    p.sync_turn("查了个配置", "查了，缓存命中率 91%，不用改")
    assert p._plan_probe_last_decision == "prescreen_skip", \
        f"F4: no-signal assistant turn must NOT reach the probe face (got {p._plan_probe_last_decision!r})"
    assert p._plan_probe_last_seen is None


# ── slice-6: capture plan/spec write-paths from host transcript, read the
# file head, feed the LLM probe face (owner 2026-10-17 "肯定是特征") ────────
# The 5-step chain: 捕获 -> 分析(会话+落盘路径) -> 读文件头 -> 确认是plan ->
# 入plan记忆. Host tool_calls structure (verified in agent_runtime_helpers.py):
# role="assistant" rows carry tool_calls[] = [{"function": {"name", "arguments"}}]
# where arguments may be a JSON string OR a dict. Host agent/ is ZERO diff —
# we only read the transcript the host already writes.

def test_plan_path_features_table_exists():
    """The feature-word table is module-level and non-empty (slice-6 gate:
    only extract write-paths whose filename/directory carries a plan feature)."""
    assert isinstance(fm._PLAN_PATH_FEATURES, tuple) and len(fm._PLAN_PATH_FEATURES) >= 3, \
        "slice-6: _PLAN_PATH_FEATURES table missing or too thin"
    # the owner's own PRD vocabulary must be covered:
    for word in ("plan", "prd", "spec", "roadmap"):
        assert word in [w.lower() for w in fm._PLAN_PATH_FEATURES], \
            f"slice-6: _PLAN_PATH_FEATURES does not cover {word!r}"


def test_extract_plan_write_paths_filters_by_feature(tmp_path):
    """Given a host transcript with write_file/patch tool_calls, extract the
    candidate plan/spec paths — only those whose filename OR directory carries
    a _PLAN_PATH_FEATURES word. Non-feature paths (e.g. src/foo.py) are dropped."""
    p = _provider()
    # Build a host-shaped transcript: assistant row with tool_calls, arguments
    # as a JSON STRING (the host's real shape per agent_runtime_helpers L133).
    write_args = json.dumps({
        "path": str(tmp_path / "specs" / "水位PRD-plan.md"),
        "content": "# PRD ...",
    })
    patch_args = json.dumps({
        "path": str(tmp_path / "src" / "lib.py"),
        "old_string": "a", "new_string": "b",
    })
    history = [
        {"role": "user", "content": "写一份改造计划"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "write_file", "arguments": write_args}},
            {"function": {"name": "patch", "arguments": patch_args}},
        ]},
    ]
    paths = p._extract_plan_write_paths(history)
    # the spec path (carries "spec" in dir + "PRD" in name) must be present
    assert any("specs" in pp or "plan" in pp for pp in paths), \
        f"slice-6: spec/plan path not extracted from transcript: {paths!r}"
    # the non-feature path (src/lib.py — no plan/prd/spec word) must be dropped
    assert not any("lib.py" in pp for pp in paths), \
        f"slice-6: non-feature path {paths!r} must be filtered out"
    # dedup: same dir twice collapses to one
    assert len(paths) == 1, f"slice-6: expected 1 dedup'd path, got {paths!r}"


def test_extract_plan_write_paths_empty_when_no_tool_calls():
    """A transcript with no tool_calls (or no assistant rows) yields an empty
    candidate list — no false positives."""
    p = _provider()
    assert p._extract_plan_write_paths([
        {"role": "user", "content": "改水位"},
        {"role": "assistant", "content": "改好了"},
    ]) == []
    # None / malformed history must not raise
    assert p._extract_plan_write_paths(None) == []
    assert p._extract_plan_write_paths([{"role": "assistant"}]) == []


def test_plan_probe_file_reads_head_and_probes(tmp_path, monkeypatch):
    """Given a candidate plan file path, _plan_probe_file reads the file head
    (~400 chars) and feeds it to the probe_plan LLM face; a hit returns a
    marker carrying the source path + the file's structured steps. No auto-land
    (the marker still rides the clarify channel via _plan_probe_push)."""
    plan_file = tmp_path / "specs" / "改造计划.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        "# 改造计划\n步骤1：加正则表\n步骤2：加 LLM 面\n步骤3：加测试\n"
        "完成条件：全绿\n", encoding="utf-8")
    p = _provider()
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3

    fed = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            fed.append(content)
            return {"probe": True, "title": "改造计划",
                    "steps": ["加正则表", "加 LLM 面", "加测试"],
                    "done_when": "全绿", "model_used": True}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)

    marker = p._plan_probe_file(str(plan_file))
    assert marker is not None, "slice-6: a probe hit must yield a marker"
    # the LLM face was fed the file's HEAD (not the assistant's paraphrase)
    assert fed, "slice-6: the probe LLM face must be issued with the file head"
    assert "步骤1" in fed[0], \
        "slice-6: the LLM must receive the file head content (file body, not paraphrase)"
    # the marker carries the source path so the consumption face can record it
    assert marker.get("source_path") == str(plan_file), \
        f"slice-6: marker must carry the source file path (got {marker.get('source_path')!r})"
    # no auto-land: the marker is a PENDING probe marker, not a written row
    assert marker["kind"] == "plan_probe"


def test_plan_probe_file_missing_path_is_noop(tmp_path):
    """A candidate path that does not exist on disk -> None (fail-soft, no
    LLM call issued, no crash into the turn-close path)."""
    p = _provider()
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    calls = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            calls.append(content)
            return {"probe": True, "title": "t", "steps": ["a"], "done_when": "x"}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    try:
        res = p._plan_probe_file(str(tmp_path / "does_not_exist_plan.md"))
    finally:
        monkeypatch.undo()
    assert res is None, "slice-6: a missing file must yield None"
    assert calls == [], "slice-6: no LLM call for a missing file (fail-soft)"


def test_sync_turn_feeds_file_body_to_probe(tmp_path, monkeypatch):
    """End-to-end: sync_turn whose transcript shows a write_file into a spec
    dir (feature hit) reads the file head and feeds the probe face — the owner's
    5-step chain 捕获->分析->读文件头->确认->入plan, with the LLM seeing the
    FILE BODY (not just the assistant's one-line report)."""
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    plan_md = spec_dir / "水位递进带PRD-降档序.md"
    plan_md.write_text(
        "# 水位递进带 PRD\n步骤S1：降档分档骨架\n步骤S2：账本旧页 evict\n"
        "步骤S3：输出面裁剪\n完成条件：全绿 + 宿主零 diff\n", encoding="utf-8")
    store = _make_store(tmp_path / "store", "s-file")
    home = _make_home(tmp_path / "home", "h-file")
    p = _make_provider(tmp_path, store, home, session_id="s-file")
    p._plan_probe_budget = {}
    p._plan_probe_limit = 3
    p._plan_probe_marker_pushed = []

    fed = []

    class RecordingLoader:
        @staticmethod
        def probe_plan(main_runtime, content):
            fed.append(content)
            return {"probe": True, "title": "水位递进带 PRD",
                    "steps": ["降档分档", "旧页 evict", "输出面裁剪"],
                    "done_when": "全绿", "model_used": True}

    monkeypatch.setattr(p, "_live_main_runtime", lambda: {"model": "f", "provider": "f"})
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: RecordingLoader)
    monkeypatch.setattr(p, "_plan_probe_push", lambda m: p._plan_probe_marker_pushed.append(m))

    history = [
        {"role": "user", "content": "出一份降档 PRD"},
        {"role": "assistant", "content": "PRD 落地了。", "tool_calls": [
            {"function": {"name": "write_file",
                          "arguments": json.dumps({"path": str(plan_md), "content": "# 水位递进带 PRD"})}},
        ]},
    ]
    p.sync_turn("出一份降档 PRD", "PRD 落地了。", session_id="s-file", messages=history)
    # the LLM face must have received the FILE BODY (the real 5-step PRD),
    # not just the one-line "PRD 落地了" report:
    assert fed, "slice-6: the probe LLM face must be issued for a spec write"
    assert any("降档分档" in c for c in fed), \
        ("slice-6: the LLM must receive the file head/body (the PRD steps), "
         "not only the assistant's one-line report")
    # and a marker was pushed through the clarify channel (no auto-land)
    assert p._plan_probe_marker_pushed, \
        "slice-6: a probe hit must push a PENDING marker for the clarify channel"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
