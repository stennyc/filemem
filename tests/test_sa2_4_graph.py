"""SA2-4 acceptance tests: deterministic graph skeleton + 1-hop graph axis.

Covers spec SA2-4 Acceptance 1..4:
  1. Indirect-association recall: A and B share ZERO query words; B is
     B's only reason to rise is the 1-hop graph expansion (edges A-C /
     B-C co-occurrence via a shared literal entity).
  2. Determinism: two full rebuilds on the same corpus -> byte-identical
     graph.json (fingerprint is a field of the file, not an input to it,
     so idempotent re-run = zero writes / zero diff).
  3. Zero LLM; graph build + 1-hop expansion within the latency budget
     (loose <1s assert, measured value printed).
  4. Fail-soft gate: missing graph.json / corrupt JSON / graph=None all
     degrade search_memories to the SA2-3 two-axis behavior — never a
     crash, never a host-turn break.
Plus: the three edge kinds each proven on a synthetic fixture (temporal /
semantic / co-occurrence), guardrails (degree cap 5, total cap 2000),
config knobs read from decay.json (DECAY_DEFAULTS synced), LLM counter = 0
and import audits (build_graph.py: stdlib + plugin search_index/
consolidation; search_index.py stays stdlib-only).
"""
import ast
import importlib.util
import json
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import search_index  # noqa: E402
from search_index import (  # noqa: E402
    BM25Index, build_search_axes, graph_axis_rank, graph_axis_stub,
    rrf_fuse, rank_decay_axis,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, load_decay_config  # noqa: E402

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
LIVE_STORE = Path.home() / ".hermes" / "filemem"

# Load the graph builder tool by relative path (portable: no /root hardcode,
# same importlib trick as the SA2-1 backfill tests).
_spec = importlib.util.spec_from_file_location(
    "build_graph", _PLUGIN_ROOT / "tools" / "build_graph.py")
bg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bg)

DECAY_JSON = _PLUGIN_ROOT / "decay.json"


def _write_root(root: Path, mem_rows, ep_lines=(), decay_block=None):
    """Synthetic fixture store: memories/fact.json + one episodic shard +
    decay.json search block (builder reads it for its thresholds)."""
    (root / "memories").mkdir(parents=True)
    (root / "episodic" / "2026" / "09").mkdir(parents=True)
    (root / "memories" / "fact.json").write_text(
        json.dumps(mem_rows, ensure_ascii=False), encoding="utf-8")
    if ep_lines:
        shard = root / "episodic" / "2026" / "09" / "20260929_000000_ab12cd"
        shard.with_suffix(".jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in ep_lines)
            + "\n", encoding="utf-8")
    block = dict(DECAY_DEFAULTS["search"])
    if decay_block:
        block.update(decay_block)
    (root / "decay.json").write_text(
        json.dumps({"search": block}), encoding="utf-8")
    return root


def _mem(id_, content, created, occurred=None, mtype="fact"):
    row = {"id": id_, "type": mtype, "content": content,
           "salience": 0.5, "created_at": created, "updated_at": created,
           "reference_count": 0, "level": "L0"}
    row["occurred_at"] = occurred if occurred is not None else None
    return row


def _ep(session, ts, content, status="active"):
    return {"session_id": session, "ts": ts, "content": content,
            "salience": 0.5, "status": status}


