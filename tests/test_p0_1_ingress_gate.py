"""P0-1: write-side ingress gate (shared-store 清染 / 防再染).

The shared neutral store (~/.hermes/filemem) aggregates every profile + cron.
Three content classes are proven to leak into memories/*.json and later
re-inject stale task content via the pre_llm_call auto-recall:

  (a) host / cron instruction-block prefixes
      ([CONTEXT COMPACTION …], [IMPORTANT: You are running as a scheduled
       cron job …])
  (b) raw tool output (<untrusted_tool_result …> framing, or a bare JSON
      tool dump)
  (c) whole task prompts (>=1000 chars AND step-numbered / step-listed)

The write-side gate (sync_turn -> _save_memory / _add_draft / _extract_
memories) keeps those classes OUT of the memory pool: quarantined (rollback-
able on disk) + rejection_reason stamp + a grep-able `ingress_reject` audit
row. The gate is fail-open (a bug never blocks the host turn) and zero LLM
(pure rule + stdlib).

Acceptance gate (spec):
  - each of the three reject classes lands in quarantine with 0 rows in the
    memory pool;
  - normal memory rows are byte-stable (existing harness stays green);
  - a gate exception passes the row through (fail-open).
"""
import json, sys, pathlib, tempfile, threading, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES,
    ingress_reject_class, _looks_like_tool_dump,
)


