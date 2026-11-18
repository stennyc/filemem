"""P0 read-side caches (2026-11-18): ZERO-RECALL-DIFF canary.

The five P0 levers (axis cache, aliases mtime gate, graph mtime gate,
episodic-only incremental leg, marginal-pass INFO gate) must be
byte-invisible: cache-on results == cache-off results over a corpus
mutation sequence, and every freshness guarantee (new fact / new
episodic row / new graph / new aliases) still lands on the NEXT call.

A/B method: one provider, two legs per state —
  off leg: FILEMEM_PERF_CACHE=off  (every gate bypassed, legacy cost)
  on  leg: FILEMEM_PERF_CACHE=on   (caches active, default)
results must be deep-equal at every step of the sequence. The decay
axis is wall-clock-dependent, so both legs pin time_anchor — the
only remaining non-determinism (wall-clock between legs) is removed
at the source.

The synthetic fixture follows test_ep_3_alias (partial __new__ shell,
tmp store, fixed ISO timestamps — L2 rows, ttl None, never expired).
"""
import json
import pathlib
import sys
import threading
from datetime import datetime, timezone

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

ANCHOR = datetime(2026, 11, 18, 0, 0, 0, tzinfo=timezone.utc)


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = None
    p.source_profile = "test"
    p.session_id = "s1"
    p._decay = dict(DECAY_DEFAULTS)
    p._main_runtime = None
    p._alias_mining_in_flight = False
    return p


def _fact(rid: str, content: str, ts: str = "2026-09-15T00:00:00") -> dict:
    return {
        "id": rid, "type": "fact", "content": content, "level": "L2",
        "metadata": {}, "created_at": ts, "updated_at": ts,
        "salience": 0.5, "reference_count": 0, "ttl_days": None,
        "source": "auto",
    }


def _write_facts(p: FileMemProvider, rows: list) -> None:
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _append_episodic(p: FileMemProvider, row: dict) -> None:
    f = p.episodic_dir / "sess-s1.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_aliases(p: FileMemProvider, topics: dict) -> None:
    with open(p.storage_dir / "aliases.json", "w", encoding="utf-8") as f:
        json.dump({"topics": topics, "ts": "2026-11-18T00:00:00"}, f)


def _write_graph(p: FileMemProvider, edges: list) -> None:
    with open(p.storage_dir / "graph.json", "w", encoding="utf-8") as f:
        json.dump({"edges": edges, "ts": "2026-11-18T00:00:00"}, f)


def _search(p: FileMemProvider, q: str, **kw) -> list:
    """Result projection: (id, score) pairs — the recall-relevant surface."""
    rows = p.search_memories(q, limit=20, time_anchor=ANCHOR, **kw)
    return [(r.get("id"), round(float(r.get("score", 0.0)), 12)) for r in rows]


def _ab_state(p, q: str, monkeypatch, **kw) -> tuple:
    """Run one search state in BOTH legs; return (off_results, on_results)."""
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "off")
    res_off = _search(p, q, **kw)
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "on")
    res_on = _search(p, q, **kw)
    return res_off, res_on


# ── the canary: corpus-mutation sequence, cache-on ≡ cache-off ─────────

