"""EP-2 story: supersedes 候选确认（宿主主模型 LLM 面 + 确定性人工 CLI 面）.

Acceptance coverage (spec EP-2 blueprint §5, 11 gates):
1. confirmed=True -> 新行 state=confirmed + 旧行 superseded_confirmed=True
2. confirmed=False -> 新行保持 candidate（不吞脏）
3. mock 抛异常（模型不可用）-> confirmed=False 且 model_used=False，候选不动
4. 坏引用（supersedes[0] 指向不存在的旧 id）-> 跳过留痕不炸，批次继续
5. 预算 cap：supersede_max_confirms=2 + 3 候选 -> 只确认 2，剩 1 滚下一 tick
6. 幂等：已 confirmed 的行不进候选集（LLM 计数 = 1，只判新候选）
7. CLI --list 只读：前后 store 文件 hash 一致
8. CLI --confirm/--reject 确定性翻态 + 审计行追加
9. 锁：_shard_lock 并发（4 进程同 shard）不撕裂
10. 零硬编码模型审计：llm_caller.py confirm 段 grep 模型名/provider/SDK = 0
11. 三态 schema round-trip：candidate/confirmed/rejected to_dict/from_dict 无损

Portability: 合成 fixture（tempfile trees），不依赖活数据 / provider 实例；
provider 是 partial (__new__)，同 test_ep_1_lineage。CLI 走 subprocess +
--store tmp（零 HERMES_HOME 依赖）。零硬编码路径。
"""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import types

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MemoryItem  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
CLI_SCRIPT = PLUGIN_DIR / "tools" / "confirm_supersede.py"


def _make_provider(tmpdir: pathlib.Path, main_runtime=None) -> FileMemProvider:
    """Partial provider (test_ep_1 shape) + EP-2 attrs: a fake host main_runtime
    snapshot so the REAL consolidation/llm_caller.confirm_supersede host path
    runs — zero provider/model config, zero hardcoded model names."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p._decay = dict(DECAY_DEFAULTS)
    p._main_runtime = main_runtime if main_runtime is not None else _MAIN_RUNTIME
    p._supersede_confirming = False
    p.knowledge_entries_path = None
    return p


def _old_row(rid: str, content: str, new_id: str) -> dict:
    """EP-1 stamped old row: capped, out of the RRF pool, back-linked."""
    return {
        "id": rid,
        "type": "fact",
        "content": content,
        "level": "L0",
        "metadata": {
            "origin": {"kind": "user", "trust": "medium"},
            "superseded": True,
            "superseded_by": new_id,
        },
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None,
        "source": "auto",
        "reference_count": 0,
        "salience": 0.5,
        "last_referenced": "",
        "ttl_days": None,
    }


def _new_row(rid: str, content: str, old_ids, state: str = "candidate") -> dict:
    """EP-1 stamped new row: candidate supersedes link + state."""
    return {
        "id": rid,
        "type": "fact",
        "content": content,
        "level": "L0",
        "metadata": {
            "origin": {"kind": "user", "trust": "medium"},
            "supersedes": list(old_ids),
            "supersede_state": state,
            "superseded": False,
        },
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
        "occurred_at": None,
        "source": "auto",
        "reference_count": 0,
        "salience": 0.6,
        "last_referenced": "",
        "ttl_days": None,
    }


def _write_shard(p, rows: list) -> pathlib.Path:
    f = p.memories_dir / "fact.json"
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    return f


def _read_shard(p) -> list:
    with open(p.memories_dir / "fact.json", encoding="utf-8") as fh:
        return json.load(fh)


class FakeLLM:
    """Scripted ctx.llm: complete_structured returns canned parsed payloads."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        r = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return types.SimpleNamespace(parsed=r)


def _audit_rows(p) -> list:
    """The append-only AUDIT-CHAIN rows (supersede_confirm / *_human verdicts)
    — epic card 3 adds a SEPARATE `llm_call` telemetry row type to the SAME
    consolidation_log.jsonl; the audit-chain assertions filter it out so the
    existing verdict-row schema / counts stay the pre-card-3 surface."""
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("action") != "llm_call"]