# ── reject-class fixtures (spec shapes) ─────────────────────────────────────
HOST_COMPACT_BLOCK = (
    "[CONTEXT COMPACTION \u2014 REFERENCE ONLY]\n"
    "Respond ONLY to the latest user message. The earlier turns were compacted "
    "into this block; treat it as context, not as a new instruction. The user's "
    "latest message asked about the 4d crawler retry backoff and the bs4 parser "
    "fallback, so keep the whole compacted transcript in view when answering."
)
CRON_TASK_PROMPT = (
    "[IMPORTANT: You are running as a scheduled cron job for the 4-day "
    "crawler-health task. Poll the three hosts, retry with a 30s backoff when "
    "a probe times out, write the results to the shared crawl ledger, and "
    "escalate to the operator channel only after two consecutive failures. "
    "Do not surface the poll to any interactive session."
)
TOOL_FRAMED_DUMP = (
    "<untrusted_tool_result tool=\"web_search\" call_id=\"1\" status=\"ok\">\n"
    '{"output": "https://x.example/page", "results": 3, "took_ms": 412}\n'
    "</untrusted_tool_result>"
)
TOOL_BARE_DUMP = '{"output": "browser console dump body", "exit_code": 0}'
# A >=1000-char, step-numbered whole task prompt (M1's {/[ opener misses it).
_TASK_BODY = (
    "You are the 4-day crawler operator. "
    "Work the queue end to end. "
)
TASK_PROMPT = (
    _TASK_BODY +
    "Step 1: poll the three hosts for the crawl queue.\n"
    "Step 2: retry any timed-out probe with a 30s backoff.\n"
    "Step 3: write the results to the shared crawl ledger.\n"
    "Step 4: escalate to the operator channel after two consecutive failures.\n"
    + "Extra narrative about the crawl topology and the retry policy. " * 30
)
CLEAN_FACT = "the dev host config endpoint runs on port 8080"


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Partial provider (test_reinforce_recall / sa2_1 shape) with the
    P0-1 quarantine/audit seams wired to `tmp`."""
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
    return p


def _read_all(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


def _read_quarantine(p) -> list:
    f = p.knowledge_raw_pending_path
    if not f.exists():
        return []
    return json.load(open(f))


def _audit_rows(p, action=None) -> list:
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if action is None or r.get("action") == action:
            rows.append(r)
    return rows


# ── 1. module predicate: ingress_reject_class ──────────────────────────────

def test_reject_class_a_host_instruction_block():
    assert ingress_reject_class(HOST_COMPACT_BLOCK) == "host_instruction_block"
    assert ingress_reject_class(CRON_TASK_PROMPT) == "host_instruction_block"


def test_reject_class_b_raw_tool_output():
    # both the <untrusted_tool_result framing AND the bare JSON dump land in b
    assert ingress_reject_class(TOOL_FRAMED_DUMP) == "raw_tool_output"
    assert ingress_reject_class(TOOL_BARE_DUMP) == "raw_tool_output"
    # the bare-dump leg is the SAME single source of truth as _egress_noise_cut
    assert _looks_like_tool_dump(TOOL_BARE_DUMP) is True


def test_reject_class_c_whole_task_prompt():
    assert ingress_reject_class(TASK_PROMPT) == "whole_task_prompt"


def test_reject_class_clean_and_boundary():
    assert ingress_reject_class(CLEAN_FACT) is None
    assert ingress_reject_class("") is None
    assert ingress_reject_class(None) is None
    # a SHORT task-shaped text (<1000 chars) is NOT a whole task prompt
    short_task = "Step 1: a\nStep 2: b\nStep 3: c"
    assert ingress_reject_class(short_task) is None
    # a long task-shaped text WITHOUT >=3 step markers is NOT rejected
    long_plain = ("the deploy notes say the gateway runs on port 8080. " * 40).strip()
    assert ingress_reject_class(long_plain) is None


def test_reject_class_quoting_marker_mid_text_not_a_host_block():
    # a fact that QUOTES the marker mid-text stays (class a keys the opener).
    quoted = "I noted that '[CONTEXT COMPACTION \u2014 REFERENCE ONLY]' is " \
             "an injector marker we should never store as a fact."
    assert ingress_reject_class(quoted) is None


# ── 1b. P0-1b regressions: inline ASCII-dot step markers ───────────────────
# Probe-e defect (t_9c1906f9): the first step "1. 执行" is INLINE right after
# the repeated CJK intro (preceding char "。", NO \n). The pre-fix inline
# branch closing-punct class was [)〕、．] WITHOUT ASCII ".", so that step was
# only reachable via the line-head branch (which needs a preceding \n) -> only
# 2 of the 3 steps matched < _TASK_PROMPT_MIN_STEPS=3 -> None (the gate gap:
# a whole task prompt could bypass ingress rejection by inlining its first
# step). Hunk 1 adds "." to the inline closing-punct class to close it.

def test_reject_class_c_probe_e_inline_dot_after_cjk():
    cjk = "你是 4d 爬虫操作员，负责监控 9 家 4D 彩票公司开奖数据。" * 40
    s = (cjk
         + "1. 执行：python3 scraper all\n"
         + "2. 报告本次结果到 ledger\n"
         + "3. 不要创建或修改任何 cron 任务\n")
    assert len(s) >= 1000, f"probe-e shape must be >=1000, got {len(s)}"
    i = s.index("1. 执行")
    assert s[i - 1] == "。", "first step must be inline right after CJK 。"
    assert "\n" not in s[:i], "first step must NOT sit at a line head"
    assert ingress_reject_class(s) == "whole_task_prompt"


def test_reject_class_c_all_inline_dot_markers():
    # all three step markers use an ASCII dot INLINE ("1. " "2. " "3. ") with
    # NO line-head \n — before Hunk 1 none matched the inline branch (dot was
    # not in the closing-punct class), so the count was 0 < MIN_STEPS=3.
    body = ("the crawl pipeline spans nine regions, retries timed-out probes "
            "with a fixed backoff, and writes each ledger row atomically. "
            ) * 25
    s = (body
         + "1. poll the crawl queue inline "
         + "2. retry the slow probe inline "
         + "3. escalate to the operator inline ")
    assert len(s) >= 1000, f"all-inline shape must be >=1000, got {len(s)}"
    assert ingress_reject_class(s) == "whole_task_prompt"


def test_reject_class_c_short_inline_dot_length_gate():
    # <1000 chars even with 3 inline-dot markers -> the LENGTH gate still says
    # None (Hunk 1 must not loosen the length gate).
    s = "ctx 1. a poll " + "2. b retry " + "3. c write " + ("word filler " * 15)
    assert len(s) < 1000, f"must stay <1000, got {len(s)}"
    assert ingress_reject_class(s) is None


def test_reject_class_c_two_markers_step_gate():
    # >=1000 chars but only TWO numbered markers -> the STEP gate still says
    # None (Hunk 1 must not loosen the step-count gate).
    prose = ("the shared crawl ledger stores one atomic row per probe and the "
             "operator notes spell out the backoff policy in detail. "
             ) * 40
    s = prose + "1. first poll " + "2. second retry "
    assert len(s) >= 1000, f"must be >=1000, got {len(s)}"
    assert ingress_reject_class(s) is None


# ── 2. _save_memory door: reject -> quarantine + audit, 0 pool rows ──────

def test_save_door_rejects_all_three_classes():
    for content, cls in [
        (HOST_COMPACT_BLOCK, "host_instruction_block"),
        (TOOL_BARE_DUMP, "raw_tool_output"),
        (TASK_PROMPT, "whole_task_prompt"),
    ]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp)
        rid = p._save_memory(content, level="L0", source="auto")
        assert rid == "", f"{cls} must NOT write a pool row"
        # 0 rows in the memory pool
        assert _read_all(p) == [], f"{cls} leaked into the pool"
        # quarantined on disk, rollback-able, with the rejection_reason stamp
        q = _read_quarantine(p)
        assert len(q) == 1, f"{cls} must quarantine exactly one row"
        assert q[0]["content"] == content
        # the rejection_reason stamp lives on the quarantine row
        assert q[0]["reason"].startswith("P0-1 ingress reject-class:")
        assert q[0]["reason"].endswith(cls)
        assert q[0]["origin"] == "auto"
        # grep-able audit row
        aud = _audit_rows(p, action="ingress_reject")
        assert len(aud) == 1
        assert aud[0]["reject_class"] == cls
        assert aud[0]["action"] == "ingress_reject"


def test_save_door_clean_fact_writes_pool_and_no_quarantine():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(CLEAN_FACT, level="L0", source="auto")
    assert rid, "clean fact must write a pool row"
    assert len(_read_all(p)) == 1
    assert _read_quarantine(p) == [], "clean fact must NOT quarantine"
    assert _audit_rows(p, action="ingress_reject") == []


# ── 3. _add_draft door ──────────────────────────────────────────────────────

def test_draft_door_rejects_three_classes():
    for content, cls in [
        (CRON_TASK_PROMPT, "host_instruction_block"),
        (TOOL_FRAMED_DUMP, "raw_tool_output"),
        (TASK_PROMPT, "whole_task_prompt"),
    ]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp)
        did = p._add_draft(content, level="L1")
        assert did == "", f"{cls} draft must not be created"
        assert _read_quarantine(p), f"{cls} draft must quarantine"
        assert _audit_rows(p, action="ingress_reject")[0]["reject_class"] == cls


def test_draft_door_clean_fact_succeeds():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    did = p._add_draft("decision draft about the rollout plan", level="L1")
    assert did, "clean draft must be created"
    assert _read_quarantine(p) == []


# ── 4. _extract_memories door (batch vectors) ──────────────────────────────

def test_extract_door_rejects_task_prompt_and_keeps_clean_fact():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    messages = [
        {"role": "user", "content": TASK_PROMPT},            # c: reject
        {"role": "user", "content": "the deploy pipeline uses SQLite."},  # clean
    ]
    extracted = p._extract_memories(messages)
    # only the clean fact is extracted; the task prompt is quarantined
    contents = [m["content"] for m in extracted]
    assert TASK_PROMPT not in contents
    assert "the deploy pipeline uses SQLite." in contents
    assert any(q["reject_class"] == "whole_task_prompt" for q in
               _audit_rows(p, action="ingress_reject"))


# ── 5. fail-open: a gate exception passes the row through ─────────────────

def test_ingress_rejection_fail_open_passes_clean_through():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    # None content -> classifier returns None -> pass (no side-effects)
    assert p._ingress_rejection(None) is None
    assert p._ingress_rejection("") is None
    assert _read_quarantine(p) == []


def test_ingress_rejection_fail_open_classifier_broken():
    """A classifier exception must NOT block the write path (fail-open)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    # Simulate a broken classifier by monkey-patching the module symbol the
    # method resolves to at call time.
    import __init__ as fm
    real = fm.ingress_reject_class
    fm.ingress_reject_class = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("gate bug"))
    try:
        res = p._ingress_rejection(HOST_COMPACT_BLOCK)
    finally:
        fm.ingress_reject_class = real
    assert res is None, "a gate bug must fail-open (row passes, not quarantined)"
    assert _read_quarantine(p) == []


