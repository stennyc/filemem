"""mem_type filter tests (统一召回、分类做成参数): search_memories gains an
optional memory-type slice, the filemem_search tool schema gains a `type`
param, and _tool_search threads it through.

Structure mirrors tests/test_sa2_2_window.py (same tmp-isolated provider
helper, seeded synthetic pools, zero LLM, zero writes to live data).

Covers:
  1. type omitted / type == "all" -> byte-identical to the pre-mem_type
     full pool (memories shards + episodic + knowledge extensions).
  2. type == "fact" -> only fact.json rows surface; episodic rows and
     knowledge rows are excluded from the sliced pool.
  3. type == "preference" -> only preference.json rows surface.
  4. invalid type (e.g. "bogus") -> ValueError at the API layer; through
     handle_tool_call('filemem_search', ...) it lands as an error JSON
     result (never a 500 — the host turn survives).
  5. tool schema surface: filemem_search properties carry `type` with
     enum ["all"] + MEMORY_TYPES; required is still ["query"].
  6. RRF runs over the SLICED pool (filter before axis construction):
     with type="fact" the pool_size shrinks and normalized scores are
     computed over the small pool (same shape as the SA2-2 window test).
"""
import json
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402


# ── synthetic pool helpers (mirrors test_sa2_2_window.py L37-80) ──────────

def _make_provider(tmp: Path) -> FileMemProvider:
    """Partial provider over an empty tmpdir (no initialize(), no writes
    to live data — the search pool is seeded via memories/*.json +
    episodic/*.jsonl + knowledge/entries.json)."""
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
    p.source_profile = "type-filter-test"
    p._decay = {}
    p._llm_ctx = None
    return p


def _item(iid: str, content: str, created_at: str = "2026-09-10T00:00:00",
          mtype: str = "fact", level: str = "L2") -> Dict[str, Any]:
    """L2 = no TTL (never expires), so old fixtures stay in the pool."""
    return {
        "id": iid, "type": mtype, "content": content,
        "level": level, "metadata": {},
        "created_at": created_at, "updated_at": created_at,
        "occurred_at": None, "source": "test",
        "reference_count": 0, "salience": 0.5,
    }


def _seed_by_type(tmp: Path, rows: List[Dict[str, Any]]) -> None:
    """Extended _seed: write rows to the shard of their OWN type
    (memories/<type>.json), not a single fact.json."""
    shards: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        shards.setdefault(r.get("type", "fact"), []).append(r)
    for t, rows_t in shards.items():
        (tmp / "memories" / f"{t}.json").write_text(
            json.dumps(rows_t, ensure_ascii=False), encoding="utf-8")


def _seed_episodic(tmp: Path, iid: str, content: str,
                   ts: str = "2026-09-12T00:00:00") -> None:
    """Seed one non-fixture episodic shard row (normalized to
    type='episodic' by _load_episodic_pool)."""
    shard_dir = tmp / "episodic" / "2026" / "09"
    shard_dir.mkdir(parents=True, exist_ok=True)
    row = {"id": iid, "ts": ts, "content": content, "salience": 0.5,
           "session_id": "s-type-filter-test", "fixture": False}
    with open(shard_dir / "shard.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _seed_knowledge(tmp: Path, iid: str, content: str,
                    created_ts: str = "2026-09-13T00:00:00") -> None:
    """Seed one active knowledge entry (normalized to type='knowledge',
    tier-boosted by the pool loader)."""
    entry = {"id": iid, "content": content, "status": "active",
             "created_ts": created_ts}
    (tmp / "knowledge" / "entries.json").write_text(
        json.dumps([entry], ensure_ascii=False), encoding="utf-8")


def _ids(results: List[Dict[str, Any]]) -> List[str]:
    return [r.get("id") for r in results]


