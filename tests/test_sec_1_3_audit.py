"""SEC-1-3 story: 注入门控审计留痕（injection_gate 事件）— acceptance tests.

spec SEC-1-3（S 级）: prefetch 注入门的非平凡决策（drop / directive-quote）
逐事件写一行 action=injection_gate 到 consolidation_log.jsonl（含 entry id +
决策原因 + origin + session/profile 上下文）。reject-class 静默丢弃 = 违规；
allow = 热路径默认态，不逐行留痕（活数据 0 drop/0 quote -> 0 审计行，天然有界）。

Acceptance coverage (blueprint specs/SEC-1-3-impl-notes.md §2, 8 gates):
1. 1 low-trust 行 + 1 普通行 -> consolidation_log 恰 1 行 injection_gate
   (decision=drop, entry_id=low 行 id)；低信任行不入注入面。
2. 合成 directive-hit 行 -> 恰 1 行 injection_gate (decision=directive-quote,
   entry_id 对)；该行仍被注入但渲染为 quote block。
3. 全 allow（活数据形状 user/medium + user/high，0 drop 0 quote）->
   0 行 injection_gate（恒等零噪声门）。
4. 审计行 shape：必备键 {action, entry_id, decision, reason, ts} + JSON 可解析。
5. 审计写 fail-soft：log 路径不可写（模拟 hiccup）-> prefetch 不炸、gate 语义
   不变（drop 的仍 drop，kept 的仍 kept）。
6. 同 tick 多 drop（2 low 行）-> 逐事件多行（2 行审计，非汇总）。
7. 审计与 EP-2 supersede_confirm 行同文件不交叠（action 判别可各自读回）。
8. 锁：审计写走 _shard_lock（真实 helper 落盘 + .lock sidecar 证明；
   2 进程并发 prefetch 不撕裂 log 行）。

Portability: 合成 fixture（memories/ 行 + 最小 provider shell + stub search），
不依赖活数据 / provider 实例 / LLM；模块从 parents[1] 相对路径 import。
"""
import json
import pathlib
import subprocess
import sys
import threading

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
QUERY = "zephyr"
QUERY_TOK = "zephyr"

FACTORIES = {
    # gate rule 2: explicit low stamp -> drop by default
    "low": lambda rid: {
        "id": rid, "type": "fact", "content": f"{QUERY_TOK} note low {rid}",
        "level": "L0",
        "metadata": {"origin": {"kind": "tool", "trust": "low"}, "confirmed": True},
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.5, "last_referenced": "", "ttl_days": None,
    },
    # gate rule 5: trusted stamps -> allow
    "medium": lambda rid: {
        "id": rid, "type": "fact", "content": f"{QUERY_TOK} note medium {rid}",
        "level": "L0",
        "metadata": {"origin": {"kind": "user", "trust": "medium"}, "confirmed": True},
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.5, "last_referenced": "", "ttl_days": None,
    },
    "high": lambda rid: {
        "id": rid, "type": "fact", "content": f"{QUERY_TOK} note high {rid}",
        "level": "L0",
        "metadata": {"origin": {"kind": "user", "trust": "high"}, "confirmed": True},
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.5, "last_referenced": "", "ttl_days": None,
    },
    # gate rule 4: directive-grammar hit -> allow + directive-quote mark
    "directive": lambda rid: {
        "id": rid, "type": "fact",
        "content": f"{QUERY_TOK} note: YOU MUST do exactly this {rid}",
        "level": "L0",
        "metadata": {"origin": {"kind": "user", "trust": "medium"}, "confirmed": True},
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.5, "last_referenced": "", "ttl_days": None,
    },
}


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Minimal provider shell: the REAL prefetch gate loop + the REAL audit
    helper (_append_consolidation_event -> _shard_lock + jsonl append).
    Only the read-side surface (search_memories) is stubbed, so synthetic
    pool rows flow through the actual decision point without live data."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.episodic_dir = tmpdir / "episodic"
    p.knowledge_dir = tmpdir / "knowledge"
    p.source_profile = "test"
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p._decay = dict(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._last_recall = None
    p._pending_confirm = {}
    p._pending_drafts_notified = {}
    p.min_search_score = 0.0
    return p


def _seed_facts(p, rows: list) -> None:
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


def _read_facts(p) -> list:
    with open(p.memories_dir / "fact.json", encoding="utf-8") as fh:
        return json.load(fh)


def _rows_by_kind(*specs: str) -> list:
    """specs like ('low', 'a') -> factory('a'); bare strings start at fact_0."""
    counter = 0
    out = []
    for spec in specs:
        rid = spec[1] if len(spec) == 2 else f"fact_{counter}"
        counter += 1
        out.append(FACTORIES[spec[0]](rid))
    return out


def _prefetch(p, rows: list, *, session_id: str = "s1") -> str:
    """Stub the read surface so synthetic rows hit the REAL gate loop."""
    p.search_memories = lambda *a, **k: rows  # type: ignore[method-override]
    return p.prefetch(QUERY, session_id=session_id, confidence=0.8)


def _audit_rows(p) -> list:
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]


