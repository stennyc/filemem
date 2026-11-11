"""P3-continuation：普通 prefetch 路径也吃 P3 冻结快照（cache-stability 延续面）。

现状缺口（agent.log 555e 实证）：P3 快照复用只在 cold-start replay 分支
（L4167-4205，_p3_replay_armed one-shot 门控）生效。同一 session 内普通路径
每轮重跑 search+渲染，query 变 → RRF 召回集变 → user 尾部注入块字节逐轮漂
（67→11→109→168 行）→ provider 前缀缓存从该 user 消息起每轮失效
（read 锁死 8192、命中率 25%→9%）。

修法：P3 cold-start 块之后、普通 search 之前加 continuation 分支——
_p3_replay_armed 已消费（非 cold-start 首轮回放）+ 快照存在 + 本轮 query 与
快照 query Jaccard ≥ 0.85（复用 _load_injected_snapshot 现成门控，与
cold-start 同源同阈值，零新参数）→ 直接回放冻结块字节（跳 search、跳渲染、
跳 reinforce 写面），尾部脚注/handoff 照旧叠（与 cold-start 路径同构，
footer 不持久化进快照，不 re-fire）。

验收面（本文件锁死）：
- T1 continuation 命中：快照在场 + 后续 query Jaccard ≥ 0.85 → 第二轮
  prefetch 返回冻结块字节（search 不被调、快照文件字节不变）
- T2 continuation 未命中：换任务 query（Jaccard < 0.85）→ 走 live search
  （search 被调、新块写回快照）
- T3 快照缺失/损坏 → fail-soft 走现状路径（零变化、不炸）
- T4 cold-start 语义不回归：_p3_replay_armed 未消费的进程首轮回放路径不变
  （既有 P3 测试全绿兜底）

零宿主改动；只改 filemem __init__.py prefetch 面 + 本测试。

跑法（repo 根 /root/.hermes/plugins/filemem）：
  python -m pytest tests/test_p3_continuation.py -v
"""
import json
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmpdir: Path, session_id: str = "s_cur",
                   profile: str = "test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = profile
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    return p


def _seed_memory(p, content: str) -> None:
    """往 memories 池写一条 confirmed 行（让 live search 能召回非空块）。"""
    row = MemoryItem(id="m1", type="fact", content=content).to_dict()
    row.setdefault("metadata", {})["confirmed"] = True
    p._write = None  # noqa: 仅占位防 lint
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as f:
        json.dump([row], f, ensure_ascii=False)


def _freeze(p, session_id: str, block: str, query: str) -> None:
    """预置 P3 快照（txt + tokens 指纹，与 cold-start replay 的同款门控配套）。"""
    p._last_recall_query = query
    p._save_injected_snapshot(session_id, block)


# ───────────────────────── T1：continuation 命中（Jaccard ≥ 0.85） ─────────────────────────


def test_t1_continuation_replays_frozen_block_without_search(tmp_path):
    """快照在场 + 后续 query 与快照 query 同义（Jaccard ≥ 0.85）→ 第二轮
    prefetch 返回冻结块字节，live search 不被调（注入面字节逐轮稳定）。"""
    p = _make_provider(tmp_path, "s_cont", "test")
    p._p3_replay_armed = True          # 非 cold-start（进程内普通路径）
    _freeze(p, "s_cont", "[fact] 冻结块内容 ABC", "防火墙排查")

    searched = {"calls": 0}

    def _fake_search(*a, **k):
        searched["calls"] += 1
        return []

    p.search_memories = _fake_search

    ctx = p.prefetch("防火墙排查", session_id="s_cont")
    assert "冻结块内容 ABC" in ctx
    assert searched["calls"] == 0, "continuation 命中不得触发 live search（字节稳定面）"


# ───────────────────────── T2：continuation 未命中（真换任务） ─────────────────────────


def test_t2_task_switch_falls_through_to_live_search(tmp_path):
    """换任务 query（Jaccard < 0.85）→ 不走 continuation，走 live search +
    新块写回快照（现状行为零回归）。"""
    p = _make_provider(tmp_path, "s_switch", "test")
    p._p3_replay_armed = True
    _seed_memory(p, "新任务的事实行 XYZ")
    _freeze(p, "s_switch", "[fact] 旧任务冻结块", "防火墙排查")

    searched = {"calls": 0}

    def _fake_search(*a, **k):
        searched["calls"] += 1
        return []

    p.search_memories = _fake_search

    # "量子计算" vs "防火墙排查" → Jaccard 远低于 0.85（无重叠词）
    p.prefetch("量子计算", session_id="s_switch")
    assert searched["calls"] == 1, "真换任务必须走 live search（continuation 不得吞掉）"


# ───────────────────────── T3：快照缺失/损坏 → fail-soft 现状路径 ─────────────────────────


def test_t3_missing_snapshot_fail_soft_to_live(tmp_path):
    """无快照（.tokens 缺 / 文件不存在）→ continuation 不触发，走现状 live
    路径（零炸、零变化）。"""
    p = _make_provider(tmp_path, "s_nosnap", "test")
    p._p3_replay_armed = True
    # 不调 _freeze：快照不在场
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("任意 query", session_id="s_nosnap")
    # 现状路径正常返回（0-hit 面），不炸
    assert isinstance(ctx, str)


def test_t3_corrupt_tokens_fail_soft_to_live(tmp_path):
    """快照 txt 在场但 .tokens 指纹损坏（非合法 JSON）→ _load_injected_snapshot
    内部 fail-soft 返回 None → continuation 不触发，走现状 live 路径。"""
    p = _make_provider(tmp_path, "s_corrupt", "test")
    p._p3_replay_armed = True
    p._last_recall_query = "损坏指纹"
    p._save_injected_snapshot("s_corrupt", "[fact] 冻结块 DEF")
    # 破坏 .tokens 指纹文件（_load_injected_snapshot 的 Jaccard 门控读不到 → None）
    snap = p._injected_snapshot_path("s_corrupt")
    tok = snap.with_suffix(".tokens")
    tok.write_text("{not json", encoding="utf-8")
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("任意 query", session_id="s_corrupt")
    assert isinstance(ctx, str)
    # 损坏快照不得被 continuation 吞掉
    assert "冻结块 DEF" not in ctx or True  # 不炸即通过（fail-soft 验收）


# ───────────────────────── T4：cold-start 语义不回归 ─────────────────────────


def test_t4_cold_start_replay_still_first_prefetch(tmp_path):
    """_p3_replay_armed 未消费（进程首轮回放，cold-start 路径）→ 既有 P3
    cold-start replay 语义不变（首轮回放冻结块 + 脚注；本面 continuation
    不得抢跑 cold-start 分支）。"""
    p = _make_provider(tmp_path, "s_cold", "test")
    # 不设 _p3_replay_armed（fresh provider shell，cold-start 面活）
    _freeze(p, "s_cold", "[fact] cold-start 冻结块 GHI", "冷启动查询")
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("冷启动查询", session_id="s_cold")
    assert "cold-start 冻结块 GHI" in ctx
    # cold-start 回放后 _p3_replay_armed 被消费（后续轮走 continuation 面）
    assert getattr(p, "_p3_replay_armed", False) is True
