"""P3-1: the pre-queue low-value silent discard gate (1A 保守档, 通知面根治).

The user complaint (2026-10-05): "为什么低价值的老是要问？" — every
low-value row (过程描述 / 工具输出碎句) still reached TWO user-visible
faces:

  ① the historical-draft popup `[未决草稿]` (the pending drafts files on
     disk, announced by _pending_draft_notice with the hardcoded
     "请询问用户" wording);
  ② the memory-report clarify panel (the _pending_confirm queue →
     _turn_report_line → the [记忆报告] "确认/丢弃" ask on every turn).

The P3-1 root-fix: a conservative 3-leg judgment (NO new heuristics, zero
LLM, zero new Chinese patterns — the existing config assets only):

  1. the shared P0-3 egress-noise OPENER tables (_noise_table_hit —
     tool_dump_openers + fragment_prefixes, all config-driven);
  2. too short: len(normalized content) < no_popup.min_chars (40);
  3. an EXPLICIT salience below no_popup.salience_floor (0.5) — a missing
     salience (L0 auto / draft rows default to 0.5, i.e. AT the floor) is
     NOT a hit (the conservative posture does not mis-kill on salience).

Hit → the row NEVER enters the user-visible queue face: not enqueued in
the [记忆报告] clarify panel (the central _push_pending_confirm door,
which BOTH the L0-auto and draft enqueue paths flow through), and — for
drafts — not written to the drafts files at all (the [未决草稿] notice
reads those files from disk). Each discard writes ONE audit row via the
EXISTING _append_consolidation_event primitive (action=no_popup_discard,
consolidation_log.jsonl — no new log file) + the spec'd logger.info line.
No hit = high-value → queued exactly as today (behavior unchanged).

Config face: decay.json + DECAY_DEFAULTS gained a `no_popup` block
({"enable": true, "min_chars": 40, "salience_floor": 0.5}); enable=false
(or a missing/malformed block) = full rollback to the legacy queue face.

The one-off tools/sweep_low_value.py (T3) cleans the historical 存量 with
the SAME judgment (verbatim copies, AST byte-identity asserted below —
the sweep face and the live gate can never drift apart); it runs by
@devops on the restarted gateway.

Acceptance legs (spec T4):
  ① a noise-table-hit L0 auto row never enters the queue + the audit row
     exists (the ingress face already quarantines table rows pre-P0-3 —
     the queue gate is the belt-and-suspenders second face);
  ② a <min_chars fragment never enters the queue (both the save face and
     the draft face — the draft is discarded before it lands on disk);
  ③ a high-value item (long + non-noise) queues as before → the panel
     face behavior is unchanged;
  ④ enable=false rolls back fully (every row queues, zero audit rows);
  ⑤ a judgment exception fails soft to QUEUE (宁多弹不可崩 — a gate bug
     must never crash the host turn or silently drop a memory);
  ⑥ the sweep tool: --dry-run (the default) writes NOTHING (byte-identical
     store), an apply run moves/rewrites/audits, and a second apply run
     is idempotent (0 candidates, 0 writes).
"""
import ast
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402  (the plugin root, P0-3 test convention)
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES,
    _no_popup_silent_discard, _noise_table_hit,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]

# load the sweep tool by path (module name sweep_low_value, pure stdlib —
# no plugin import, no host hooks; the S2 govern_memories precedent)
_spec = importlib.util.spec_from_file_location(
    "sweep_low_value", PLUGIN_DIR / "tools" / "sweep_low_value.py")
sweep_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep_mod)


# ── config source: the SHIPPED decay.json (改表不改码 — the tests exercise
#    the actual shipped tables + the new no_popup block, not a copy) ───────

def _load_decay() -> dict:
    p = PLUGIN_DIR / "decay.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    merged = dict(DECAY_DEFAULTS)
    for k, v in (data or {}).items():
        merged[k] = v
    return merged


DECAY = _load_decay()


# ── fixtures ────────────────────────────────────────────────────────────────