def _inj_rows(p) -> list:
    return [r for r in _audit_rows(p) if r.get("action") == "injection_gate"]


# ── 1. low-trust 行 -> 恰 1 行 injection_gate (decision=drop) ───────────────

def test_low_trust_row_writes_one_drop_audit(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("medium", "m1"), ("low", "low_a")))
    ctx = _prefetch(p, _rows_by_kind(("medium", "m1"), ("low", "low_a")))
    # gate semantics: the low row must NOT reach the inject face
    assert "note medium" in ctx
    assert "note low" not in ctx
    rows = _inj_rows(p)
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "drop"
    assert row["entry_id"] == "low_a"
    assert row["reason"] == "low-trust origin auto-inject disabled"
    # origin context rides the row (data-engineer: stamp at write, propagate at audit)
    assert row["origin"]["trust"] == "low"


# ── 2. directive-hit 行 -> 恰 1 行 injection_gate (decision=directive-quote) ─

def test_directive_row_writes_one_quote_audit(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("medium", "m1"), ("directive", "dir_x")))
    ctx = _prefetch(p, _rows_by_kind(("medium", "m1"), ("directive", "dir_x")))
    # gate semantics: the row IS kept, but rendered as a quote block
    q0, q1 = DECAY_DEFAULTS["injection"]["quote_delims"]
    assert f"{q0} " in ctx and "YOU MUST do exactly this" in ctx
    assert f" {q1}" in ctx
    rows = _inj_rows(p)
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "directive-quote"
    assert row["entry_id"] == "dir_x"
    assert row["reason"] == "instruction-grammar hit; rendered as quote block"
    assert row["origin"]["trust"] == "medium"


# ── 3. 全 allow（活数据形状）-> 0 行（恒等零噪声门）────────────────────────────

def test_all_allow_rows_write_zero_audit(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("medium", "m1"), ("high", "h1")))
    ctx = _prefetch(p, _rows_by_kind(("medium", "m1"), ("high", "h1")))
    assert "note medium" in ctx and "note high" in ctx
    assert _inj_rows(p) == []
    # allow = 热路径默认态: 注入门流恒 0 行。S1b F5 后 log 文件可能因
    # recall_audit 多流而存在（action 判别、同 _shard_lock 协议）——契约
    # 按 action-scoped 判定 gate 行 0，不再要求文件不存在。


# ── 4. 审计行 shape：必备键 + JSON 可解析 ─────────────────────────────────────

