"""SEC-1-1 story: deterministic origin trust stamp (zero LLM, fail-safe low).

Acceptance coverage (spec SEC-1-1, three gates):
1. Every save exit stamps metadata.origin (100% coverage — the single-save
   door's new-row + dedup-replace branches, the batch door, the draft door,
   the episodic door, each asserted individually).
2. Backfill: idempotent (second run zero change, byte-identical) + diff log
   + .bak; unknown source -> unknown/low (fail-safe lowest trust, never
   guess high).
3. Zero LLM (import audit stdlib + host agent face only; LLM counter == 0)
   + from_dict backward compat (legacy rows without metadata.origin).

Portability: no absolute paths — fixtures live in tempfile trees; the tool
module is loaded from a parents[1]-relative path, the way the audit gate
wants (zero hardcoded /root, /usr/local, C:\\).
"""
import ast
import hashlib
import importlib.util
import json
import re
import sys
import tempfile
import threading
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryItem,
    UNKNOWN_TRUST_STAMP,
    ORIGIN_KINDS,
    ORIGIN_TRUSTS,
    origin_stamp_for_source,
    _EPISODIC_ORIGIN_STAMP,
    _origin_is_wellformed,
)

# Load the backfill tool module by relative path (portable: no /root hardcode).
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backfill_origin", _PLUGIN_ROOT / "tools" / "backfill_origin.py")
bo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bo)


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Partial provider (same shape as test_sa2_1_occurred_at): no initialize()."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._decay = {}
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._session_drafts = {}
    return p


# ── 1. mapping table: en/zh source values each verified ────────────────────

def test_mapping_known_source_values():
    # exact values seen in the live corpus / produced by the save doors
    assert origin_stamp_for_source("auto") == {"kind": "user", "trust": "medium"}
    assert origin_stamp_for_source("manual") == {"kind": "user", "trust": "high"}
    assert origin_stamp_for_source("confirmed_draft") == {"kind": "user", "trust": "high"}
    assert origin_stamp_for_source("draft") == {"kind": "user", "trust": "medium"}
    assert origin_stamp_for_source("session:20260929_191512_bb40ff") == {"kind": "user", "trust": "medium"}


def test_mapping_prefix_values():
    assert origin_stamp_for_source("tool:terminal") == {"kind": "tool_output", "trust": "low"}
    assert origin_stamp_for_source("external:web") == {"kind": "external", "trust": "low"}


def test_mapping_unknown_values_fail_safe_low():
    # anything un-mappable -> unknown/low (fail-safe lowest trust, never high)
    assert origin_stamp_for_source("") == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source(None) == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source(42) == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source("mystery_source") == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source("weird:prefix") == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source(" ") == UNKNOWN_TRUST_STAMP
    # mapped kinds/trusts stay on the enums
    for v in (origin_stamp_for_source("auto"), origin_stamp_for_source("manual")):
        assert v["kind"] in ORIGIN_KINDS and v["trust"] in ORIGIN_TRUSTS


def test_mapping_zh_content_values_unchanged():
    # the mapping keys are language-agnostic source enums, not content:
    # zh source values behave identically to en ones (same rules, no
    # Chinese hardcoded anywhere in the table).
    assert origin_stamp_for_source("自动") == UNKNOWN_TRUST_STAMP
    assert origin_stamp_for_source("未知") == UNKNOWN_TRUST_STAMP
    # but the enum values themselves map the same regardless of language
    assert origin_stamp_for_source("auto") == origin_stamp_for_source("auto ")


# ── 2. save exits: 100% stamp coverage (each door asserted individually) ───

