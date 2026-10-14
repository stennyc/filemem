#!/usr/bin/env python3
"""t_090d6f3d: _detect_type multilingual (en+zh) + optional host-LLM fallback.

Spike-3 hybrid:
  * _detect_type is a pure REGEX pass (zero LLM, deterministic). A multilingual
    strong-signal table (en/zh) routes CJK preference/decision/task items to the
    right type; everything else falls to the default `fact`.
  * At the single-save door, a regex `fact`-miss MAY be reclassified by the
    host main-model classify_type face (llm_caller.classify_type), gated by a
    per-session budget. No host runtime / over-budget / any failure -> the
    regex `fact` verdict stands (fail-soft, never blocks, never raises).

Red-line compliance asserted here:
  * regex path makes ZERO LLM calls (no main_runtime -> state["calls"]==0).
  * the LLM path goes through the host main-model face (no task= kwarg,
    explicit main_runtime, zero hardcoded model/provider config).
  * over-budget / no-runtime -> no LLM call, verdict stays fact.
"""
import sys
import pathlib
import threading
import json
import tempfile
from pathlib import Path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from __init__ import FileMemProvider, MEMORY_TYPES, _MEMORY_TYPE_REGEX
from consolidation.llm_caller import classify_type, TYPE_CLASSIFY_SCHEMA
from _llm_host_fake import _MAIN_RUNTIME, install


# ── REGEX layer: _detect_type (zero LLM) ──────────────────────────────────────

def _provider():
    """Bare provider shell: _detect_type only touches the module table + re."""
    p = FileMemProvider.__new__(FileMemProvider)
    p.session_id = "s1"
    return p


def test_zh_preference():
    p = _provider()
    for text in (
        "我更喜欢用 vim 而不是 vscode",
        "用户喜欢深色模式，不喜欢浅色",
        "我讨厌冗长的英文回复",
        "我倾向于用 python 写脚本",
    ):
        assert p._detect_type(text) == "preference", f"expected preference for {text!r}"
    print("✓ zh preference -> preference")


def test_zh_decision():
    p = _provider()
    for text in (
        "我决定选用 redis 作为缓存",
        "我们定下来用 kubernetes 部署",
        "拍板了，前端用 react",
        "我最终选定 postgres",
    ):
        assert p._detect_type(text) == "decision", f"expected decision for {text!r}"
    print("✓ zh decision -> decision")


def test_zh_task():
    p = _provider()
    for text in (
        "待办：周五前把 CI 修好",
        "记得去重启生产集群",
        "这个 bug 必须在下周之前解决",
        "计划下个月做一次全量备份",
        "别忘了更新 API 文档",
    ):
        assert p._detect_type(text) == "task", f"expected task for {text!r}"
    print("✓ zh task -> task")


def test_en_regression_unchanged():
    """The existing English verdicts must NOT regress."""
    p = _provider()
    assert p._detect_type("i prefer dark mode") == "preference"
    assert p._detect_type("i choose option B") == "decision"
    assert p._detect_type("todo: fix the CI pipeline") == "task"
    # plain facts (en) stay fact
    assert p._detect_type("the server runs on port 8080") == "fact"
    print("✓ en regression + en fact baseline")


def test_zh_plain_fact_stays_fact():
    """A CJK item with no strong-signal keyword must fall to fact (the
    baseline the LLM fallback is allowed to improve, but regex alone must
    NOT misroute it)."""
    p = _provider()
    for text in (
        "服务部署在 10.0.0.1 的 8080 端口上",
        "数据库是 postgres 15，每天 3 点备份",
        "项目用的是 go 1.21 编译的",
    ):
        assert p._detect_type(text) == "fact", f"expected fact for {text!r}"
    print("✓ zh plain fact -> fact (no misroute)")


def test_regex_is_zero_llm_and_table_driven():
    """_detect_type touches only the module table + re: no LLM seam, no
    provider LLM field needed. Every declared type (except fact, the default
    with no pattern row) has >=1 pattern row.

    PLAN-type PRD §1.3: the table gained a "plan" entry; the invariant is
    "every non-default type carries a pattern row", not a hardcoded key set
    (a hardcoded set broke the day plan landed — this assertion now derives
    from MEMORY_TYPES so adding a type can't silently regress the gate)."""
    expected_keys = set(MEMORY_TYPES) - {"fact"}
    assert set(_MEMORY_TYPE_REGEX) == expected_keys, \
        f"_MEMORY_TYPE_REGEX keys {set(_MEMORY_TYPE_REGEX)} != {expected_keys}"
    for t in expected_keys:
        assert _MEMORY_TYPE_REGEX[t], f"empty pattern row for {t}"
    p = _provider()
    # A provider with NO _main_runtime at all must still classify (regex-only).
    assert not hasattr(p, "_main_runtime")
    assert p._detect_type("我需要记住这个配置项") in MEMORY_TYPES
    print("✓ regex layer is table-driven + zero-LLM (no LLM field required)")


