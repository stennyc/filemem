"""P0-3: 噪声指纹表扩 3 确定性类 + 写侧 ingress 切同表（P0-1 保守零误杀）.

The P0-1 write-side gate's raw_tool_output leg only recognized the 3-key
heads (`{"output"` / `{"content"` / `{"error"` via the module-level
_looks_like_tool_dump single source of truth) + the
`<untrusted_tool_result` framing prefix. A live read-only probe of the
shared store (2026-10-05 03:16) found three deterministic gap shapes that
leaked into memories/*.json:

  - `{"total_count":` heads (search_files raw dump, ×24)
  - `{"success":` heads (patch/terminal echo, ×38)
  - a process-fragment anchor ("搞清楚了。我把情况说明清楚…", ×1)

P0-3 extends the config-driven egress-noise fingerprint tables in
decay.json (`egress_noise` block, 改表不改码) and wires the WRITE face
onto the SAME tables, so ingress reject and egress drop can never drift
apart:

  1. decay.json: `egress_noise.tool_dump_openers` en/zh += the two new
     JSON-opener heads; NEW key `egress_noise.fragment_prefixes`
     (multilingual table, STARTSWITH semantics — 锚点保留=全句开头,
     deliberately NO substring matching, 零误杀).
  2. `_egress_noise_cut` gains leg ⑤ (fragment_prefixes startswith, same
     shape as ③, table-driven; missing/malformed block = fail-soft
     pass-through of that leg; ① the 3-key opener stays always-live and
     unchanged).
  3. write-side `ingress_reject_class` gains `decay_cfg: Optional[Dict]`
     (default None): the raw_tool_output leg is now 「3 键恒活 (the code
     default, ZERO regression) OR the shared tool_dump_openers /
     fragment_prefixes tables」. The caller `_ingress_rejection` passes
     `getattr(self, "_decay", None)`. Superset gate: every row the P0-1
     3-key gate rejected is STILL rejected.
  4. Zero LLM, pure stdlib (startswith/len); host source files are
     untouched; `_looks_like_tool_dump` stays the 3-key single source.

Acceptance gate (spec) — the seven legs below:
  ① the three gap-class rows are ingress-rejected (quarantined, 0 pool
     rows, an ingress_reject audit row)
  ② `_egress_noise_cut` drops the same table rows (one table, two faces)
  ③ the P0-1 3-key legacy rows are STILL rejected (superset, zero
     regression)
  ④ a normal fact row is byte-stable (零误杀)
  ⑤ a malformed table fails open (a broken block never takes the gate
     down, and never raises)
  ⑥ a fragment anchor contained MID-TEXT in a normal row is NOT killed
     (startswith semantics)
  ⑦ decay_cfg=None behaves exactly like the P0-1 code-only gate
     (backward compatibility)
"""
import json, sys, pathlib, tempfile, threading, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES,
    ingress_reject_class, _looks_like_tool_dump, _egress_noise_cut,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402


# ── config source: the SHIPPED decay.json tables (改表不改码 means the ───
# ── tests exercise the actual shipped table, not a synthetic copy) ────────

def _load_decay() -> dict:
    """The plugin's shipped decay.json — the single egress-noise table
    both faces read (load-time merge with DECAY_DEFAULTS, the same
    posture as the provider's load_decay_config)."""
    p = pathlib.Path(__file__).resolve().parents[1] / "decay.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    merged = dict(DECAY_DEFAULTS)
    for k, v in (data or {}).items():
        merged[k] = v
    return merged


DECAY = _load_decay()


# ── P0-1 fixtures (legacy 3-key rows — the zero-regression baseline) ──────
LEGACY_OUTPUT_DUMP = '{"output": "browser console dump body", "exit_code": 0}'
LEGACY_CONTENT_DUMP = '{"content": "raw tool content body", "took_ms": 412}'
LEGACY_ERROR_DUMP = '{"error": "probe timed out after 30s", "code": 408}'
TOOL_FRAMED_DUMP = (
    '<untrusted_tool_result tool="web_search" call_id="1" status="ok">\n'
    '{"output": "https://x.example/page", "results": 3}\n'
    '</untrusted_tool_result>'
)
# clean, durable facts (zero-误杀 legs)
CLEAN_FACT = "the dev host config endpoint runs on port 8080"
MID_TEXT_ANCHOR_ROW = (
    "the 4d crawler ledger notes said 搞清楚了。我把情况说明清楚 "
    "about the retry backoff policy before the operator shift"
)