# ── 1. confirmed=True -> 翻态 + 旧行标记 ─────────────────────────────────────

def test_confirm_true_flips_state_and_marks_targets(tmp_path, monkeypatch):
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": True, "reason": "same fact updated"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
                     _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"])])
    p._supersede_confirm_candidates()
    assert state["calls"] == 1
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_0"]["metadata"]["superseded_confirmed"] is True
    # append-only audit: exactly one supersede_confirm row, model used
    aud = _audit_rows(p)
    assert len(aud) == 1 and aud[0]["action"] == "supersede_confirm"
    assert aud[0]["new_id"] == "fact_1" and aud[0]["target_id"] == "fact_0"
    assert aud[0]["confirmed"] is True and aud[0]["model_used"] is True


# ── 2. confirmed=False -> 保持 candidate（不吞脏）────────────────────────────

def test_confirm_false_keeps_candidate(tmp_path, monkeypatch):
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": False, "reason": "merely overlap"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
                     _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"])])
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "candidate"  # 不动
    assert "superseded_confirmed" not in rows["fact_0"]["metadata"]
    assert state["calls"] == 1
    aud = _audit_rows(p)
    assert aud[0]["confirmed"] is False and aud[0]["model_used"] is True


# ── 3. 模型不可用（complete_structured 抛异常）-> fail-soft 不自动信任 ───────

def test_model_failure_keeps_candidate_and_flags_not_used(tmp_path, monkeypatch):
    fake, state = install(monkeypatch,
                          replies=[RuntimeError("host model unavailable")])
    p = _make_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
                     _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"])])
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "candidate"  # 候选不动
    aud = _audit_rows(p)
    assert aud[0]["confirmed"] is False and aud[0]["model_used"] is False
    assert "model_unavailable_fallback" in aud[0]["reason"]


def test_real_confirm_supersede_failsoft_unit(monkeypatch):
    """The real host-path function: ANY exception from the host call
    surface -> confirmed=False, model_used=False (不吞脏); a healthy
    reply parses to confirmed=True with model_used=True."""
    from consolidation import llm_caller as real_caller  # noqa: E402

    install(monkeypatch, raise_exc=TimeoutError("host llm down"))
    res = real_caller.confirm_supersede(_MAIN_RUNTIME, "NEWER", "OLDER")
    assert res == {"confirmed": False, "reason": "model_unavailable_fallback=host llm down",
                   "model_used": False}
    # and a healthy reply parses to confirmed=True with model_used=True
    install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    res2 = real_caller.confirm_supersede(_MAIN_RUNTIME, "N", "O")
    assert res2["confirmed"] is True and res2["model_used"] is True


# ── 4. 坏引用 -> 跳过留痕不炸，批次继续 ──────────────────────────────────────

def test_bad_reference_skipped_traced_batch_continues(tmp_path, monkeypatch):
    fake, state = install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    # fact_2's target fact_99 does not exist in the shard (bad reference);
    # fact_1's target fact_0 does. Both in one tick (cap default 5 >= 2).
    _write_shard(p, [
        _old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
        _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"]),
        _new_row("fact_2", "deploy on host 117 uses k8s", ["fact_99"]),
    ])
    p._supersede_confirm_candidates()  # must not raise
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"   # 批次继续
    assert rows["fact_2"]["metadata"]["supersede_state"] == "candidate"   # 坏引用保持
    aud = _audit_rows(p)
    assert len(aud) == 2
    assert any("bad_reference" in a["reason"] for a in aud)               # 留痕
    assert all(a["action"] == "supersede_confirm" for a in aud)


# ── 5. 预算 cap：N=2 + 3 候选 -> 只确认 2，剩 1 滚下一 tick ─────────────────

