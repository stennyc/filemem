"""P4: structural source-cut (owner 2026-10-15: 垃圾直接静默丢弃，源头过滤).

The live pool census (owner review, 2026-10-15) proved two garbage classes
land in memories/*.json because the P0-1 ingress gate is NARROWER than M1's
structural doctrine:

  * M1 doctrine (single source `_NONPROSE_OPENERS`, shared with
    `_is_structural_noise` + M3 purge): a line that OPENS with `{` or `[`
    is a JSON-dump / room-transcript skeleton / system scaffolding —
    "never a durable user fact". But M1's gate only guarded the BATCH
    EXTRACT path. The auto-save / L0-fallback / draft faces ran only the
    P0-1 `ingress_reject_class`, whose raw-tool-output leg (b) knows just
    the 3-key dumps (`{"output":` / `{"content":` / `{"error":`) + the
    P0-3 config tables — so `{"status":` multi-key dumps and every
    `[Group chat …]` room skeleton sailed straight into the pool
    (census: 71 live room-skeleton rows + ~30 multi-key JSON dumps, all
    L0, many confirmed=True, all auto-injected).

  * kanban-block-relay notifications (`⏸ [default] @coder Kanban t_x …`,
    `✔ …`, `⏱ …`) carry no structural opener and no step-numbering — they
    were absent from every noise table (census: 41 live rows).

Fix (this commit, single vertical slice):
  ① WRITE face: `ingress_reject_class` gains a STRUCTURAL leg —
    `content.lstrip()[:1] in _NONPROSE_OPENERS` → reject class
    `structural_opener`. Code default: ALWAYS live, independent of the
    P0-3 config tables (same posture as the 3-key code leg and egress
    leg ① — a missing/corrupt decay block never disables this leg).
    Placed AFTER leg (b), so table-classified rows keep their
    `raw_tool_output` class (zero P0-3 regression); the new class catches
    exactly the `{[` rows the table missed.
  ② READ face: `_egress_noise_cut` gains structural leg ⑥ (the same
    `_NONPROSE_OPENERS` primitive, unconditional — the P2 leg ② length
    gate only caught >max_chars rows, so short room rows kept
    re-injecting; now every `{[`-opening row is cut off the
    auto-inject face regardless of length or confirmed status).
  ③ P2 canary exemption: `run_canary_turn`'s synthetic room meta turn
    (double-stamped self_test+canary, the self-test positive-leg audit
    dependency) is the ONLY structural row that still lands — the write
    gate passes `exempt_structural=True` during a canary turn.
  ④ TABLE extension (改表不改码, 零代码): the kanban relay prefixes
    (`⏸ [`, `✔ [`, `⏱ [`) join `egress_noise.tool_dump_openers`
    (decay.json + DECAY_DEFAULTS lock-step, the P0-3 parity contract) so
    the shared write-ingress / egress / no-popup faces all reject them.
  ⑤ One-off 存量 sweep: tools/sweep_structural_noise.py (archive-then-
    rewrite under the sibling-flock protocol, jsonl 留痕非删 + one
    consolidation_log audit row per archived row, dry-run default,
    idempotent).

Acceptance (spec):
  - `{"status": …}` / `[Group chat …]` / `[`-array rows: rejected on the
    WRITE face (0 pool rows + 1 quarantine + 1 ingress_reject audit row
    stamped structural_opener) under ANY cfg (None / {} / malformed —
    the structural leg is a code default, not a table leg).
  - the same rows are cut on the READ face by leg ⑥ (unconditional).
  - a fact that QUOTES `{`/`[` mid-sentence passes both faces (the
    line-opening test only — 零误杀).
  - the canary turn still lands its double-stamped item (P2 positive
    leg intact); NON-canary room turns land NOTHING.
  - kanban relay prefixes are rejected/cut via the shared table (with
    the shipped decay.json; cfg=None degrades the TABLE legs to
    pass-through by P0-1 design — the structural leg is unaffected).
  - all pre-P4 assertions stay green except the ones whose EXPECTED
    CLASS changes from "passes" to "structural_opener" (P0-3 ⑤/⑦,
    v2 self-test room-stamp face) — updated in place with their
    docstrings restated.
"""
import json, sys, pathlib, tempfile, threading, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES,
    ingress_reject_class, _egress_noise_cut, _NONPROSE_OPENERS,
)
from test_p0_3_noise_table import (  # noqa: E402
    _make_provider, _read_all, _read_quarantine, _audit_rows, DECAY,
)

# ── P4 fixtures ───────────────────────────────────────────────────────────────
MULTIKEY_DUMP = '{"status": "success", "output": "platforms:\\n    clawchat:"}'
ROOM_SKELETON = ('[Group chat: "T"] You are @coder, one participant in a '
                 "group chat. DevOps host config endpoint port 8080 verified.")