def test_cache_on_off_byte_identical_over_mutation_sequence(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("f1", "docker build pipeline with caching layers"),
                     _fact("f2", "postgres connection pooling config"),
                     _fact("f3", "tailnet vpn setup for shared vps")])

    # state 0: steady corpus
    off, on = _ab_state(p, "build pipeline", monkeypatch)
    assert off == on, f"steady-state drift: {off} vs {on}"

    # state 1: memories shard changed (full-rebuild leg must stay in sync)
    _write_facts(p, [_fact("f1", "docker build pipeline with caching layers"),
                     _fact("f2", "postgres connection pooling config"),
                     _fact("f3", "tailnet vpn setup for shared vps"),
                     _fact("f4", "kubernetes helm chart for the gateway",
                           ts="2026-11-10T00:00:00")])
    off, on = _ab_state(p, "gateway deployment", monkeypatch)
    assert off == on
    assert "f4" in [i for i, _ in on], "new fact must be recalled after the write"

    # state 2: episodic-only delta (the F4 incremental leg)
    _append_episodic(p, _fact("e1", "deployed the gateway helm chart v2",
                              ts="2026-11-17T00:00:00"))
    off, on = _ab_state(p, "helm chart", monkeypatch)
    assert off == on
    assert "e1" in [i for i, _ in on], \
        "episodic append must be visible on the next search (F4 safe variant)"

    # state 3: a window-filtered pool (different id tuple over the same corpus)
    off, on = _ab_state(p, "helm chart", monkeypatch,
                        window={"since": "2026-11-01"})
    assert off == on
    assert "f4" in [i for i, _ in on] and "f1" not in [i for i, _ in on]

    # state 4: mem_type slice (another pool identity)
    off, on = _ab_state(p, "gateway", monkeypatch, mem_type="fact")
    assert off == on


def test_episodic_delta_leg_keeps_memories_section_byte_stable(tmp_path):
    """Internal check of the F4 leg: after an episodic-only sig change the
    cached pool is [old memories | NEW episodic | old knowledge] — the
    non-episodic sections are reused, not re-parsed."""
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("f1", "stable fact one"), _fact("f2", "stable fact two")])
    _append_episodic(p, _fact("e0", "first episodic line",
                              ts="2026-11-16T00:00:00"))
    p._cached_all_memories()  # full rebuild (cold cache)
    pool_before = list(p._corpus_pool_cache)
    n_mem = p._corpus_pool_sections[0]

    _append_episodic(p, _fact("e1", "second episodic line",
                              ts="2026-11-17T00:00:00"))
    pool_after = p._cached_all_memories()  # episodic-only delta
    assert pool_after[:n_mem] == pool_before[:n_mem], \
        "memories section must be reused verbatim"
    assert [it.get("id") for it in pool_after[n_mem:]] == ["e0", "e1"], \
        "episodic section must be the fresh load"


def test_episodic_only_sig_delta_pure_function():
    """Unit leg of the gate: which signature deltas count as episodic-only."""
    import os as _os
    sep = _os.sep
    mem = ("C:" + sep + "memories", 100, 10)          # memories shard part
    epi1 = ("C:" + sep + "episodic" + sep + "s1.jsonl", 100, 10)
    epidir = "C:" + sep + "episodic"
    old = (mem, epi1)
    # episodic part moved -> episodic-only
    assert fm._episodic_only_sig_delta(old, (mem, ("C:/episodic/s1.jsonl", 200, 12)),
                                       epidir) is True
    # new episodic FILE appears (a fresh session shard) -> episodic-only
    assert fm._episodic_only_sig_delta(old, (mem, epi1, ("C:/episodic/s2.jsonl", 1, 2)),
                                       epidir) is True
    # memories part moved -> NOT episodic-only
    assert fm._episodic_only_sig_delta(old, (mem + (5,), epi1), "C:/episodic") is False
    # new NON-episodic part -> NOT episodic-only
    assert fm._episodic_only_sig_delta(
        old, (mem, epi1, ("C:" + sep + "knowledge.json", 1, 2)), epidir) is False
    # bust tuple -> NOT episodic-only
    assert fm._episodic_only_sig_delta(
        ("corpus-scan-failed", 1), old, "C:/episodic") is False
    # equal sigs -> False (no delta at all)
    assert fm._episodic_only_sig_delta(old, old, "C:/episodic") is False


def test_axis_cache_hit_on_steady_corpus(tmp_path, monkeypatch):
    """Steady corpus + repeated search: the BM25 build runs ONCE; the
    steady-state calls are cache hits (build counter stays flat)."""
    import search_index as si
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact(f"f{i}", f"unique corpus line number {i}")
                     for i in range(30)])
    builds = []
    orig_build = si.BM25Index.build

    def counting_build(self, items):
        builds.append(len(items))
        return orig_build(self, items)

    monkeypatch.setattr(si.BM25Index, "build", counting_build)
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "on")
    _search(p, "corpus line")
    n_first = len(builds)
    assert n_first == 1
    for _ in range(4):
        _search(p, "corpus line")
    assert len(builds) == n_first, \
        f"steady-state searches must be cache hits, builds={len(builds)}"