def test_budget_cap_overflow_rolls_to_next_tick(tmp_path, monkeypatch):
    fake, state = install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    p._decay["consolidation"]["supersede_max_confirms"] = 2
    _write_shard(p, [
        _old_row("fact_0", "a", "fact_1"),
        _old_row("fact_3", "b", "fact_4"),
        _old_row("fact_5", "c", "fact_6"),
        _new_row("fact_1", "a", ["fact_0"]),
        _new_row("fact_4", "b", ["fact_3"]),
        _new_row("fact_6", "c", ["fact_5"]),
    ])
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_4"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_6"]["metadata"]["supersede_state"] == "candidate"  # overflow 滚下一 tick
    assert state["calls"] == 2
    # next tick: only the overflow candidate remains
    p._supersede_confirming = False
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_6"]["metadata"]["supersede_state"] == "confirmed"
    assert state["calls"] == 3


# ── 6. 幂等：已 confirmed 不进候选集 ────────────────────────────────────────

def test_already_confirmed_rows_not_rescanned(tmp_path, monkeypatch):
    fake, state = install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [
        _old_row("fact_0", "a", "fact_1"),
        _new_row("fact_1", "a", ["fact_0"], state="confirmed"),  # 已决
        _old_row("fact_3", "b", "fact_4"),
        _new_row("fact_4", "b", ["fact_3"], state="candidate"),
    ])
    p._supersede_confirm_candidates()
    assert state["calls"] == 1  # 只判 fact_4
    aud = [a for a in _audit_rows(p)]
    assert [a["new_id"] for a in aud] == ["fact_4"]
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_4"]["metadata"]["supersede_state"] == "confirmed"


def test_main_runtime_none_skips_silently(tmp_path):
    p = _make_provider(tmp_path, None)
    p._main_runtime = None  # standalone（无宿主 main_runtime）-> skip 非 error
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "a", ["fact_0"])])
    p._supersede_confirm_candidates()  # must not raise
    assert {r["id"]: r for r in _read_shard(p)}["fact_1"]["metadata"]["supersede_state"] == "candidate"
    assert _audit_rows(p) == []  # 零写入（call-level gate 在扫描前）


# ── 附加 fail-soft：单条异常不炸批次 / 整 tick 异常候选保持 ────────────────

def test_single_item_exception_does_not_break_batch(tmp_path, monkeypatch):
    fake, state = install(monkeypatch,
                          replies=[RuntimeError("one bad pair"),
                                   {"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [
        _old_row("fact_0", "a", "fact_1"),
        _old_row("fact_3", "b", "fact_4"),
        _new_row("fact_1", "a", ["fact_0"]),
        _new_row("fact_4", "b", ["fact_3"]),
    ])
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in _read_shard(p)}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "candidate"  # 异常条保持
    assert rows["fact_4"]["metadata"]["supersede_state"] == "confirmed"  # 批次继续


def test_tick_level_failure_keeps_candidates(tmp_path, monkeypatch):
    fake, state = install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "a", ["fact_0"])])
    monkeypatch.setattr(fm, "_load_llm_caller",
                       lambda: (_ for _ in ()).throw(RuntimeError("consolidation pkg missing")))
    p._supersede_confirm_candidates()  # 整 tick fail-soft：不炸
    assert {r["id"]: r for r in _read_shard(p)}["fact_1"]["metadata"]["supersede_state"] == "candidate"
    assert state["calls"] == 0


def test_concurrency_guard_skips_second_call(tmp_path, monkeypatch):
    fake, state = install(monkeypatch, replies=[{"confirmed": True, "reason": "ok"}])
    p = _make_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "a", ["fact_0"])])
    p._supersede_confirming = True  # 另一 tick 在飞 -> 幂等 guard
    p._supersede_confirm_candidates()
    assert {r["id"]: r for r in _read_shard(p)}["fact_1"]["metadata"]["supersede_state"] == "candidate"


# ── 7/8/9. CLI：--list 只读 / --confirm / --reject 确定性翻态 + 审计 / 并发锁 ─

def _run_cli(store, *args):
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--store", str(store), *args],
        capture_output=True, text=True, cwd=str(PLUGIN_DIR), timeout=60)