def _full_pool_fixtures(tmp: Path) -> None:
    """One row per face: two fact shard rows, one preference shard row,
    one episodic extension row, one knowledge extension row — all
    matching query 'flock' so every face is keyword-eligible."""
    _seed_by_type(tmp, [
        _item("fact-a", "flock rare alpha", mtype="fact"),
        _item("fact-b", "flock common beta", mtype="fact"),
        _item("pref-a", "flock preference gamma", mtype="preference"),
    ])
    _seed_episodic(tmp, "epi-a", "flock episodic delta")
    _seed_knowledge(tmp, "know-a", "flock knowledge epsilon")


# ── 1. omitted / "all" -> byte-identical full pool (regression gate) ─────

def test_type_omitted_and_all_full_pool_regression_gate():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _full_pool_fixtures(tmp)
    for q in ("flock", "alpha", "knowledge"):
        omitted = p.search_memories(q, limit=100, min_score=0.0)
        explicit_all = p.search_memories(q, limit=100, min_score=0.0,
                                         mem_type="all")
        explicit_none = p.search_memories(q, limit=100, min_score=0.0,
                                          mem_type=None)
        # byte-level (json round-trip) identity, not just equality
        assert json.dumps(omitted, ensure_ascii=False, sort_keys=True) == \
            json.dumps(explicit_all, ensure_ascii=False, sort_keys=True), \
            f"mem_type='all' must be byte-identical for {q!r}"
        assert json.dumps(omitted, ensure_ascii=False, sort_keys=True) == \
            json.dumps(explicit_none, ensure_ascii=False, sort_keys=True), \
            f"mem_type=None must be byte-identical for {q!r}"
    # the 'flock' query matches every fixture face (shard rows + episodic +
    # knowledge extensions) -> the un-sliced result set carries all 5 ids
    got = set(_ids(p.search_memories("flock", limit=100, min_score=0.0)))
    assert "fact-a" in got and "fact-b" in got, \
        f"fact shard rows must surface in full pool: {got}"
    assert "pref-a" in got, f"preference shard rows must surface: {got}"
    assert "epi-a" in got, \
        f"episodic extension must surface in full pool: {got}"
    assert "know-a" in got, \
        f"knowledge extension must surface in full pool: {got}"


# ── 2. type=fact -> fact.json only, episodic + knowledge excluded ───────

def test_type_fact_slices_pool_to_fact_shard():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _full_pool_fixtures(tmp)
    res = p.search_memories("flock", limit=100, min_score=0.0,
                            mem_type="fact")
    got = set(_ids(res))
    assert got == {"fact-a", "fact-b"}, \
        f"type='fact' must return exactly the fact.json rows: {res}"
    assert "pref-a" not in got, \
        f"other shards must be excluded from the fact slice: {got}"
    assert "epi-a" not in got, \
        f"episodic extension must be excluded from the fact slice: {got}"
    assert "know-a" not in got, \
        f"knowledge extension must be excluded from the fact slice: {got}"


# ── 3. type=preference -> preference.json only ───────────────────────────

def test_type_preference_slices_pool_to_preference_shard():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _full_pool_fixtures(tmp)
    res = p.search_memories("flock", limit=100, min_score=0.0,
                            mem_type="preference")
    got = set(_ids(res))
    assert got == {"pref-a"}, \
        f"type='preference' must return exactly the preference.json rows: {res}"
    for leak in ("fact-a", "fact-b", "epi-a", "know-a"):
        assert leak not in got, \
            f"{leak!r} must not leak into the preference slice: {got}"


# ── 4. invalid type -> ValueError / tool-layer error JSON ────────────────

def test_invalid_type_raises_value_error():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed_by_type(tmp, [_item("a", "flock alpha", mtype="fact")])
    for bad in ("bogus", "Facts", "knowledge", "episodic", "", 12345,
                "preference extra"):
        try:
            p.search_memories("flock", mem_type=bad)
            raise AssertionError(f"bad mem_type {bad!r} must raise ValueError")
        except ValueError:
            pass  # tool layer converts to error JSON; host turn survives
    # legal values must NOT raise
    assert _ids(p.search_memories("flock", min_score=0.0, mem_type="all")) \
        == ["a"]
    assert _ids(p.search_memories("flock", min_score=0.0, mem_type="fact")) \
        == ["a"]


