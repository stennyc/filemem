"""scheduled 门 Step 4 (owner 2026-11-12): 活体自动探测（live due-todo probe）。

Mirrors _plan_probe (owner ruling "搬活体自动探测，照 plan_probe 模板"):
  * _scheduled_probe is the ONLY LLM face that can raise a scheduled
    candidate — budget-gated (default 3/session, decay scheduled_probe
    rollback switch), wide due-signal pre-screen is the ONLY cost gate
    (layer B / regex layer was deleted: scheduled is write-side-only,
    exempt from the auto-land regex table per
    test_regex_is_zero_llm_and_table_driven's scheduled exemption).
  * A prescreen miss fires ZERO LLM calls and returns None (cost red
    line — 宁漏勿炸 applies to the both-miss case).
  * A prescreen pass feeds consolidation.llm_caller.probe_scheduled
    (host main-model face, fail-soft: ANY host failure -> None, no
    marker, no panel, 不炸宿主; the ATTEMPT charges the budget).
  * A LLM hit whose on_due_do is empty is downgraded to a miss
    (unstructured_hit_downgraded, mirrors probe_plan's posture).
  * An auto-kind hit with a missing expects is downgraded to remind
    (owner "auto 双必填" invariant — the panel entry must stay
    confirmable without violating it).
  * The PENDING panel push + confirm chain is Step 1's existing
    _scheduled_probe_push / filemem_review_drafts(action="scheduled") —
    this test file does NOT re-wire them, it only asserts the marker
    shape + that the push lands a side-stamped probe marker (the
    confirm consumption itself is covered by test_scheduled_slice1).
"""
import json, sys, threading, types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import _SCHEDULED_DUE_SIGNALS  # noqa: E402
import consolidation.llm_caller as llm  # noqa: E402


def _make_provider(tmp: Path, sid: str = "s1") -> fm.FileMemProvider:
    p = fm.FileMemProvider.__new__(fm.FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.episodic_dir = None
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.session_id = sid
    p._turn_active_sid = sid
    p.trigger_router = fm.MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._last_save_time = 0
    p._save_interval = 0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._pending_drafts_notified = {}
    p._shared_mem = None
    p._decay = {}
    p._append_consolidation_event = lambda ev: None
    return p


def _with_runtime(p):
    p._main_runtime = {"model": "test-model", "provider": "test",
                       "base_url": "http://x", "api_key": "k",
                       "api_mode": "chat"}
    return p


def _stub_probe_scheduled(p, res):
    """Route _load_llm_caller's probe_scheduled to a fixed reply (zero real
    host call) — mirrors the classify_type test seam's install() helper."""
    real = fm._load_llm_caller
    fake_mod = types.SimpleNamespace(probe_scheduled=lambda mr, content: dict(res))
    p._load_llm_caller_test_override = fake_mod
    fm._load_llm_caller = lambda: fake_mod
    return real


# ── prescreen cost-gate (zero LLM on a miss) ─────────────────────────────

def test_probe_prescreen_miss_zero_llm(tmp_path):
    p = _make_provider(tmp_path)
    p = _with_runtime(p)
    calls = []
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: (calls.append(1),
                                       {"probe": True, "content": "x",
                                        "on_due_do": "x"})[1])
    # "今天天气不错" — no due-signal word, no LLM call, no marker.
    assert p._scheduled_probe("今天天气不错") is None
    assert calls == []
    fm._load_llm_caller = real
    assert p._scheduled_probe_last_decision == "prescreen_skip"

def test_probe_prescreen_pass_feeds_llm(tmp_path):
    p = _make_provider(tmp_path)
    p = _with_runtime(p)
    # "重启完让我跑一轮 X 验证" — hits _SCHEDULED_DUE_SIGNALS ("重启完").
    hit = {"probe": True, "content": "重启后跑 X 验证", "due_at_hint": "",
           "action_kind": "auto", "on_due_do": "跑 X 验证",
           "expects": "X 全绿", "model_used": True}
    calls = []
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: (calls.append(1), dict(hit))[1])
    marker = p._scheduled_probe("重启完让我跑一轮 X 活体验证")
    fm._load_llm_caller = real
    assert len(calls) == 1
    assert marker is not None
    assert marker["kind"] == "scheduled"
    assert marker["on_due_do"] == "跑 X 验证"
    assert marker["action_kind"] == "auto"
    assert p._scheduled_probe_last_decision == "prescreen_pass"


