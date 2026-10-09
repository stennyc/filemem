"""SA2-3 acceptance tests: BM25 keyword axis + RRF multi-axis fusion.

Covers spec SA2-3 Acceptance 1..4:
  1. RETRIEVABILITY gate on the fixture set: every TRUE pre-SA2-3 legacy
     top-5 id must still be eligible (in the post-RRF pool). NOT top-5
     set-invariance — RRF + idf re-ranking within the eligible pool is
     by design (Acceptance 1 also demands ranking improvement, which
     set-invariance would forbid; that half is gated separately by
     test_bm25_idf_rare_term_outranks_common).
  2. CJK fixture zero regression: zh golden queries keep their baseline
     ids ELIGIBLE under RRF (bigram tokenizer, legacy substring gate
     preserved verbatim — nothing previously recallable is lost).
  3. index build perf on the 227-item live corpus: <500ms loose, <50ms
     tight target (measured value reported in notes, asserted loose).
  4. zero LLM (search path asserts no caller loaded) + import audit
     (search_index.py is stdlib-only).
Plus: k1/b read from decay.json `search` block (DECAY_DEFAULTS source),
graph-stub-empty fail-soft, RRF normalized-score formula locked.
"""
import ast
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import search_index  # noqa: E402
from search_index import (  # noqa: E402
    BM25Index, graph_axis_stub, is_cjk_char, rrf_fuse,
    rank_decay_axis, tokenize, build_search_axes,
)
from __init__ import FileMemProvider, load_decay_config  # noqa: E402

LIVE_STORE = Path.home() / ".hermes" / "filemem"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sa2_3_golden_queries.json"


def _make_live_provider():
    """Read-only provider over the live store (zero initialize(), zero writes)."""
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
    p.source_profile = "sa2-3-test"
    p._decay = load_decay_config()
    p._llm_ctx = None
    return p


def _fixture_queries():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["queries"]


# ── Acceptance 1+2 (corrected gate): RETRIEVABILITY, not top5-set-invariance ─
def test_retrievability_gate_en_zh():
    """Every TRUE pre-SA2-3 legacy top-5 id must still be ELIGIBLE in the
    post-RRF pool (min_score=0.0 over the full pool). The legacy substring
    qualification gate is preserved verbatim by SA2-3, so nothing
    previously recallable may be lost. RRF/idf re-ranking WITHIN the
    eligible pool is by design and is NOT gated here (spec SA2-3
    Acceptance 1 also demands ranking improvement — gated separately by
    test_bm25_idf_rare_term_outranks_common)."""
    p = _make_live_provider()
    checked = 0
    for lang, entries in _fixture_queries().items():
        assert len(entries) >= 5, f"fixture must hold >=5 golden {lang} queries"
        for e in entries:
            q, base = e["query"], e.get("baseline_top5") or []
            if not base:  # baseline had zero hits: nothing to regress
                continue
            eligible = set(r.get("id") for r in
                           p.search_memories(q, limit=1000, min_score=0.0))
            missing = [i for i in base if i not in eligible]
            assert not missing, (
                f"{lang} query {q!r}: {missing} were recallable pre-SA2-3 "
                f"but are no longer eligible post-RRF — legacy substring "
                f"qualification gate regressed")
            checked += 1
    assert checked >= 10, f"expected >=10 golden queries to check, got {checked}"

# ── Acceptance 1 (half 2): idf — rare proper/technical terms rank up ────
def test_bm25_idf_rare_term_outranks_common():
    pool = [
        {"id": "c1", "content": "the cat the dog", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "c2", "content": "demo runtime crash", "salience": 0.9, "created_at": "2026-09-01T00:00:00"},
        {"id": "c3", "content": "the cat sat on the mat", "salience": 0.3, "created_at": "2026-09-01T00:00:00"},
        {"id": "c4", "content": "notes about the dog", "salience": 0.3, "created_at": "2026-09-01T00:00:00"},
    ]
    for decay in ({}, {"search": {"bm25_k1": 5.0, "bm25_b": 0.9}}):
        scores = build_search_axes(pool, "demo", decay=decay)
        assert scores[1] == 1.0 == max(scores), f"rare term must top the pool: {scores}"
    # multi-word technical query: the doc carrying BOTH rare terms wins even
    # though a doc repeats the common word 4x — idf makes rarity dominate
    pool2 = [
        {"id": "a", "content": "hermes gateway restart", "salience": 0.3, "created_at": "2026-09-01T00:00:00"},
        {"id": "b", "content": "hermes hermes hermes hermes", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "c", "content": "hermes gateway plugin decay curve", "salience": 0.9, "created_at": "2026-09-01T00:00:00"},
    ]
    scores = build_search_axes(pool2, "hermes gateway", decay={})
    assert scores[2] == 1.0 == max(scores), f"both-rare-term doc must lead: {scores}"


