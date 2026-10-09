"""S3⑤ _status_triage_consolidate — task 记忆状态三选 clarify 面板.

背景 (用户主诉 10-09): 每次新 session 都要重说红线; 对存活的 [task] 记忆,
不该由 agent 静默改状态 (上一轮我"替你选了已完成"就是跑偏), 而是:

  * dream tick 扫到 存活 + 多回合未被引用 (reference_count==0) 的 task 行时,
    生成 恰好三选项 clarify 面板 (已完成 / 已过时 / 待确认), 走现成
    _push_pending_confirm(kind="triage") 通道, 下一 turn 的 [记忆报告] 弹给你裁决;
  * 面板 不预填推荐项 (no default pick) — 裁决权 100% 在用户;
  * 消费: filemem_review_drafts(action="triage", status="completed|stale|pending")
    落对应 status, 且消费后不再重报 (T4① 同 _consume_pending_confirm).

规格 (已落地): 本文件是 S3⑤ status-triage 面的回归门控。6 条规格用例 +
  1 条旋钮 parity。面实现后 xfail 标记已摘除（用例全 PASS），若某实现偏离
  规格（如预填了推荐项 / 选项数 != 3 / 非 task 行被误纳入）则用例 FAIL 暴露。

宿主零改动: 本卡只动 plugins/filemem/{__init__.py, consolidation/__init__.py,
decay.json, tests/}; hermes-agent 安装根 grep _status_triage_consolidate = 0.

复用: test_s3_dream_faces.py 的 _make_store/_make_provider/_decay_copy/_row 骨架.
"""
import copy
import json
import pathlib
import sys
import threading

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))


# 复用 test_s3_dream_faces 的 fixture 骨架 (同源, 不复制实现).
from test_s3_dream_faces import (  # noqa: E402
    _row, _make_store, _make_provider, _decay_copy, _cons_log_rows,
)


def _task_row(rid, content, **extra):
    return _row(rid, content, mem_type="task", **extra)


def _decay_with_triage() -> dict:
    d = _decay_copy()
    d.setdefault("consolidation", {})
    return d


def _pending_triage(p, sid="s-s3"):
    pc = getattr(p, "_pending_confirm", None) or {}
    q = pc.get(sid, [])
    return [e for e in q if e.get("kind") == "triage"]


