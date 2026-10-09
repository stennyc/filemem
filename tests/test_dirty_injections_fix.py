"""P4 (20261007 清染): write-side role gate + one-shot 存量 sweep.

The 2026-10-07 contamination census of the shared store
(/root/.hermes/filemem) proved the dirty classes that reached the pool /
the pending-draft face, and this suite locks the two coordinated fixes:

  1. WRITE-SIDE ROLE GATE — the ``sync_turn`` HWM router loop and the batch
     preference extraction ran over ALL message roles before this: an
     assistant reply / tool result carrying an L0 trigger word ("config",
     "port", "修复"…) was auto-saved and mis-stamped
     ``origin {kind: user, trust: medium}``. That is how the 8 model-reply
     prose rows (trace-verified via the session traces) and the raw tool-dump
     bodies leaked in. The gate routes/extracts USER utterances only; an
     explicit non-user role is skipped, and a legacy no-role host shape keeps
     today's routing (the gate only narrows, never crashes).

  2. ONE-SHOT 存量 SWEEP — tools/sweep_dirty_injections.py removes the
     evidence-vetted rows by stable row id (append-only archive + audit,
     留痕非删, flock, dry-run default, idempotent). The 17 pinned rows are
     the 8 model-reply prose rows + 6 LLM-JSON decision rows + 3 pending-
     draft JSON dumps; the sibling USER-utterance rows and the v2
     canary/audit assets are deliberately KEPT.

Acceptance gate:
  - assistant / tool messages with trigger words produce ZERO pool rows;
    user + legacy no-role shapes still auto-save;
  - batch preference extraction is user-only (an assistant "I prefer …" is
    NOT mined into the pool as a user preference);
  - the sweep tool: dry-run writes nothing, apply moves the manifest rows to
    the archive + audit trail (kept rows byte-intact), a second apply finds
    0 candidates (idempotent).

Run (matches the p1_1 recipe):
    cd /root/.hermes/plugins/filemem
    PYTHONPATH=/usr/local/lib/hermes-agent \
      /usr/bin/python3 -m pytest tests/test_dirty_injections_fix.py -q -p no:cacheprovider
"""
import json, sys, tempfile, threading, traceback, subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]


# ── provider harness (the p0_1 shape: interval 0 -> immediate writes) ──────
def _make_provider(tmp: Path, sid: str = "s1") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
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
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = {k: (v.copy() if isinstance(v, (dict, list)) else v)
                for k, v in DECAY_DEFAULTS.items()}
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._session_drafts = {}
    p._pending_memories = []
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._last_recall = None
    p._main_runtime = None
    p._llm_consolidating = False
    p._last_consolidation_ts = 0.0
    p._session_hwm = {}
    return p


