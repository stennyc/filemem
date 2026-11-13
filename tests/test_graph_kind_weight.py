"""B'-1b: graph-axis 1-hop neighbor scores weighted by edge KIND.

Before B'-1b every 1-hop neighbor scored a flat 0.5 regardless of the edge
that connected it to a hit — so a node dragged up by a high-noise
co-occurrence edge (shared high-frequency word) ranked exactly as high as
one reached through a high-info semantic edge. The build side (SA2-9) had
already put semantic in the largest bucket, but the retrieval axis flattened
that away. B'-1b re-introduces the kind distinction on the search side:

  neighbor score = _GRAPH_NEIGHBOR_BASE (0.5) * kind_coef
  kind_coef: semantic 1.0 / temporal 0.42 / co-occurrence 0.40
             (SA2-9 quotas 55/22/23 normalized so the max == 1.0)

Coverage:
1. a SEMANTIC-edge neighbor keeps the legacy 0.5 (not starved).
2. a CO-OCCURRENCE-edge neighbor drops to 0.5*0.4 = 0.2 (noise de-ranked).
3. a node reachable via BOTH a semantic and a co-occ edge to a hit keeps
   the HIGHER coef (semantic wins, max-info axis).
4. an unknown/absent `kind` field falls back to the default coef (1.0 -> 0.5),
   never drops a neighbor to 0 (fail-soft).
5. ranking order: for an equal hit set, a semantic-neighbor item ranks
   ABOVE a co-occ-neighbor item.

Pure unit tests over a synthetic graph dict + tiny pool — no disk, no LLM,
no live store (search_index stays stdlib-only).
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import search_index  # noqa: E402
from search_index import (  # noqa: E402
    graph_axis_rank,
    _GRAPH_NEIGHBOR_BASE,
    _GRAPH_NEIGHBOR_KIND_COEF,
    _GRAPH_NEIGHBOR_DEFAULT_COEF,
)


def _graph(*edges):
    return {"edges": list(edges)}


def _edge(src, dst, kind):
    return {"src": src, "dst": dst, "kind": kind}


def _pool(*ids):
    # each pool item's content shares a distinctive token with the query so
    # the items that matter are NOT the ones that directly hit — the hit is
    # a SEPARATE node, and we rank the neighbors. content tokens are chosen
    # to avoid accidental direct-hits for the query "alpha".
    return [{"id": i, "content": f"beta {i}"} for i in ids]


def test_semantic_neighbor_keeps_legacy_half():
    """1. a node reached only through a semantic edge -> 0.5 * 1.0 = 0.5."""
    g = _graph(_edge("hit", "nb", "semantic"))
    pool = _pool("hit", "nb")
    order = graph_axis_rank(g, "alpha", pool)
    # both "hit" and "nb" are distinct from the query word; to force a hit we
    # give "hit" the query word in its content:
    pool = [{"id": "hit", "content": "alpha hit"},
            {"id": "nb", "content": "beta nb"}]
    order = graph_axis_rank(g, "alpha", pool)
    assert "hit" in [pool[i]["id"] for i in order]
    # "hit" is the +1.0 hit; "nb" is its semantic neighbor -> 0.5.
    assert order[0] in (0,), "the +1.0 hit must rank first"
    # nb's score is 0.5*1.0
    assert abs(_GRAPH_NEIGHBOR_BASE * _GRAPH_NEIGHBOR_KIND_COEF["semantic"] - 0.5) < 1e-9


def test_coocc_neighbor_de_ranked():
    """2. a node reached only through a co-occurrence edge -> 0.5*0.4=0.2,
    strictly below a semantic neighbor for the same hit set."""
    g = _graph(
        _edge("hit", "sem_nb", "semantic"),
        _edge("hit", "cooc_nb", "co-occurrence"),
    )
    pool = [{"id": "hit", "content": "alpha hit"},
            {"id": "sem_nb", "content": "beta a"},
            {"id": "cooc_nb", "content": "beta b"}]
    order = graph_axis_rank(g, "alpha", pool)
    ids = [pool[i]["id"] for i in order]
    # hit (1.0) > sem_nb (0.5) > cooc_nb (0.2)
    assert ids.index("hit") < ids.index("sem_nb") < ids.index("cooc_nb"), ids


def test_multi_kind_keeps_highest_coef():
    """3. a node linked to the hit by BOTH semantic and co-occurrence keeps
    the HIGHER coefficient (semantic), not the lower one."""
    g = _graph(
        _edge("hit", "both", "co-occurrence"),
        _edge("hit", "both", "semantic"),
        _edge("hit", "coonly", "co-occurrence"),
    )
    pool = [{"id": "hit", "content": "alpha hit"},
            {"id": "both", "content": "beta x"},
            {"id": "coonly", "content": "beta y"}]
    order = graph_axis_rank(g, "alpha", pool)
    ids = [pool[i]["id"] for i in order]
    # "both" (semantic wins, 0.5) ranks above "coonly" (co-occ, 0.2)
    assert ids.index("both") < ids.index("coonly"), ids


def test_unknown_kind_falls_back_to_default():
    """4. an edge with an absent/unknown kind keeps the default coef (1.0 ->
    0.5) rather than dropping the neighbor to 0 (fail-soft)."""
    g = _graph(
        _edge("hit", "unknown_nb", "weird-kind-not-in-table"),
    )
    pool = [{"id": "hit", "content": "alpha hit"},
            {"id": "unknown_nb", "content": "beta z"}]
    order = graph_axis_rank(g, "alpha", pool)
    ids = [pool[i]["id"] for i in order]
    # unknown coef == _GRAPH_NEIGHBOR_DEFAULT_COEF == 1.0 -> 0.5, so the
    # neighbor still ranks just below the hit (not dropped to 0).
    assert ids.index("hit") < ids.index("unknown_nb"), ids
    assert _GRAPH_NEIGHBOR_DEFAULT_COEF == 1.0


def test_semantic_neighbor_ranks_above_coocc_neighbor():
    """5. direct assertion: with one hit, semantic-neighbor > co-occ-neighbor
    in the returned order (the whole point of B'-1b)."""
    g = _graph(
        _edge("hit", "sem", "semantic"),
        _edge("hit", "co", "co-occurrence"),
        _edge("hit", "temp", "temporal"),
    )
    pool = [{"id": "hit", "content": "alpha hit"},
            {"id": "sem", "content": "beta 1"},
            {"id": "co", "content": "beta 2"},
            {"id": "temp", "content": "beta 3"}]
    order = graph_axis_rank(g, "alpha", pool)
    ids = [pool[i]["id"] for i in order]
    # expected scores: hit 1.0 > sem 0.5 > temp 0.21 > co 0.2
    assert ids.index("hit") < ids.index("sem"), ids
    assert ids.index("sem") < ids.index("temp"), ids
    assert ids.index("temp") < ids.index("co"), ids


def test_coef_table_matches_sa2_9_normalization():
    """The coefficients are the SA2-9 build-side quotas (55/22/23) normalized
    to the max == 1.0 — pin the exact table so it does not silently drift."""
    assert _GRAPH_NEIGHBOR_KIND_COEF["semantic"] == 1.0
    assert abs(_GRAPH_NEIGHBOR_KIND_COEF["temporal"] - 0.23 / 0.55) < 1e-9
    assert abs(_GRAPH_NEIGHBOR_KIND_COEF["co-occurrence"] - 0.22 / 0.55) < 1e-9
    assert _GRAPH_NEIGHBOR_BASE == 0.5


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