ARRAY_ROW = '[{"id": 1}, {"id": 2}]'
MID_TEXT_QUOTE = "把 `[1,2,3]` 改成 `[4]`，rotation config 的 port 是 8080."
KANBAN_PREFIXES = ("⏸ [", "✔ [", "⏱ [")

CANARY_CONTENT = ('[Group chat: "P4"] P2 v2 canary — synthetic meta turn. '
                  "DevOps host config endpoint port 8080 verified reachable.")


# ── ① WRITE face: the structural leg (code default, cfg-independent) ─────────

def test_structural_leg_rejects_multikey_dump_any_cfg():
    """a `{"status": …` multi-key JSON dump (a 3-key MISS, not in any
    P0-3 table) is rejected by the STRUCTURAL leg under EVERY cfg shape —
    the leg is a code default, not a table leg."""
    for cfg in (None, {}, {"egress_noise": "corrupted"}):
        assert ingress_reject_class(MULTIKEY_DUMP, cfg) == "structural_opener", \
            f"cfg {cfg!r}: the structural leg must stay live (code default)"


def test_structural_leg_rejects_room_skeleton_and_array():
    assert ingress_reject_class(ROOM_SKELETON) == "structural_opener"
    assert ingress_reject_class(ROOM_SKELETON, DECAY) == "structural_opener"
    assert ingress_reject_class(ARRAY_ROW) == "structural_opener"


def test_table_rows_keep_raw_tool_output_class():
    """leg ORDER: the P0-3 table leg (b) runs before the structural leg,
    so a row the shipped table classifies keeps its raw_tool_output
    reject class (zero P0-3 regression — P0-3 test ①/③ shapes)."""
    from test_p0_3_noise_table import (GAP_TOTAL_COUNT_ROW, GAP_SUCCESS_ROW,
                                        LEGACY_OUTPUT_DUMP)
    assert ingress_reject_class(GAP_TOTAL_COUNT_ROW, DECAY) == "raw_tool_output"
    assert ingress_reject_class(GAP_SUCCESS_ROW, DECAY) == "raw_tool_output"
    assert ingress_reject_class(LEGACY_OUTPUT_DUMP, DECAY) == "raw_tool_output"
    # …while the SAME row under cfg=None (table legs dead) falls to the
    # structural leg instead of passing:
    assert ingress_reject_class(GAP_TOTAL_COUNT_ROW, None) == "structural_opener"
    assert ingress_reject_class(GAP_SUCCESS_ROW, None) == "structural_opener"


def test_mid_text_quote_zero_false_kill():
    """a durable fact that QUOTES `[`/`{` mid-sentence passes the whole
    chain — only the line-opening test rejects (M1 doctrine, 零误杀)."""
    for cfg in (None, DECAY):
        assert ingress_reject_class(MID_TEXT_QUOTE, cfg) is None
        assert _egress_noise_cut(MID_TEXT_QUOTE, cfg) is False


# ── ② READ face: egress structural leg ⑥ (unconditional) ─────────────────────

def test_egress_structural_leg_cuts_short_and_confirmed_rows():
    """leg ⑥ cuts EVERY `{[`-opening row off the auto-inject face —
    short rows (the P2 length-gated leg ② blind spot) AND regardless of
    confirmed status (confirmed room rows are the exact garbage census)."""
    assert _egress_noise_cut(ROOM_SKELETON, None) is True
    assert _egress_noise_cut(ROOM_SKELETON, DECAY) is True
    assert _egress_noise_cut(MULTIKEY_DUMP, None) is True
    assert _egress_noise_cut(ARRAY_ROW, DECAY) is True
    # a mid-text quote survives the whole egress chain:
    assert _egress_noise_cut(MID_TEXT_QUOTE, DECAY) is False


# ── ③ provider faces: auto-save / draft / canary exemption ───────────────────

def test_auto_save_rejects_structural_rows():
    for row in (MULTIKEY_DUMP, ROOM_SKELETON, ARRAY_ROW):
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp, decay=DECAY)
        rid = p._save_memory(row, level="L0", source="auto")
        assert rid == "", f"structural row {row[:30]!r} must NOT write a pool row"
        assert _read_all(p) == [], "structural row leaked into the pool"
        q = _read_quarantine(p)
        assert len(q) == 1 and q[0]["content"] == row, "quarantine keeps the raw"
        aud = _audit_rows(p, action="ingress_reject")
        assert len(aud) == 1 and aud[0]["reject_class"] == "structural_opener"


def test_draft_door_rejects_structural_rows():
    for row in (MULTIKEY_DUMP, ROOM_SKELETON):
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp, decay=DECAY)
        rid = p._add_draft(row, level="L1")
        assert rid == "", "structural draft must not enter the draft file"
        assert _read_all(p) == []
        assert len(_read_quarantine(p)) == 1
        assert _audit_rows(p, action="ingress_reject")[0]["reject_class"] == \
            "structural_opener"


