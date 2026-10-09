"""S3⑤-C: triage 消费后确定性派生 L1 裁决经验行 — TDD spec.

背景 (用户拍板 C 案): task 行的三选项裁决 (已完成/已过时/待确认) 是
用户已做的决定 — 裁决本身直接落记忆, 不再二次 clarify (违反"定案
就不再问"偏好). 但现状 (test_s3_status_triage ⑤) 只把 status 写回 task
行, 不产生 "用户已裁决 task X 为 Y" 的 L1 经验行 — 于是下次相似 task
出现在注入面时, agent 仍不知道这件事已被裁决过, 会重问.

规格 (钉死, 确定性, 零 LLM):
  * filemem_review_drafts(action="triage", status=...) 消费成功时, 同一
    写事务内 (task.json shard lock 内) 在 task 分片派生一条 L1 行:
      - id 由本插件生成 (triage_derived_* 前缀, 不复用 task 行 id),
      - type="task", level="L1", source="triage_derived",
      - content = 人类可读的裁决记录 (task 原 content 截断 + 裁决值),
      - metadata.source_task = 原 task 行 id,
      - metadata.triage_status = 裁决值,
      - metadata.triage_decided_by = "user",
      - reference_count=0, salience 继承原行 (缺省 0.5),
      - 该行 status 保持缺省/active (它是活经验, 不是被裁决对象).
  * 幂等: 同一 task 行重复 triage 消费 (面板重弹 / 用户改判) -> 派生行
    先查 metadata.source_task == 原 id 的既有派生行, 有则 UPDATE 其
    content/metadata (triage_status 跟着改判), 不产生第二条派生行.
  * 原 task 行本身 status 照旧写回 (现状行为不回退), 派生行是新增,
    不改原行其他字段.
  * 裁决为 pending (待确认) 时同样派生 — "用户选择保持待确认" 本身是
    一条可回忆的经验 (下次注入时 agent 知道: 此事悬而未决, 别再推进).
  * 派生写入失败 (shard 锁内 IO 异常) -> 整个 triage 消费 fail-soft:
    返回 success=False 不假装 (不吞脏), 原行 status 不落, 面板不消费
    (下轮重报, 可重试).
  * 宿主零改动: 只动 plugins/filemem/{__init__.py, tests/};
    /usr/local/lib/hermes-agent grep triage_derived = 0.
"""
import copy
import json
import pathlib
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

# 复用 S3 triage 测试的 fixture 骨架 (同源).
from test_s3_status_triage import (  # noqa: E402
    _task_row, _decay_with_triage, _pending_triage,
)
from test_s3_dream_faces import _make_store, _make_provider  # noqa: E402


def _task_rows(store):
    return json.loads((store / "memories" / "task.json").read_text(encoding="utf-8"))


def _derived_rows(store):
    return [r for r in _task_rows(store)
            if str(r.get("source", "")) == "triage_derived"]


def _triage_consume(p, rid, status):
    return json.loads(p._tool_review_drafts({
        "action": "triage", "memory_id": rid, "status": status}))