def test_invalid_type_lands_as_error_json_not_500():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed_by_type(tmp, [_item("a", "flock alpha", mtype="fact")])
    out = p.handle_tool_call(
        "filemem_search", {"query": "flock", "type": "bogus"})
    parsed = json.loads(out)
    assert "error" in parsed, \
        f"invalid type must land as an error JSON result: {parsed}"
    # a valid slice through the same tool surface still succeeds
    out_ok = p.handle_tool_call(
        "filemem_search",
        {"query": "flock", "limit": 5, "min_score": 0.0, "type": "fact"})
    parsed_ok = json.loads(out_ok)
    # P0-2: a valid tool face returns the ENVELOPE dict
    # {"recall_scope", "results"} (not a bare results list, and not an error).
    # recall_scope rides at the envelope level; the rows live under "results".
    assert isinstance(parsed_ok, dict) and "error" not in parsed_ok, \
        f"valid type slice must return the P0-2 envelope dict, not an error: {out_ok}"
    assert "results" in parsed_ok, \
        f"P0-2 envelope must carry the results list: {parsed_ok}"
    assert _ids(parsed_ok["results"]) == ["a"], \
        f"the valid slice must surface the fact row: {parsed_ok}"


# ── 5. tool schema surface: `type` param, enum, required unchanged ───────

def test_tool_schema_has_type_param():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    schemas = p.get_tool_schemas()
    s = next(x for x in schemas if x["name"] == "filemem_search")
    props = s["parameters"]["properties"]
    assert "type" in props, "filemem_search schema must expose type"
    assert props["type"]["enum"] == ["all"] + list(MEMORY_TYPES), \
        f"enum must be ['all'] + MEMORY_TYPES: {props['type']['enum']}"
    assert props["type"]["type"] == "string"
    desc = props["type"]["description"]
    assert "all" in desc and "omitted" in desc, \
        f"type description must document the full-pool default: {desc}"
    assert s["parameters"]["required"] == ["query"], \
        "type is optional — required list unchanged"


# ── 6. RRF runs over the SLICED pool (filter precedes axis build) ───────

def test_rrf_runs_on_sliced_pool_not_full_pool():
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _seed_by_type(tmp, [
        _item("fact-rare", "flock rare delta", mtype="fact"),
        _item("fact-common", "flock notes", mtype="fact"),
        _item("pref-distractor", "flock preference epsilon",
              mtype="preference"),
    ])
    _seed_episodic(tmp, "epi-distractor", "flock episodic zeta")
    _seed_knowledge(tmp, "know-distractor", "flock knowledge eta")
    # sliced pool = 2 fact rows -> RRF normalized scores are exactly
    # {1.0, 0.5}, independent of the 4 distractor rows
    res = p.search_memories("flock", min_score=0.0, mem_type="fact")
    assert sorted(_ids(res)) == ["fact-common", "fact-rare"], \
        f"RRF must rank only the fact-sliced pool (2 items): {res}"
    assert sorted(r["score"] for r in res) == [0.5, 1.0], \
        f"2-item sliced pool -> normalized scores [0.5, 1.0]: " \
        f"{[(r.get('id'), r.get('score')) for r in res]}"
    # and the full pool (no slice) must see all 5 faces — proof the cut
    # is the parameter, not the corpus
    full = p.search_memories("flock", limit=100, min_score=0.0)
    assert len(_ids(full)) == 5, \
        f"un-sliced pool must keep all 5 rows: {_ids(full)}"


if __name__ == "__main__":
    for fn in (test_type_omitted_and_all_full_pool_regression_gate,
               test_type_fact_slices_pool_to_fact_shard,
               test_type_preference_slices_pool_to_preference_shard,
               test_invalid_type_raises_value_error,
               test_invalid_type_lands_as_error_json_not_500,
               test_tool_schema_has_type_param,
               test_rrf_runs_on_sliced_pool_not_full_pool):
        fn()
        print("ok", fn.__name__)