# a P0-3 table row (fragment_prefixes startswith anchor) — the ① class
GAP_FRAGMENT_ROW = "搞清楚了。我把情况说明清楚了，写侧 ingress 现在切到同一张表。"
# a P0-3 tool-dump table row (tool_dump_openers) — ① class, second shape
GAP_TOTAL_COUNT_ROW = (
    '{"total_count": 24, "matches": ["/root/.hermes/plugins/filemem/__init__.py"]}'
)
# a <min_chars fragment (passes the P0-1/P0-3 ingress gates — it is a
# normal process-description, not a dump/opener — so it reaches the
# queue face; the P3-1 leg ② is what keeps it out of the panel)
SHORT_FRAGMENT = "moved the report file"
# a high-value durable fact (>= min_chars, no table opener, non-noise)
HIGH_VALUE_FACT = (
    "the dev host config endpoint runs on port 8080, pinned by the "
    "2026-10-05 ops note so the canary probes stay deterministic"
)


def _make_provider(tmpdir: pathlib.Path, decay=None) -> FileMemProvider:
    """The P0-3/P0-1 harness shape (the __new__ partial instance), extended
    with the attrs the P3-1 faces touch (_main_runtime None = the LLM
    distill pass skips, exactly like every zero-LLM test)."""
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
    p._decay = {"lambda_days": 0.05} if decay is None else decay
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
    return p


def _queue_rows(p, sid="s1") -> list:
    return (getattr(p, "_pending_confirm", None) or {}).get(sid, [])


def _audit_rows(p, action: str) -> list:
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    rows = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("action") == action:
            rows.append(r)
    return rows


def _draft_file(p) -> list:
    f = p.storage_dir / f"drafts_{p.session_id}.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8"))