# ── LLM fallback: classify_type face (host main-model, fail-soft) ─────────────

def test_classify_type_happy_path(monkeypatch):
    install(monkeypatch, replies=[{"type": "task"}])
    res = classify_type(_MAIN_RUNTIME, "这个需求需要在下个迭代处理")
    assert res["type"] == "task"
    assert res["model_used"] is True
    print("✓ classify_type happy path")


def test_classify_type_invalid_type_fallback(monkeypatch):
    install(monkeypatch, replies=[{"type": "bogus"}])
    res = classify_type(_MAIN_RUNTIME, "x")
    assert res["type"] is None
    assert res["model_used"] is False
    print("✓ classify_type invalid type -> fail-soft None")


def test_classify_type_exception(monkeypatch):
    install(monkeypatch, raise_exc=Exception("host down"))
    res = classify_type(_MAIN_RUNTIME, "x")
    assert res["type"] is None
    assert res["model_used"] is False
    print("✓ classify_type exception -> fail-soft None")


def test_classify_type_no_main_runtime():
    """None main_runtime (standalone) -> exact fail-soft verdict, ZERO call."""
    res = classify_type(None, "x")
    assert res["type"] is None
    assert res["model_used"] is False
    print("✓ classify_type no main_runtime -> fail-soft, no call")


def test_classify_type_no_task_key_and_budgets(monkeypatch):
    fake, state = install(monkeypatch, replies=[{"type": "decision"}])
    classify_type(_MAIN_RUNTIME, "我们定下来用 x")
    assert state["calls"] == 1
    kw = state["kwarg_log"][0]
    assert "task" not in kw, "no auxiliary task pinning (host main-model route)"
    assert kw["temperature"] == 0.0
    assert kw["max_tokens"] == 64
    assert kw["main_runtime"] == _MAIN_RUNTIME
    print("✓ classify_type: temp0/mt64, no task key, explicit main_runtime")


# ── provider._reclassify_type_llm (budget gate + fail-soft) ───────────────────

def _full_provider(tmpdir):
    p = FileMemProvider.__new__(FileMemProvider)
    p.session_id = "s1"
    p.source_profile = "default"
    p._turn_active_sid = "s1"
    p.memories_dir = Path(tmpdir) / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir = Path(tmpdir)
    p._lock = threading.Lock()
    p._last_save_time = 0.0
    p._pending_failure_meta = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p._turn_marker_index = {}
    p._rate_limited_queue = []
    p._type_classify_limit = 5
    p._type_classify_budget = {}
    p._main_runtime = dict(_MAIN_RUNTIME)
    return p


def test_reclassify_overrides_fact(monkeypatch):
    install(monkeypatch, replies=[{"type": "task"}])
    p = _full_provider(tempfile.mkdtemp())
    out = p._reclassify_type_llm("这个需求需要在下个迭代处理", "s1")
    assert out == "task"
    assert p._type_classify_budget.get("s1") == 1
    print("✓ _reclassify_type_llm overrides fact -> task (budget consumed)")


def test_reclassify_no_runtime_keeps_fact(monkeypatch):
    install(monkeypatch, replies=[{"type": "task"}])
    p = _full_provider(tempfile.mkdtemp())
    p._main_runtime = None  # standalone / non-host
    out = p._reclassify_type_llm("这个需求需要在下个迭代处理", "s1")
    assert out is None  # regex fact stands
    print("✓ _reclassify_type_llm no runtime -> None (keep fact, zero call)")


def test_reclassify_llm_agrees_fact_keeps_fact(monkeypatch):
    """LLM returns the SAME fact verdict -> treated as no-override (None)."""
    install(monkeypatch, replies=[{"type": "fact"}])
    p = _full_provider(tempfile.mkdtemp())
    out = p._reclassify_type_llm("服务部署在 10.0.0.1 上", "s1")
    assert out is None  # stays fact; verdict unchanged
    print("✓ _reclassify_type_llm LLM-fact == regex fact -> no override")


def test_reclassify_budget_gate_zero(monkeypatch):
    install(monkeypatch, replies=[{"type": "task"}])
    p = _full_provider(tempfile.mkdtemp())
    p._type_classify_limit = 0  # feature off / budget closed
    out = p._reclassify_type_llm("这个需求需要在下个迭代处理", "s1")
    assert out is None
    print("✓ _reclassify_type_llm limit=0 -> no call")


