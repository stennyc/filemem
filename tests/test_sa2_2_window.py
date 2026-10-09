"""SA2-2 acceptance tests: time-window predicate (since/until, pure Python,
zero LLM, zero external deps).

Covers spec SA2-2 Acceptance:
  1. windowed queries return ONLY in-window items (fixture: out-of-window
     items must never surface, en + zh synthetic pools).
  2. window=None is byte-identical to the pre-SA2-2 path (regression gate,
     run on the live corpus: with-window-absent == explicit None == old
     call shape).
  3. en/zh fixture query sets show no regression (live-store smoke: a
     one-week window returns in-window results; a far-past window
     returns none).
Plus: occurred_at hit/miss/null-both fallbacks, one-sided since/until,
inclusive endpoints, empty-pool fail-soft, malformed-args ValueError,
tool schema surface, zero-LLM counter, import audit.
"""
import ast
import json
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, filter_pool_by_window, load_decay_config,
)
from datetime import datetime, timedelta, timezone  # noqa: E402

LIVE_STORE = Path.home() / ".hermes" / "filemem"


# ── synthetic pool helpers ─────────────────────────────────────────────────

def _make_provider(tmp: Path) -> FileMemProvider:
    """Partial provider over an empty tmpdir (no initialize(), no writes
    to live data — the search pool is seeded via memories/*.json)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = tmp / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = tmp / "knowledge" / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "sa2-2-test"
    p._decay = {}
    p._llm_ctx = None
    return p


def _item(iid: str, content: str, created_at: str,
          occurred_at: Any = None, level: str = "L2") -> Dict[str, Any]:
    """L2 = no TTL (ttl_days None -> never expires), so fixtures with old
    created_at dates (e.g. 2020) stay in the pool; only the
    occurred_at None-vs-value interplay is under test."""
    return {
        "id": iid, "type": "fact", "content": content,
        "level": level, "metadata": {},
        "created_at": created_at, "updated_at": created_at,
        "occurred_at": occurred_at, "source": "test",
        "reference_count": 0, "salience": 0.5,
    }


def _seed(tmp: Path, rows: List[Dict[str, Any]]) -> None:
    (tmp / "memories" / "fact.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _ids(results: List[Dict[str, Any]]) -> List[str]:
    return [r.get("id") for r in results]


# ── 1. occurred_at HIT governs the predicate (created_at irrelevant) ─────

def test_occurred_at_hit_inside_window_kept():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("a", "deploy of the build pipeline", "2020-01-01T00:00:00",
              "2026-09-10T00:00:00"),   # created 2020, event 2026-09-10
        _item("b", "demo incident log", "2026-09-01T00:00:00",
              "2026-09-01T00:00:00"),
    ])
    res = p.search_memories("pipeline", window={"since": "2026-09-01",
                                                "until": "2026-09-30"})
    assert "a" in _ids(res), f"occurred_at hit must pull in-window item: {res}"
    # event time outside the window -> out even though created_at is inside
    res2 = p.search_memories("demo", window={"since": "2026-09-05",
                                               "until": "2026-09-30"})
    assert "b" not in _ids(res2), \
        f"occurred_at 2026-09-01 must miss since=09-05: {res2}"


# ── 2. occurred_at MISS (None) falls back to created_at ───────────────────

def test_null_occurred_at_falls_back_to_created_at():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("in", "gateway config note", "2026-09-15T10:00:00", None),
        _item("out", "gateway cache note", "2026-08-01T00:00:00", None),
    ])
    res = p.search_memories("gateway", min_score=0.0,
                            window={"since": "2026-09-01",
                                   "until": "2026-09-30"})
    got = _ids(res)
    assert "in" in got, f"created_at fallback must keep in-window item: {res}"
    assert "out" not in got, f"created_at 2026-08-01 must miss the window: {res}"


# ── 3. no parseable timestamp at all -> dropped under a window ───────────

def test_no_timestamps_dropped_by_window():
    pool = [
        _item("x", "alpha fact", "garbage-timestamp", None),
        _item("y", "beta fact", "2026-09-10T00:00:00", None),
    ]
    kept = filter_pool_by_window(pool, {"since": "2026-09-01",
                                        "until": "2026-09-30"})
    assert [i["id"] for i in kept] == ["y"], \
        "unparseable ts cannot prove in-window; must be dropped, not raise"
    # ...but with NO window the item stays eligible (regression behavior):
    # window=None skips the filter entirely; an empty dict {..} has no bounds.
    kept_all = filter_pool_by_window(pool, {})
    assert [i["id"] for i in kept_all] == ["x", "y"], \
        "empty window dict = no bounds = keep all (window=None skips filter entirely)"


# ── 4. windowed query on an emptied pool returns [] (fail-soft, no crash) ─

def test_window_empties_pool_returns_empty_list():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [_item("only", "hermes gateway", "2026-09-10T00:00:00")])
    res = p.search_memories("hermes", window={"since": "2026-01-01",
                                              "until": "2026-02-01"})
    assert res == [], "in-window pool empty -> [] (never raise)"
    # legacy keyword-qualified path too (ranking='recency')
    res2 = p.search_memories("hermes", ranking="recency",
                            window={"since": "2026-01-01",
                                   "until": "2026-02-01"})
    assert res2 == []


# ── 5. one-sided windows ───────────────────────────────────────────────────

def test_one_sided_since_only():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("new", "flock rotation log", "2026-09-20T00:00:00"),
        _item("old", "flock startup log", "2026-07-01T00:00:00"),
    ])
    res = p.search_memories("flock", min_score=0.0,
                            window={"since": "2026-09-01"})
    got = _ids(res)
    assert "new" in got and "old" not in got, \
        f"since-only: upper bound open, {got}"


def test_one_sided_until_only():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("new", "flock rotation log", "2026-09-20T00:00:00"),
        _item("old", "flock startup log", "2026-07-01T00:00:00"),
    ])
    res = p.search_memories("flock", min_score=0.0,
                            window={"until": "2026-08-31"})
    got = _ids(res)
    assert "old" in got and "new" not in got, \
        f"until-only: lower bound open, {got}"


# ── 6. INCLUSIVE endpoints [since, until] ─────────────────────────────────

def test_inclusive_endpoints():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("lo", "edge lo", "2026-09-01T23:59:59"),
        _item("mid", "edge mid", "2026-09-15T00:00:00"),
        _item("hi", "edge hi", "2026-09-30T00:00:01"),
        _item("below", "edge below", "2026-08-31T23:59:59"),
        _item("above", "edge above", "2026-10-01T00:00:00"),
    ])
    res = p.search_memories("edge", min_score=0.0,
                            window={"since": "2026-09-01",
                                   "until": "2026-09-30"})
    assert sorted(_ids(res)) == ["hi", "lo", "mid"], \
        f"endpoints are INCLUSIVE (calendar date): {res}"


# ── 7. zh content + occurred_at extracted from 日期文本 ───────────────────

def test_zh_occurred_at_extraction_governs_window():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("zh1", "2026年9月5日的发布失败了", "2020-01-01T00:00:00",
              "2026-09-05T00:00:00"),
        _item("zh2", "上周的发布成功了 没有日期", "2026-09-28T00:00:00",
              None),
    ])
    res = p.search_memories("发布", min_score=0.0,
                            window={"since": "2026-09-01",
                                   "until": "2026-09-30"})
    got = _ids(res)
    assert set(got) == {"zh1", "zh2"}, \
        f"zh: CJK-date occurred_at hit + null fallback both in window: {res}"
    res2 = p.search_memories("发布", min_score=0.0,
                             window={"since": "2026-09-10"})
    assert set(_ids(res2)) == {"zh2"}, \
        f"zh1's 09-05 event must miss since=09-10: {res2}"


# ── 8. window=None regression gate: byte-identical to the old call shape ──

def test_window_none_matches_no_window_on_live_corpus():
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = LIVE_STORE / "memories"
    p.episodic_dir = LIVE_STORE / "episodic"
    p.sessions_dir = LIVE_STORE / "sessions"
    p.storage_dir = LIVE_STORE
    p.knowledge_dir = LIVE_STORE / "knowledge"
    p.knowledge_entries_path = LIVE_STORE / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = LIVE_STORE / "knowledge" / "raw_pending.json"
    p.pending_queue_path = LIVE_STORE / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "sa2-2-test"
    p._decay = load_decay_config()
    p._llm_ctx = None
    for q in ("hermes", "文件", "gateway", "hermes gateway 文件锁"):
        plain = p.search_memories(q, limit=1000, min_score=0.0)
        none_explicit = p.search_memories(q, limit=1000, min_score=0.0,
                                          window=None)
        # byte-level (json round-trip) identity, not just equality
        assert json.dumps(plain, ensure_ascii=False, sort_keys=True) == \
            json.dumps(none_explicit, ensure_ascii=False, sort_keys=True), \
            f"window=None must be byte-identical for {q!r}"
    assert len(p.search_memories("hermes", limit=5, min_score=0.0)) > 0, \
        "live corpus must actually be populated (gate precondition)"


# ── 9. live data smoke (read-only): a week window yields in-window hits ──

def test_live_week_window_smoke():
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = LIVE_STORE / "memories"
    p.episodic_dir = LIVE_STORE / "episodic"
    p.sessions_dir = LIVE_STORE / "sessions"
    p.storage_dir = LIVE_STORE
    p.knowledge_dir = LIVE_STORE / "knowledge"
    p.knowledge_entries_path = LIVE_STORE / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = LIVE_STORE / "knowledge" / "raw_pending.json"
    p.pending_queue_path = LIVE_STORE / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "sa2-2-test"
    p._decay = load_decay_config()
    p._llm_ctx = None
    # Dynamic one-week window: span the trailing 7 days of the corpus so
    # the smoke stays valid as the live store grows (read-only scan).
    live_ts = []
    for f in p.memories_dir.glob("*.json"):
        for it in json.loads(f.read_text(encoding="utf-8")):
            ts = it.get("created_at") or ""
            if ts:
                live_ts.append(ts)
    for shard in (p.episodic_dir.rglob("*.jsonl") if p.episodic_dir
                  and p.episodic_dir.exists() else []):
        for line in shard.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("ts") or ""
            if ts:
                live_ts.append(ts)
    assert live_ts, "live corpus must be populated (smoke precondition)"
    newest = max(live_ts)
    new_d = datetime.fromisoformat(newest)
    # naive (UTC convention of the store) or aware -> normalize to UTC date
    if new_d.tzinfo is None:
        new_d = new_d.replace(tzinfo=timezone.utc)
    win_hi = new_d.date()
    win_lo = win_hi - timedelta(days=7)
    # A real word that actually occurs in in-window content, so the legacy
    # substring gate has something to qualify (query-independent of content).
    in_window_content = []
    for f in p.memories_dir.glob("*.json"):
        for it in json.loads(f.read_text(encoding="utf-8")):
            eff = it.get("occurred_at") or it.get("created_at") or ""
            try:
                d = datetime.fromisoformat(eff).date()
            except ValueError:
                continue
            if win_lo <= d <= win_hi:
                in_window_content.append(it.get("content") or "")
    words = []
    for c in in_window_content:
        words.extend(re.findall(r"[a-z]{4,}", c.lower()))
    q = (max(words, key=words.count) if words else "the")
    res = p.search_memories(q, limit=1000, min_score=0.0,
                            window={"since": win_lo.isoformat(),
                                   "until": win_hi.isoformat()})
    assert len(res) > 0, f"one-week live window must return results for {q!r}"
    for r in res:
        eff = r.get("occurred_at") or r.get("created_at")
        assert eff, f"live result without any timestamp: {r.get('id')}"
        d = datetime.fromisoformat(eff).date()
        assert win_lo <= d <= win_hi, \
            f"live result {r.get('id')} effective date {d} outside the window"
    # a window far before the corpus must be empty (no out-of-window leak)
    far_past = p.search_memories(q, limit=1000, min_score=0.0,
                                 window={"until": "1990-01-01"})
    assert far_past == []


# ── 10. malformed window ARGS raise ValueError (arg error, not crash) ────

def test_malformed_window_args_raise_value_error():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [_item("a", "alpha", "2026-09-10T00:00:00")])
    for bad in ("", "2026-09-01", {"since": "09/01/2026"},
                {"since": "2026-13-01"}, {"since": "2026-02-30"},
                {"since": "2026-09-30", "until": "2026-09-01"},
                "not-a-dict", 12345, True):
        try:
            p.search_memories("alpha", window=bad)
            raise AssertionError(f"bad window {bad!r} must raise ValueError")
        except ValueError:
            pass  # tool layer converts to error JSON; host turn survives
    # valid no-op forms must NOT raise: None skips the filter, {} = no bounds
    assert _ids(p.search_memories("alpha", min_score=0.0)) == ["a"]
    assert _ids(p.search_memories("alpha", min_score=0.0, window={})) == ["a"]
    assert _ids(p.search_memories("alpha", min_score=0.0, window=None)) == ["a"]
    # the tool call surface proves it lands as an error result, not a 500
    out = p.handle_tool_call("filemem_search",
                             {"query": "alpha", "window": {"since": "bogus"}})
    assert "error" in json.loads(out)


# ── 11. tool schema exposes the window param (no misleading "semantic" tag)

def test_tool_schema_has_window_param():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    schemas = p.get_tool_schemas()
    s = next(x for x in schemas if x["name"] == "filemem_search")
    props = s["parameters"]["properties"]
    assert "window" in props, "filemem_search schema must expose window"
    assert set(props["window"]["properties"]) == {"since", "until"}
    # zero NEW misleading labels: the window param's own text must not
    # claim semantic similarity (the pre-existing tool description may
    # disclaim it — "not semantic similarity" — that is SA2-3's wording).
    win_desc = json.dumps(props["window"], ensure_ascii=False).lower()
    assert "since" in win_desc and "until" in win_desc
    assert "semantic" not in win_desc, \
        "window param text must not introduce a 'semantic' label"
    assert s["parameters"]["required"] == ["query"], \
        "window is optional — required list unchanged"


# ── 12. RRF runs over the FILTERED pool (window precedes axis build) ─────

def test_rrf_runs_on_filtered_pool_not_full_pool():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [
        _item("win-rare", "demo deploy log", "2026-09-10T00:00:00"),
        _item("win-common", "deploy notes", "2026-09-11T00:00:00"),
        _item("out-rare", "demo crash report", "2020-01-01T00:00:00"),
        _item("out-common", "deploy checklist", "2020-01-01T00:00:00"),
    ])
    res = p.search_memories("deploy", min_score=0.0,
                            window={"since": "2026-09-01",
                                   "until": "2026-09-30"})
    assert sorted(_ids(res)) == ["win-common", "win-rare"], \
        f"RRF must rank only the in-window pool (2 items): {res}"
    # and the out-of-window items must not leak in via idf either
    assert sorted(_ids(res)) == ["win-common", "win-rare"], \
        f"RRF must rank only the in-window pool (2 items): {res}"
    # 2-item filtered pool -> RRF normalized scores are exactly {1.0, 0.5}
    assert sorted(r["score"] for r in res) == [0.5, 1.0]


# ── 12b. EP-1 x SA2-2: the lineage backfill source is windowed too ─────────
def test_lineage_backfill_respects_window():
    """EP-1 split-then-backfill runs with the window predicate BEFORE the
    main pool filter, so the backfill source would escape the window
    (the backfill loop appends after scoring and bypasses the min_score
    gate). Fix under test: windowed calls window _lineage_superseded with
    the SAME predicate. Synthetic seed — no live-data dependency:
      in-old  : superseded=True, event 2020-01-01 (OUT of the 2026 window)
      in-new  : superseded=True, event 2026-09-10 (IN window)
      active  : superseded=False, event 2026-09-11 (main-pool item)
    window [2026-09-01, 2026-09-30], min_score=0.0, limit big enough
    that the backfill fires: 'in-new' must surface as lineage_expansion,
    'in-old' must NOT; with window=None both superseded rows backfill
    (legacy no-window behavior unchanged)."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    rows = [
        _item("in-old", "legacy pipeline config", "2020-01-01T00:00:00",
              "2020-01-01T00:00:00"),
        _item("in-new", "current pipeline config", "2026-09-10T00:00:00",
              "2026-09-10T00:00:00"),
        _item("active", "active pipeline config", "2026-09-11T00:00:00",
              "2026-09-11T00:00:00"),
    ]
    for r in rows:
        if r["id"] in ("in-old", "in-new"):
            r["metadata"] = {"superseded": True,
                             "superseded_by": "active"}
    _seed(tmp, rows)
    win = {"since": "2026-09-01", "until": "2026-09-30"}
    res = p.search_memories("pipeline", min_score=0.0, limit=100,
                            window=win)
    got = _ids(res)
    tags = {r.get("id"): r.get("lineage_expansion") for r in res}
    assert "in-new" in got, f"in-window superseded row must backfill: {res}"
    assert "in-old" not in got, \
        f"OUT-of-window superseded row must NOT escape the window: {res}"
    assert tags.get("in-new") is True, \
        f"backfilled row must carry lineage_expansion=True: {tags}"
    assert "active" in got, f"main-pool in-window item must surface: {got}"
    # window=None: the pre-fix behavior — BOTH superseded rows backfill
    res_none = p.search_memories("pipeline", min_score=0.0, limit=100)
    got_none = _ids(res_none)
    assert "in-old" in got_none and "in-new" in got_none, \
        f"window=None must backfill all superseded rows: {got_none}"