def _store_hashes(store: pathlib.Path) -> dict:
    out = {}
    for f in sorted(store.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(store))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _cli_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    store = tmp_path / "filemem"
    (store / "memories").mkdir(parents=True)
    rows = [
        _old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
        _old_row("fact_3", "bookkeeping cron is 03:00", "fact_4"),
        _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"]),
        _new_row("fact_4", "bookkeeping cron is 04:00", ["fact_3"]),
    ]
    with open(store / "memories" / "fact.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    return store


def test_cli_list_is_readonly(tmp_path):
    store = _cli_fixture(tmp_path)
    before = _store_hashes(store)
    r = _run_cli(store, "--list")
    assert r.returncode == 0, r.stderr
    assert "fact_1" in r.stdout and "fact_4" in r.stdout
    assert "2 candidate" in r.stdout
    assert _store_hashes(store) == before  # 前后字节一致，零写入


def test_cli_confirm_flips_state_and_appends_audit(tmp_path):
    store = _cli_fixture(tmp_path)
    r = _run_cli(store, "--new", "fact_1", "--confirm")
    assert r.returncode == 0, r.stderr
    rows = {x["id"]: x for x in json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_0"]["metadata"]["superseded_confirmed"] is True
    aud = [json.loads(l) for l in (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(aud) == 1
    assert aud[0]["action"] == "supersede_confirm_human"
    assert aud[0]["new_id"] == "fact_1" and aud[0]["verdict"] == "confirmed"
    assert aud[0]["target_ids"] == ["fact_0"] and "ts" in aud[0]


def test_cli_reject_flips_state_and_appends_audit(tmp_path):
    store = _cli_fixture(tmp_path)
    r = _run_cli(store, "--new", "fact_4", "--reject")
    assert r.returncode == 0, r.stderr
    rows = {x["id"]: x for x in json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert rows["fact_4"]["metadata"]["supersede_state"] == "rejected"
    assert "superseded_confirmed" not in rows["fact_3"]["metadata"]  # reject 不标记
    aud = [json.loads(l) for l in (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert aud[0]["verdict"] == "rejected" and aud[0]["new_id"] == "fact_4"


def test_cli_refuses_non_candidate(tmp_path):
    store = _cli_fixture(tmp_path)
    assert _run_cli(store, "--new", "fact_1", "--confirm").returncode == 0
    # 同向重复 = 幂等 no-op（rc 0，不堆叠审计）
    r_same = _run_cli(store, "--new", "fact_1", "--confirm")
    assert r_same.returncode == 0 and "idempotent no-op" in r_same.stdout
    # 反向（已 confirmed -> reject）= 拒绝再翻
    r_rev = _run_cli(store, "--new", "fact_1", "--reject")
    assert r_rev.returncode == 1 and "not 'candidate'" in r_rev.stdout
    r3 = _run_cli(store, "--new", "no_such_row", "--confirm")
    assert r3.returncode == 1  # 找不到行 -> 明确报错不炸


def test_concurrent_cli_confirms_no_tear(tmp_path):
    """4 进程同 shard 并发 _shard_lock RMW：文件不撕裂，4 条全翻，4 条审计。"""
    store = _cli_fixture(tmp_path)
    procs = [subprocess.Popen(
        [sys.executable, str(CLI_SCRIPT), "--store", str(store),
         "--new", nid, "--confirm"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(PLUGIN_DIR))
        for nid in ("fact_1", "fact_4")] * 2
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o for o in outs]  # 全部成功（重复=幂等 no-op）
    data = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))  # 不撕裂 = 可解析
    rows = {x["id"]: x for x in data}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_4"]["metadata"]["supersede_state"] == "confirmed"
    assert rows["fact_0"]["metadata"]["superseded_confirmed"] is True
    assert rows["fact_3"]["metadata"]["superseded_confirmed"] is True
    aud = [json.loads(l) for l in (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    # 每行恰好一条审计（重复 confirm = 幂等 no-op，不堆叠审计行）
    assert len(aud) == 2
    assert {a["new_id"] for a in aud} == {"fact_1", "fact_4"}
    assert all(a["action"] == "supersede_confirm_human" for a in aud)


# ── 10. 零硬编码模型审计（confirm 段）───────────────────────────────────────

def test_no_hardcoded_model_or_provider_in_confirm_segment():
    text = (PLUGIN_DIR / "consolidation" / "llm_caller.py").read_text(encoding="utf-8")
    seg = text.split("EP-2: supersedes-candidate confirmation", 1)[1]
    # 蓝图 §6 验收 grep：model 赋值 / 第三方 SDK 全部 = 0
    assert not re.search(r'model\s*[:=]\s*["a-z]|openai|anthropic|httpx|requests', seg), \
        "hardcoded model/provider/SDK leaked into the EP-2 confirm segment"
    # 宿主主模型面：唯一入口是 host call surface（_structured_call ->
    # _HOST_CALL_LLM，epic card 2），且调用不带 task= 关键字（宿主主模型落点，
    # 同 distill）。
    assert "_structured_call(" in seg
    assert "task=" not in seg
    # 无新增 import（段内不出现 import 语句）
    assert not re.search(r"^\s*import |^\s*from ", seg, re.M)


def test_cli_zero_llm_imports():
    src = CLI_SCRIPT.read_text(encoding="utf-8")
    # 零 LLM import：任何 import/调用行都不接触 llm 面（docstring 的
    # 文档性提及不算；审计对象是 import 语句 + 调用表达式）。
    import_lines = [l for l in src.splitlines()
                    if re.match(r"\s*(import |from .+ import )", l)]
    joined = "\n".join(import_lines)
    assert "llm" not in joined and "consolidation" not in joined
    # 硬审计：全文无 llm_caller / complete_structured 的调用表达式
    assert not re.search(r"\bllm_caller\.\w+\(|complete_structured\(", src)
    for banned in ("openai", "anthropic", "httpx", "requests"):
        assert banned not in src
    # 零硬编码路径（同 sibling 工具风格）
    assert not re.search(r'[\'"](/root|/usr/local|C:\\\\)', src)
    assert 'os.environ.get("HERMES_HOME"' in src


# ── 11. 三态 schema round-trip（无损）───────────────────────────────────────

@pytest.mark.parametrize("state", ["candidate", "confirmed", "rejected"])
def test_supersede_state_round_trip(state):
    item = MemoryItem(
        id="fact_1", type="fact", content="deploy on host 117 uses k8s",
        supersedes=["fact_0"],
        metadata={"supersede_state": state},
    )
    d = item.to_dict()
    assert d["metadata"]["supersede_state"] == state
    assert d["metadata"]["supersedes"] == ["fact_0"]
    back = MemoryItem.from_dict(json.loads(json.dumps(d)))  # 经 JSON 的无损 round-trip
    assert back.metadata["supersede_state"] == state
    assert back.metadata["supersedes"] == ["fact_0"]
    assert back.supersedes == ["fact_0"]
    # lineage_of 读侧视图（EP-1 契约，legacy 行无键 = None 不炸）
    lv = fm.lineage_of(d)
    assert lv["supersede_state"] == state


# ── L8 (2026-11-11): write-after-verify guard (tools/confirm_supersede.py) ─
#
# Root cause this locks down: _verdict's RMW (read under lock -> json.dump
# whole shard) can LOSE its write when a sibling unlocked RMW (consolidation
# / save-door in another process) clobbers the shard with an older snapshot
# between our read-snapshot and our write. The lock serializes one RMW; it
# does not CAS against a later writer's stale read. The guard re-reads the
# shard (same provider, same lock path) after the write and retries a
# bounded number of times instead of returning 0 for a flip that did not
# survive on disk.

import importlib.util as _ilu
_CS_CLI = PLUGIN_DIR / "tools" / "confirm_supersede.py"
_spec = _ilu.spec_from_file_location("confirm_supersede_under_test", str(_CS_CLI))
cs_cli = _ilu.module_from_spec(_spec)
# tools/confirm_supersede.py does `import __init__ as fm` with its own
# sys.path insert; load the module object directly so the test can reach
# _build_provider / _verdict / _verify_flip without depending on the test
# runner's sys.path configuration.
import sys as _sys
_sys.modules.setdefault("confirm_supersede_under_test", cs_cli)
_spec.loader.exec_module(cs_cli)


def _cli_load_state(store: pathlib.Path, row_id: str) -> str:
    rows = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))
    return {r["id"]: r for r in rows}[row_id]["metadata"]["supersede_state"]


def _cli_audit_count(store: pathlib.Path) -> int:
    f = store / "consolidation_log.jsonl"
    if not f.exists():
        return 0
    return len([l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()])


def test_l8_cli_confirm_happy_path_persists_and_audits_once(tmp_path):
    store = _cli_fixture(tmp_path)
    p = cs_cli._build_provider(store)
    rc = cs_cli._verdict(p, "fact_1", "confirm")
    assert rc == 0
    assert _cli_load_state(store, "fact_1") == "confirmed"
    assert _cli_audit_count(store) == 1
    aud = [json.loads(l) for l in (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert aud[0]["write_verified"] is True
    assert aud[0]["new_id"] == "fact_1" and aud[0]["verdict"] == "confirmed"


def test_l8_cli_confirm_clobbered_once_retries_and_persists(tmp_path, monkeypatch):
    store = _cli_fixture(tmp_path)
    p = cs_cli._build_provider(store)
    calls = {"n": 0}
    original = cs_cli._verify_flip
    shard_path = store / "memories" / "fact.json"

    def flaky_verify(p_arg, shard, new_id, target_state):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate the lost-update: a concurrent RMW clobbered our write,
            # so the on-disk state is back to 'candidate'. Return False AND
            # roll the row back so the next attempt genuinely re-flips it.
            rows = json.loads(shard_path.read_text(encoding="utf-8"))
            for r in rows:
                if r.get("id") == new_id:
                    r["metadata"]["supersede_state"] = "candidate"
            shard_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            return False
        return original(p_arg, shard, new_id, target_state)

    monkeypatch.setattr(cs_cli, "_verify_flip", flaky_verify)
    rc = cs_cli._verdict(p, "fact_1", "confirm")
    assert rc == 0
    assert calls["n"] == 2  # first verify clobbered, second survived
    assert _cli_load_state(store, "fact_1") == "confirmed"
    assert _cli_audit_count(store) == 1  # no double-stack from the retry


def test_l8_cli_confirm_never_persists_fails_without_audit(tmp_path, monkeypatch):
    store = _cli_fixture(tmp_path)
    p = cs_cli._build_provider(store)
    shard_path = store / "memories" / "fact.json"

    def never_persists(p_arg, shard, new_id, target_state):
        # Every verify reports a clobber and rolls the row back to candidate,
        # so no attempt ever sees a durable flip.
        rows = json.loads(shard_path.read_text(encoding="utf-8"))
        for r in rows:
            if r.get("id") == new_id:
                r["metadata"]["supersede_state"] = "candidate"
        shard_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        return False

    monkeypatch.setattr(cs_cli, "_verify_flip", never_persists)
    rc = cs_cli._verdict(p, "fact_1", "confirm")
    assert rc == 1
    assert _cli_load_state(store, "fact_1") == "candidate"  # rolled back, never durable
    assert _cli_audit_count(store) == 0  # a non-durable flip is NOT audited


def test_l8_cli_reject_happy_path_persists_and_audits_once(tmp_path):
    store = _cli_fixture(tmp_path)
    p = cs_cli._build_provider(store)
    rc = cs_cli._verdict(p, "fact_4", "reject")
    assert rc == 0
    assert _cli_load_state(store, "fact_4") == "rejected"
    assert _cli_audit_count(store) == 1
    aud = [json.loads(l) for l in (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert aud[0]["verdict"] == "rejected" and aud[0]["write_verified"] is True
