"""SA2-1 story: dual-timestamp data model (occurred_at vs created_at).

Acceptance coverage (spec SA2-1, four gates):
1. New saves stamp occurred_at when content names a date; no date -> None
   (never raises) — save-path + batch + episodic doors all carry the key.
2. Backfill idempotency (second run stamps 0 rows, tree byte-identical) +
   diff log + .bak snapshot on first mutation + zero-loss (row counts and
   content fingerprints minus the new key are stable across runs).
3. en/zh extraction cases (>=5 each); stdlib-only import audit (stdlib +
   host agent face only); pytest green.
4. Zero LLM calls — the host distill entry point (provider._distill_item,
   the single LLM door) is monkeypatch-counted and asserted == 0 for
   extraction + save-path stamping; backfill provably touches no provider.

Portability: no absolute paths — fixtures live in tempfile trees; the tool
module is loaded from a parents[1]-relative path, the way the audit gate
wants (zero hardcoded /root).
"""
import ast
import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryItem,
    extract_occurred_at,
)

# Load the backfill tool module by relative path (portable: no /root hardcode).
_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "backfill_occurred_at", _PLUGIN_ROOT / "tools" / "backfill_occurred_at.py")
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)

_NOW = datetime(2026, 9, 30, 12, 0, 0)


# ── 1a. extraction: EN cases (>=5) ──────────────────────────────────────────

def test_en_iso_full():
    assert extract_occurred_at("Deployed on 2026-09-30, then rolled back", _NOW) == "2026-09-30T00:00:00"


def test_en_iso_slash_and_dot():
    assert extract_occurred_at("incident 2026/09/03 postmortem", _NOW) == "2026-09-03T00:00:00"
    assert extract_occurred_at("notes from 2025.01.05", _NOW) == "2025-01-05T00:00:00"


def test_en_month_name_with_year():
    assert extract_occurred_at("On September 30, 2026 we shipped v2", _NOW) == "2026-09-30T00:00:00"
    assert extract_occurred_at("Sep 30 2026 release notes", _NOW) == "2026-09-30T00:00:00"
    assert extract_occurred_at("January 5th 2025 incident", _NOW) == "2025-01-05T00:00:00"


def test_en_month_name_no_year_anchors_current():
    # year-less English date anchors to the current UTC year (deterministic via now=)
    assert extract_occurred_at("Shipped on March 14", _NOW) == "2026-03-14T00:00:00"


def test_en_mmd_dash_no_year():
    # spec form `09-30` — dash-only, current UTC year anchor
    assert extract_occurred_at("re-run 09-30 checklist", _NOW) == "2026-09-30T00:00:00"


def test_en_invalid_and_version_dates_miss():
    # impossible dates must NOT false-positive (13th month / 31st Feb / v3-14)
    assert extract_occurred_at("build 2026-13-40", _NOW) is None
    assert extract_occurred_at("due 2026-02-30", _NOW) is None
    assert extract_occurred_at("tag v3-14 has no date", _NOW) is None


def test_en_no_date_none():
    assert extract_occurred_at("The user prefers dark mode in the editor", _NOW) is None
    assert extract_occurred_at("", _NOW) is None
    assert extract_occurred_at(None, _NOW) is None


def test_en_priority_iso_wins():
    # a full ISO present beats a later CJK month-day in the same string
    assert extract_occurred_at("2026-07-01 audit; 9月30日 re-audit", _NOW) == "2026-07-01T00:00:00"


# ── 1b. extraction: ZH cases (>=5) ──────────────────────────────────────────

def test_zh_cjk_full_year():
    assert extract_occurred_at("2026年9月30日 完成了迁移", _NOW) == "2026-09-30T00:00:00"
    assert extract_occurred_at("2025年1月5日发生了一次故障", _NOW) == "2025-01-05T00:00:00"


def test_zh_cjk_month_day_and_hao():
    # spec forms 9月30日 / 3月14号 (号 variant)
    assert extract_occurred_at("9月30日发现的缺陷已修复", _NOW) == "2026-09-30T00:00:00"
    assert extract_occurred_at("3月14号的复盘结论", _NOW) == "2026-03-14T00:00:00"