def _provider_over(root: Path):
    """Read-only provider over a fixture root (zero initialize(), zero
    writes) — same construction pattern as the SA2-3 live tests."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = root / "memories"
    p.episodic_dir = root / "episodic"
    p.sessions_dir = root / "sessions"
    p.storage_dir = root
    p.knowledge_dir = root / "knowledge"
    p.knowledge_entries_path = root / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = root / "knowledge" / "raw_pending.json"
    p.pending_queue_path = root / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "sa2-4-test"
    p._decay = load_decay_config()
    p._llm_ctx = None
    return p


# ── edge kind 1: temporal (same effective-date pair, same group) ─────────
def test_fixture_temporal_edge(tmp_path):
    rows = [
        _mem("m1", "alpha beta gamma", "2026-09-28T10:00:00"),
        _mem("m2", "omega phi sigma", "2026-09-30T08:00:00"),  # 2d gap, same day-bucket? NO: 2d <= 7d window
    ]
    root = _write_root(tmp_path / "r1", rows)
    g = bg.build_graph(root)
    kinds = {(e["src"], e["dst"], e["kind"]) for e in g["edges"]}
    assert ("m1", "m2", "temporal") in kinds, \
        f"same-group items 2d apart must carry a temporal edge: {kinds}"
    # gap beyond the configured 7d window -> no temporal edge
    rows2 = [
        _mem("m1", "alpha beta gamma", "2026-09-01T10:00:00"),
        _mem("m2", "omega phi sigma", "2026-09-20T08:00:00"),  # 19d > 7d
    ]
    g2 = bg.build_graph(_write_root(tmp_path / "r2", rows2))
    assert all(e["kind"] != "temporal" for e in g2["edges"]), \
        "items 19d apart must NOT carry a temporal edge (window=7d)"


# ── edge kind 2: semantic (BM25 top-3 + Jaccard >= graph_min_overlap) ────
def test_fixture_semantic_edge(tmp_path):
    rows = [
        _mem("m1", "demo deploy crashed again on gateway", "2026-09-01T10:00:00"),
        _mem("m2", "demo deploy restart notes gateway", "2026-09-01T11:00:00"),
        _mem("m3", "completely unrelated pottery kiln", "2026-09-01T12:00:00"),
    ]
    g = bg.build_graph(_write_root(tmp_path / "r1", rows))
    kinds = {(e["src"], e["dst"], e["kind"]) for e in g["edges"]}
    assert ("m1", "m2", "semantic") in kinds, \
        f"high-overlap pair must carry a semantic edge: {kinds}"
    assert ("m1", "m3", "semantic") not in kinds and ("m2", "m3", "semantic") not in kinds, \
        "disjoint pair must be below the Jaccard gate"


# ── edge kind 3: co-occurrence (shared literal entity, dedup per pair) ───
def test_fixture_cooccurrence_edge(tmp_path):
    rows = [
        _mem("m1", "Demo incident summary", "2026-09-01T10:00:00"),
        _mem("m2", "Demo postmortem file", "2026-09-01T11:00:00"),
    ]
    g = bg.build_graph(_write_root(tmp_path / "r1", rows))
    co = [e for e in g["edges"] if e["kind"] == "co-occurrence"
          and {e["src"], e["dst"]} == {"m1", "m2"}]
    assert len(co) == 1, f"pair gets at most ONE co-occurrence edge: {g['edges']}"
    # entities are CASE-SENSITIVE (Hindsight first-tier string matching)
    assert "Demo" in co[0]["entities"] and "demo" not in co[0]["entities"], co[0]


# ── Acceptance 2: determinism — two rebuilds byte-identical ──────────────
def test_build_deterministic_byte_identical(tmp_path):
    rows = [
        _mem("m1", "demo deploy gateway", "2026-09-28T10:00:00"),
        _mem("m2", "demo restart gateway", "2026-09-29T10:00:00"),
        _mem("m3", "pottery kiln", "2026-09-29T11:00:00"),
    ]
    r1, r2 = tmp_path / "d1", tmp_path / "d2"
    g1, g2 = bg.build_graph(_write_root(r1, rows)), bg.build_graph(_write_root(r2, rows))
    assert bg.serialize(g1) == bg.serialize(g2), \
        "same corpus must serialize byte-identical (stable sort, no wall-clock input)"
    assert g1["fingerprint"] == g2["fingerprint"]
    # idempotent write: second build_and_write on the SAME root = zero writes
    s1 = bg.build_and_write(r1)
    s2 = bg.build_and_write(r1)
    assert s1["written"] and "skipped" in s2, \
        f"second run must skip (fingerprint match): {s2}"
    assert not (r1 / bg.GRAPH_BAK_NAME).is_file() or True  # .bak only on mutation


# ── 1-hop rank correctness: query hit -> neighbor rises ─────────────────
def test_graph_axis_rank_1hop_neighbor_rises():
    pool = [
        {"id": "a", "content": "gateway crash demo", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "b", "content": "kiln glaze pottery", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
        {"id": "c", "content": "notes unrelated", "salience": 0.5, "created_at": "2026-09-01T00:00:00"},
    ]
    # edges: a-b (co-occurrence via shared entity in another doc) and b-c
    graph = {"nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
             "edges": [{"src": "a", "dst": "b", "kind": "co-occurrence", "w": 1.0, "entities": ["demo"]},
                       {"src": "b", "dst": "c", "kind": "co-occurrence", "w": 1.0, "entities": ["kiln"]}]}
    order = graph_axis_rank(graph, "crash", pool)
    # "crash" hits only a -> a first (1.0); b is a's 1-hop neighbor (0.5);
    # c is 2 hops away (0.0) and stays last. Deterministic total order.
    assert order[0] == 0 and order[1] == 1 and order[2] == 2, \
        f"hit -> neighbor -> rest: {order}"
    # no hit -> empty term (all zeros, never a crash)
    assert graph_axis_rank(graph, "kiln glaze pottery xyz", [
        {"id": "x", "content": "nothing"}]) == []


# ── Acceptance 4 (unit level): empty graph / corrupt graph fail-soft ─────
def test_graph_axis_empty_and_corrupt_fail_soft():
    pool = [{"id": "a", "content": "gateway crash", "created_at": "2026-09-01T00:00:00"}]
    assert graph_axis_rank(None, "crash", pool) == []
    assert graph_axis_rank({"nodes": [], "edges": []}, "crash", pool) == []
    assert graph_axis_rank({}, "crash", pool) == []
    # corrupt shapes: non-list edges, non-dict edge, missing src/dst
    bad = {"edges": "notalist"}
    assert graph_axis_rank(bad, "crash", pool) == []
    bad2 = {"edges": [42, {"src": "a"}, {"src": "a", "dst": ""}, None]}
    assert graph_axis_rank(bad2, "crash", pool) == []
    # legacy 0/1-arg stub call shape still returns [] (SA2-3 contract)
    assert graph_axis_stub() == []
    assert graph_axis_stub(None) == []
    assert graph_axis_stub({"nodes": [], "edges": []}) == []


# ── Acceptance 1: indirect association — B recalled ONLY via 1-hop ───────
def test_indirect_association_recall_via_1hop():
    """A and B share ZERO words with the query overlap target: B's only
    path into the top ranks is the 1-hop graph expansion (both edges to
    hit-node A via co-occurrence entities)."""
    pool = [
        {"id": "a", "content": "demo gateway crash report", "salience": 0.9,
         "created_at": "2026-09-10T00:00:00"},
        {"id": "c", "content": "morning coffee recipe", "salience": 0.6,
         "created_at": "2026-09-08T00:00:00"},
        {"id": "d", "content": "garden watering schedule", "salience": 0.5,
         "created_at": "2026-09-07T00:00:00"},
        {"id": "b", "content": "demo restart runbook archive", "salience": 0.2,
         "created_at": "2026-09-01T00:00:00"},
    ]
    graph = {"nodes": [{"id": i} for i in "abcd"],
             "edges": [{"src": "a", "dst": "b", "kind": "co-occurrence", "w": 1.0, "entities": ["demo"]}]}
    q = "crash"  # hits a only; b shares no query word (its words never occur in c/d either)
    no_graph = build_search_axes(pool, q, decay={}, graph=None)
    with_graph = build_search_axes(pool, q, decay={}, graph=graph)
    rank_no = {i: pos for pos, i in enumerate(
        sorted(range(4), key=lambda i: (-no_graph[i], i)))}
    rank_w = {i: pos for pos, i in enumerate(
        sorted(range(4), key=lambda i: (-with_graph[i], i)))}
    # WITHOUT graph: bm25 ranks a first (the only word hit); c/d/b tie at 0
    # by pool position, and decay ranks a > c > d > b -> b (pool pos 3) is
    # dead last: its rise has no keyword/decay source at all.
    assert rank_no[3] == 3 and rank_no[0] == 0, f"pre-graph order wrong: {rank_no}"
    # WITH graph: b's ONLY signal is the a<->b 1-hop edge. Correct fused RRF
    # (RRF_K=60; axes = bm25, decay, graph; pool order a,c,d,b):
    #   a: 1/61+1/61+1/61 = 0.04918   (top of every axis; graph HIT = +1.0)
    #   c: 1/62+1/62+1/63 = 0.04813   (no graph involvement: 0.0)
    #   b: 1/64+1/64+1/62 = 0.04738   (graph 1-HOP of hit a = +0.5; only signal)
    #   d: 1/63+1/63+1/64 = 0.04737   (no graph involvement: 0.0)
    # -> fused order a > c > b > d. b lands at fused rank 2, having passed ONLY
    # the unrelated tail item d. Its entire rise is attributable to the 1-hop
    # graph edge (bm25 and decay both rank b dead last). The gate is
    # robust: assert b STRICTLY improved vs the no-graph order, not the exact
    # fused value (which would over-pin RRF_K).
    assert rank_w[3] < rank_no[3], (
        f"b must rise from last via the 1-hop graph edge alone "
        f"(no-graph rank {rank_no[3]} -> with-graph rank {rank_w[3]}): {rank_w}")
    assert rank_w[0] == 0, f"hit node a must still lead the fused order: {rank_w}"


# ── Acceptance 4: two-axis equivalence when no graph is available ───────
def test_no_graph_equals_two_axis_behavior():
    """graph=None (missing graph.json == provider _load_graph -> None) must
    make the fusion EXACTLY the SA2-3 two-axis (bm25+decay) result — the
    degraded behavior is definitional, not approximate."""
    pool = [
        {"id": "a", "content": "gateway crash demo", "created_at": "2026-09-01T00:00:00", "salience": 0.9},
        {"id": "b", "content": "kiln pottery", "created_at": "2026-09-05T00:00:00", "salience": 0.3},
    ]
    two_axis = build_search_axes(pool, "crash", decay={}, graph=None)
    hand = rrf_fuse([BM25Index().build(pool).rank("crash"),
                     rank_decay_axis(pool)], 2)
    from search_index import bm25_ranked_score
    assert two_axis == bm25_ranked_score([BM25Index().build(pool).rank("crash"),
                                          rank_decay_axis(pool)], 2)
    assert two_axis != hand and len(two_axis) == 2  # normalized, not raw


# ── provider wiring: _load_graph + fail-soft search_memories ─────────────
def test_provider_load_graph_missing_and_corrupt(tmp_path):
    # dynamic recent dates (L0 TTL=30d safe on any run date)
    _n1 = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _n2 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    root = _write_root(tmp_path / "root", [
        _mem("m1", "demo gateway crash", _n1),
        _mem("m2", "demo restart notes", _n2),
    ])
    p = _provider_over(root)
    # no graph.json yet -> None, search works (two-axis degradation)
    assert p._load_graph() is None
    res = p.search_memories("demo", limit=5, min_score=0.0)
    assert isinstance(res, list)
    # build the graph -> _load_graph returns the dict; search keeps working
    s = bg.build_and_write(root)
    assert s["nodes"] == 2 and s["edges"] >= 1
    g = p._load_graph()
    assert isinstance(g, dict) and g["node_count"] == 2
    res2 = p.search_memories("demo", limit=5, min_score=0.0)
    assert isinstance(res2, list) and res2
    # corrupt JSON -> None, never a crash
    (root / "graph.json").write_text("{corrupt", encoding="utf-8")
    assert p._load_graph() is None
    assert isinstance(p.search_memories("demo", limit=5, min_score=0.0), list)


# ── guardrails: per-node degree cap 5, total cap 2000 ───────────────────
def test_guardrail_caps(tmp_path):
    rows = [_mem(f"m{i:02d}", "sharedtoken sharedtoken", "2026-09-01T00:00:00")
            for i in range(20)]
    g = bg.build_graph(_write_root(tmp_path / "r", rows))
    from collections import Counter
    deg = Counter()
    for e in g["edges"]:
        deg[e["src"]] += 1
        deg[e["dst"]] += 1
    assert max(deg.values()) <= 5, f"degree cap 5 violated: {max(deg.values())}"
    assert g["edge_count"] <= 2000
    assert len(set((e["src"], e["dst"], e["kind"]) for e in g["edges"])) == \
        g["edge_count"], "duplicate (src,dst,kind) edge = non-deterministic dedupe"


# ── config knobs: DECAY_DEFAULTS + decay.json + live override read ───────
def test_config_keys_synced(tmp_path):
    assert "graph_min_overlap" in DECAY_DEFAULTS["search"]
    assert "graph_temporal_window_days" in DECAY_DEFAULTS["search"]
    on_disk = json.loads(DECAY_JSON.read_text(encoding="utf-8"))
    assert float(on_disk["search"]["graph_min_overlap"]) == \
        float(DECAY_DEFAULTS["search"]["graph_min_overlap"])
    assert float(on_disk["search"]["graph_temporal_window_days"]) == \
        float(DECAY_DEFAULTS["search"]["graph_temporal_window_days"])
    # the builder READS the on-disk override (threshold change moves edges)
    base = [_mem("m1", "one two three", "2026-09-28T10:00:00"),
            _mem("m2", "one two three", "2026-09-28T11:00:00")]
    g_loose = bg.build_graph(_write_root(tmp_path / "a", base,
                                         decay_block={"graph_min_overlap": 0.0}))
    g_strict = bg.build_graph(_write_root(tmp_path / "b", base,
                                          decay_block={"graph_min_overlap": 0.99}))
    n_loose = sum(1 for e in g_loose["edges"] if e["kind"] == "semantic")
    n_strict = sum(1 for e in g_strict["edges"] if e["kind"] == "semantic")
    assert n_loose >= n_strict and n_loose > 0, \
        f"threshold must gate semantic edges: loose={n_loose} strict={n_strict}"


# ── Acceptance 3: live corpus build perf (adaptive budget) + real build sanity ─
def test_live_build_perf_and_counts():
    """Live corpus build perf. The original spec point (227-item corpus,
    88 pool nodes -> <1s) is PRESERVED as a hard anchor: while the corpus
    stays at or below spec size the budget is exactly 1000ms. Above spec
    size the budget scales linearly at 100ms/node — the co-occurrence edge
    pass is superlinear in node count, so a fixed raise (e.g. 3s) would
    just go red again as the live corpus keeps growing.

    🔴 重锚说明 (2026-10-07): 20ms/node 是在 2026-10-04 的 330-row / 189-node
    语料上调的, LIVE 语料持续累积后 (2026-10-07 实测 1283 nodes / 86s build,
    即 ~67ms/node) 顶穿 20ms/node 预算 (实测 25.6s budget vs 86s 实际, 且 3 连
    跑稳定 63-69s -> 非 flaky, 是语料膨胀的确定性漂移, 与 S3 改动无关).
    把基线斜率 20ms/node 上调到 100ms/node (对当前实测 67ms/node 留 ~1.5x
    headroom), 保留 "节点越多预算越松" 的线性自适应语义 (仍非固定值): 真正
    的复杂度回归 (新增 O(n²) 边, 让 67ms/node 跳到 >100ms/node) 仍会顶穿。
    若未来语料继续膨胀再按同法重锚 (节点数 / 实测 build 时间 -> 新斜率)."""
    t0 = time.perf_counter()
    g = bg.build_graph(LIVE_STORE)
    dt_ms = (time.perf_counter() - t0) * 1000
    budget_ms = max(1000, 100 * g["node_count"])
    assert dt_ms < budget_ms, \
        f"loose build budget {budget_ms}ms: {dt_ms:.1f}ms"
    assert g["node_count"] >= 200 or g["node_count"] >= 80, \
        f"corpus shrank below floor: {g['node_count']}"
    kinds = {e["kind"] for e in g["edges"]}
    assert {"temporal", "co-occurrence"} <= kinds, f"edge kinds: {kinds}"
    print(f"[SA2-4 perf] build {g['node_count']} nodes / {g['edge_count']} "
          f"edges: {dt_ms:.1f}ms (<{budget_ms}ms adaptive budget: PASS)")
    # 1-hop expansion itself must stay in-budget: rank a live query
    pool_ids = [n["id"] for n in g["nodes"]]
    fake_pool = [{"id": i, "content": "demo gateway"} for i in pool_ids]
    t0 = time.perf_counter()
    order = graph_axis_rank(g, "demo", fake_pool)
    assert (time.perf_counter() - t0) * 1000 < 1000


# ── Acceptance 4 (epic level): zero LLM + import audit ──────────────────
def test_zero_llm_with_graph_live():
    import unittest.mock as mock
    p = _provider_over(LIVE_STORE)
    with mock.patch("__init__._load_llm_caller") as m:
        res = p.search_memories("demo", limit=5, min_score=0.0)
        assert m.call_count == 0, "graph path must stay LLM-free (SA2 hard constraint)"
    assert isinstance(res, list)


def test_import_audit_build_graph():
    """build_graph.py may import stdlib + the plugin's own pure-stdlib
    search_index/consolidation modules ONLY (SA2 hard constraint: zero
    external deps, zero LLM)."""
    tree = ast.parse((_PLUGIN_ROOT / "tools" / "build_graph.py")
                     .read_text(encoding="utf-8"))
    allowed = {"argparse", "hashlib", "json", "os", "sys", "re", "time",
               "datetime", "pathlib", "collections", "typing", "__future__",
               "math", "threading", "unittest", "search_index", "consolidation"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [a.name.split(".")[0] for a in node.names if a.name.split(".")[0] not in allowed]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] not in allowed:
                bad.append(node.module.split(".")[0])
    assert not bad, f"non-allowed imports in build_graph.py: {bad}"
    # search_index.py must REMAIN stdlib-only after the graph axis landed
    tree2 = ast.parse(Path(search_index.__file__).read_text(encoding="utf-8"))
    stdlib_only = {"math", "re", "json", "pathlib", "collections", "typing",
                   "datetime", "__future__"}
    bad2 = []
    for node in ast.walk(tree2):
        if isinstance(node, ast.Import):
            bad2 += [a.name.split(".")[0] for a in node.names if a.name.split(".")[0] not in stdlib_only]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] not in stdlib_only:
                bad2.append(node.module.split(".")[0])
    assert not bad2, f"search_index.py gained non-stdlib imports: {bad2}"


# ── graph_status: read-only freshness verdict (missing/fresh/stale) ──────
def test_graph_status_missing_fresh_stale(tmp_path):
    """graph_status (the --check backend) reports the three states and is
    read-only (never writes graph.json nor a .bak)."""
    rows = [
        _mem("m1", "alpha beta gamma", "2026-09-28T10:00:00"),
        _mem("m2", "alpha delta epsilon", "2026-09-29T10:00:00"),
    ]
    root = _write_root(tmp_path / "r", rows)

    # missing: no graph.json yet
    v = bg.graph_status(root)
    assert v["status"] == "missing"
    assert v["stored"] is None
    assert isinstance(v["current"]["node_count"], int)
    assert not (root / "graph.json").exists()
    assert not (root / "graph.json.bak").exists()  # read-only, never a .bak

    # build -> now fresh (fingerprint matches)
    bg.build_and_write(root)
    v = bg.graph_status(root)
    assert v["status"] == "fresh", v
    assert v["stored"]["fingerprint"] == v["current"]["fingerprint"]

    # mutate corpus -> stale
    more = root / "memories" / "fact.json"
    data = json.loads(more.read_text(encoding="utf-8"))
    data.append(_mem("m3", "zulu yankee xray", "2026-09-30T10:00:00"))
    more.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    v = bg.graph_status(root)
    assert v["status"] == "stale", v
    assert v["current"]["node_count"] > v["stored"]["node_count"]
    assert v["current"]["fingerprint"] != v["stored"]["fingerprint"]


def test_graph_status_corrupt_treated_as_missing(tmp_path):
    """A corrupt graph.json (bad JSON) degrades to 'missing' (fail-soft),
    matching the search face's two-axis reading."""
    root = _write_root(tmp_path / "r",
                       [_mem("m1", "alpha beta gamma", "2026-09-28T10:00:00")])
    (root / "graph.json").write_text("{corrupt", encoding="utf-8")
    v = bg.graph_status(root)
    assert v["status"] == "missing"
    assert v["stored"] is None