# ── Acceptance 1 (half 3): k1/b are read from the decay.json search block ──
def test_bm25_params_from_config():
    pool = [
        {"id": "x", "content": "deploy deploy deploy deploy deploy", "created_at": "2026-09-01T00:00:00"},
        {"id": "y", "content": "deploy once", "created_at": "2026-09-01T00:00:00"},
        {"id": "z", "content": "unrelated fact", "created_at": "2026-09-01T00:00:00"},
    ]
    # k1/b knobs change the RAW axis (proven via BM25Index directly)
    from search_index import rrf_fuse, rank_decay_axis
    s_def = BM25Index(1.2, 0.75).build(pool).score("deploy")
    s_alt = BM25Index(9.0, 0.1).build(pool).score("deploy")
    assert s_def != s_alt, "k1/b must actually change the bm25 axis"
    # and build_search_axes must READ them from the decay `search` block:
    # hand-build the fusion with the custom params and demand an exact match
    expected = rrf_fuse([BM25Index(9.0, 0.1).build(pool).rank("deploy"),
                         rank_decay_axis(pool)], 3)
    got = build_search_axes(pool, "deploy", decay={"search": {"bm25_k1": 9.0, "bm25_b": 0.1}})
    expected_norm = [0.0] * 3
    for pos, idx in enumerate(sorted(range(3), key=lambda i: (-expected[i], i))):
        expected_norm[idx] = 1.0 - pos / 3
    assert all(abs(a - b) < 1e-9 for a, b in zip(got, expected_norm)), \
        f"k1/b from config not honored: {got} != {expected_norm}"
    # defaults come from DECAY_DEFAULTS (consolidation) — the config source
    from consolidation import DECAY_DEFAULTS
    assert float(DECAY_DEFAULTS["search"]["bm25_k1"]) == 1.2
    assert float(DECAY_DEFAULTS["search"]["bm25_b"]) == 0.75
    # and the on-disk decay.json ships the same keys (user-tunable surface)
    on_disk = json.loads(Path(__file__).resolve().parents[1].joinpath("decay.json").read_text(encoding="utf-8"))
    assert float(on_disk["search"]["bm25_k1"]) == 1.2 and float(on_disk["search"]["bm25_b"]) == 0.75


# ── graph stub: empty axis must not break fusion (fail-soft) ────────────
def test_graph_stub_empty_fails_soft():
    assert graph_axis_stub() == []
    assert graph_axis_stub(None) == []
    pool = [
        {"id": "a", "content": "flock lock", "created_at": "2026-09-01T00:00:00"},
        {"id": "b", "content": "other", "created_at": "2026-09-02T00:00:00"},
    ]
    s_with = build_search_axes(pool, "flock", decay={}, graph=None)
    s_explicit = build_search_axes(pool, "flock", decay={}, graph={"nodes": [], "edges": []})
    assert s_with == s_explicit, "graph stub must contribute all zeros both ways"
    assert s_with[0] > s_with[1]
    p = _make_live_provider()
    res = p.search_memories("flock lock", limit=5, min_score=0.0)  # no graph.json: no crash
    assert isinstance(res, list)