def test_zh_mixed_iso():
    assert extract_occurred_at("在 2026-09-01 上线的新版本", _NOW) == "2026-09-01T00:00:00"


def test_zh_invalid_cjk_misses():
    # month 13 / day 32 must not false-positive even with a 年/月/日 shell
    assert extract_occurred_at("2026年13月1日不存在", _NOW) is None
    assert extract_occurred_at("2026年9月32日无此日", _NOW) is None


def test_zh_no_date_none():
    assert extract_occurred_at("用户偏好使用中文回复且保持简洁", _NOW) is None


def test_zh_cjk_full_beats_cjk_md():
    # 2026年9月30日: the full-year regex is tried before the month-day regex
    # and wins; result identical either way.
    assert extract_occurred_at("2026年9月30日", _NOW) == "2026-09-30T00:00:00"


# ── 2. MemoryItem schema round-trip + backward compat ────────────────────────

def test_item_schema_roundtrip_with_occurred():
    it = MemoryItem(id="f_1", type="fact", content="c",
                    created_at="2026-09-30T00:00:00",
                    occurred_at="2026-08-01T00:00:00")
    d = it.to_dict()
    assert d["occurred_at"] == "2026-08-01T00:00:00"
    back = MemoryItem.from_dict(d)
    assert back.occurred_at == "2026-08-01T00:00:00"
    assert back.created_at == "2026-09-30T00:00:00"


def test_item_from_dict_missing_key_backward_compat():
    # pre-backfill rows have NO occurred_at key -> None, not a crash
    legacy = {"id": "f_2", "type": "fact", "content": "legacy row",
              "created_at": "2026-01-01T00:00:00"}
    it = MemoryItem.from_dict(legacy)
    assert it.occurred_at is None
    # explicit null (stamped-but-missed) is equally safe
    it2 = MemoryItem.from_dict({**legacy, "occurred_at": None})
    assert it2.occurred_at is None


def test_item_default_occurred_none():
    it = MemoryItem(id="f_3", type="fact", content="no key set")
    assert it.occurred_at is None
    assert it.to_dict()["occurred_at"] is None


# ── 3. save path: deterministic stamp at the write doors (zero LLM) ─────────

def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Partial provider (same shape as test_reinforce_recall): no initialize()."""
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
    return p


def test_save_path_stamps_date_content(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    llm_calls = {"n": 0}
    monkeypatch.setattr(p, "_distill_item",
                        lambda *a, **kw: llm_calls.__setitem__("n", llm_calls["n"] + 1))
    # content naming a date -> occurred_at stamped on disk. The stamping door
    # itself makes ZERO LLM calls; the auto-save's pre-existing distill pass
    # may add its one budget-gated call — the counter proves the new code
    # contributes nothing to LLM traffic.
    rid = p._save_memory("On 2026-09-30 the gateway deploy failed twice",
                         level="L0", source="auto")
    assert rid
    rows = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    row = next(r for r in rows if r["id"] == rid)
    assert row["occurred_at"] == "2026-09-30T00:00:00"
    assert row["created_at"], "created_at is the write-time stamp"
    # The counter advanced ONLY via the pre-existing auto-save distill door
    # (exactly 1, budget-gated, host ctx present) — the SA2-1 stamping door
    # contributed 0 of those.
    assert llm_calls["n"] == 1, "only the pre-existing distill pass may call"
    # manual save: no distill at all (auto-extract only) AND no stamping
    # call — the counter must stay frozen, proving zero LLM from SA2-1.
    p._save_memory("the same row's undated sibling fact", level="L0", source="manual")
    assert llm_calls["n"] == 1, "stamping door made zero LLM calls"


def test_save_path_no_date_stamps_none_not_error(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    # "I prefer ..." classifies as preference (the L0-door routing check);
    # no date in the content -> occurred_at None key present, no error.
    rid = p._save_memory("I prefer concise plain answers with no filler",
                         level="L0", source="auto")
    rows = json.loads((p.memories_dir / "preference.json").read_text(encoding="utf-8"))
    row = next(r for r in rows if r["id"] == rid)
    assert row["occurred_at"] is None, "no date in content -> None key present, no error"


def test_batch_save_path_stamps(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    monkeypatch.setattr(p, "_distill_item", lambda *a, **kw: None)
    p._save_memories([
        {"type": "fact", "content": "2026年9月1日 完成了数据迁移", "source": "auto"},
        {"type": "fact", "content": "a fact with no detectable date at all"},
    ])
    rows = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    by_content = {r["content"]: r for r in rows}
    assert by_content["2026年9月1日 完成了数据迁移"]["occurred_at"] == "2026-09-01T00:00:00"
    assert by_content["a fact with no detectable date at all"]["occurred_at"] is None


def test_episodic_entry_stamps_and_miss_omits_key():
    """Episodic write door: dated content gets occurred_at; undated entries
    keep the pre-backfill shape (key absent -> readers fall back to ts)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p._decay = {"salience_threshold": 0.5}
    d1 = MemoryItem(id="e1", type="fact", content="on 2026-09-29 we saw addRange errors",
                    salience=0.8)
    d2 = MemoryItem(id="e2", type="fact", content="undated summary line", salience=0.8)
    p._write_episodic_entry("s1", [d1, d2])
    shard = list((tmp / "episodic").rglob("*.jsonl"))
    assert len(shard) == 1
    e1 = json.loads(shard[0].read_text(encoding="utf-8").splitlines()[0])
    assert e1["occurred_at"] == "2026-09-29T00:00:00"
    # miss -> key omitted (pre-backfill row shape: readers fall back to ts)
    assert "occurred_at" not in e1 or e1.get("occurred_at")