def _pool_rows(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


# ── ① noise-table-hit L0 auto row: never queued + the audit row exists ─────

def test_noise_table_row_discarded_at_queue_door_with_audit():
    """① the door face: a table row pushed through the CENTRAL
    _push_pending_confirm door (both enqueue paths flow through it) is
    NOT enqueued — the [记忆报告] panel stays silent — and leaves exactly
    ONE audit row (action=no_popup_discard, the existing
    consolidation_log.jsonl face, no new log file)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    p._push_pending_confirm("s1", "l0_x", "L0", GAP_FRAGMENT_ROW, kind="l0")
    assert _queue_rows(p) == [], "table row leaked into the pending queue"
    aud = _audit_rows(p, "no_popup_discard")
    assert len(aud) == 1, f"expected exactly 1 audit row, got {len(aud)}"
    assert aud[0]["kind"] == "l0"
    assert aud[0]["content_sha1"] and aud[0]["char_len"]
    # the sibling high-value door behavior: a second PUSH of the same
    # content re-judges (still discarded, still one NEW audit row) —
    # there is no queue entry to dedup against, so the audit trail is
    # per-occurrence (a grep-able trail, @data-engineer: no silent drop).
    p._push_pending_confirm("s1", "l0_y", "L0", GAP_TOTAL_COUNT_ROW, kind="l0")
    assert _queue_rows(p) == []
    assert len(_audit_rows(p, "no_popup_discard")) == 2


def test_noise_table_row_stays_out_of_pool_and_queue_e2e():
    """① belt-and-suspenders (defense in depth, zero regression): the
    P0-3 ingress face already quarantines a table row at the SAVE door —
    it never reaches the queue face in the first place; assert both
    faces agree (0 pool rows, 0 queue entries)."""
    for row in (GAP_TOTAL_COUNT_ROW, GAP_FRAGMENT_ROW):
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp, decay=DECAY)
        rid = p._save_memory(row, level="L0", source="auto")
        assert rid == "", "table row must not write a pool row (P0-3)"
        assert _pool_rows(p) == []
        assert _queue_rows(p) == []


# ── ② <min_chars fragment: never queued (save face + draft face) ───────────

def test_short_fragment_save_face_not_queued_pool_write_unchanged():
    """② a <min_chars process-fragment passes the P0-1/P0-3 ingress gates
    (it is a normal, non-dump row) and LANDS in the pool exactly as
    today (the write behavior is UNCHANGED — the P3-1 gate is the
    notification face, not the write face); but it NEVER enters the
    pending-confirm queue, and leaves the one audit row."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    rid = p._save_memory(SHORT_FRAGMENT, level="L0", source="auto")
    assert rid, "the pool write itself is unchanged (row still lands)"
    pool = _pool_rows(p)
    assert [r for r in pool if r.get("content") == SHORT_FRAGMENT]
    assert _queue_rows(p) == [], "short fragment leaked into the panel face"
    aud = _audit_rows(p, "no_popup_discard")
    assert len(aud) == 1 and aud[0]["kind"] == "l0"


def test_short_fragment_draft_face_discarded_before_disk():
    """② the draft face: a <min_chars draft is discarded BEFORE it is
    appended to _session_drafts / written to the drafts file — so the
    [未决草稿] historical-notice face (which reads those files from disk)
    never sees it either, and the pending queue stays empty. The discard
    mirrors the P0-1 ingress reject's `""` return (a no-op id, not an
    error)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    did = p._add_draft(SHORT_FRAGMENT, level="L1")
    assert did == "", "a low-value draft must be discarded (no-op id)"
    assert _draft_file(p) == [], "low-value draft leaked onto disk"
    assert _queue_rows(p) == []
    assert len(_audit_rows(p, "no_popup_discard")) == 1


# ── ③ high-value item: queues as before, panel behavior unchanged ──────────

def test_high_value_row_queues_as_before_panel_unchanged():
    """③ a high-value row (>= min_chars, no table opener) goes through
    BOTH doors exactly as today: the L0-auto save queues kind=l0 and the
    draft queues kind=draft + lands in the drafts file; the [记忆报告]
    panel face (_turn_report_line) renders it — behavior UNCHANGED,
    zero no_popup_discard audit rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    rid = p._save_memory(HIGH_VALUE_FACT, level="L0", source="auto")
    assert rid
    q = _queue_rows(p)
    assert [e for e in q if e.get("kind") == "l0"], "l0 queue entry missing"
    did = p._add_draft(HIGH_VALUE_FACT, level="L1")
    assert did
    q = _queue_rows(p)
    assert [e for e in q if e.get("kind") == "draft"], "draft queue missing"
    assert _draft_file(p), "the draft file must still be written (unchanged)"
    # the panel face renders the queued entries exactly as pre-P3-1:
    panel = p._turn_report_line("s1")
    assert "[记忆报告]" in panel and HIGH_VALUE_FACT[:40] in panel
    assert _audit_rows(p, "no_popup_discard") == []


# ── ④ enable=false: full rollback (every row queues, zero audit rows) ──────

def test_enable_false_full_rollback():
    """④ no_popup.enable=false = full legacy rollback: the SAME rows that
    the enabled gate discards now queue + land on disk exactly as
    pre-P3-1, and NO audit rows are written (the gate is off, not just
    lenient)."""
    decay_off = dict(DECAY)
    decay_off["no_popup"] = dict(DECAY["no_popup"])
    decay_off["no_popup"]["enable"] = False
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=decay_off)
    p._push_pending_confirm("s1", "l0_x", "L0", GAP_FRAGMENT_ROW, kind="l0")
    assert len(_queue_rows(p)) == 1, "enable=false must queue table rows"
    rid = p._save_memory(SHORT_FRAGMENT, level="L0", source="auto")
    assert rid, "the pool write is still unchanged"
    assert any(e.get("content") == SHORT_FRAGMENT for e in _queue_rows(p)), \
        "enable=false must queue short fragments"
    assert p._add_draft(SHORT_FRAGMENT, level="L1"), \
        "enable=false must keep the draft on disk"
    assert _audit_rows(p, "no_popup_discard") == []


# ── ⑤ judgment exception: fail-soft to queue (宁多弹不可崩) ─────────────────