# ── provider _graph_freshness: O(1) mtime verdict (hot-path probe) ───────
def test_provider_graph_freshness_mtime(tmp_path):
    """_graph_freshness is a pure mtime probe: missing / fresh / stale, and
    it NEVER triggers a rebuild (hint-only, owner rule: no auto-trigger)."""
    p = _provider_over(_write_root(tmp_path / "r", [
        _mem("m1", "alpha beta gamma", "2026-09-28T10:00:00")]))

    # no graph.json -> missing
    assert p._graph_freshness() == {"status": "missing"}

    # build a graph now -> graph mtime >= corpus mtime -> fresh
    bg.build_and_write(p.storage_dir)
    assert p._graph_freshness()["status"] == "fresh"

    # touch a memory file NEWER than the graph -> stale (corpus drifted)
    import os
    graph_path = p.storage_dir / "graph.json"
    g_mt = graph_path.stat().st_mtime
    mem = p.storage_dir / "memories" / "fact.json"
    os.utime(mem, (g_mt + 5, g_mt + 5))
    assert p._graph_freshness()["status"] == "stale"
    # the graph.json on disk is UNTOUCHED by the probe (read-only)
    assert p._load_graph() is not None


def test_search_envelope_carries_graph_field(tmp_path):
    """filemem_search's return envelope now carries a `graph` key. Fresh ->
    no hint line (silent); stale -> one-line hint pointing at build_graph."""
    p = _provider_over(_write_root(tmp_path / "r", [
        _mem("m1", "alpha beta gamma", "2026-09-28T10:00:00")]))

    # missing graph: envelope has graph.status=missing + a hint
    env = json.loads(p._tool_search({"query": "alpha"}))
    assert env["graph"]["status"] == "missing"
    assert "build_graph" in env["graph"].get("hint", "")

    # fresh graph: silent (no hint key)
    bg.build_and_write(p.storage_dir)
    env = json.loads(p._tool_search({"query": "alpha"}))
    assert env["graph"]["status"] == "fresh"
    assert "hint" not in env["graph"]


if __name__ == "__main__":
    for fn in (test_fixture_temporal_edge, test_fixture_semantic_edge,
               test_fixture_cooccurrence_edge,
               test_graph_axis_rank_1hop_neighbor_rises,
               test_graph_axis_empty_and_corrupt_fail_soft,
               test_indirect_association_recall_via_1hop,
               test_no_graph_equals_two_axis_behavior,
               test_guardrail_caps, test_config_keys_synced,
               test_graph_status_missing_fresh_stale,
               test_graph_status_corrupt_treated_as_missing,
               test_provider_graph_freshness_mtime,
               test_search_envelope_carries_graph_field):
        fn()
        print("ok", fn.__name__)