# ═══════════════════════════════════════════════════════════════════════════
# ① completed 消费 -> 恰好 1 条 L1 派生行, 字段全对
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c1_completed_derives_single_l1_row(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [_task_row("task-x", "修 task-state-hook 的 StateDbReplacedError",
                            reference_count=0, status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}

    res = _triage_consume(p, "task-x", "completed")
    assert res.get("success") is True, f"triage 消费应成功, 实得 {res}"

    derived = _derived_rows(store)
    assert len(derived) == 1, f"completed 必须派生恰好 1 条经验行, 实得 {len(derived)}"
    d = derived[0]
    assert d["type"] == "task"
    assert str(d["level"]) == "L1"
    assert str(d.get("source")) == "triage_derived"
    m = d.get("metadata", {})
    assert str(m.get("source_task")) == "task-x"
    assert m.get("triage_status") == "completed"
    assert m.get("triage_decided_by") == "user"
    # 原 task 行行为不回退: status 已落 completed, 且原行不是派生行:
    by_id = {r["id"]: r for r in _task_rows(store)}
    assert by_id["task-x"].get("status") == "completed"
    assert str(by_id["task-x"].get("source") or "") != "triage_derived"


# ═══════════════════════════════════════════════════════════════════════════
# ② 幂等: 同一 task 改判 stale -> 仍只有 1 条派生行 (UPDATE, 非追加)
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c2_reconsume_updates_not_duplicates(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [_task_row("task-y", "某 task", reference_count=0, status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}

    _triage_consume(p, "task-y", "completed")
    assert len(_derived_rows(store)) == 1
    # 用户改判: 同一 task 再消费为 stale:
    res = _triage_consume(p, "task-y", "stale")
    assert res.get("success") is True
    derived = _derived_rows(store)
    assert len(derived) == 1, \
        f"重复 triage 必须 UPDATE 派生行而非追加, 实得 {len(derived)} 条"
    assert derived[0].get("metadata", {}).get("triage_status") == "stale"
    by_id = {r["id"]: r for r in _task_rows(store)}
    assert by_id["task-y"].get("status") == "stale"


# ═══════════════════════════════════════════════════════════════════════════
# ③ pending 裁决同样派生 (悬而未决本身是可回忆的经验)
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c3_pending_also_derives(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [_task_row("task-z", "悬而未决 task", reference_count=0,
                           status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    res = _triage_consume(p, "task-z", "pending")
    assert res.get("success") is True
    derived = _derived_rows(store)
    assert len(derived) == 1
    assert derived[0].get("metadata", {}).get("triage_status") == "pending"


# ═══════════════════════════════════════════════════════════════════════════
# ④ 两个不同 task 各派生各的 (派生行按 source_task 隔离)
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c4_two_tasks_two_derived_rows(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [_task_row("task-a", "task A", reference_count=0, status="active"),
                 _task_row("task-b", "task B", reference_count=0, status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    _triage_consume(p, "task-a", "completed")
    _triage_consume(p, "task-b", "stale")
    derived = _derived_rows(store)
    assert len(derived) == 2
    srcs = {d.get("metadata", {}).get("source_task") for d in derived}
    assert srcs == {"task-a", "task-b"}, f"派生行必须按 source_task 隔离, 实得 {srcs}"


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ fail-soft: 派生写入失败 -> 消费返回 success=False, 原行 status 不落,
#    面板不消费 (下轮重报可重试) — 不吞脏不静默
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c5_derive_failure_fails_soft(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [_task_row("task-f", "task F", reference_count=0, status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    p._status_triage_consolidate()  # 推面板 (前置)

    # 让派生行写入环节失败: 把 task.json 的父目录写权限拿掉不可行,
    # 改为 monkeypatch _append_triage_derived_row 抛出 IO 异常 (face 内
    # 的派生写入点):
    import __init__ as fm
    real = getattr(fm.FileMemProvider, "_append_triage_derived_row", None)
    def _boom(self, *a, **kw):
        raise OSError("simulated shard write failure")
    fm.FileMemProvider._append_triage_derived_row = _boom
    try:
        res = _triage_consume(p, "task-f", "completed")
    finally:
        if real is not None:
            fm.FileMemProvider._append_triage_derived_row = real
        else:
            del fm.FileMemProvider._append_triage_derived_row

    assert res.get("success") is False, f"派生失败必须 success=False, 实得 {res}"
    # 原行 status 不得已落 (写失败 = 未应用):
    by_id = {r["id"]: r for r in _task_rows(store)}
    assert by_id["task-f"].get("status") in (None, "active"), \
        "fail-soft: 派生失败时原行 status 不得写入"
    # 面板未消费 (可重试):
    assert any(e.get("id") == "task-f" for e in _pending_triage(p)), \
        "fail-soft: 消费失败后面板必须保留 (下轮重报)"


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ P1-29 (reviewer P1-2): 派生行不入 triage 候选池 (自放大环护栏)
#    规格钉死 "该行 status 保持缺省/active (它是活经验, 不是被裁决对象)" —
#    但派生行特征 (task 分片 + ref_count=0 + 无 status 键) 恰好命中 triage
#    face 的候选筛选 (L9667-9669), 下一 tick 会把裁决记录自己当裁决对象问.
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c6_derived_row_not_reasked_as_candidate(tmp_path, monkeypatch):
    """派生行 (source='triage_derived') 不得进入 _status_triage_consolidate
    的候选池 — 否则自放大环: 用户裁决 task-x -> 派生行 D1 -> 下一 tick
    面板问 'D1 已完成/过时/待确认?' -> 再裁决 -> 再派生 D2 -> … 无限循环."""
    store = _make_store(tmp_path, {
        "task": [_task_row("task-x", "task X", reference_count=0, status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    p._status_triage_consolidate()  # 推面板 (前置)
    res = _triage_consume(p, "task-x", "completed")
    assert res.get("success") is True
    derived = _derived_rows(store)
    assert len(derived) == 1  # 派生行已写入
    derived_id = derived[0]["id"]

    # 再跑一次 triage face: 派生行不得成为新候选 (面板里不得出现 derived_id):
    p._pending_confirm = {}  # 清掉 task-x 的面板, 只看新推的
    p._status_triage_consolidate()
    panel_ids = [e.get("id") for e in _pending_triage(p)]
    assert derived_id not in panel_ids, \
        f"派生行 {derived_id} 不得进入 triage 候选池 (P1-2 自放大环)"
    # 原 task-x (status=completed) 也不得重问:
    assert "task-x" not in panel_ids, \
        "已裁决 task (status=completed) 不得重弹面板"