# ═══════════════════════════════════════════════════════════════════════════
# ① 存活 + reference_count==0 的 task 行 -> 每条恰好三选项, 不预填
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_a_live_unref_task_pushes_three_option_panel(tmp_path, monkeypatch):
    """存活 + 0 引用 的 task 行必须推入一条 kind='triage' 面板, options 恰好
    ['completed','stale','pending'] 三选, 且 entry 无 default/recommended 字段
    (不预填推荐项 — 守用户第 1 点纠正)."""
    store = _make_store(tmp_path, {
        "task": [_task_row("task-live-1", "修 filemem 近义去重", reference_count=0,
                            status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}

    p._status_triage_consolidate()

    pending = _pending_triage(p)
    assert len(pending) == 1, f"应有 1 条 triage 面板, 实得 {len(pending)}"
    entry = pending[0]
    assert entry["id"] == "task-live-1"
    assert entry["kind"] == "triage"
    # 恰好三选项, 顺序无关, 集合严格相等:
    opts = set(entry.get("options", []))
    assert opts == {"completed", "stale", "pending"}, \
        f"三选项必须是 已完成/已过时/待确认, 实得 {opts}"
    # 不预填推荐项: 无 default/recommended/suggested 任一字段:
    for pref_key in ("default", "recommended", "suggested", "prefilled", "hint"):
        assert pref_key not in entry, f"面板预填了 {pref_key!r} — 违反 不预填 规格"


# ═══════════════════════════════════════════════════════════════════════════
# ② 已被引用 (reference_count>0) 或 非 task 行 -> 不推入
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_b_referenced_and_non_task_not_pushed(tmp_path, monkeypatch):
    """reference_count>0 的 task 行 与 非 task 行 都不进入三选面板 (scope 钉死:
    只 triage task shard, 且只 0 引用 的存活行 — 避免打扰正在用的活任务)."""
    store = _make_store(tmp_path, {
        "task": [
            _task_row("task-ref-1", "被引用的活任务", reference_count=3,
                      status="active"),
            _task_row("task-sup-1", "已 supersede", reference_count=0,
                      status="superseded"),
            _task_row("task-dec-1", "已 decay", reference_count=0,
                      status="decayed"),
            _task_row("task-live-2", "存活 0 引用", reference_count=0,
                      status="active"),
        ],
        "fact": [_row("fact-1", "一条 fact 记忆", reference_count=0)],
        "preference": [_row("pref-1", "一条偏好", reference_count=0)],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}

    p._status_triage_consolidate()

    ids = {e["id"] for e in _pending_triage(p)}
    assert ids == {"task-live-2"}, \
        f"只有 存活+0引用 的 task-live-2 应入面板, 实得 {ids}"


# ═══════════════════════════════════════════════════════════════════════════
# ③ 非 task 记忆 (fact/preference/decision) 绝不误纳入 triage 面板
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_c_non_task_shards_never_triaged(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "fact": [_row("f-1", "f", reference_count=0)],
        "preference": [_row("pf-1", "p", reference_count=0)],
        "decision": [_row("d-1", "dc", reference_count=0)],
        "task": [_task_row("t-live", "task 存活 0 引用", reference_count=0,
                           status="active")],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    p._status_triage_consolidate()
    ids = {e["id"] for e in _pending_triage(p)}
    assert ids == {"t-live"}, f"只有 task 行入面板, 实得 {ids}"


# ═══════════════════════════════════════════════════════════════════════════
# ④ N-cap: triage_max_per_tick 超额滚动 (一次只推 cap 条, 其余留下次 tick)
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_d_n_cap_rolls_overflow(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {
        "task": [
            _task_row(f"t{i}", f"task {i}", reference_count=0, status="active")
            for i in range(5)
        ],
    })
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._decay["consolidation"]["triage_max_per_tick"] = 3
    p._pending_confirm = {}

    p._status_triage_consolidate()

    assert len(_pending_triage(p)) == 3, \
        f"N-cap=3 应只推 3 条, 实得 {len(_pending_triage(p))}"


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ 消费闭环: review_drafts(action=triage, status=...) 落 status + 不再重报
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_e_consume_sets_status_and_stops_rereporting(tmp_path, monkeypatch):
    """triage 面板消费: action='triage' + status in {completed,stale,pending}
    -> 对应 task 行 status 落为该值; 消费后 _consume_pending_confirm(kind='triage')
    把面板移除, 下一 tick 该 id 不再重报 (已裁决 = 已离开 0 引用活池 或
    status 已非 active)."""
    store = _make_store(tmp_path, {"task": [_task_row("t-x", "待裁决 task",
                                                       reference_count=0,
                                                       status="active")]})
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    p._status_triage_consolidate()
    assert len(_pending_triage(p)) == 1

    res = json.loads(p._tool_review_drafts({
        "action": "triage", "memory_id": "t-x", "status": "stale"}))
    assert res.get("success") is True, f"triage 消费应成功, 实得 {res}"

    # 面板已被消费: 该 id 不再在 pending:
    assert all(e["id"] != "t-x" for e in _pending_triage(p)), \
        "triage 消费后该条目不应再重报"
    # task 行 status 已落 stale:
    rows = json.loads((store / "memories" / "task.json")
                      .read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in rows}
    assert by_id["t-x"].get("status") == "stale", \
        f"triage status=stale 应落 task 行 status, 实得 {by_id['t-x'].get('status')}"


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ fail-soft: main_runtime 缺失 / 0 候选 -> tick 干净跳过, 不写脏
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_f_zero_task_clean_skip_no_side_effect(tmp_path, monkeypatch):
    store = _make_store(tmp_path, {"task": []})
    p = _make_provider(tmp_path, store, _decay_with_triage())
    p._pending_confirm = {}
    p._main_runtime = None  # LLM 面 clean-skip 位
    p._status_triage_consolidate()  # 必须不 raise
    assert _pending_triage(p) == [], "0 候选时不得推入任何 triage 面板"


# ═══════════════════════════════════════════════════════════════════════════
# decay 旋钮 parity (triage_max_per_tick 必须 DECAY_DEFAULTS + decay.json 两侧)
# ═══════════════════════════════════════════════════════════════════════════

def test_triage_knob_parity():
    """triage_max_per_tick 一旦落地必须进两侧 (S2 invariant: 唯一允许 drift 是
    search). 两侧都无 -> 校验 '若一侧有则另一侧必须同值'; 都有且相等 -> PASS."""
    dj = DECAY_JSON.get("consolidation", {})
    dd = DECAY_DEFAULTS.get("consolidation", {})
    knob = "triage_max_per_tick"
    in_dj, in_dd = knob in dj, knob in dd
    if in_dj or in_dd:
        assert in_dj and in_dd, f"{knob} 只在 {'decay.json' if in_dj else 'DECAY_DEFAULTS'} 有 (parity 破)"
        assert dj.get(knob) == dd.get(knob), f"{knob} 两侧值不等"