# ── P0-3 gap-class fixtures (the three deterministic shapes the live probe
#    found in the store; P0-1 let all three through) ───────────────────────
GAP_TOTAL_COUNT_ROW = (
    '{"total_count": 24, "matches": ["/root/.hermes/plugins/filemem/__init__.py"]}'
)
GAP_SUCCESS_ROW = (
    '{"success": true, "diff_applied": true, "lines": 12, "path": "__init__.py"}'
)
GAP_FRAGMENT_ROW = "搞清楚了。我把情况说明清楚了，写侧 ingress 现在切到同一张表。"


# ── provider harness (P0-1 test shape, with the P0-3 table wired in) ──────

def _make_provider(tmpdir: pathlib.Path, decay=None) -> FileMemProvider:
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


# ── ① the three gap-class rows are ingress-rejected (quarantine + audit) ─

def test_ingress_rejects_three_gap_classes_with_tables():
    """① with the P0-3 tables wired (provider _decay = shipped decay.json):
    each of the three gap shapes is kept OUT of the pool — 0 pool rows,
    exactly one quarantine row stamped raw_tool_output, one ingress_reject
    audit row (the P0-1 side-effects, now reached via the table leg)."""
    for row in (GAP_TOTAL_COUNT_ROW, GAP_SUCCESS_ROW, GAP_FRAGMENT_ROW):
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp, decay=DECAY)
        rid = p._save_memory(row, level="L0", source="auto")
        assert rid == "", "gap-class row must NOT write a pool row"
        assert _read_all(p) == [], "gap-class row leaked into the pool"
        q = _read_quarantine(p)
        assert len(q) == 1 and q[0]["content"] == row
        assert q[0]["reason"].startswith("P0-1 ingress reject-class:")
        assert q[0]["reason"].endswith("raw_tool_output")
        aud = _audit_rows(p, action="ingress_reject")
        assert len(aud) == 1 and aud[0]["reject_class"] == "raw_tool_output"


# ── ② the egress cut drops the same table rows (one table, two faces) ────

def test_egress_noise_cut_drops_gap_rows_via_same_table():
    """② egress face: the SAME rows the ingress gate now rejects are also
    cut by _egress_noise_cut under the shipped tables — the two faces
    agree because they read one table."""
    for row in (GAP_TOTAL_COUNT_ROW, GAP_SUCCESS_ROW, GAP_FRAGMENT_ROW):
        assert _egress_noise_cut(row, DECAY) is True, \
            f"egress cut must drop the table row {row[:40]!r}"
    # and clean rows are NOT cut by either face
    assert _egress_noise_cut(CLEAN_FACT, DECAY) is False
    assert _egress_noise_cut(MID_TEXT_ANCHOR_ROW, DECAY) is False


# ── ③ superset: the P0-1 3-key legacy rows are STILL rejected ─────────────

def test_three_key_legacy_rows_still_rejected_superset():
    """③ zero regression: the code-default 3-key heads + the framed prefix
    remain rejected WITH the tables present (the table leg is an
    OR-extension, never a replacement). The ingress face rejects all
    four; the egress cut additionally drops the two new JSON-opener
    table rows (the pre-existing `{"output"` head was always an egress
    ①/③ hit, so the three-gap rows are all table-extensions on that face)."""
    for dump in (LEGACY_OUTPUT_DUMP, LEGACY_CONTENT_DUMP, LEGACY_ERROR_DUMP,
                 TOOL_FRAMED_DUMP):
        assert ingress_reject_class(dump, DECAY) == "raw_tool_output"
        assert _looks_like_tool_dump(dump) or \
            dump.startswith("<untrusted_tool_result")
    # the P0-3 table rows are dropped by the egress cut under the shipped
    # tables (spec ②: same table, two faces)
    for row in (GAP_TOTAL_COUNT_ROW, GAP_SUCCESS_ROW, GAP_FRAGMENT_ROW):
        assert _egress_noise_cut(row, DECAY) is True
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    assert p._ingress_rejection(LEGACY_OUTPUT_DUMP) == "raw_tool_output"
    assert _read_all(p) == [] and len(_read_quarantine(p)) == 1
    assert _audit_rows(p, action="ingress_reject")[0]["reject_class"] == \
        "raw_tool_output"