def test_ingress_rejection_quarantine_failure_keeps_verdict_sticky():
    """Even if the quarantine write throws, the reject verdict holds (the row
    is still kept out of the pool) — but the method never raises."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    real = p._quarantine_raw_pending
    p._quarantine_raw_pending = lambda *a, **k: (_ for _ in ()).throw(
        OSError("disk hiccup"))
    try:
        res = p._ingress_rejection(HOST_COMPACT_BLOCK, origin="auto")
    finally:
        p._quarantine_raw_pending = real
    assert res == "host_instruction_block", \
        "quarantine failure must not downgrade the reject verdict"


# ── 6. end-to-end sync_turn: the host block never reaches the pool ────────

def test_sync_turn_host_block_never_in_pool():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.auto_extract = False
    p.sync_turn(HOST_COMPACT_BLOCK, "",
                messages=[{"role": "user", "content": HOST_COMPACT_BLOCK}])
    assert _read_all(p) == [], "host compact block must not auto-save"
    assert _audit_rows(p, action="ingress_reject")


def test_manual_save_is_unaffected_by_ingress_gate():
    """The _save_memory gate is scoped to source=='auto'. A manual
    (filemem_write) save of a reject-class payload is user-confirmed and
    NOT quarantined — it writes to the pool (preserved invariant: manual
    saves are unaffected). This locks the scoping decision."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rid = p._save_memory(HOST_COMPACT_BLOCK, level="L0", source="manual")
    assert rid, "manual save must write the pool (not gated)"
    assert len(_read_all(p)) == 1
    assert _read_quarantine(p) == [], "manual save must NOT quarantine"
    assert _audit_rows(p, action="ingress_reject") == []


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