# ── 13. zero LLM on the windowed search path ──────────────────────────────

def test_zero_llm_window_search():
    import unittest.mock as mock
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed(tmp, [_item("a", "flock log", "2026-09-10T00:00:00")])
    p._llm_ctx = None
    with mock.patch("__init__._load_llm_caller") as m:
        res = p.search_memories("flock", window={"since": "2026-09-01",
                                                 "until": "2026-09-30"})
        assert m.call_count == 0, \
            "windowed search path must stay LLM-free (SA2 hard constraint)"
    assert _ids(res) == ["a"]


# ── 14. import audit: __init__.py window code path adds no new imports ───

def test_init_top_level_import_audit():
    """Top-level imports in __init__.py: stdlib only, plus the host agent
    surface (agent.*) — the SA2 hard constraint (zero external deps)."""
    tree = ast.parse(Path(__file__).resolve().parents[1].joinpath(
        "__init__.py").read_text(encoding="utf-8"))
    allowed = {
        "json", "math", "logging", "os", "re", "sys", "hashlib",
        "threading", "collections", "contextlib", "datetime", "pathlib",
        "typing", "__future__", "agent",
        "time",  # S1b F4/F5 throttle timers (_audit_tick_skip/_audit_recall)
        "uuid",  # P1-3/P2 audit trace_id + dedup ledger (_audit_explicit_search/_dedup_ledger)
    }
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                assert top in allowed, f"top-level import {top!r} not allowed"
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            assert top in allowed, f"top-level from-import {top!r} not allowed"


if __name__ == "__main__":
    for fn in (test_occurred_at_hit_inside_window_kept,
               test_null_occurred_at_falls_back_to_created_at,
               test_no_timestamps_dropped_by_window,
               test_window_empties_pool_returns_empty_list,
               test_one_sided_since_only, test_one_sided_until_only,
               test_inclusive_endpoints, test_zh_occurred_at_extraction_governs_window,
               test_window_none_matches_no_window_on_live_corpus,
               test_live_week_window_smoke,
               test_malformed_window_args_raise_value_error,
               test_tool_schema_has_window_param,
               test_rrf_runs_on_filtered_pool_not_full_pool,
               test_lineage_backfill_respects_window,
               test_zero_llm_window_search,
               test_init_top_level_import_audit):
        fn()
        print("ok", fn.__name__)