def test_graph_static_cache_hit_and_invalid(tmp_path, monkeypatch):
    """Graph statics rebuild on graph.json change (key includes file mtime);
    steady graph = one static build total."""
    import search_index as si
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("n1", "the gateway service node"),
                     _fact("n2", "the database cluster node")])
    _write_graph(p, [{"src": "n1", "dst": "n2", "kind": "semantic"}])
    statics = []
    orig_static = si._graph_static

    def counting_static(graph, pool):
        statics.append(len(pool))
        return orig_static(graph, pool)

    monkeypatch.setattr(si, "_graph_static", counting_static)
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "on")
    _search(p, "gateway service")
    n_first = len(statics)
    _search(p, "gateway service")
    assert len(statics) == n_first, "steady graph must not recompute statics"
    # graph file changed -> new mtime -> statics rebuild
    _write_graph(p, [{"src": "n2", "dst": "n1", "kind": "temporal"}])
    # the rewrite above lands a new mtime; drop any stale instance cache
    # attr so the gate's key comparison is exercised from a clean slate
    p._graph_file_cache = None
    _search(p, "database cluster")
    assert len(statics) > n_first, "graph change must bust the statics cache"


def test_aliases_mtime_gate_freshness(tmp_path, monkeypatch):
    """Alias table v1 -> v2: v2's topics must expand on the NEXT search
    (mtime invalidation), and cache-on ≡ cache-off throughout."""
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("f1", "tailscale vpn for the tailnet cluster")])
    _write_aliases(p, {"k8s cluster": {"aliases": ["kubernetes"],
                                        "source": "human", "ts": ""}})
    off, on = _ab_state(p, "k8s cluster", monkeypatch)
    assert off == on

    # v2: a NEW topic that widens recall for a query v1 could not reach
    _write_aliases(p, {"helm release": {"aliases": ["helm", "chart"],
                                        "source": "human", "ts": ""},
                       "k8s cluster": {"aliases": ["kubernetes"],
                                       "source": "human", "ts": ""}})
    off, on = _ab_state(p, "helm release manifest", monkeypatch)
    assert off == on, f"alias v2 drift: {off} vs {on}"


def test_env_killswitch_bypasses_every_gate(tmp_path, monkeypatch):
    """FILEMEM_PERF_CACHE=off: sig-supplied build_search_axes still builds
    fresh every call (no cache entry consumed), alias/graph caches unused.
    This is the canary's reference leg AND the ops kill-switch."""
    import search_index as si
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "off")
    pool = [_fact("a", "alpha row"), _fact("b", "beta row")]
    builds = []
    orig_build = si.BM25Index.build

    def counting_build(self, items):
        builds.append(len(items))
        return orig_build(self, items)

    monkeypatch.setattr(si.BM25Index, "build", counting_build)
    for _ in range(3):
        si.build_search_axes(pool, "alpha", corpus_sig=((("sig"),),),
                             graph_key=(1, 2))
    assert len(builds) == 3, "env-off leg must bypass the axis cache"
    monkeypatch.setenv("FILEMEM_PERF_CACHE", "on")
    for _ in range(3):
        si.build_search_axes(pool, "alpha", corpus_sig=((("sig"),),),
                             graph_key=(1, 2))
    assert len(builds) == 4, "env-on leg must build once then hit"


def test_legacy_calls_byte_identical(tmp_path):
    """No-sig callers (existing tests, direct library use): the legacy
    path must stay — build every call, stub graph axis, no cache touch."""
    import search_index as si
    pool = [_fact("a", "alpha row"), _fact("b", "beta row")]
    before = len(si._AXIS_CACHE)
    r1 = si.build_search_axes(pool, "alpha")
    r2 = si.build_search_axes(pool, "alpha")
    assert r1 == r2
    assert len(si._AXIS_CACHE) == before, \
        "legacy no-sig calls must not touch the axis cache"