def test_audit_row_shape_has_required_keys(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("medium", "m1"), ("low", "low_a")))
    _prefetch(p, _rows_by_kind(("medium", "m1"), ("low", "low_a")), session_id="s42")
    raw_lines = [l for l in (p.storage_dir / "consolidation_log.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    all_rows = [json.loads(l) for l in raw_lines]  # 每行 JSON 可解析（no-tear）
    # S1b F5 多流: recall_audit 行与 injection_gate 行同文件共存（action
    # 判别、走同一 _shard_lock），shape 断言只统计 injection_gate 行
    inj = [r for r in all_rows if r.get("action") == "injection_gate"]
    assert len(inj) == 1
    row = inj[0]
    for key in ("action", "entry_id", "decision", "reason", "ts"):
        assert key in row, f"missing required key {key!r}"
    assert row["action"] == "injection_gate"
    # 上下文键（蓝图 shape: session_id + source_profile）
    assert row["session_id"] == "s42"
    assert row["source_profile"] == "test"
    assert row["entry_id"] == "low_a" and row["decision"] == "drop"
    # ts 是 ISO 可解析
    from datetime import datetime
    datetime.fromisoformat(row["ts"])


# ── 5. fail-soft：log 路径不可写 -> prefetch 不炸、gate 语义不变 ─────────────

def test_unwritable_audit_path_keeps_gate_semantics(tmp_path):
    """蓝图 5: log 路径不可写（审计面 hiccup）-> prefetch 宿主路径不炸，
    且 gate 语义不变（drop 的仍不 inject、kept 的仍 kept）。

    实现：把 consolidation_log.jsonl 本身做成一个目录 —— 真实 helper +
    真实 _shard_lock 走完流程后，open(log, "a") 抛 IsADirectoryError，
    被 helper 的 fail-soft 边界（try/except -> logger.warning 不 raise）
    吞掉——这正是 SEC-1-3 裁决 4 的契约：审计 hiccup 绝不炸宿主。"""
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("medium", "m1"), ("low", "low_a")))
    rows = _rows_by_kind(("medium", "m1"), ("low", "low_a"))

    log = p.storage_dir / "consolidation_log.jsonl"
    log.mkdir(parents=True, exist_ok=True)  # 审计 log 路径不可写（是个目录）
    ctx = _prefetch(p, rows)  # 不炸（helper 内部 fail-soft）
    # gate 语义不变：drop 的仍 drop（不 inject），kept 的仍 kept
    assert "note medium" in ctx
    assert "note low" not in ctx
    # 审计行没落地（hiccup 不伪造），log 目录仍为空
    assert list(log.iterdir()) == []


# ── 6. 同 tick 多 drop -> 逐事件多行 ─────────────────────────────────────────

def test_multi_drop_writes_one_audit_per_event(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("low", "low_1"), ("low", "low_2"), ("medium", "m1")))
    ctx = _prefetch(p, _rows_by_kind(("low", "low_1"), ("low", "low_2"), ("medium", "m1")))
    assert "note medium" in ctx
    assert "note low" not in ctx  # 两条 low 都没入面
    rows = _inj_rows(p)
    assert len(rows) == 2  # 逐事件，非汇总
    assert {r["entry_id"] for r in rows} == {"low_1", "low_2"}
    assert all(r["decision"] == "drop" for r in rows)


# ── 7. 与 EP-2 supersede_confirm 行同文件不交叠 ─────────────────────────────

def test_coexists_with_ep2_supersede_confirm_rows(tmp_path):
    p = _make_provider(tmp_path)
    _seed_facts(p, _rows_by_kind(("low", "low_a")))
    # EP-2 人工面 audit row（同 log 文件，action 判别）
    log = p.storage_dir / "consolidation_log.jsonl"
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": "2026-09-01T00:00:00", "action": "supersede_confirm_human",
            "new_id": "fact_1", "target_ids": ["fact_0"], "verdict": "confirmed",
        }) + "\n")
    ctx = _prefetch(p, _rows_by_kind(("low", "low_a")))
    assert "note low" not in ctx
    rows = _audit_rows(p)
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(r)
    # S1b F5 多流: supersede + injection_gate + recall_audit 同文件共存
    assert len(rows) == 3
    # action 判别：三类事件各自读回，零交叠
    assert len(by_action["supersede_confirm_human"]) == 1
    assert len(by_action["injection_gate"]) == 1
    assert len(by_action.get("recall_audit", [])) == 1  # F5 多流（判别干净）
    sup = by_action["supersede_confirm_human"][0]
    inj = by_action["injection_gate"][0]
    assert sup["new_id"] == "fact_1" and sup["verdict"] == "confirmed"
    assert inj["entry_id"] == "low_a" and inj["decision"] == "drop"
    # injection_gate 行不携带 supersede 面字段（判别干净）
    assert "verdict" not in inj and "new_id" not in inj


# ── 8. 锁：审计写走 _shard_lock；2 进程并发 prefetch 不撕裂 ─────────────────

def test_audit_write_goes_through_shard_lock(tmp_path):
    """真实 helper（不 patch）：落盘 + .lock sidecar 文件证明 _shard_lock
    实际被走（T5b 跨进程锁；同 EP-2 审计面共用一把锁）。"""
    p = _make_provider(tmp_path)
    p._append_consolidation_event({"ts": "x", "action": "injection_gate",
                                   "entry_id": "a", "decision": "drop"})
    assert (p.storage_dir / "consolidation_log.jsonl.lock").exists()
    rows = _audit_rows(p)
    assert len(rows) == 1 and rows[0]["entry_id"] == "a"


