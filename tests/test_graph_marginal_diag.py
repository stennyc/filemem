"""C1-M2 graph-axis MARGINAL contribution diagnostic.

graph_marginal_neighbors(graph, query, pool) counts the pool items whose
ONLY reason to be graph-ranked is a 1-hop NEIGHBOR edge (a non-HIT item with
a positive graph score in (0, 1.0)). Direct content-token HITs (score 1.0)
are EXCLUDED — a keyword/BM25 axis can already reach them; the marginal
count isolates what the graph alone adds.

Coverage:
1. a pure-neighbor pool (no direct content hits, only 1-hop links) -> the
   marginal count equals the neighbor set size.
2. HITs never count as marginal (a content-token hit is not graph-added).
3. empty graph / no hits / empty pool -> 0 (fail-soft).
4. graph_axis_rank still returns the same total order after the refactor to
   share _graph_scores (retrieval path unchanged; the diagnostic reads the
   same core).
5. kind weighting surfaces in the marginal set: a co-occ neighbor still
   counts as marginal (0 < 0.2 < 1.0), so the marginal count is kind-agnostic
   (it counts "how many the graph added", not "which kind").
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from search_index import (  # noqa: E402
    graph_axis_rank, graph_marginal_neighbors,
)


def _graph(*edges):
    return {"edges": list(edges)}


def _edge(src, dst, kind):
    return {"src": src, "dst": dst, "kind": kind}


def test_pure_neighbor_pool_counts_marginals():
    """1. hits are content-token matches; neighbors are 1-hop-only. The
    marginal count = number of non-HIT items with a positive graph score."""
    g = _graph(
        _edge("hit", "n1", "semantic"),
        _edge("hit", "n2", "temporal"),
        _edge("hit", "n3", "co-occurrence"),
    )
    pool = [
        {"id": "hit", "content": "alpha hit"},   # direct HIT (content token)
        {"id": "n1", "content": "beta one"},      # 1-hop semantic neighbor
        {"id": "n2", "content": "beta two"},      # 1-hop temporal neighbor
        {"id": "n3", "content": "beta three"},    # 1-hop co-occ neighbor
        {"id": "island", "content": "gamma x"},   # no graph link -> not marginal
    ]
    assert graph_marginal_neighbors(g, "alpha", pool) == 3


def test_hits_never_count_as_marginal():
    """2. an item that is BOTH a content-token hit AND has a neighbor edge
    counts as a HIT (1.0), not a marginal neighbor (it is excluded)."""
    g = _graph(_edge("a", "b", "semantic"))
    pool = [
        {"id": "a", "content": "alpha a"},   # HIT
        {"id": "b", "content": "alpha b"},   # also a HIT (shares "alpha"),
                                              # and a neighbor of a
    ]
    # both are 1.0 hits -> zero items in the (0,1) band -> marginal 0
    assert graph_marginal_neighbors(g, "alpha", pool) == 0


def test_empty_or_no_hit_is_zero():
    """3. fail-soft: no graph, empty pool, or a query that hits nothing -> 0."""
    pool = [{"id": "x", "content": "beta x"}]
    assert graph_marginal_neighbors(None, "alpha", pool) == 0
    assert graph_marginal_neighbors(_graph(_edge("a", "b", "semantic")),
                                    "alpha", []) == 0
    # no hit at all: query word appears in no pool content
    g = _graph(_edge("a", "b", "semantic"))
    assert graph_marginal_neighbors(g, "zzz-unmatched", pool) == 0


def test_rank_order_unchanged_after_refactor():
    """4. graph_axis_rank (retrieval order) still works through the shared
    _graph_scores core: hits first (1.0), then neighbors by kind, then 0."""
    g = _graph(
        _edge("hit", "sem", "semantic"),
        _edge("hit", "co", "co-occurrence"),
    )
    pool = [
        {"id": "hit", "content": "alpha hit"},
        {"id": "sem", "content": "beta s"},
        {"id": "co", "content": "beta c"},
        {"id": "zero", "content": "beta z"},
    ]
    order = graph_axis_rank(g, "alpha", pool)
    ids = [pool[i]["id"] for i in order]
    # hit (1.0) > sem (0.5) > co (0.2) > zero (0.0)
    assert ids.index("hit") < ids.index("sem") < ids.index("co") < ids.index("zero")
    # marginal count (neighbors only, hit excluded)
    assert graph_marginal_neighbors(g, "alpha", pool) == 2


def test_marginal_count_is_kind_agnostic():
    """5. the marginal count isolates HOW MANY the graph added (regardless of
    kind); kind only affects the rank WITHIN the neighbor band, not the
    count."""
    g = _graph(
        _edge("hit", "a", "semantic"),
        _edge("hit", "b", "co-occurrence"),
        _edge("hit", "c", "temporal"),
    )
    pool = [
        {"id": "hit", "content": "alpha hit"},
        {"id": "a", "content": "beta 1"},
        {"id": "b", "content": "beta 2"},
        {"id": "c", "content": "beta 3"},
    ]
    assert graph_marginal_neighbors(g, "alpha", pool) == 3


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