def test_reclassify_over_cap_no_call(monkeypatch):
    fake, state = install(monkeypatch, replies=[{"type": "task"}])
    p = _full_provider(tempfile.mkdtemp())
    p._type_classify_limit = 1
    p._type_classify_budget = {"s1": 1}  # already at cap
    out = p._reclassify_type_llm("x", "s1")
    assert out is None
    assert state["calls"] == 0, "over-cap must NOT issue an LLM call"
    print("✓ _reclassify_type_llm over-cap -> no LLM call")


def test_reclassify_llm_exception_keeps_fact(monkeypatch):
    install(monkeypatch, raise_exc=Exception("host down"))
    p = _full_provider(tempfile.mkdtemp())
    out = p._reclassify_type_llm("x", "s1")
    assert out is None
    print("✓ _reclassify_type_llm exception -> None (fail-soft, keep fact)")


# ── single-save integration: type routing + metadata + zero-LLM guarantee ─────

def _read_shard(memdir, t):
    f = Path(memdir) / f"{t}.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8"))


def test_save_zh_task_llm_override_routes_to_task_shard(monkeypatch):
    """Single-save a CJK fact-miss; the host-LLM fallback (fake face -> task)
    re-routes it into task.json and stamps type_source=llm. The REGEX path
    made zero LLM calls; only the fallback's one call is counted."""
    install(monkeypatch, replies=[{"type": "task"}])
    with tempfile.TemporaryDirectory() as tmp:
        p = _full_provider(tmp)
        p.memories_dir = Path(tmp) / "memories"
        content = "这个需求需要在下个迭代处理"  # no zh strong-signal task keyword
        item_id = p._save_memory(content, level="L0", source="manual",
                                 _force_write=True)
        assert item_id, "the row must land (fail-soft, not dropped)"
        task_rows = _read_shard(p.memories_dir, "task")
        assert any(r["id"] == item_id for r in task_rows), \
            f"expected the row in task.json, got {task_rows!r}"
        row = next(r for r in task_rows if r["id"] == item_id)
        assert row["metadata"].get("type_source") == "llm"
        print("✓ single-save LLM override -> task.json + type_source=llm")


def test_save_zh_plain_fact_no_runtime_keeps_fact_zero_llm(monkeypatch):
    """No host runtime: the save is regex-only -> fact.json, type_source=regex,
    and the fake host face records ZERO calls (regex path is zero-LLM)."""
    fake, state = install(monkeypatch, replies=[{"type": "task"}])
    with tempfile.TemporaryDirectory() as tmp:
        p = _full_provider(tmp)
        p.memories_dir = Path(tmp) / "memories"
        p._main_runtime = None
        content = "服务部署在 10.0.0.1 的 8080 端口上"
        item_id = p._save_memory(content, level="L0", source="manual",
                                 _force_write=True)
        assert item_id
        fact_rows = _read_shard(p.memories_dir, "fact")
        assert any(r["id"] == item_id for r in fact_rows), "plain fact -> fact.json"
        row = next(r for r in fact_rows if r["id"] == item_id)
        assert row["metadata"].get("type_source") == "regex"
        assert state["calls"] == 0, "regex path must make ZERO LLM calls"
        print("✓ single-save no runtime -> fact.json + type_source=regex + 0 LLM calls")


def test_save_zh_task_regex_hit_no_llm(monkeypatch):
    """A CJK item with a zh strong-signal task keyword: regex routes it to
    task.json with NO LLM call (the fallback only fires on a fact-miss)."""
    fake, state = install(monkeypatch, replies=[{"type": "preference"}])
    with tempfile.TemporaryDirectory() as tmp:
        p = _full_provider(tmp)
        p.memories_dir = Path(tmp) / "memories"
        content = "待办：周五前把 CI 修好"
        item_id = p._save_memory(content, level="L0", source="manual",
                                 _force_write=True)
        assert item_id
        task_rows = _read_shard(p.memories_dir, "task")
        assert any(r["id"] == item_id for r in task_rows), "regex zh task hit"
        row = next(r for r in task_rows if r["id"] == item_id)
        assert row["metadata"].get("type_source") == "regex"
        assert state["calls"] == 0, "regex hit must NOT call the LLM"
        print("✓ single-save zh regex-hit -> task.json + 0 LLM calls")


if __name__ == "__main__":
    import _llm_host_fake as _f
    import consolidation.llm_caller as _lc
    test_classify_type_no_main_runtime()
    test_zh_preference(); test_zh_decision(); test_zh_task()
    test_en_regression_unchanged(); test_zh_plain_fact_stays_fact()
    test_regex_is_zero_llm_and_table_driven()
    test_reclassify_no_runtime_keeps_fact()
    test_reclassify_budget_gate_zero()
    test_reclassify_llm_exception_keeps_fact()
    print("\n✅ multilingual _detect_type TDD: run the full matrix under pytest")