def test_two_processes_concurrent_prefetch_no_tear(tmp_path):
    """2 个独立进程并发跑真实 prefetch（各 1 条 low 行 -> 各 1 行审计）：
    consolidation_log.jsonl 不撕裂（2 行，每行 JSON 可解析，无半行/交织）；
    两进程经 barrier 文件锁同步后同时进 prefetch，放大锁竞争窗口。"""
    store = tmp_path / "store"
    store.mkdir()
    _seed_facts_provider_rows(store)
    procs = [
        subprocess.Popen([sys.executable, "-c", _CONCURRENT_DRIVER, str(store)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=str(PLUGIN_DIR))
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(o[0].strip() == "OK" for o in outs), outs
    # 撕裂检查：每行 JSON 可解析、无半行/交织。S1b F5 多流后：2 进程 ×
    # (1 injection_gate + 1 recall_audit，新进程节流窗 0.0 各落 1 行) = 恰 4 行
    log = store / "consolidation_log.jsonl"
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 4
    rows = [json.loads(l) for l in lines]  # 每行 JSON 可解析（no-tear，全流覆盖）
    gate_rows = [r for r in rows if r["action"] == "injection_gate"]
    assert len(gate_rows) == 2
    assert all(r["decision"] == "drop" for r in gate_rows)
    assert {r["entry_id"] for r in gate_rows} == {"low_c"}
    audit_rows = [r for r in rows if r["action"] == "recall_audit"]
    assert len(audit_rows) == 2  # F5 多流：no-tear 覆盖全部流
    # 宿主面没被锁撕裂：fact.json 可解析，kept 行引用计数正常落盘
    facts = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in facts}
    assert "note medium" in by_id["m1"]["content"]
    assert by_id["m1"]["reference_count"] == 2  # 两个进程各 reinforce 一次


def _seed_facts_provider_rows(store: pathlib.Path) -> None:
    """Write memories/ rows for the concurrent-process fixture."""
    (store / "memories").mkdir(parents=True, exist_ok=True)
    p = FileMemProvider.__new__(FileMemProvider)
    p.memories_dir = store / "memories"
    _seed_facts(p, _rows_by_kind(("low", "low_c"), ("medium", "m1")))


_CONCURRENT_DRIVER = r'''
import fcntl, json, os, sys, threading, time
from pathlib import Path
sys.path.insert(0, os.getcwd())  # PLUGIN_DIR (cwd), mirrors test import convention
import __init__ as fm
from consolidation import DECAY_DEFAULTS

store = Path(sys.argv[1])
barrier = store / "barrier"

# 2-process counting barrier: both processes must arrive before either
# proceeds, so their prefetches (audit append + fact.json RMW) overlap
# maximally and the _shard_lock contention is exercised for real.
for _ in range(20000):
    fh = open(barrier, "a+")
    fh.seek(0)
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    val = int(fh.read() or "0")
    if val >= 2:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close()
        break  # everyone arrived
    fh.seek(0); fh.truncate(); fh.write(str(val + 1)); fh.flush()
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN); fh.close()
    time.sleep(0.02)

p = fm.FileMemProvider.__new__(fm.FileMemProvider)
p._lock = threading.Lock()
p.storage_dir = store
p.memories_dir = store / "memories"
p.sessions_dir = store / "sessions"
p.episodic_dir = store / "episodic"
p.knowledge_dir = store / "knowledge"
p.knowledge_entries_path = p.knowledge_dir / "entries.json"
p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
p.source_profile = "conc"
p._decay = dict(DECAY_DEFAULTS)
p._llm_ctx = None
p._last_recall = None
p._pending_confirm = {}
p._pending_drafts_notified = {}
p.min_search_score = 0.0
rows = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
p.search_memories = lambda *a, **k: rows
# P3 cold-start replay arm: __new__ skips __init__, so the flag the real
# prefetch checks (L3055) is unset -> arm it now: this driver's FIRST
# prefetch runs the FULL gate+audit pipeline. Without this, a co-writer
# that finishes first leaves sessions/conc/last_injected.txt + .tokens
# in the shared tmp store; a later reader with the armed flag hits the P3
# replay early-return at L3061 and never emits its injection_gate /
# recall_audit rows -> the 4-line assertion flakes. The replay face is
# intentionally exercised by its OWN tests, not by the no-tear 2-proc
# fixture.
p._p3_replay_armed = True
ctx = p.prefetch("zephyr", session_id="conc", confidence=0.8)
assert "note medium" in ctx and "note low" not in ctx
print("OK")
'''

