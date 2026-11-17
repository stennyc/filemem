"""P0-1(f) leg: user meta-discourse about the system itself stays OUT of the pool.

Autopsy 2026-11-17 (the "问了又问" loop, root-cause layer #5): the user's own
meta-complaint about the memory system — e.g.

    "filemem 为何还是相同待裁决的草稿，问了又问？已裁决了为何还要无限次数重复问？"

— is NOT a durable environmental fact. It is the re-ask loop's own FUEL: the
complaint re-mentions the very topics it re-surfaces, so auto-catch keeps
re-capturing it as a fresh L0 draft row, the [记忆报告] panel re-asks it, and
the user's attempt to stop the asking re-triggers a new ask.

Fix: a new (f) reject leg in the shared write-ingress choke point
(`ingress_reject_class`, wired into the auto / draft / batch doors by the
existing P0-1 wiring). Double-feature detector `_is_meta_system_complaint`
(single source, pure rule + stdlib, zero LLM): BOTH a system-self-reference
(记忆报告 / 待裁决 / 已裁决 / 草稿 / 召回 / filemem / kbs / hermes / …) AND a
complaint-or-repetition framing (为何 / 为什么 / 老是 / 又问 / 重复问 /
无限次数 / …) must hit. Precision invariants:

  * a clean fact that merely MENTIONS the system ("filemem 和 kbs 依赖安装
    改成 code_map 式自动安装方式") keeps passing — zero 误杀;
  * a generic complaint without system markers ("怎么老是忘事") passes —
    precision-first (the owner's F2 posture);
  * manual-source saves are NOT gated (P0-1 documented invariant): a
    user-confirmed save of any text lands, the gate only stops auto-capture.

RED: fails before the (f) leg exists (ingress_reject_class returns None for
the meta-complaint, _save_memory auto writes the pool row).
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryTriggerRouter,
    ingress_reject_class,
)

# The exact #5 meta-complaint that triggered this leg (2026-11-17 live data).
REAL_COMPLAINT_5 = ("filemem 为何还是相同待裁决的草稿，问了又问？"
                    "已裁决了为何还要无限次数重复问？")

META_VARIANTS = [
    "filemem 为何老是重复问同一个话题",
    "为什么又弹相同的待裁决草稿",
    "记忆报告怎么老是重复问已裁决过的话题",
    "filemem 为何还要无限次数重复问",
    "kbs 的依赖安装为何老是失败",
    "hermes 为何老是重新注入同样的记忆",
]

# Clean rows that must KEEP passing (mention the system / look complaint-adjacent
# but miss exactly ONE of the two features).
CLEAN_SAMPLES = [
    # system names, declarative, no complaint framing (the #1 topic):
    "filemem 和 kbs 依赖安装改成 code_map 式自动安装方式",
    # system path fact:
    "记住：filemem 装在 C:/Users/user/AppData/Local/hermes/plugins/filemem",
    # system mention + declarative fact:
    "hermes gateway 的重启命令是 hermes dashboard --stop 后重新打开",
    # complaint framing, but NO system-self-reference (generic user gripe):
    "怎么老是忘事",
    "为什么 github push 失败了",
    # plain user fact, no system reference at all:
    "用户偏好：回复用中文，代码注释用英文",
]


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Same partial-provider shape as test_kanban_ingress_leg._make_provider."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.episodic_dir = None
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = {"lambda_days": 0.05}
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._pending_drafts_notified = {}
    return p


def _read_pool(p) -> list:
    rows = []
    for f in p.memories_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            rows.extend(data)
        elif isinstance(data, dict):
            rows.extend(data.values())
    return rows


# ── module-level classifier ─────────────────────────────────────────────────


def test_classifier_rejects_real_complaint_5():
    assert ingress_reject_class(REAL_COMPLAINT_5) == "meta_system_complaint"


def test_classifier_rejects_meta_variants():
    for row in META_VARIANTS:
        assert ingress_reject_class(row) == "meta_system_complaint", row[:50]


def test_clean_system_mentions_pass_all_legs():
    for row in CLEAN_SAMPLES:
        assert ingress_reject_class(row) is None, row[:50]


def test_none_and_empty_pass():
    assert ingress_reject_class(None) is None
    assert ingress_reject_class("") is None


def test_existing_legs_unaffected():
    """(a)(b)(b2)(c)(d)(e) keep their classes — the new leg only ADDS."""
    assert ingress_reject_class("[CONTEXT COMPACTION — REFERENCE ONLY] xxx") == \
        "host_instruction_block"
    assert ingress_reject_class(
        '{"output": "browser console dump body", "exit_code": 0}') == \
        "raw_tool_output"
    assert ingress_reject_class('{\n  "a": 1\n}') == "structural_opener"
    # real task_429 head shape (same as test_kanban_ingress_leg fixtures):
    assert ingress_reject_class(
        "# Kanban task t_24d4791b: M6 · Express rate-limit\n\nAssignee: coder\n"
        "Status:   running\nWorkspace: scratch @ /root/.hermes/kanban/x\n\n"
        "## Body\n# M6 · Express rate-limit\n本卡是安全红线：必须加最小防护才放行。"
    ) == "whole_kanban_task_prompt"


# ── provider wiring (the auto door) ────────────────────────────────────────


def test_save_memory_auto_source_rejects_meta_complaint():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(REAL_COMPLAINT_5, level="L0", source="auto")
    assert rid == "", "meta-complaint (auto source) must NOT write a pool row"
    assert _read_pool(p) == [], "meta-complaint leaked into the pool"


def test_save_memory_manual_source_unaffected():
    """P0-1 documented invariant: a user-confirmed (manual) save of ANY text
    lands — the gate only stops auto-capture, never an explicit user save."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(REAL_COMPLAINT_5, level="L0", source="manual")
    assert rid != "", "manual save is unaffected by the ingress gate"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