def test_clean_fact_still_lands_byte_stable():
    """the structural leg must not over-kill: a plain durable fact
    (no `{[` opener) still writes the pool untouched, under any cfg."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    rid = p._save_memory(MID_TEXT_QUOTE, level="L0", source="auto")
    assert rid
    rows = _read_all(p)
    assert [r["content"] for r in rows] == [MID_TEXT_QUOTE]
    assert _read_quarantine(p) == []


def test_canary_turn_still_lands_double_stamped():
    """the P2 self-test positive leg is the ONLY structural exemption:
    run_canary_turn's synthetic room turn still lands its
    self_test+canary double-stamped item (exempt_structural leg)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    res = p.run_canary_turn(tag="P4-CANARY")
    assert res["stamped"] is True, f"canary positive leg failed: {res}"
    assert res["items"], "canary item must land double-stamped on disk"
    # verify the on-disk row itself: double-stamp + structural skeleton
    # content (res["items"] carries only file/id, read the pool rows)
    found = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            for i in json.loads(f.read_text(encoding="utf-8")):
                md = i.get("metadata") or {}
                if md.get("canary") and md.get("self_test"):
                    found.append(i)
    assert found, "canary double-stamped row missing from the pool files"
    # and the structural content of the canary row is exactly the room
    # skeleton shape that ordinary turns now get rejected for:
    assert found[0]["content"].lstrip()[:1] in _NONPROSE_OPENERS


def test_non_canary_room_turn_lands_nothing():
    """P2 stamping face, new P4 semantics: an ordinary room meta turn is
    REJECTED at source (quarantine + audit), it no longer lands a
    self_test-stamped pool row (that design is what produced the 71-row
    census). The negative leg (count_untagged_room_markers) still works
    on pre-existing residue rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, decay=DECAY)
    p.sync_turn(ROOM_SKELETON, "",
                messages=[{"role": "user", "content": ROOM_SKELETON}])
    assert _read_all(p) == [], "ordinary room turn must land NOTHING (P4)"
    assert len(_read_quarantine(p)) == 1
    aud = _audit_rows(p, action="ingress_reject")
    assert len(aud) == 1 and aud[0]["reject_class"] == "structural_opener"
    # the self-test negative leg still counts seeded residue:
    from __init__ import MemoryItem
    rows = [MemoryItem(id="fact_99", type="fact",
                       content='[Group chat: "RESIDUE"] untagged room text port 8080',
                       level="L0", metadata={"source_profile": "test"},
                       created_at="1999-01-01T00:00:00").to_dict()]
    (tmp / "memories" / "fact.json").write_text(json.dumps(rows), encoding="utf-8")
    assert p.count_untagged_room_markers("1970-01-01T00:00:00") == 1


# ── ④ kanban relay prefixes: the shared TABLE face (改表不改码) ─────────────

def test_kanban_relays_rejected_via_table_with_shipped_decay():
    """with the shipped decay.json tables the kanban relay prefixes are a
    table hit on BOTH faces (write ingress leg b → raw_tool_output;
    egress leg ③). cfg=None degrades the table legs to pass-through by
    P0-1 design (the structural leg does NOT apply — `⏸` is not `{[`)."""
    for row in ("⏸ [default] @coder Kanban t_086371a0 blocked: Goal-mode "
                "worker exhausted",
                "✔ [default] @coder Kanban t_b9c3eea7 done — P1-4 filemem "
                "通知面 per-row 处置",
                "⏱ [default] @orchestrator Kanban t_6f6ac948 timed out "
                "(max_runtime=900)"):
        assert ingress_reject_class(row, DECAY) == "raw_tool_output", \
            f"kanban relay {row[:24]!r} must hit the shared opener table"
        assert _egress_noise_cut(row, DECAY) is True, \
            "the same table cuts the read face (one table, two faces)"
    for row in (KANBAN_PREFIXES):
        assert ingress_reject_class(row + "x", None) is None, \
            "cfg=None: the table legs are dead (P0-1 posture), no false reject"


def test_kanban_relays_rejected_on_provider_faces():
    for row in ("⏸ [default] @coder Kanban t_086371a0 blocked: x",
                "✔ [default] @coder Kanban t_b9c3eea7 done — x"):
        tmp = pathlib.Path(tempfile.mkdtemp())
        p = _make_provider(tmp, decay=DECAY)
        assert p._save_memory(row, level="L0", source="auto") == ""
        assert _read_all(p) == []
        assert _audit_rows(p, action="ingress_reject")[0]["reject_class"] == \
            "raw_tool_output"