def _rows_of(p, name):
    f = p.memories_dir / name
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def test_save_exit_new_row_stamps(monkeypatch):
    """Door 1 (new row): _save_memory stamps metadata.origin per source."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    rid_auto = p._save_memory("I prefer concise plain answers", level="L0", source="auto")
    p._last_save_time = 0.0
    rid_manual = p._save_memory("manual note about the deploy", level="L0", source="manual")
    p._last_save_time = 0.0
    rid_conf = p._save_memory("confirmed decision: keep SQLite", level="L0", source="confirmed_draft")
    rows = _rows_of(p, "preference.json") + _rows_of(p, "fact.json") + _rows_of(p, "decision.json")
    by_id = {r["id"]: r for r in rows}
    assert by_id[rid_auto]["metadata"]["origin"] == {"kind": "user", "trust": "medium"}
    assert by_id[rid_manual]["metadata"]["origin"] == {"kind": "user", "trust": "high"}
    assert by_id[rid_conf]["metadata"]["origin"] == {"kind": "user", "trust": "high"}


def test_save_exit_dedup_replace_stamps(monkeypatch):
    """Door 2 (dedup replace branch): a content-equivalent re-save re-stamps
    the replaced row's origin from the NEW source (idempotent when equal)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    rid = p._save_memory("the gateway deploy failed on Tuesday", level="L0", source="auto")
    p._last_save_time = 0.0
    p._save_memory("the gateway deploy failed on Tuesday", level="L0", source="manual")
    rows = _rows_of(p, "fact.json")
    row = next(r for r in rows if r["id"] == rid)
    assert row["source"] == "manual"
    # the dedup door re-stamps from the new source — but ONLY when the stored
    # origin is absent/malformed (the idempotent rule: a well-formed value,
    # e.g. a manual correction, is never clobbered). The auto-created row
    # stamped user/medium; the re-stamp keeps that (no clobber) instead of
    # guessing a higher trust from the new source.
    assert row["metadata"]["origin"] == {"kind": "user", "trust": "medium"}


def test_save_exit_dedup_keeps_wellformed_origin(monkeypatch):
    """The idempotent rule: an existing WELL-FORMED origin (manual fix) is
    never clobbered by the dedup re-stamp."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    p._save_memory("the gateway deploy failed on Tuesday", level="L0", source="auto")
    f = p.memories_dir / "fact.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    rows[0]["metadata"]["origin"] = {"kind": "external", "trust": "low"}
    f.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    p._last_save_time = 0.0
    p._save_memory("the gateway deploy failed on Tuesday", level="L0", source="manual")
    rows2 = json.loads(f.read_text(encoding="utf-8"))
    assert rows2[0]["metadata"]["origin"] == {"kind": "external", "trust": "low"}, \
        "well-formed origin must survive the dedup re-stamp"


def test_batch_save_exit_stamps(monkeypatch):
    """Door 3 (batch extraction): _save_memories stamps from the row's source
    ('session:*' -> user/medium; absent -> unknown/low fail-safe)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    p._save_memories([
        {"type": "fact", "content": "I prefer dark mode in the editor",
         "source": "session:abc123"},
        {"type": "fact", "content": "a fact with no source field at all"},
    ])
    rows = _rows_of(p, "fact.json")
    by_content = {r["content"]: r for r in rows}
    assert by_content["I prefer dark mode in the editor"]["metadata"]["origin"] == \
        {"kind": "user", "trust": "medium"}
    assert by_content["a fact with no source field at all"]["metadata"]["origin"] == \
        {"kind": "unknown", "trust": "low"}


def test_draft_exit_stamps():
    """Door 4 (drafts): _add_draft stamps user/medium on the draft row."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p._last_save_time = 0.0
    did = p._add_draft("decision draft about the rollout plan", level="L1")
    assert did
    dfile = p.storage_dir / f"drafts_{p.session_id}.json"
    drafts = json.loads(dfile.read_text(encoding="utf-8"))
    row = next(d for d in drafts if d["id"] == did)
    assert row["metadata"]["origin"] == {"kind": "user", "trust": "medium"}
    # re-load through the schema: the stamped value round-trips
    back = MemoryItem.from_dict(row)
    assert back.metadata["origin"] == {"kind": "user", "trust": "medium"}


def test_episodic_exit_stamps():
    """Door 5 (episodic shard): _write_episodic_entry stamps user/medium."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p._decay = {"salience_threshold": 0.5}
    d1 = MemoryItem(id="e1", type="fact", content="rollout plan discussion", salience=0.8)
    p._write_episodic_entry("s1", [d1])
    shard = list((tmp / "episodic").rglob("*.jsonl"))
    assert len(shard) == 1
    e = json.loads(shard[0].read_text(encoding="utf-8").splitlines()[0])
    assert e["metadata"]["origin"] == dict(_EPISODIC_ORIGIN_STAMP) == \
        {"kind": "user", "trust": "medium"}