def test_probe_no_main_runtime_returns_none(tmp_path):
    p = _make_provider(tmp_path)  # no _main_runtime set
    calls = []
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: (calls.append(1),
                                       {"probe": True, "on_due_do": "x"})[1])
    assert p._scheduled_probe("重启完让我跑 X") is None
    fm._load_llm_caller = real
    assert calls == []  # zero host calls, standalone posture


def test_probe_budget_gate_stops_calling(tmp_path):
    p = _make_provider(tmp_path)
    p = _with_runtime(p)
    p._scheduled_probe_limit = 1
    p._scheduled_probe_budget = {}
    res = {"probe": True, "on_due_do": "x", "content": "x"}
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: dict(res))
    m1 = p._scheduled_probe("重启完跑 X")
    m2 = p._scheduled_probe("下周再验 Y")
    fm._load_llm_caller = real
    assert m1 is not None
    assert m2 is None  # over-cap (budget[ sid ]==1 == limit): no 2nd call
    assert p._scheduled_probe_budget.get("s1") == 1


def test_probe_decay_disable_flag_rolls_back(tmp_path):
    p = _make_provider(tmp_path)
    p = _with_runtime(p)
    p._decay = {"scheduled_probe": {"enable": False}}
    calls = []
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: (calls.append(1),
                                       {"probe": True, "on_due_do": "x"})[1])
    assert p._scheduled_probe("重启完跑 X") is None
    fm._load_llm_caller = real
    assert calls == []


def test_probe_downgrade_auto_missing_expects_to_remind(tmp_path):
    """owner 'auto 双必填' invariant: an auto-kind LLM hit that fails to
    produce an accepts criterion must land as remind, so the panel entry
    stays confirmable without a broken auto row."""
    p = _make_provider(tmp_path)
    p = _with_runtime(p)
    # Simulate the LLM face's own downgrade decision (probe_scheduled already
    # downgrades auto->remind when expects is empty; this test locks that the
    # marker CARRIES the downgraded kind, not the original auto).
    res = {"probe": True, "content": "x", "action_kind": "remind",
           "on_due_do": "x", "expects": "", "model_used": True}
    real = fm._load_llm_caller
    fm._load_llm_caller = lambda: types.SimpleNamespace(
        probe_scheduled=lambda mr, c: dict(res))
    marker = p._scheduled_probe("重启完跑 X")
    fm._load_llm_caller = real
    assert marker["action_kind"] == "remind"


def test_probe_scheduled_llm_face_downgrades_auto_no_expects():
    """Direct probe_scheduled LLM-face test (no provider shell): an auto
    verdict that arrives WITHOUT an accepts criterion is downgraded to
    remind inside the face itself (the provider just carries whatever the
    face returned — locking the face, not the marker plumbing)."""
    fake_reply = {"probe": True, "content": "c", "action_kind": "auto",
                  "on_due_do": "do X", "expects": ""}
    real_parse = llm._structured_call
    llm._structured_call = lambda *a, **k: fake_reply
    try:
        out = llm.probe_scheduled({"model": "x"}, "重启完跑 X 验证")
    finally:
        llm._structured_call = real_parse
    assert out["probe"] is True
    assert out["action_kind"] == "remind"  # downgraded: auto w/o expects


def test_probe_scheduled_llm_face_no_on_due_do_downgraded():
    fake_reply = {"probe": True, "content": "c", "on_due_do": ""}
    real_parse = llm._structured_call
    llm._structured_call = lambda *a, **k: fake_reply
    try:
        out = llm.probe_scheduled({"model": "x"}, "重启完跑 X")
    finally:
        llm._structured_call = real_parse
    assert out["probe"] is False
    assert out.get("reason") == "unstructured_hit_downgraded"


def test_probe_scheduled_llm_face_failure_fail_soft():
    def _boom(*a, **k):
        raise RuntimeError("host down")
    real_parse = llm._structured_call
    llm._structured_call = _boom
    try:
        out = llm.probe_scheduled({"model": "x"}, "重启完跑 X")
    finally:
        llm._structured_call = real_parse
    assert out["probe"] is False
    assert out["model_used"] is False
    assert "fallback" in out["reason"]


# ── wide table itself is what the owner approved (over-feed) ─────────────

def test_wide_due_signals_table_is_nonempty_and_used():
    assert len(_SCHEDULED_DUE_SIGNALS) >= 10
    # The owner's own 555e-shape example must be caught by the wide table:
    assert any("重启完" in s for s in _SCHEDULED_DUE_SIGNALS)