def _read_all(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


# ── a. write-side role gate ────────────────────────────────────────────────

# carries an L0 environment trigger word (config/port/endpoint)
ASSISTANT_L0_BODY = "The config on port 8443 was verified at the endpoint."
USER_L0_BODY = "note: server config on port 9000 in staging"
TOOL_DUMP_BODY = '{"status": "success", "output": "config dump for port 8443"}'


def test_sync_turn_assistant_trigger_message_not_routed():
    """The router loop must NOT auto-save an assistant reply that merely
    carries an L0 trigger word — the 8-row model-prose mis-stamp class."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.sync_turn("plain user note", "ok", session_id="s1",
                messages=[{"role": "user", "content": "plain user note"},
                          {"role": "assistant", "content": ASSISTANT_L0_BODY}])
    rows = _read_all(p)
    assert rows == [], f"assistant trigger body leaked into the pool: {rows}"


def test_sync_turn_tool_message_not_routed():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.sync_turn("plain user note", "ok", session_id="s1",
                messages=[{"role": "user", "content": "plain user note"},
                          {"role": "tool", "content": TOOL_DUMP_BODY}])
    assert _read_all(p) == [], "tool-result trigger body must not route"


def test_sync_turn_user_trigger_message_still_routed():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.sync_turn(USER_L0_BODY, "ok", session_id="s1",
                messages=[{"role": "user", "content": USER_L0_BODY},
                          {"role": "assistant", "content": "got it"}])
    rows = _read_all(p)
    assert len(rows) == 1, f"user trigger body must auto-save exactly once: {rows}"
    assert "port 9000" in rows[0]["content"]


def test_sync_turn_legacy_no_role_shape_still_routed():
    """Host shapes without a role key (pre-role-era histories) keep
    today's routing — the gate only narrows EXPLICIT non-user roles."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    legacy = {"content": USER_L0_BODY}  # no "role" key at all
    p.sync_turn("", "ok", session_id="s1", messages=[legacy])
    rows = _read_all(p)
    assert len(rows) == 1, f"legacy no-role shape must keep routing: {rows}"


def test_batch_preference_extraction_is_user_only():
    """``_extract_memories`` pref_patterns must not mine an ASSISTANT
    "I prefer X" into the pool as a user preference (the fact branch
    already carried the role gate; the pref branch did not)."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    extracted = p._extract_memories([
        {"role": "user", "content": "I prefer dark mode."},
        {"role": "assistant", "content": "I prefer compact output."},
    ])
    contents = [m["content"] for m in extracted]
    assert any("dark mode" in c for c in contents), "user pref must still extract"
    assert not any("compact output" in c for c in contents), \
        "assistant 'I prefer …' must NOT extract as a user preference"


# ── c. one-shot 存量 sweep tool ─────────────────────────────────────────────

SWEEP_MOD = PLUGIN_DIR / "tools" / "sweep_dirty_injections.py"


def _run_sweep_cli(store: Path, yes: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SWEEP_MOD), "--store", str(store)]
    if yes:
        cmd.append("--yes")
    return subprocess.run(cmd, capture_output=True, text=True)


def _fixture_store(tmp: Path) -> Path:
    """A minimal store carrying the 17 pinned manifest rows (all present,
    so a dry-run judges 17) + v2 canary / high-value rows that must survive
    an apply pass byte-intact."""
    store = tmp / "filemem"
    mem = store / "memories"
    mem.mkdir(parents=True)

    # fact shard: 5 manifest rows + a v2 canary row + a kept user row
    fact_rows = [
        {"id": "fact_414_1791196731", "type": "fact",
         "content": "收到，这条修正我全盘接受——是我读偏了…（模型回复）"},
        {"id": "fact_443_1791226607", "type": "fact",
         "content": "收到，两个指令都清楚了…（模型回复）"},
        {"id": "fact_496_1791232242", "type": "fact",
         "content": "收到，卡收口了（done）…（模型回复）"},
        {"id": "fact_666_1791330031", "type": "fact",
         "content": "让我先停掉 terminal…（模型回复）"},
        {"id": "fact_670_1791331803", "type": "fact",
         "content": "探针回来了，**bug 钉死…（模型回复）"},
        # KEPT — a v2 room-meta canary row (self_test)
        {"id": "fact_canary_1", "type": "fact",
         "content": '[Group chat: "知识库"] You are @orchestrator …',
         "metadata": {"self_test": True, "source_profile": "orchestrator"}},
        # KEPT — a trace-verified USER utterance (not in the manifest)
        {"id": "fact_309_1791180509", "type": "fact",
         "content": "好的，而且这个方式如果行得通…（用户原话，保留）"},
    ]
    (mem / "fact.json").write_text(
        json.dumps(fact_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    pref_rows = [
        {"id": "preference_140_1791324303", "type": "preference",
         "content": "好的，不管 kanban-block 插件了…（模型回复）"},
        {"id": "preference_keep_1", "type": "preference",
         "content": "user prefers dark mode in the editor"},
    ]
    (mem / "preference.json").write_text(
        json.dumps(pref_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    task_rows = [
        {"id": "task_148_1791230039", "type": "task",
         "content": "收到，两个指令都清楚了…（模型回复）"},
        {"id": "task_256_1791313313", "type": "task",
         "content": "收到，记死：**任何 hermes 原生代码…（模型回复）"},
        {"id": "task_keep_1", "type": "task",
         "content": "用户待办：下周五前提交发布单（活体任务，保留）"},
    ]
    (mem / "task.json").write_text(
        json.dumps(task_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    dec_rows = [
        {"id": "decision_4_1791153827", "type": "decision",
         "content": '{"responses": [{"question": "草稿处置？"}]}'},
        {"id": "decision_6_1791168800", "type": "decision",
         "content": '{"responses": [{"question": "线2 缺口？"}]}'},
        {"id": "decision_7_1791193514", "type": "decision",
         "content": '{"responses": [{"question": "回显死结？"}]}'},
        {"id": "decision_15_1791231949", "type": "decision",
         "content": '{"responses": [{"question": "capability 活卡？"}]}'},
        {"id": "decision_33_1791251050", "type": "decision",
         "content": '{"responses": [{"question": "prompt cache 派单？"}]}'},
        {"id": "decision_37_1791271073", "type": "decision",
         "content": '{"py_compile": "UNRUN — no terminal lane"}'},
        # KEPT — a genuine user decision (not in the manifest)
        {"id": "decision_keep_1", "type": "decision",
         "content": "我们决定用 SQLite 作为本地存储（用户拍板，保留）"},
    ]
    (mem / "decision.json").write_text(
        json.dumps(dec_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    # one pending-draft file: 3 manifest JSON-dump rows + 1 high-value kept row
    drafts = store / "drafts_20261006_193446_c8113b.json"
    drafts.write_text(json.dumps([
        {"id": "draft_1791281419534860_1", "type": "fact",
         "content": "事实清单已存档：…（高价值草稿，保留）"},
        {"id": "draft_1791282035882371_2", "type": "fact",
         "content": '{\n  "success": true,\n  "job_id": "29aff84acd79"\n}'},
        {"id": "draft_1791283506313959_4", "type": "fact",
         "content": '{\n  "success": true,\n  "job_id": "8efddd840e4a"\n}'},
        {"id": "draft_1791338848463817_9", "type": "fact",
         "content": '{"tools": {"todo_list": {"description": "…"}}}'},
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def test_sweep_dry_run_writes_nothing():
    tmp = Path(tempfile.mkdtemp())
    store = _fixture_store(tmp)
    res = _run_sweep_cli(store)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["dry_run"] is True
    assert out["judged"] == 17, f"expected 17 manifest rows present: {out}"
    # dry-run writes NOTHING: no archive dir, no audit file, shards/draft intact
    assert not (store / "knowledge" / "archive").exists()
    assert not (store / "consolidation_log.jsonl").exists()
    fact = [r["id"] for r in json.load(open(store / "memories" / "fact.json"))]
    assert fact[0] == "fact_414_1791196731" and "fact_309_1791180509" in fact
    draft = [r["id"] for r in json.load(
        open(store / "drafts_20261006_193446_c8113b.json"))]
    assert len(draft) == 4, "dry-run must not rewrite the draft file"


def test_sweep_apply_moves_rows_to_append_only_archive_keeps_canary():
    tmp = Path(tempfile.mkdtemp())
    store = _fixture_store(tmp)
    res = _run_sweep_cli(store, yes=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["dry_run"] is False
    assert out["judged"] == 17
    # memory shards: manifest rows gone, kept rows (incl. the v2 canary + the
    # trace-verified USER row + the genuine decision) byte-intact
    fact = json.load(open(store / "memories" / "fact.json"))
    fact_ids = {r["id"] for r in fact}
    assert fact_ids == {"fact_canary_1", "fact_309_1791180509"}, \
        f"only canary + user rows may survive in fact: {fact_ids}"
    pref = {r["id"] for r in json.load(open(store / "memories" / "preference.json"))}
    assert pref == {"preference_keep_1"}
    task = {r["id"] for r in json.load(open(store / "memories" / "task.json"))}
    assert task == {"task_keep_1"}
    dec = {r["id"] for r in json.load(open(store / "memories" / "decision.json"))}
    assert dec == {"decision_keep_1"}
    # the swept draft rows are out; the high-value draft survives
    draft = json.load(open(store / "drafts_20261006_193446_c8113b.json"))
    assert [r["id"] for r in draft] == ["draft_1791281419534860_1"]
    # 留痕非删: all 17 swept rows archived as JSONL lines (recoverable)
    arch_files = list((store / "knowledge" / "archive")
                      .glob("dirty_injection_sweep_*.jsonl"))
    assert len(arch_files) == 1
    arch = [json.loads(l) for l in arch_files[0].read_text(
        encoding="utf-8").splitlines() if l.strip()]
    assert len(arch) == 17, f"expected 17 archived lines, got {len(arch)}"
    # one audit row per swept row in the EXISTING consolidation log
    audit = [json.loads(l) for l in
             (store / "consolidation_log.jsonl").read_text(
                 encoding="utf-8").splitlines() if l.strip()]
    assert [a["action"] for a in audit] == ["dirty_injection_sweep"] * 17


def test_sweep_apply_is_idempotent():
    tmp = Path(tempfile.mkdtemp())
    store = _fixture_store(tmp)
    first = _run_sweep_cli(store, yes=True)
    assert first.returncode == 0, first.stderr
    second = _run_sweep_cli(store, yes=True)
    assert second.returncode == 0, second.stderr
    out2 = json.loads(second.stdout)
    assert out2["judged"] == 0, "a re-apply must find 0 manifest rows left"
    arch_files = list((store / "knowledge" / "archive")
                      .glob("dirty_injection_sweep_*.jsonl"))
    assert len(arch_files) == 1, "a 0-candidate apply must create no new trace"


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception:
                print(f"FAIL  {name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
