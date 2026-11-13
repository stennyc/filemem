"""SA2-3: BM25 keyword axis + RRF multi-axis fusion (pure stdlib, zero LLM).

Module-level, no host-state dependency: the caller passes the pool (plain
dicts, item.get("content")) + decay config, so the module stays unit-testable
without a provider instance. Axes:
* bm25 — Okapi BM25 over a latin-word + CJK-ngram tokenizer; k1 / b are read
  from the decay.json `search` block (bm25_k1 / bm25_b, defaults 1.2 / 0.75
  — see DECAY_DEFAULTS in consolidation/).
* decay — the legacy anthropomorphic curve `salience * exp(-lambda * age_days)`
  (M4 aware/naive ts handling preserved), demoted from primary score to ONE
  ranking axis.
* graph — SA2-4 1-hop expansion: query tokens hitting a node pull its
  neighbors up. `graph_axis_rank(graph, query, pool)` ranks the POOL by a
  deterministic per-pool-position score (hit node +1.0 / neighbor +0.5,
  ties by pool position) over the graph loaded from `<root>/graph.json`
  (built by tools/build_graph.py, zero LLM). Fail-soft by construction:
  graph=None / missing nodes / corrupt adjacency -> an empty rank list
  (RRF term all zeros) = the two-axis SA2-3 behavior, never an exception.
  `graph_axis_stub` is kept as the zero-graph delegate (same call site,
  same contract — replaced-in-place, not deleted, for test compat).

Normalization (locked by tests/test_sa2_3_bm25_rrf.py):
    score(i) = 1.0 - (rank(i) - 1) / pool_size
with 1-based rank over the RRF ordering and pool_size = len(pool) (min 1).
The knowledge tier multiplies the normalized score by 1.5 (pin-first
semantics, only affects the min_score gate, never the fused order).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

__all__ = ["is_cjk_char", "tokenize", "BM25Index", "rrf_fuse",
           "rank_decay_axis", "graph_axis_rank", "graph_axis_stub",
           "build_search_axes", "graph_marginal_neighbors"]

_RRF_K = 60  # standard RRF constant (no config surface for SA2-3; YAGNI)
_LATIN_TOKEN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_CJK_NGRAMS = (2, 3, 4)  # CJK runs emit ALL char substrings of len 2..4
# B'-1b (owner 2026-11-13): 1-hop neighbor scores are weighted by the KIND of
# the edge connecting them, so a node reached through a high-info SEMANTIC
# edge ranks above one reached through a co-occurrence / temporal edge.
# Coefficients are the SA2-9 build-side bucket quotas (55/22/23) normalized
# so the strongest kind (semantic) == 1.0: semantic 0.55/0.55=1.0, temporal
# 0.23/0.55=0.42, co-occurrence 0.22/0.55=0.40. Deliberately a search-side
# HARD-CODED constant (NOT read from decay.json): build-side quotas and
# search-side neighbor weights are independent tuning faces — changing the
# build mix must not silently rescale the retrieval axis. Neighbor score =
# _GRAPH_NEIGHBOR_BASE (0.5, unchanged) * kind coef, so a semantic-edge
# neighbor keeps the legacy 0.5 and only the co-occ/temporal neighbors drop.
_GRAPH_NEIGHBOR_BASE = 0.5
# SA2-9 build-side quotas 55/22/23 normalized so the strongest kind
# (semantic, 0.55) == 1.0; the others are the EXACT fractions (0.23/0.55,
# 0.22/0.55) so the search-side weights are pinned to the build-side numbers.
_GRAPH_NEIGHBOR_KIND_COEF = {
    "semantic": 1.0,            # 0.55/0.55
    "temporal": 0.23 / 0.55,    # 0.418181...
    "co-occurrence": 0.22 / 0.55,  # 0.4
}
_GRAPH_NEIGHBOR_DEFAULT_COEF = 1.0  # unknown/absent kind keeps the base 0.5


def is_cjk_char(ch: str) -> bool:
    """One CJK Unified Ideograph? Same \\u4e00-\\u9fff range as the plugin's
    zh detection (_detect_language) — the tokenizer must not drift from the
    legacy Chinese behavior."""
    return "\u4e00" <= ch <= "\u9fff"


def _cjk_ngrams(run: str) -> List[str]:
    out: List[str] = []
    n = len(run)
    for size in _CJK_NGRAMS:
        if n >= size:
            out.extend(run[i:i + size] for i in range(n - size + 1))
    return out


def tokenize(text: Any) -> List[str]:
    """latin words + CJK char n-grams (len 2..4, every position of every
    maximal CJK run). The 2..4 fan-out keeps the legacy Chinese golden
    queries behaving: the old keyword axis did substring matching
    (`w in content`), so a CJK query term of any length matched anywhere in
    a CJK run — emitting every 2-4-gram makes 2-char terms exact and
    3-4-char terms hit on overlapping windows (recall can only grow).
    NOTE the host gate still runs the LEGACY substring qualification, so a
    single-CJK-char query stays eligible exactly as before; the token
    stream feeds the bm25 RANK axis only.
    Regex-based (CJK runs + latin words) to stay fast on the <430-item
    corpus; char order within the stream is irrelevant to BM25.
    """
    if not text:
        return []
    t = str(text).lower()
    out = _LATIN_TOKEN.findall(t)
    for run in _CJK_RUN.findall(t):
        out.extend(_cjk_ngrams(run))
    return out


class BM25Index:
    """In-memory Okapi BM25 over plain item dicts. `build(items)` tokenizes
    item["content"] (+ item.get("topic") for the knowledge layer);
    `rank(query)` returns the FULL pool order (zero-score items at the tail,
    ties by pool position — deterministic) so the axis is a total ordering,
    which RRF is defined over. Rebuilt on every search: the corpus is
    <430 items, in-memory build stays within the perf budget (see test)."""

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = float(k1), float(b)
        self._doc_lens: List[int] = []
        self._df: Dict[str, int] = {}
        self._tf: List[Counter] = []
        self._n = 0
        self._avgdl = 0.0

    def build(self, items: List[Dict]) -> "BM25Index":
        self._doc_lens, self._tf, self._df = [], [], {}
        for item in items:
            toks = tokenize((item.get("content") or "") + " " + (item.get("topic") or ""))
            self._tf.append(Counter(toks))
            self._doc_lens.append(len(toks))
            for term in set(toks):
                self._df[term] = self._df.get(term, 0) + 1
        self._n = len(items)
        self._avgdl = (sum(self._doc_lens) / self._n) if self._n else 0.0
        return self

    def score(self, query: str) -> List[float]:
        """Raw BM25 score per pool position (0.0 when the term is absent)."""
        if not self._n:
            return []
        qtoks = tokenize(query)
        if not qtoks:
            return [0.0] * self._n
        out = []
        for i in range(self._n):
            tf, dl = self._tf[i], (self._doc_lens[i] or 1)
            s = 0.0
            for q in qtoks:
                f = tf.get(q, 0)
                if not f:
                    continue
                idf = math.log(1 + (self._n - self._df[q] + 0.5) / (self._df[q] + 0.5))
                s += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            out.append(s)
        return out

    def rank(self, query: str) -> List[int]:
        scores = self.score(query)
        return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def rank_decay_axis(pool: List[Dict], lam: float = 0.05,
                    salience_boost: float = 1.2,
                    now: Optional[datetime] = None) -> List[int]:
    """The legacy decay curve as a ranking axis: `salience *
    exp(-lambda * age_days)` (reference_count >= 3 gets the salience boost,
    M4 aware/naive ts semantics preserved verbatim).

    F2: `now` pins the decay anchor. None (default) = wall-clock at call
    time — byte-identical to the pre-F2 behavior (regression gate). A
    pinned aware datetime makes the axis deterministic for a given pool
    snapshot, so a same-session re-prefetch over an unchanged pool is
    byte-stable (provider prefix-cache friendly)."""
    if now is None:
        now = datetime.now(timezone.utc)

    def rank_score(i: int) -> float:
        item = pool[i]
        sal = float(item.get("salience", 0.5))
        if int(item.get("reference_count", 0) or 0) >= 3:
            sal = min(1.0, sal * float(salience_boost))
        try:
            ts = item.get("updated_at") or item.get("created_at") or ""
            if ts:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
            else:
                age_days = 0.0
        except (ValueError, TypeError):
            age_days = 0.0
        return sal * math.exp(-float(lam) * age_days)

    return sorted(range(len(pool)), key=lambda i: (-rank_score(i), i))


def _add_adj(adjacency: Dict[str, Dict[str, str]], src: str, dst: str,
             kind: str, coef: float) -> None:
    """B'-1b: record dst in src's adjacency, keeping the HIGHEST-coefficient
    kind when a pair is linked by multiple edge kinds (max-info axis wins).
    A corrupt/unknown kind falls back to the default coefficient (1.0), so a
    missing `kind` field keeps the legacy 0.5 neighbor score rather than
    dropping to 0."""
    d = adjacency.setdefault(src, {})
    existing = d.get(dst)
    existing_coef = _GRAPH_NEIGHBOR_KIND_COEF.get(
        existing, _GRAPH_NEIGHBOR_DEFAULT_COEF) if existing else 0.0
    if coef > existing_coef:
        d[dst] = kind


def graph_axis_rank(graph: Optional[Dict], query: str, pool: List[Dict]) -> List[int]:
    """SA2-4: the graph 1-hop expansion axis over the RRF pool (deterministic,
    zero LLM, pure stdlib).

    Semantics: tokenize the QUERY; a pool item whose `id` is a graph node and
    whose content tokens intersect the query tokens is a HIT (+1.0); any node
    1-hop NEIGHBOR of a hit (shared edge, either direction) rises to +0.5.
    Items with no graph involvement stay 0.0. Returns the total pool order
    `sorted by (-score, pool position)` — a deterministic total order RRF is
    defined over. EMPTY SIGNAL RULE: graph=None / empty graph / no hits ->
    [] (an empty RRF term = all zeros), which by construction degrades the
    fusion to the two-axis SA2-3 behavior (fail-soft gate, spec SA2-4/4).
    A corrupt graph shape (bad edges list / non-dict edge / missing src|dst)
    is skipped per-edge or per-graph -> [] as well, never an exception.
    """
    if not pool:
        return []
    scores, hit_ids = _graph_scores(graph, query, pool)
    # EMPTY SIGNAL RULE: a degenerate graph term (no adjacency / no query
    # tokens / no hits) yields an empty order, which RRF treats as all-zeros
    # (the SA2-3 two-axis fail-soft gate, spec SA2-4/4).
    if not hit_ids:
        return []
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def _graph_scores(graph: Optional[Dict], query: str,
                  pool: List[Dict]):
    """C1-M2 shared graph-axis scoring core (B'-1b). Returns
    ``(scores, hit_ids)`` where ``scores[i]`` is the graph-axis score
    (0.0 / 1.0 for a content-token HIT / 0.2~0.5 for a 1-hop NEIGHBOR,
    weighted by the connecting edge kind) and ``hit_ids`` is the set of
    pool-node ids that directly hit. Both ``graph_axis_rank`` (retrieval
    order) and ``graph_marginal_neighbors`` (observability) read from this
    one core so the scoring never diverges between the retrieval path and
    the diagnostic path.

    Deterministic + fail-soft by construction: a corrupt graph shape (bad
    edges list / non-dict edge / missing src|dst) is skipped per-edge;
    an empty pool / empty query / no adjacency returns (all-zeros, empty
    hit set)."""
    scores = [0.0] * len(pool)
    if not pool:
        return scores, set()
    # B'-1b: adjacency maps node -> {neighbor: kind}; the 1-hop neighbor
    # score is weighted by the connecting edge kind (max-coef kind wins).
    adjacency: Dict[str, Dict[str, str]] = {}
    if graph:
        edges = graph.get("edges")
        if isinstance(edges, list):
            for e in edges:
                if not isinstance(e, dict):
                    continue  # fail-soft: skip a corrupt edge, keep the rest
                src, dst = str(e.get("src") or ""), str(e.get("dst") or "")
                if not src or not dst:
                    continue
                _kind = str(e.get("kind") or "")
                _coef = _GRAPH_NEIGHBOR_KIND_COEF.get(
                    _kind, _GRAPH_NEIGHBOR_DEFAULT_COEF)
                _add_adj(adjacency, src, dst, _kind, _coef)
                _add_adj(adjacency, dst, src, _kind, _coef)
    qtoks = set(tokenize(query))
    if not qtoks or not adjacency:
        return scores, set()  # empty graph / empty query = all-zeros term
    hit_ids = set()
    for i, item in enumerate(pool):
        nid = str(item.get("id") or "")
        if not nid or nid not in adjacency:
            continue
        if qtoks & set(tokenize(item.get("content"))):
            scores[i] = 1.0
            hit_ids.add(nid)
    if not hit_ids:
        return scores, hit_ids
    for i, item in enumerate(pool):
        nid = str(item.get("id") or "")
        if nid and nid not in hit_ids:
            # B'-1b: among this node's neighbors that are hits, take the
            # HIGHEST-coefficient edge kind and scale the base neighbor
            # score by it (semantic-neighbor keeps 0.5, co-occ/temporal drop
            # to 0.2/0.21). A node with no hit-neighbor stays 0.0.
            best = 0.0
            for nb, kind in adjacency.get(nid, {}).items():
                if nb in hit_ids:
                    _c = _GRAPH_NEIGHBOR_KIND_COEF.get(
                        kind, _GRAPH_NEIGHBOR_DEFAULT_COEF)
                    if _c > best:
                        best = _c
            if best > 0.0:
                scores[i] = _GRAPH_NEIGHBOR_BASE * best
    return scores, hit_ids


def graph_marginal_neighbors(graph: Optional[Dict], query: str,
                             pool: List[Dict]) -> int:
    """C1-M2 diagnostic (observability only, NOT part of RRF fusion): the
    number of pool items whose ONLY reason to be graph-ranked is a 1-hop
    NEIGHBOR edge — a non-HIT item carrying a positive graph score. This is
    the graph axis's MARGINAL contribution: the items a keyword/BM25 axis
    alone would not have surfaced. Direct HITs (content-token overlap) are
    EXCLUDED because a BM25 axis can already reach them. Returns 0 when the
    graph axis is empty (fail-soft: no graph / no hits / empty pool)."""
    scores, hit_ids = _graph_scores(graph, query, pool)
    # neighbors are exactly the items with a positive-but-not-1.0 graph
    # score (HITs are 1.0, neighbors are in (0, 0.5]). Counting the
    # strictly-positive, non-1.0 band isolates the marginal neighbor set.
    return sum(1 for s in scores if 0.0 < s < 1.0)


def graph_axis_stub(graph: Optional[Dict] = None,
                   query: str = "", pool: Optional[List[Dict]] = None) -> List[int]:
    """SA2-4 in-place stub replacement (same name, same call site in
    build_search_axes — the spec pins the position, not the 1-arg shape):
    delegates to graph_axis_rank. Called with the legacy 0/1-arg form
    (graph only, no query/pool) it still returns [] — the zero-graph
    fail-soft contract the SA2-3 tests lock."""
    return graph_axis_rank(graph, query, pool or [])


def rrf_fuse(rank_lists: List[List[int]], pool_size: int) -> List[float]:
    """fused(i) = sum over axes 1 / (_RRF_K + rank_i), 1-based ranks;
    missing items contribute 0. Raw floats are NOT the returned score —
    normalize with bm25_ranked_score (see module docstring formula)."""
    fused = [0.0] * pool_size
    for rlist in rank_lists:
        for pos, idx in enumerate(rlist):
            fused[idx] += 1.0 / (_RRF_K + pos + 1)
    return fused


def bm25_ranked_score(rank_lists: List[List[int]], pool_size: int,
                      rank1: bool = False) -> List[float]:
    """RRF fused -> [0,1] scores: `1.0 - (rank-1)/pool_size` (min pool 1).
    `rank1` is a no-op kept for SA2-2 window-axis interface stability."""
    fused = rrf_fuse(rank_lists, pool_size)
    order = sorted(range(pool_size), key=lambda i: (-fused[i], i))
    out = [0.0] * pool_size
    n = max(pool_size, 1)
    for pos, idx in enumerate(order):
        out[idx] = 1.0 - pos / n
    return out


def build_search_axes(pool: List[Dict], query: str,
                      decay: Optional[Dict] = None,
                      graph: Optional[Dict] = None,
                      now: Optional[datetime] = None) -> List[float]:
    """One-stop host entry: build the bm25 + decay + graph axes over `pool`,
    fuse, normalize -> per-position [0,1] scores. `graph` = the loaded
    graph.json dict (or None = fail-soft two-axis behavior).

    F2: `now` pins the decay axis anchor (None = wall-clock, legacy)."""
    decay = decay or {}
    cfg = decay.get("search") or {}
    idx = BM25Index(k1=float(cfg.get("bm25_k1", 1.2)),
                    b=float(cfg.get("bm25_b", 0.75))).build(pool)
    axes: List[List[int]] = [idx.rank(query)]
    axes.append(rank_decay_axis(pool, lam=float(decay.get("lambda_days", 0.05)),
                                salience_boost=float(decay.get("salience_boost", 1.2)),
                                now=now))
    axes.append(graph_axis_stub(graph, query, pool))
    return bm25_ranked_score(axes, len(pool))