# ── RRF normalized score formula, locked ────────────────────────────────
def test_rrf_normalized_formula_locked():
    pool = [{"id": str(i), "content": f"word{i} filler", "created_at": "2026-09-01T00:00:00"}
            for i in range(4)]
    idx = BM25Index().build(pool)
    fused = rrf_fuse([idx.rank("word3")], 4)
    assert fused[3] > fused[0] > fused[1]
    # lock bm25_ranked_score semantics via build_search_axes on a pool where
    # only the decay axis has signal: rank order = pool order (all equal),
    # so scores must be exactly 1 - pos/pool_size
    same_pool = [
        {"id": "1", "content": "", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "2", "content": "", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "3", "content": "", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
    ]
    scores = build_search_axes(same_pool, "", decay={})
    # formula locked numerically (float-safe compare, not ==)
    expected = [1.0 - pos / 3 for pos in range(3)]
    assert all(abs(a - b) < 1e-9 for a, b in zip(scores, expected)), \
        f"formula 1-(rank-1)/n broken: {scores} != {expected}"
    assert build_search_axes([], "", decay={}) == []
    singleton = build_search_axes([{"id": "1", "content": ""}], "", decay={})
    assert singleton == [1.0], "singleton pool -> its item gets 1.0 (min 1 rule)"


def test_tokenize_cjk_ngrams():
    assert is_cjk_char("中") and not is_cjk_char("a")
    toks = tokenize("中文分词 deploy")
    assert "中文" in toks and "中" not in toks and "deploy" in toks
    # 3- and 4-gram windows (overlapping bigram superset)
    t4 = tokenize("文件锁机制")
    assert "文件" in t4 and "文件锁" in t4 and "文件锁机" in t4
    # latin case fold + punctuation split
    assert tokenize("CJS MODULE") == ["cjs", "module"]
    assert tokenize(None) == []


# ── decay axis sanity (M4 semantics preserved as an axis) ──────────────
def test_decay_axis_prefers_high_salience_recent():
    pool = [
        {"id": "old", "content": "x", "salience": 0.9, "created_at": "2020-01-01T00:00:00"},
        {"id": "new", "content": "y", "salience": 0.5, "created_at": "2026-09-20T00:00:00"},
    ]
    order = rank_decay_axis(pool, lam=0.05)
    assert order == [1, 0], f"recent 0.5 must beat 3-year-old 0.9: {order}"


# ── Acceptance 3: build perf, 227-item live corpus ──────────────────────
def test_index_build_perf_227_items():
    p = _make_live_provider()
    # pool exactly as search_memories assembles it
    pool = []
    pool.extend(p._load_episodic_pool(include_fixture=False))
    pool.extend(p._load_knowledge_pool())
    for f in p.memories_dir.glob("*.json"):
        pool.extend(json.loads(f.read_text(encoding="utf-8")))
    assert len(pool) >= 200, f"corpus shrank below perf-test floor: {len(pool)}"
    t0 = time.perf_counter()
    BM25Index().build(pool)
    dt_ms = (time.perf_counter() - t0) * 1000
    assert dt_ms < 500, f"loose budget: build took {dt_ms:.1f}ms"
    print(f"[SA2-3 perf] build {len(pool)} items: {dt_ms:.1f}ms "
          f"(tight target <50ms: {'PASS' if dt_ms < 50 else 'missed by ' + format(dt_ms - 50, '.1f') + 'ms'})")


# ── Acceptance 4: zero LLM + import audit ───────────────────────────────
def test_zero_llm_on_search_path():
    import unittest.mock as mock
    p = _make_live_provider()
    p._llm_ctx = None  # model-assist re-rank gate: off by construction
    with mock.patch("__init__._load_llm_caller") as m:
        res = p.search_memories("flock", limit=5, min_score=0.0)
        assert m.call_count == 0, "search path must stay LLM-free (SA2 hard constraint)"
    assert isinstance(res, list)


def test_search_index_stdlib_import_audit():
    """Import audit: search_index.py may only import stdlib (SA2 hard
    constraint: zero external deps, zero LLM)."""
    tree = ast.parse(Path(search_index.__file__).read_text(encoding="utf-8"))
    allowed = {"math", "re", "json", "pathlib", "collections", "typing", "datetime", "__future__"}
    non_stdlib = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top not in allowed:
                    non_stdlib.append(top)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top not in allowed:
                non_stdlib.append(top)
    assert not non_stdlib, f"non-stdlib imports in search_index.py: {non_stdlib}"


if __name__ == "__main__":
    # quick self-run (pytest runs the same tests)
    for fn in (test_tokenize_cjk_ngrams, test_rrf_normalized_formula_locked,
               test_decay_axis_prefers_high_salience_recent,
               test_bm25_params_from_config, test_graph_stub_empty_fails_soft,
               test_bm25_idf_rare_term_outranks_common,
               test_search_index_stdlib_import_audit,
               test_retrievability_gate_en_zh,
               test_index_build_perf_227_items,
               test_zero_llm_on_search_path):
        fn()
        print("ok", fn.__name__)