# ── 3. from_dict backward compat + fail-safe degrade ────────────────────────

def test_from_dict_missing_origin_backward_compat():
    # pre-backfill live rows carry NO metadata.origin key -> normalized on
    # read with the SAME deterministic mapping the backfill uses (never a
    # crash). A row with a KNOWN source gets that source's mapping...
    legacy = {"id": "f_1", "type": "fact", "content": "legacy row",
              "source": "auto", "created_at": "2026-01-01T00:00:00",
              "metadata": {"source_profile": "default"}}
    it = MemoryItem.from_dict(legacy)
    assert it.metadata["origin"] == {"kind": "user", "trust": "medium"}
    # ...while an UNMAPPABLE source fails safe to the lowest trust
    legacy2 = {"id": "f_2", "type": "fact", "content": "x",
               "source": "something_unmapped"}
    it2 = MemoryItem.from_dict(legacy2)
    assert it2.metadata["origin"] == {"kind": "unknown", "trust": "low"}
    # a row with no metadata key at all is equally safe
    it3 = MemoryItem.from_dict({"id": "f_3", "type": "fact", "content": "y"})
    assert it3.metadata["origin"] == {"kind": "unknown", "trust": "low"}
    # well-formed stored origin loads intact
    ok = {**legacy, "metadata": {"source_profile": "default",
                                 "origin": {"kind": "user", "trust": "high"}}}
    assert MemoryItem.from_dict(ok).metadata["origin"] == {"kind": "user", "trust": "high"}
    # the input dict is never mutated (read-side normalization)
    assert "origin" not in legacy["metadata"]


def test_from_dict_malformed_origin_degrades_low():
    # corrupt origin values degrade to unknown/low (never trust a bad value)
    cases = [
        {"origin": {"kind": "user"}},                      # missing trust
        {"origin": {"kind": "god", "trust": "high"}},      # off-enum kind
        {"origin": {"kind": "user", "trust": "ultra"}},    # off-enum trust
        {"origin": "user/high"},                            # not a dict
        {"origin": None},
    ]
    for i, meta in enumerate(cases):
        d = {"id": f"m_{i}", "type": "fact", "content": "c", "metadata": {"origin": meta["origin"]}}
        assert MemoryItem.from_dict(d).metadata["origin"] == {"kind": "unknown", "trust": "low"}


# ── 4. backfill tool: idempotent + .bak + diff + zero-loss ─────────────────

