"""settled 机制行为测试（RED->GREEN）。

验证 5 个挂点：
  ① _SETTLED_DONE_SIGNALS / _SETTLED_SCAN_SHARDS 常量存在
  ② auto 面命中完成信号 -> settled_candidate（不折出）
  ③ confirmed_draft 拍板 -> settled（折出）
  ④ _o1_fold_out_of_auto 对 settled 折出、对 candidate 不折出
  ⑤ _unsettle_by_kanban_id：扫全四分片、清 settled/candidate + reopened 痕迹 + audit

纯 stdlib、零 LLM、不碰宿主。synthetic provider 走 __new__ 绕过 initialize。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
import __init__ as fm  # noqa: E402


def _make_provider(tmp_path: Path, memories=None, sessions=None) -> "fm.FileMemProvider":
    """minimal synthetic provider wired against a tmp store (no initialize).
    Mirrors test_s4_corrupt_heal._make_provider's attribute set."""
    import threading
    p = fm.FileMemProvider.__new__(fm.FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "test-settled"
    p.source_profile = None
    p.storage_dir = tmp_path / "storage"
    p.storage_dir.mkdir(parents=True, exist_ok=True)
    p.memories_dir = tmp_path / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp_path / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = tmp_path / "episodic"
    p.knowledge_dir = tmp_path / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = None
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.knowledge_archive_dir = p.knowledge_dir / "archive"
    p.pending_queue_path = p.storage_dir / "consolidated" / "pending_upgrades.jsonl"
    p._decay = {}
    p._llm_ctx = None
    p._main_runtime = None
    p._llm_consolidating = False
    p._supersede_confirming = False
    p._alias_mining_in_flight = False
    p.trigger_router = None
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p._turn_marker_index = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._last_save_time = 0.0
    p._save_interval = 0.0
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._last_consolidation_ts = 0.0
    p._last_skip_audit_ts = 0.0
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {"reinforced": 0, "durable_hits": 0}
    p.min_search_score = 0.3
    p.auto_extract = False
    p._turn_active_sid = "test-settled"
    p._turn_foreign = False
    p._turn_self_test = False
    return p


# ── ① 常量存在 ─────────────────────────────────────────────────────────────
def test_settled_constants_present():
    assert hasattr(fm, "_SETTLED_DONE_SIGNALS")
    assert hasattr(fm, "_SETTLED_KANBAN_ID_RE")
    assert hasattr(fm, "_SETTLED_SCAN_SHARDS")
    # 全四分片一步到位（不留后路）
    assert fm._SETTLED_SCAN_SHARDS == ("task", "decision", "preference", "fact")
    # 正则能匹配 kanban id
    assert fm._SETTLED_KANBAN_ID_RE.fullmatch("t_086371a0")
    assert not fm._SETTLED_KANBAN_ID_RE.fullmatch("t_086371a0xx")


# ── ② auto 面命中完成信号 -> settled_candidate ────────────────────────────
def test_auto_save_done_signal_stamps_candidate(tmp_path):
    p = _make_provider(tmp_path)
    rid = p._save_memory("六个全 done，正式关账", level="L0", source="auto")
    assert rid
    rows = json.loads((p.memories_dir / "task.json").read_text(encoding="utf-8")
                      if (p.memories_dir / "task.json").exists() else "[]")
    # task 或 decision 都可能，扫全部分片找
    allrows = []
    for f in (p.memories_dir).glob("*.json"):
        allrows += json.loads(f.read_text(encoding="utf-8"))
    mine = [r for r in allrows if r.get("id") == rid]
    assert mine, "row not found"
    assert mine[0]["metadata"].get("settled_candidate") is True
    # 纯自动不直接折出（candidate 不 = settled）
    assert mine[0]["metadata"].get("settled") is None


# ── ③ confirmed_draft 拍板 -> settled ──────────────────────────────────────
def test_confirmed_draft_stamps_settled(tmp_path):
    p = _make_provider(tmp_path)
    rid = p._save_memory("收工归档卡已落地", level="L1", source="confirmed_draft")
    assert rid
    allrows = []
    for f in (p.memories_dir).glob("*.json"):
        allrows += json.loads(f.read_text(encoding="utf-8"))
    mine = [r for r in allrows if r.get("id") == rid]
    assert mine
    assert mine[0]["metadata"].get("settled") is True


# ── ④ 折出谓词：settled 折出 / candidate 不折出 / superseded 折出 ─────────
def test_fold_out_of_auto_settled_vs_candidate():
    assert fm._o1_fold_out_of_auto({"metadata": {"settled": True}}) is True
    assert fm._o1_fold_out_of_auto({"metadata": {"settled_candidate": True}}) is False
    assert fm._o1_fold_out_of_auto({"metadata": {"superseded": True}}) is True
    assert fm._o1_fold_out_of_auto({"metadata": {}}) is False
    assert fm._o1_fold_out_of_auto({}) is False


# ── ⑤ unsettle：扫全四分片 + 清标记 + reopened 痕迹 + audit ────────────────
def _seed_settled_rows(p, tid, shards):
    """在多个分片里各造一条带 settled + kanban 外键的行。"""
    for st in shards:
        mem_file = p.memories_dir / f"{st}.json"
        rows = json.loads(mem_file.read_text(encoding="utf-8")) if mem_file.exists() else []
        rid = f"{st}_settled_test_{tid}"
        rows.append({
            "id": rid, "type": st, "content": f"卡 {tid} 已 done 收口",
            "level": "L0", "source": "auto", "created_at": "2026-10-08T00:00:00",
            "updated_at": "2026-10-08T00:00:00",
            "metadata": {"settled": True, "settled_candidate": False},
        })
        with open(mem_file, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)


def test_unsettle_scans_all_shards_and_revives(tmp_path):
    p = _make_provider(tmp_path)
    p._read_memories_file = lambda m: (
        json.loads(m.read_text(encoding="utf-8")) if m.exists() else [])
    # 四个分片各塞一条 settled + 外键
    _seed_settled_rows(p, "t_086371a0",
                       ["task", "decision", "preference", "fact"])
    n = p._unsettle_by_kanban_id("t_086371a0", source_event="kanban_task_event")
    assert n == 4, f"expect 4 revived across 4 shards, got {n}"
    # 逐片复核：标记已清 + reopened 痕迹在
    for st in ["task", "decision", "preference", "fact"]:
        rows = json.loads((p.memories_dir / f"{st}.json").read_text(encoding="utf-8"))
        r = [x for x in rows if x["id"].endswith("t_086371a0")]
        assert r, f"row missing in {st}"
        assert r[0]["metadata"]["settled"] is False
        assert r[0]["metadata"]["settled_candidate"] is False
        assert r[0]["metadata"]["reopened"] is True
        assert r[0]["metadata"]["reopened_by"] == "kanban_task_event:t_086371a0"


def test_unsettle_noop_when_no_matching_row(tmp_path):
    p = _make_provider(tmp_path)
    p._read_memories_file = lambda m: (
        json.loads(m.read_text(encoding="utf-8")) if m.exists() else [])
    # 不 seed 任何行 -> 全 no-op
    assert p._unsettle_by_kanban_id("t_zznotthere", source_event="kanban_task_event") == 0


def test_unsettle_skips_rows_without_settled_flag(tmp_path):
    p = _make_provider(tmp_path)
    p._read_memories_file = lambda m: (
        json.loads(m.read_text(encoding="utf-8")) if m.exists() else [])
    # 该行含外键但无 settled/candidate -> 不动
    mem = p.memories_dir / "task.json"
    with open(mem, "w", encoding="utf-8") as f:
        json.dump([{"id": "task_x", "type": "task",
                    "content": "卡 t_086371a0 进行中（未定案）",
                    "metadata": {}}], f, ensure_ascii=False)
    assert p._unsettle_by_kanban_id("t_086371a0") == 0
    rows = json.loads(mem.read_text(encoding="utf-8"))
    assert "reopened" not in rows[0]["metadata"]


def test_unsettle_writes_audit_row(tmp_path):
    p = _make_provider(tmp_path)
    p._read_memories_file = lambda m: (
        json.loads(m.read_text(encoding="utf-8")) if m.exists() else [])
    p._append_consolidation_event = lambda ev: _capture_audit.append(ev)
    _capture_audit.clear()
    _seed_settled_rows(p, "t_aaaa1111", ["task"])
    p._unsettle_by_kanban_id("t_aaaa1111")
    assert any(a.get("action") == "kanban_unsettle" and a.get("task_id") == "t_aaaa1111"
               for a in _capture_audit), "audit row missing"


_capture_audit: list = []