def test_judgment_exception_fails_soft_to_queue():
    """⑤ if the judgment itself raises (a future table-shape bug, a
    corrupted decay block the fail-soft leg missed), the gate MUST
    degrade to TODAY's behavior — queue the row — and never crash the
    host turn or silently drop the memory."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    orig = fm._no_popup_silent_discard
    try:
        def _boom(*a, **k):
            raise RuntimeError("injected judgment failure")
        fm._no_popup_silent_discard = _boom
        p._push_pending_confirm("s1", "l0_z", "L0", GAP_FRAGMENT_ROW,
                                kind="l0")
    finally:
        fm._no_popup_silent_discard = orig
    assert len(_queue_rows(p)) == 1, "a judgment exception must QUEUE (fail-soft)"
    assert _audit_rows(p, "no_popup_discard") == []


def test_judgment_malformed_configs_fail_soft_off():
    """⑤'s companion (the module-level fail-soft legs, same 宁多弹不可崩
    posture): a missing block, decay_cfg=None, a malformed no_popup
    shape, and enable=false all degrade to "NOT low-value" (the caller
    queues) — a bad config can never turn the gate ON accidentally, and
    the judgment never raises."""
    assert _no_popup_silent_discard(GAP_FRAGMENT_ROW, None) is False
    assert _no_popup_silent_discard(GAP_FRAGMENT_ROW, {}) is False
    # malformed block shapes (non-dict / missing keys): every leg
    # fail-softs to pass-through, never raises, never mis-fires:
    assert _no_popup_silent_discard(
        GAP_FRAGMENT_ROW, {"no_popup": "corrupted-non-dict"}) is False
    # but a WELL-FORMED enabled block still discards (the gate is live):
    assert _no_popup_silent_discard(GAP_FRAGMENT_ROW, DECAY) is True
    # leg ③ needs an EXPLICIT sub-floor salience (an absent salience is
    # NOT a hit — the conservative posture, 零误杀):
    assert _no_popup_silent_discard(
        HIGH_VALUE_FACT, DECAY, salience=None) is False
    assert _no_popup_silent_discard(
        HIGH_VALUE_FACT, DECAY, salience=0.2) is True
    assert _no_popup_silent_discard(
        HIGH_VALUE_FACT, DECAY, salience=0.5) is False  # AT the floor
    # a malformed no_popup dict shape still fails soft to legacy queueing:
    bad = dict(DECAY)
    bad["no_popup"] = {"enable": True}
    assert _no_popup_silent_discard("ok", bad) is True  # leg ② via defaults


# ── ⑥ sweep tool: dry-run zero changes / apply idempotent ──────────────────

def _build_sweep_store(root: pathlib.Path) -> None:
    """A synthetic 存量: one MIXED drafts file (1 low + 1 high row), one
    ALL-low-value drafts file, and a memories/fact.json with one L0-auto
    table-hit row + one L0-auto high-value row."""
    (root / "memories").mkdir(parents=True)
    (root / "knowledge" / "archive").mkdir(parents=True)
    low_draft = {"id": "d_a1", "type": "fact", "level": "L1",
                 "source": "draft", "content": SHORT_FRAGMENT,
                 "salience": 0.5}
    high_draft = {"id": "d_a2", "type": "fact", "level": "L1",
                  "source": "draft", "content": HIGH_VALUE_FACT,
                  "salience": 0.5}
    (root / "drafts_s_a.json").write_text(
        json.dumps([low_draft, high_draft], ensure_ascii=False), encoding="utf-8")
    (root / "drafts_s_b.json").write_text(
        json.dumps([{"id": "d_b1", "type": "fact", "level": "L1",
                     "source": "draft", "content": "done, cleaned up",
                     "salience": 0.5}], ensure_ascii=False),
        encoding="utf-8")
    mem_noise = {"id": "fact_0", "type": "fact", "level": "L0",
                 "source": "auto", "content": GAP_TOTAL_COUNT_ROW,
                 "salience": 0.5}
    mem_clean = {"id": "fact_1", "type": "fact", "level": "L0",
                 "source": "auto", "content": HIGH_VALUE_FACT,
                 "salience": 0.5}
    (root / "memories" / "fact.json").write_text(
        json.dumps([mem_noise, mem_clean], ensure_ascii=False), encoding="utf-8")


def _store_snapshot(root: pathlib.Path) -> dict:
    """Byte + path map of the whole store (the dry-run zero-change proof:
    no file touched, NO new file created)."""
    return {str(f.relative_to(root)): f.read_bytes()
            for f in sorted(root.rglob("*")) if f.is_file()}


def test_sweep_dry_run_zero_changes():
    """⑥a the default dry-run reports the judgment counts and writes
    NOTHING: every file byte-identical, no trace jsonl, no archive, no
    consolidation_log row (the store has none yet — it must not appear)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    _build_sweep_store(tmp)
    snap = _store_snapshot(tmp)
    res = sweep_mod.sweep(tmp, DECAY, dry_run=True, yes=False)
    assert res["dry_run"] is True
    # 2 draft rows (the mixed file's low row + the all-low file's row)
    # + 1 memory row (the L0-auto table hit) = 3 judged low-value.
    assert res["judged_low_value"] == 3, res
    assert res["memories"]["rows_swept"] == 1
    assert res["drafts"]["rows_discarded"] == 2
    assert _store_snapshot(tmp) == snap, "dry-run wrote something"
    assert not list(tmp.glob("drafts_discarded_sweep_*.jsonl"))
    assert not list((tmp / "knowledge" / "archive").glob("sweep_low_value_*"))
    assert not (tmp / "consolidation_log.jsonl").exists()