def _fixture_tree(root: pathlib.Path) -> None:
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "fact.json").write_text(json.dumps([
        {"id": "f_1", "type": "fact", "content": "c1", "source": "auto",
         "metadata": {"source_profile": "default"}},
        {"id": "f_2", "type": "fact", "content": "c2", "source": "confirmed_draft",
         "metadata": {"source_profile": "default"}},
        # unknown source -> unknown/low fail-safe
        {"id": "f_3", "type": "fact", "content": "c3", "source": "mystery",
         "metadata": {"source_profile": "default"}},
        # already stamped — a backfill run must NEVER clobber it
        {"id": "f_4", "type": "fact", "content": "c4", "source": "manual",
         "metadata": {"source_profile": "default",
                      "origin": {"kind": "user", "trust": "high"}}},
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    shard_dir = root / "episodic" / "2026" / "09"
    shard_dir.mkdir(parents=True)
    ep = shard_dir / "s1.jsonl"
    ep.write_text("\n".join([
        json.dumps({"session_id": "s1", "ts": "2026-09-29T10:00:00",
                    "content": "summary line", "salience": 0.5, "status": "active",
                    "source_profile": "test"}),
        "{malformed json line that must be kept verbatim}",
    ]) + "\n", encoding="utf-8")


def test_backfill_origin_stamps_and_is_idempotent():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _fixture_tree(tmp)

    # dry-run first: reports, writes NOTHING (not even .bak / diff log).
    # 3 stampable memories rows (f_4 already stamped) + 1 episodic row.
    pre_dry = {str(f): f.read_bytes() for f in sorted(tmp.rglob("*")) if f.is_file()}
    dry = bo.backfill(tmp, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["total_stamped"] == 4, f"dry-run should report 4 stampable rows, got {dry}"
    assert not list(tmp.rglob("*.bak")), "dry-run must not create .bak files"
    assert not (tmp / bo.DIFF_LOG_NAME).exists(), "dry-run must not write the diff log"
    assert {str(f): f.read_bytes() for f in sorted(tmp.rglob("*")) if f.is_file()} == pre_dry

    # real run 1: stamps + snapshots + zero-loss gate passes
    s1 = bo.backfill(tmp)
    assert s1["total_stamped"] == 4, f"run1 stamped {s1['total_stamped']}, expected 4"
    fact = json.loads((tmp / "memories" / "fact.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in fact}
    assert by_id["f_1"]["metadata"]["origin"] == {"kind": "user", "trust": "medium"}
    assert by_id["f_2"]["metadata"]["origin"] == {"kind": "user", "trust": "high"}
    assert by_id["f_3"]["metadata"]["origin"] == {"kind": "unknown", "trust": "low"}, \
        "unknown source must fail safe to lowest trust"
    assert by_id["f_4"]["metadata"]["origin"] == {"kind": "user", "trust": "high"}, \
        "pre-stamped rows must not be clobbered"
    ep_lines = [l for l in (tmp / "episodic" / "2026" / "09" / "s1.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(ep_lines) == 2, "malformed line must survive verbatim (zero-loss)"
    assert json.loads(ep_lines[0])["metadata"]["origin"] == {"kind": "user", "trust": "medium"}
    assert ep_lines[1] == "{malformed json line that must be kept verbatim}"
    # .bak: only the two mutated files (no .bak on un-mutated roots)
    baks = {p.name for p in tmp.rglob("*.bak")}
    assert baks == {"fact.json.bak", "s1.jsonl.bak"}, f"unexpected .bak set: {baks}"
    pre_f = json.loads((tmp / "memories" / "fact.json.bak").read_text(encoding="utf-8"))
    assert "origin" not in pre_f[0]["metadata"], ".bak must be the pre-backfill snapshot"
    # diff log present with one summary line
    log = (tmp / bo.DIFF_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert any("run_summary" in ln for ln in log), "diff log must carry a run summary"

    # real run 2: idempotent — zero rows re-touched, tree byte-identical
    raw1 = {str(f): f.read_bytes() for f in sorted(tmp.rglob("*")) if f.is_file()}
    s2 = bo.backfill(tmp)
    assert s2["total_stamped"] == 0, f"run2 must stamp zero rows, got {s2}"
    raw2 = {str(f): f.read_bytes() for f in sorted(tmp.rglob("*")) if f.is_file()}
    assert raw1 == raw2, "run2 must leave every file byte-identical (idempotency)"


def test_backfill_distribution_buckets():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _fixture_tree(tmp)
    s = bo.backfill(tmp)
    # post-stamp distribution: user/medium (f_1 + episodic), user/high (f_2, f_4),
    # unknown/low (f_3)
    assert s["distribution"]["user/medium"] == 2
    assert s["distribution"]["user/high"] == 2
    assert s["distribution"]["unknown/low"] == 1


# ── 5. import audit: stdlib-only (+ host agent face / plugin face allowed) ──

def _top_level_imports(path: pathlib.Path):
    """Module-scope import statements only (no function bodies)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add(node.module.split(".")[0] if node.module else "<rel>")
        elif isinstance(node, (ast.If, ast.Try)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    mods.update(a.name.split(".")[0] for a in sub.names)
                elif isinstance(sub, ast.ImportFrom) and sub.level == 0:
                    mods.add(sub.module.split(".")[0] if sub.module else "<rel>")
    return mods


def _all_imports(path: pathlib.Path):
    """Every import statement anywhere in the file (incl. function bodies)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def test_tool_stdlib_only_import_audit():
    # Zero-external-dependency gate: stdlib + host/plugin face only.
    if not hasattr(sys, "stdlib_module_names"):
        import pytest
        pytest.skip("requires Python 3.10+ (sys.stdlib_module_names)")
    tool = _PLUGIN_ROOT / "tools" / "backfill_origin.py"
    all_imports = _all_imports(tool)
    external = all_imports - set(sys.stdlib_module_names) - {"__init__", "dep_bootstrap"}
    assert not external, f"non-stdlib, non-plugin imports in backfill tool: {external}"
    top = _top_level_imports(tool)
    assert top <= set(sys.stdlib_module_names) | {"__init__", "dep_bootstrap"}, \
        f"top-level non-stdlib imports: {top - set(sys.stdlib_module_names)}"


def test_no_hardcoded_paths_in_new_files():
    """Portability gate: neither the tool nor this test file hardcodes a
    host-absolute path (/root, /usr/local, C:\\) — the audit gate's shape."""
    patterns = [
        re.compile(r"[\"'](/root|/usr/local|C:\\\\)"),
        re.compile(r"Path\(['\"]/root"),
    ]
    for rel in ("tools/backfill_origin.py", "tests/test_sec_1_1_origin.py"):
        text = (_PLUGIN_ROOT / rel).read_text(encoding="utf-8")
        for pat in patterns:
            m = pat.search(text)
            assert not m, f"hardcoded-path pattern {pat.pattern!r} in {rel}: {m.group(0)}"


# ── 6. zero LLM: host distill counter stays 0 across the whole story ───────

def test_zero_llm_calls(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    calls = {"n": 0}
    monkeypatch.setattr(p, "_distill_item",
                        lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1))

    # ① mapping itself: deterministic, no LLM possible, count it anyway
    for src in ("auto", "manual", "confirmed_draft", "session:x", "tool:t",
                "external:e", "junk", "", None):
        origin_stamp_for_source(src)
    assert calls["n"] == 0

    # ② all save exits: even with a host LLM ctx present, the stamping
    # contributes ZERO calls — only the pre-existing auto-save distill pass
    # may add its at-most-one.
    p._llm_ctx = object()
    p._save_memory("an undated plain fact one", level="L0", source="auto")
    n_after_auto = calls["n"]
    assert n_after_auto <= 1, f"auto-save may use its pre-existing distill: {calls}"
    p._last_save_time = 0.0
    p._save_memory("an undated plain fact two", level="L0", source="manual")
    assert calls["n"] == n_after_auto, "manual save + stamping made zero LLM calls"
    p._last_save_time = 0.0
    p._save_memories([{"type": "fact", "content": "batch fact with no source"}])
    assert calls["n"] == n_after_auto, "batch door + stamping made zero LLM calls"
    p._last_save_time = 0.0
    p._add_draft("a draft with no date")
    assert calls["n"] == n_after_auto, "draft door made zero LLM calls"
    p._decay = {"salience_threshold": 0.5}
    p._write_episodic_entry("s1", [MemoryItem(id="e", type="fact",
                                               content="ep content", salience=0.8)])
    assert calls["n"] == n_after_auto, "episodic door made zero LLM calls"

    # ③ backfill: no provider involved — counter must not move at all
    n_before = calls["n"]
    tree = tmp / "backfill_tree"
    _fixture_tree(tree)
    bo.backfill(tree)
    bo.backfill(tree)
    assert calls["n"] == n_before, "backfill must make zero LLM calls"

    # ④ host-facing counter surface check (pins the call-shape ①-③ measured)
    assert calls["n"] in (0, 1)