# ── ④ normal fact rows are byte-stable (零误杀) ───────────────────────────

def test_normal_fact_row_byte_stable_zero_false_kill():
    """④ a durable fact row passes the whole chain untouched: no pool
    mutation, no quarantine, no ingress_reject audit row; byte-identical
    content lands in the pool."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    rid = p._save_memory(CLEAN_FACT, level="L0", source="auto")
    assert rid, "a clean fact must write a pool row"
    rows = _read_all(p)
    assert len(rows) == 1
    assert rows[0]["content"] == CLEAN_FACT  # byte-stable
    assert _read_quarantine(p) == []
    assert _audit_rows(p, action="ingress_reject") == []
    # and the clean fact is not an egress cut either
    assert _egress_noise_cut(CLEAN_FACT, DECAY) is False


# ── ⑤ malformed tables fail open (never raise, ①-keys stay live) ─────────

def test_malformed_table_fails_open():
    """⑤ a broken block shape degrades the TABLE legs to "no hit" without
    raising and without taking down the sibling legs: (a) a malformed
    table lets gap-class rows through the table legs (fail-open), while
    the 3-key legacy rows are STILL rejected by the always-live code
    default; (b) the same malformed tables fail open on the egress table
    legs; (c) the host-instruction class is unaffected by any table
    shape.

    P4 note (2026-10-15): the structural leg (b2) and the egress
    structural leg ⑥ are CODE DEFAULTS, not table legs — a malformed
    table does NOT disable them. A `{[`-opening gap row is therefore
    rejected as structural_opener (not "None" as pre-P4) even with a
    broken cfg; the pre-P4 assertion "table row passes with malformed
    cfg" now means "passes the table legs, still caught by the
    structural leg". The 3-key/legacy/framed rows keep their
    raw_tool_output class (code default ①), and clean prose facts still
    pass everything."""
    malformed = [
        {"egress_noise": "corrupted"},  # block not a dict
        {"egress_noise": {"tool_dump_openers": ["not", "a", "dict"]}},
        {"egress_noise": {"fragment_prefixes": {"zh": "搞清楚了。"}}},
        {"egress_noise": {"tool_dump_openers": {"en": "Fetch finished"}}},
        {"egress_noise": {"fragment_prefixes": None}},
        None,
    ]
    for cfg in malformed:
        # the table legs are dead, the 3-key code default is ALWAYS live
        assert ingress_reject_class(LEGACY_OUTPUT_DUMP, cfg) == "raw_tool_output"
        # P4: the `{[` gap rows now reject via the CODE-DEFAULT structural
        # leg regardless of table shape (pre-P4: None with malformed cfg)
        assert ingress_reject_class(GAP_TOTAL_COUNT_ROW, cfg) == \
            "structural_opener"
        assert ingress_reject_class(GAP_SUCCESS_ROW, cfg) == "structural_opener"
        # the CJK fragment row (no `{[` opener) still passes with the
        # table legs dead — proving the structural leg does not over-kill
        assert ingress_reject_class(GAP_FRAGMENT_ROW, cfg) is None
        # egress: the TABLE legs fail open, but the code-default legs
        # (① 3-key + ⑥ structural) stay live on the same rows
        assert _egress_noise_cut(GAP_SUCCESS_ROW, cfg) is True, \
            "P4 leg ⑥: a {/[ opener is cut even with a malformed cfg"
        assert _egress_noise_cut(GAP_FRAGMENT_ROW, cfg) is False
    # a clean host block rejects regardless of table shape
    host_block = ("[CONTEXT COMPACTION — REFERENCE ONLY]\n"
                  "the earlier turns were compacted into this block")
    assert ingress_reject_class(host_block, {"egress_noise": "corrupted"}) == \
        "host_instruction_block"


# ── ⑥ fragment anchor mid-text is NOT killed (startswith semantics) ──────

def test_fragment_anchor_mid_text_not_killed():
    """⑥ STARTSWITH semantics: a normal row that merely CONTAINS the
    fragment anchor mid-text passes both faces — no substring matching,
    零误杀; only a row that OPENS the anchor is caught."""
    assert not MID_TEXT_ANCHOR_ROW.startswith("搞清楚了。我把情况说明清楚")
    assert ingress_reject_class(MID_TEXT_ANCHOR_ROW, DECAY) is None
    assert _egress_noise_cut(MID_TEXT_ANCHOR_ROW, DECAY) is False
    # contrast: the row that OPENS the anchor IS caught by both faces
    assert ingress_reject_class(GAP_FRAGMENT_ROW, DECAY) == "raw_tool_output"
    assert _egress_noise_cut(GAP_FRAGMENT_ROW, DECAY) is True
    # ...and it lands in the provider's quarantine, not the pool
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    assert p._save_memory(MID_TEXT_ANCHOR_ROW, level="L0", source="auto")
    assert [r["content"] for r in _read_all(p)] == [MID_TEXT_ANCHOR_ROW]
    assert _read_quarantine(p) == []


# ── ⑦ decay_cfg=None = P0-1 code-only behavior (backward compatible) ──────

def test_decay_cfg_none_is_p0_1_code_only_behavior():
    """⑦ backward compatibility: with no decay config consulted the gate is
    the CODE-ONLY behavior — the P0-1 code legs (3-key dump / framed
    prefix / host block / task prompt / relay) are live, and the P4
    structural leg (b2) is live too: it is a code default, not a table
    leg (same posture as the 3-key default — a missing decay block never
    disables it). (A provider whose _decay block simply lacks the tables
    — e.g. the P0-1 fixture `{"lambda_days": 0.05}` — degrades to the
    same posture: no table hits, code legs only.)

    P4 note (2026-10-15): pre-P4 this test asserted the `{[`-opening gap
    rows PASS with cfg=None (table-extension only). Post-P4 they reject
    via the code-default structural leg — the source-cut semantics the
    owner ruled (垃圾在源头丢弃)."""
    # module-level, decay_cfg omitted entirely
    assert ingress_reject_class(GAP_TOTAL_COUNT_ROW) == "structural_opener"
    assert ingress_reject_class(GAP_SUCCESS_ROW) == "structural_opener"
    # the CJK fragment row: no `{[` opener, table legs dead with cfg=None
    # -> the P0-1 code-only posture still lets it through (table-only hit)
    assert ingress_reject_class(GAP_FRAGMENT_ROW) is None
    assert ingress_reject_class(LEGACY_OUTPUT_DUMP) == "raw_tool_output"
    assert ingress_reject_class(TOOL_FRAMED_DUMP) == "raw_tool_output"
    # and an empty config block (no tables at all) behaves identically
    assert ingress_reject_class(GAP_TOTAL_COUNT_ROW, {}) == "structural_opener"
    assert ingress_reject_class(GAP_SUCCESS_ROW, {}) == "structural_opener"
    # provider face: P0-1-style fixture decay (no egress_noise block)
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)  # default {"lambda_days": 0.05}, no tables
    assert p._ingress_rejection(GAP_SUCCESS_ROW) == "structural_opener", \
        "no table block -> code-default structural leg still rejects"
    assert p._ingress_rejection(LEGACY_CONTENT_DUMP) == "raw_tool_output", \
        "the 3-key default stays live even without any table"


# ── config parity: decay.json <-> DECAY_DEFAULTS on the P0-3 keys ──────────

def test_decay_json_and_defaults_p0_3_table_parity():
    """the shipped decay.json and DECAY_DEFAULTS carry the identical
    P0-3 tables (改表: BOTH files extend in lock-step, mirroring the S2
    egress_noise key-parity contract) — the two new tool_dump_openers
    heads on en AND zh, and the new fragment_prefixes multilingual key."""
    decay_json = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "decay.json")
        .read_text(encoding="utf-8"))
    assert decay_json["egress_noise"] == DECAY_DEFAULTS["egress_noise"]
    for src, dst in ((decay_json, "decay.json"),
                     (DECAY_DEFAULTS, "DECAY_DEFAULTS")):
        en = src["egress_noise"]
        assert "{\"total_count\":" in en["tool_dump_openers"]["en"]
        assert "{\"success\":" in en["tool_dump_openers"]["zh"]
        assert "Fetch finished" in en["tool_dump_openers"]["en"]
        assert set(en["fragment_prefixes"].keys()) == {"en", "zh"}
        for lang in ("en", "zh"):
            assert "搞清楚了。我把情况说明清楚" in en["fragment_prefixes"][lang]
            # first-batch identical: en == zh for the P0-3 additions
            assert en["tool_dump_openers"][lang] == \
                en["tool_dump_openers"]["en"]


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