def test_sweep_apply_moves_rewrites_and_audits():
    """⑥b the apply pass (--yes): the ALL-low drafts file moves whole
    into the trace jsonl (留痕非删) and is unlinked; the MIXED file is
    rewritten with only its surviving row (the low row is in the trace,
    not deleted); the L0-auto table-hit memory row moves to
    knowledge/archive/ (the P1-1 precedent) + ONE consolidation_log.jsonl
    audit row per swept row (action=no_popup_sweep, source=sweep, the
    original content's first 64 chars)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    _build_sweep_store(tmp)
    res = sweep_mod.sweep(tmp, DECAY, dry_run=True, yes=True)
    assert res["dry_run"] is False and res["judged_low_value"] == 3
    # 1. the all-low file is gone (its row lives in the trace jsonl):
    assert not (tmp / "drafts_s_b.json").exists()
    trace_files = sorted(tmp.glob("drafts_discarded_sweep_*.jsonl"))
    assert len(trace_files) == 1
    trace_lines = [json.loads(l) for l in
                   trace_files[0].read_text(encoding="utf-8").splitlines()
                   if l.strip()]
    srcs = sorted(t["source_file"] for t in trace_lines)
    assert srcs == ["drafts_s_a.json", "drafts_s_b.json"], \
        "trace jsonl must carry BOTH low rows with provenance"
    # 2. the mixed file keeps only its high-value row:
    mixed = json.loads((tmp / "drafts_s_a.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in mixed] == ["d_a2"]
    # 3. the memory file keeps only its high-value row:
    mem = json.loads((tmp / "memories" / "fact.json").read_text(
        encoding="utf-8"))
    assert [r["id"] for r in mem] == ["fact_1"], "the noise row must be swept"
    # the P1-1 archive precedent: the swept row is append-only recoverable:
    arch = sorted((tmp / "knowledge" / "archive").glob("sweep_low_value_*"))
    assert len(arch) == 1
    arch_rows = [json.loads(l) for l in
                 arch[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(arch_rows) == 1
    assert arch_rows[0]["row"]["id"] == "fact_0"
    # the audit row: action=no_popup_sweep, source=sweep, first 64 chars
    # of the ORIGINAL content, into the EXISTING consolidation_log face:
    log_rows = [json.loads(l) for l in
                (tmp / "consolidation_log.jsonl").read_text(
                    encoding="utf-8").splitlines() if l.strip()]
    assert len(log_rows) == 1
    row = log_rows[0]
    assert row["action"] == "no_popup_sweep" and row["source"] == "sweep"
    assert row["row_id"] == "fact_0"
    assert row["content_preview"] == GAP_TOTAL_COUNT_ROW[:64]
    assert row["archived_to"] == arch[0].name


def test_sweep_apply_idempotent_second_run():
    """⑥c idempotent: after an apply, the SECOND apply finds 0 candidates
    (the swept rows are gone from the pools, the all-low file is
    unlinked) and writes NOTHING: no new trace/archive files, and every
    surviving file byte-identical (the second run's 0-placement proof)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    _build_sweep_store(tmp)
    r1 = sweep_mod.sweep(tmp, DECAY, dry_run=True, yes=True)
    assert r1["judged_low_value"] == 3
    snap = _store_snapshot(tmp)
    r2 = sweep_mod.sweep(tmp, DECAY, dry_run=True, yes=True)
    assert r2["judged_low_value"] == 0, \
        f"second apply must judge 0 low-value rows: {r2}"
    assert r2["memories"]["rows_swept"] == 0
    assert r2["drafts"]["rows_discarded"] == 0
    assert _store_snapshot(tmp) == snap, "second apply wrote something"


# ── ⑦ single-source AST byte identity (the sweep copies can't drift) ───────

def test_sweep_tool_copies_ast_byte_identical():
    """The sweep tool's `_noise_table_hit` + `_no_popup_silent_discard`
    are VERBATIM copies of the plugin root's single source — assert AST
    source-segment identity (the S2 govern_memories precedent) so the
    sweep face and the live gate can never drift apart: the spec's
    「一致性：判定用新代码语义」 is the copied predicate itself."""
    plugin_src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    tool_src = (PLUGIN_DIR / "tools" / "sweep_low_value.py") \
        .read_text(encoding="utf-8")
    for name in ("_noise_table_hit", "_no_popup_silent_discard"):
        p_fn = next(
            n for n in ast.walk(ast.parse(plugin_src))
            if isinstance(n, ast.FunctionDef) and n.name == name)
        t_fn = next(
            n for n in ast.walk(ast.parse(tool_src))
            if isinstance(n, ast.FunctionDef) and n.name == name)
        p_seg = ast.get_source_segment(plugin_src, p_fn).strip()
        t_seg = ast.get_source_segment(tool_src, t_fn).strip()
        assert p_seg == t_seg, f"the {name} copy drifted from the plugin"


# ── ④a-e P3-2b leg ④: same-session draft-content dedupe + self-kill guard ──
# The 165735 root cause: a retry-loop re-wrote the SAME L2 analysis 10x and
# all 10 rows leaked past the P3-1 3-leg gate to the user's confirm queue.
# Leg ④ adds the missing "same-session repeated content" leg: the FIRST copy
# lands, every repeat is discarded with a `dedupe: true` audit row. The
# draft face only (l0 auto-save has its own dedup face, out of scope here),
# config-driven via no_popup.dedupe_in_session (default ON, user can opt out),
# and a `skip_id` guard so a draft's own second gate never mis-kills itself.

def _decay_dedupe_off() -> dict:
    """A decay block with the P3-2b dedupe leg explicitly off (knob off)."""
    d = dict(DECAY)
    np = dict(d.get("no_popup") or {})
    np["dedupe_in_session"] = False
    d["no_popup"] = np
    return d


def _draft_rows(p) -> list:
    """The in-memory session draft pool — the exact face the dedupe leg scans."""
    return p._session_drafts.get(p.session_id, [])


def test_dedupe_a_second_same_content_draft_discarded_with_dedupe_audit():
    """④a 165735 scenario repro: a retry-loop re-writes the SAME L2 analysis;
    the FIRST copy lands, the repeat is discarded by the NEW leg ④ with a
    `dedupe: true` audit row, and both the in-memory pool and the on-disk
    drafts file keep exactly ONE row."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    d1 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    assert d1, "the first copy must land (no dup yet)"
    d2 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    assert d2 == "", "the repeat must be discarded (no-op id)"
    assert len(_draft_rows(p)) == 1, "pool must keep exactly one row"
    assert len(_draft_file(p)) == 1, "drafts file must keep one row"
    aud = _audit_rows(p, "no_popup_discard")
    assert len(aud) == 1, f"exactly one dup audit row, got {len(aud)}"
    assert aud[0].get("dedupe") is True, "dup row must carry dedupe=true"
    # the 3 old-leg audit schema bytes are UNCHANGED on the dedupe row:
    for k in ("ts", "action", "kind", "char_len", "content_sha1",
              "session_id", "source_profile"):
        assert k in aud[0], f"dedupe row lost legacy field {k}"
    assert aud[0]["action"] == "no_popup_discard" and aud[0]["kind"] == "draft"
    # the first (surviving) draft is still the one queued for the panel:
    assert [e for e in _queue_rows(p) if e.get("kind") == "draft"]


def test_dedupe_b_different_content_both_land_zero_false_kill():
    """④b 零误杀: two DIFFERENT high-value drafts both land (leg ④ fires
    only on an exact stripped-content match) — zero dedupe audit rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    fact_b = ("a second durable fact, distinct content so the dedupe leg "
              "must NOT fire on it, padded long enough to clear min_chars")
    d1 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    d2 = p._add_draft(fact_b, level="L2")
    assert d1 and d2, "both distinct drafts must land"
    assert len(_draft_rows(p)) == 2
    assert len(_draft_file(p)) == 2
    assert _audit_rows(p, "no_popup_discard") == [], \
        "no dedupe audit for distinct content (零误杀)"


def test_dedupe_c_knob_off_repeats_release():
    """④c 开关可关: no_popup.dedupe_in_session=false releases repeats —
    two SAME-content drafts both land, zero dedupe audit rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=_decay_dedupe_off())
    d1 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    d2 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    assert d1 and d2, "knob off must release the repeat"
    assert len(_draft_rows(p)) == 2
    assert len(_draft_file(p)) == 2
    assert _audit_rows(p, "no_popup_discard") == []


def test_dedupe_d_l0_face_does_not_run_dedupe_leg():
    """④d scope 钉死: kind="l0" (the L0-auto save face) repeats do NOT run
    the dedupe leg — the l0 pool keeps its own dedup face; two same-content
    l0 rows both queue exactly as pre-P3-2b (zero new behavior on l0)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    p._push_pending_confirm("s1", "l0_a", "L0", HIGH_VALUE_FACT, kind="l0")
    p._push_pending_confirm("s1", "l0_b", "L0", HIGH_VALUE_FACT, kind="l0")
    q = _queue_rows(p)
    assert len(q) == 2, "l0 repeats must both queue (dedupe leg off for l0)"
    assert _audit_rows(p, "no_popup_discard") == []


def test_dedupe_e_skip_guard_first_draft_not_self_killed():
    """④e 防自杀: a LEGITIMATE first draft runs _add_draft's full path —
    gate#1 (skip_id=None, pool empty) passes, the item lands, then the
    SECOND gate (_push_pending_confirm → skip_id=item.id) must NOT count the
    just-appended row as a dup. The draft reaches the pending-confirm queue
    + the [记忆报告] panel: 1 queue entry, 1 pool row, 0 dedupe audit rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    d1 = p._add_draft(HIGH_VALUE_FACT, level="L2")
    assert d1, "a legitimate first draft must land (not self-killed)"
    assert len(_draft_rows(p)) == 1
    assert len(_draft_file(p)) == 1
    q = _queue_rows(p)
    drafts_q = [e for e in q if e.get("kind") == "draft"]
    assert len(drafts_q) == 1, "exactly one draft queue entry survives"
    assert drafts_q[0]["id"] == d1, "the queued entry must be this draft"
    panel = p._turn_report_line("s1")
    assert HIGH_VALUE_FACT[:40] in panel, "the panel must list the draft"
    assert _audit_rows(p, "no_popup_discard") == [], \
        "no self-kill: the first draft leaves no dedupe audit row"