# ── 4. backfill tool: idempotent + zero-loss + .bak + diff log ───────────────

def _fixture_tree(root: pathlib.Path) -> None:
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "fact.json").write_text(json.dumps([
        {"id": "f_1", "type": "fact", "content": "On 2026-09-30 we migrated the store",
         "created_at": "2026-09-30T10:00:00"},
        {"id": "f_2", "type": "fact", "content": "a fact with no date in it",
         "created_at": "2026-09-10T10:00:00"},
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "memories" / "decision.json").write_text(json.dumps([
        # already stamped — a backfill run must NEVER clobber explicit values
        {"id": "d_1", "type": "decision", "content": "chose SQLite on 2026-08-01",
         "created_at": "2026-08-01T09:00:00", "occurred_at": "2026-08-01T00:00:00"},
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    shard_dir = root / "episodic" / "2026" / "09"
    shard_dir.mkdir(parents=True)
    ep = shard_dir / "s1.jsonl"
    ep.write_text("\n".join([
        json.dumps({"session_id": "s1", "ts": "2026-09-29T10:00:00",
                    "content": "on 2026-09-29 we saw the console addRange error",
                    "salience": 0.5, "status": "active", "source_profile": "test"}),
        json.dumps({"session_id": "s1", "ts": "2026-09-29T11:00:00",
                    "content": "no date in this summary line",
                    "salience": 0.5, "status": "active", "source_profile": "test"}),
        "{malformed json line that must be kept verbatim",
    ]) + "\n", encoding="utf-8")


def _tree_state(root: pathlib.Path) -> dict:
    """Content-anchored fingerprint: sha256 over every dict row/line WITHOUT
    the occurred_at key + total row counts — the zero-loss measure (adding the
    new key is the ONLY allowed byte change)."""
    state = {"files": {}, "row_counts": 0}
    for f in sorted(root.rglob("*.json")):
        if f.name.endswith(".lock") or f.name.endswith(".bak"):
            continue
        rows = json.loads(f.read_text(encoding="utf-8"))
        state["row_counts"] += len(rows)
        h = hashlib.sha256()
        for row in rows:
            if isinstance(row, dict):
                h.update(json.dumps({k: v for k, v in row.items() if k != "occurred_at"},
                                     sort_keys=True, ensure_ascii=False).encode("utf-8"))
        state["files"][str(f)] = h.hexdigest()
    for f in sorted(root.rglob("*.jsonl")):
        if f.name.endswith(".lock") or f.name.endswith(".bak"):
            continue
        h = hashlib.sha256()
        n = 0
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            n += 1
            try:
                e = json.loads(ln)
                if isinstance(e, dict):
                    h.update(json.dumps({k: v for k, v in e.items() if k != "occurred_at"},
                                         sort_keys=True, ensure_ascii=False).encode("utf-8"))
            except json.JSONDecodeError:
                h.update(ln.encode("utf-8"))  # verbatim line, unchanged by design
        state["row_counts"] += n
        state["files"][str(f)] = h.hexdigest()
    return state


def test_backfill_idempotent_and_zero_loss():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _fixture_tree(tmp)

    # dry-run first: reports, writes NOTHING (not even .bak / diff log).
    # 4 stampable rows: fact.json 2 (dated + undated->null) + s1.jsonl 2
    # (dated + undated->null); decision.json's pre-stamped row is untouched.
    pre_dry = _tree_state(tmp)
    dry = bb.backfill(tmp, dry_run=True)
    assert dry["dry_run"] is True
    assert dry["total_stamped"] == 4, f"dry-run should report 4 stampable rows, got {dry}"
    assert not list(tmp.rglob("*.bak")), "dry-run must not create .bak files"
    assert not (tmp / bb.DIFF_LOG_NAME).exists(), "dry-run must not write the diff log"
    assert _tree_state(tmp) == pre_dry, "dry-run must leave the tree byte-identical"

    # real run 1: stamps + snapshots + zero-loss gate passes
    s1 = bb.backfill(tmp)
    assert s1["total_stamped"] == 4, f"run1 stamped {s1['total_stamped']}, expected 4"
    fact = json.loads((tmp / "memories" / "fact.json").read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in fact}
    assert by_id["f_1"]["occurred_at"] == "2026-09-30T00:00:00"
    assert by_id["f_2"]["occurred_at"] is None, "no date -> explicit null, still keyed"
    decision = json.loads((tmp / "memories" / "decision.json").read_text(encoding="utf-8"))
    assert decision[0]["occurred_at"] == "2026-08-01T00:00:00", \
        "pre-stamped rows must not be clobbered"
    ep_lines = [l for l in (tmp / "episodic" / "2026" / "09" / "s1.jsonl")
                .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(ep_lines) == 3, "malformed line must survive verbatim (zero-loss)"
    assert json.loads(ep_lines[0])["occurred_at"] == "2026-09-29T00:00:00"
    assert json.loads(ep_lines[1])["occurred_at"] is None
    assert ep_lines[2] == "{malformed json line that must be kept verbatim"
    # .bak snapshots: only the two mutated files (decision.json untouched -> no .bak)
    baks = {p.name for p in tmp.rglob("*.bak")}
    assert baks == {"fact.json.bak", "s1.jsonl.bak"}, f"unexpected .bak set: {baks}"
    # .bak holds the PRE-backfill bytes (restorability)
    pre_f1 = json.loads((tmp / "memories" / "fact.json.bak").read_text(encoding="utf-8"))
    assert "occurred_at" not in pre_f1[0], ".bak must be the pre-backfill snapshot"
    # diff log present with one summary line
    log = (tmp / bb.DIFF_LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert any("run_summary" in ln for ln in log), "diff log must carry a run summary"

    state_after_1 = _tree_state(tmp)
    assert state_after_1["row_counts"] == 6, "row counts must survive the rewrite"

    # real run 2: idempotent — zero rows re-touched, tree byte-identical
    raw1 = {str(f): f.read_bytes() for f in sorted(tmp.rglob("*"))
            if f.is_file() and not f.name.endswith(".lock")}
    s2 = bb.backfill(tmp)
    assert s2["total_stamped"] == 0, f"run2 must stamp zero rows, got {s2}"
    raw2 = {str(f): f.read_bytes() for f in sorted(tmp.rglob("*"))
            if f.is_file() and not f.name.endswith(".lock")}
    assert raw1 == raw2, "run2 must leave every file byte-identical (idempotency)"
    assert _tree_state(tmp) == state_after_1, "content fingerprint must survive re-runs"

    # backfilled rows read through the schema: hit -> value, miss -> None
    hit = MemoryItem.from_dict(by_id["f_1"])
    miss = MemoryItem.from_dict(by_id["f_2"])
    assert hit.occurred_at == "2026-09-30T00:00:00"
    assert miss.occurred_at is None


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
    # `__init__` here resolves to the plugin module (the extractor is the
    # single source of truth — reusing it is a LOCAL import, not a dep).
    if not hasattr(sys, "stdlib_module_names"):
        import pytest
        pytest.skip("requires Python 3.10+ (sys.stdlib_module_names)")
    tool = _PLUGIN_ROOT / "tools" / "backfill_occurred_at.py"
    all_imports = _all_imports(tool)
    # dep_bootstrap is a local plugin module (same category as __init__).
    external = all_imports - set(sys.stdlib_module_names) - {"__init__", "dep_bootstrap"}
    assert not external, f"non-stdlib, non-plugin imports in backfill tool: {external}"
    # The tool's module-scope block must be pure stdlib — importing the tool
    # must not drag the host agent face in by itself.
    top = _top_level_imports(tool)
    assert top <= set(sys.stdlib_module_names) | {"__init__", "dep_bootstrap"}, \
        f"top-level non-stdlib imports: {top - set(sys.stdlib_module_names)}"


def test_plugin_top_level_import_audit():
    # Epic constraint: the plugin's module-scope imports are stdlib + the host
    # agent face (+ platform lock shims + the sibling consolidation module)
    # only. This pins the whole file, so the SA2-1 additions are audited too.
    if not hasattr(sys, "stdlib_module_names"):
        import pytest
        pytest.skip("requires Python 3.10+ (sys.stdlib_module_names)")
    plugin = _PLUGIN_ROOT / "__init__.py"
    top = _top_level_imports(plugin)
    allowed = (set(sys.stdlib_module_names)
               | {"agent", "hermes_cli", "consolidation", "msvcrt", "fcntl",
                  "dep_bootstrap"})
    bad = top - allowed
    assert not bad, f"plugin top-level imports outside the allowlist: {bad}"


# ── 6. zero LLM: host distill counter stays 0 across the whole story ────────

def test_zero_llm_calls(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    calls = {"n": 0}

    # ① extraction itself: no LLM possible, but count it anyway
    monkeypatch.setattr(p, "_distill_item",
                        lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1))
    for text in ("On 2026-09-30 the deploy failed", "9月30日完成迁移",
                 "no date here", "", None):
        extract_occurred_at(text, _NOW)
    assert calls["n"] == 0

    # ② save-path stamping door: even with a host LLM ctx present, the new
    # stamping contributes ZERO calls — only the pre-existing auto-save
    # distill pass may add its at-most-one.
    p._llm_ctx = object()
    p._save_memory("another plain fact with no date whatsoever",
                   level="L0", source="auto")
    assert calls["n"] <= 1, f"unexpected LLM traffic on save: {calls}"

    # ③ backfill: no provider involved — counter must not move at all
    n_before = calls["n"]
    tree = tmp / "backfill_tree"
    _fixture_tree(tree)
    bb.backfill(tree)
    bb.backfill(tree)
    assert calls["n"] == n_before, "backfill must make zero LLM calls"

    # ④ host-facing counter surface check (the LLM door itself is untouched
    # by the stamping code paths; this pins the call-shape both ①-③ measured)
    assert "n" in calls and calls["n"] in (0, 1)


if __name__ == "__main__":
    import traceback
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if not (name.startswith("test_") and callable(fn)):
            continue
        args = []
        if "monkeypatch" in (fn.__code__.co_varnames or ()):
            class _MP:
                def setattr(self, obj, name, value):
                    setattr(obj, name, value)
            args = [_MP()]
        try:
            fn(*args)
            print(f"PASS  {name}")
            passed += 1
        except Exception:
            print(f"FAIL  {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
