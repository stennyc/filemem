"""P0-1(e) leg: whole kanban task-card prompts are kept OUT of the pool.

Autopsy 2026-11-06: task_429 (t_24d4791b card, 4707 chars) + task_430
(t_1a8da114 card, 7106 chars) entered the shared store via the
failure-catch auto path (kanban_block tool_error / patch retry_loop),
were distilled to confirmed/value=high, and the auto-inject face
re-injected the whole cards into [记忆上下文] on 2026-11-05 22:14 —
the P8 'kanban task mixed into memory context' incident.

The new (e) leg rejects the card shape at the write ingress choke
point; clean experience lines that merely MENTION a t_xxx id stay
passing (no 误杀).

RED: fails before the (e) leg is implemented (ingress_reject_class
returns None for the card, _save_memory writes the pool row).
"""
import json
import pathlib
import re
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryTriggerRouter,
    ingress_reject_class,
)

# The real task_429 card head (abridged to the 3 markers, same shape).
KANBAN_CARD = """# Kanban task t_24d4791b: M6 · Express rate-limit + Socket.IO CORS 白名单（外网开放安全加固）

Assignee: coder
Status:   running
Workspace: scratch @ /root/.hermes/kanban/workspaces/t_24d4791b

## Body
# M6 · Express rate-limit + Socket.IO CORS 白名单（外网开放安全加固）
> **parent**: t_4dc50ae3（devops compose 端口改 0.0.0.0，18080 已对外）
本卡是安全红线：node 绑 0.0.0.0 后外网直连 Express，必须加最小防护才放行玩家。
"""

# Clean rows that must KEEP passing (提及卡 id 的经验行不是卡本身).
CLEAN_SAMPLES = [
    "kanban_block 工具对 unknown id 返回 error，不能用来判断卡状态。t_24d4791b 已被 t_1a8da114 取代。",
    "P8 收口：probeStoredSession 只走 cache → active-profile by-id，404 透传。commit 8035380。",
    "9200 dist rebuild：vite build + background + 硬刷，chunk 名按 mtime 验。",
    "M6 Express rate-limit 卡被 t_1a8da114 取代（trust proxy + zoraxy 域名 CORS）。",
]

# Truncated card: head present but BOTH body + assignee markers missing.
TRUNC_HEAD_ONLY = "# Kanban task t_abc12345: 某标题\n\n只有一行截断内容。"


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Same partial-provider shape as test_p0_1_ingress_gate._make_provider."""
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
    """All pool rows across every shard file (memories/*.json)."""
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


def test_classifier_rejects_whole_kanban_card():
    assert ingress_reject_class(KANBAN_CARD) == "whole_kanban_task_prompt"


def test_classifier_rejects_real_task429_shape():
    # the exact head + 3 markers of the on-disk task_429 row
    real = ("# Kanban task t_24d4791b: M6 · Express rate-limit + Socket.IO CORS 白名单（外网开放安全加固）\n"
            "\nAssignee: coder\nStatus:   running\n"
            "Workspace: scratch @ /root/.hermes/kanban/workspaces/t_24d4791b\n"
            "\n## Body\n# M6 · Express rate-limit + Socket.IO CORS 白名单")
    assert ingress_reject_class(real) == "whole_kanban_task_prompt"


def test_clean_mention_rows_pass_all_legs():
    for row in CLEAN_SAMPLES:
        assert ingress_reject_class(row) is None, row[:50]


def test_truncated_card_passes_fail_soft():
    # head present but BOTH body + assignee markers missing → dual-feature
    # gate (ii) misses → not rejected (accepted fail-soft, P1 posture).
    assert ingress_reject_class(TRUNC_HEAD_ONLY) is None


def test_none_and_empty_pass():
    assert ingress_reject_class(None) is None
    assert ingress_reject_class("") is None


def test_save_memory_auto_source_rejects_kanban_card():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(KANBAN_CARD, level="L0", source="auto")
    assert rid == "", "kanban card (auto source) must NOT write a pool row"
    assert _read_pool(p) == [], "kanban card leaked into the pool"


def test_save_memory_manual_source_unaffected():
    """The P0-1 gate is scoped to source=='auto'; a manual save of card
    text is user-confirmed and NOT gated (the documented invariant)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(KANBAN_CARD, level="L0", source="manual")
    assert rid != "", "manual save is unaffected by the ingress gate"
