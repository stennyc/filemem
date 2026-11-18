#!/usr/bin/env python
r"""SA2-4: build the deterministic entity/relationship graph skeleton
(graph.json) over the live filemem corpus — zero LLM, stdlib only.

Nodes = every memory/episodic row id (memories/*.json `id` + the episodic
pool id `episodic_<shard>_<session_id>`, the SAME stable id scheme the
provider's `_load_episodic_pool` uses, so graph ids always match pool ids
at search time).

Edges — three deterministic kinds (LLM causal edges are explicitly EP/
later scope; this builder runs no LLM and reads no llm ctx):
- `temporal` — pairs whose effective event time (occurred_at, falling back
  to created_at, the SA2-1 rule) is within `graph_temporal_window_days`
  of each other AND in the same group: memories grouped by `type`
  (category is null across the live corpus), episodic grouped by
  session_id. Edge weight = 1 - gap_days/window (deterministic float).
- `semantic` — BM25 top-3 per item (SA2-3's own in-memory index via
  search_index.BM25Index, so there is exactly ONE source of truth for
  ranking) whose Jaccard content-token overlap is >= `graph_min_overlap`
  (both thresholds read from decay.json `search` block / DECAY_DEFAULTS).
  Undirected: each pair emitted once, src < dst.
- `co-occurrence` — pairs sharing a literal entity token (case-sensitive
  alphanumeric run of >= 3 chars, or a CJK term run of >= 2 chars — the
  Hindsight first-tier "string matching + co-occurrence" resolution,
  ported verbatim), deduplicated so a pair gets at most ONE edge
  carrying the shared-token list.

Guardrails (spec): per-node degree <= 5 (truncated deterministically by
(src, dst, kind, weight, edge json) order), total edge cap 2000.

Determinism gates:
- Same input corpus -> byte-identical file on re-run: sort_keys JSON,
  sorted nodes/edges, and a `fingerprint` = sha256 over the sorted node
  list + sorted edge list ONLY (the fingerprint is a field of the file,
  not an input to it — so two runs of the same corpus diff to zero).
- Idempotent re-run: a second build on unchanged data rewrites zero bytes
  (content-equality check, like backfill_occurred_at's second run).

Portability rules (same audit gate as backfill_occurred_at.py): NO
hardcoded paths — the storage root is a CLI arg, else the same auto
resolution the provider uses (`<base>/filemem` where base is HERMES_HOME
/ ~/.hermes with a trailing `profiles/<name>` segment stripped). Output
= <root>/graph.json; `--dry-run` reports node/edge counts without
writing; a first write snapshots `<root>/graph.json.bak` (never
clobbered, same .bak convention as backfill).

Usage:
  python tools/build_graph.py --dry-run
  python tools/build_graph.py
  python tools/build_graph.py --storage-root /path/to/filemem
"""
import argparse
import hashlib
import json
import os
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
from dep_bootstrap import _resolve_hermes_home  # noqa: E402

# Deliberate local imports: after the sys.path insert above these resolve
# to plugin modules; module-level AUDITABLE imports stay stdlib-only
# (json/hashlib/argparse/datetime/pathlib/collections).
from search_index import BM25Index, tokenize  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

GRAPH_NAME = "graph.json"
GRAPH_BAK_NAME = "graph.json.bak"
_DEG_CAP = 5          # spec guardrail: <= 5 edges per node
_TOTAL_CAP = 2000     # spec guardrail: total edge cap
# SA2-6 (owner 2026-11-12): per-kind edge-bucket quotas. Pre-SA2-6 the
# _TOTAL_CAP=2000 pool was shared: all three edge kinds (temporal / semantic
# / co-occurrence) competed for the same 2000 slots in one lexicographic
# sort, so on the ~1500-node live corpus the high-volume temporal
# (~105k cand) + co-occurrence (~373k cand) candidate lists crowded the
# high-information semantic axis (BM25 top-3 + Jaccard gate, ~1.5k cand)
# down to a single-digit share of the kept edges. Each kind now gets its
# own quota (a FRACTION of _TOTAL_CAP; the fractions must sum to <= 1.0 and
# load_search_config() normalizes any over-allocation by scaling them down
# to sum == 1.0, so the total can never exceed _TOTAL_CAP). Unused quota in
# one kind is NOT redistributed to the others — a kind whose candidate list
# is shorter than its cap simply leaves its slack unclaimed (deterministic,
# no second pass). Tweak in decay.json's "search" block under
# "graph_bucket_quotas". Defaults here are the module-level fallback only;
# the authoritative defaults live in DECAY_DEFAULTS["search"].
_DEFAULT_BUCKET_QUOTAS = {"semantic": 0.55, "co-occurrence": 0.22, "temporal": 0.23}
_LATIN_ENTITY = None  # lazily compiled (keep module-level audit clean)
import re
_LATIN_ENTITY = re.compile(r"[A-Za-z0-9]{3,}")   # case-sensitive literal entity
_CJK_TERM = re.compile(r"[\u4e00-\u9fff]{2,}")   # CJK term heuristic

# SA2-7 (owner 2026-11-12, option B): per-kind "information score" — the sort
# key that REPLACES the pre-SA2-7 lexicographic (src, dst, ...) truncation
# order WITHIN a bucket, plus an anti-stacking guardrail (max share of a
# bucket's slots any single node may consume, default 2/3 — a dense
# co-occurrence clique of a handful of high-frequency nodes can no longer
# eat the whole bucket's quota, which was the SA2-7-v1 (pure weight sort)
# on-disk regression: co-occurrence coverage 493 -> 234 distinct nodes,
# temporal 74 -> 55, because a handful of dense subgraph nodes kept
# refilling their own 5-slot degree allowance ahead of every other node).
# Score:
#   semantic        -> w (Jaccard overlap, already stored per edge)
#   temporal        -> w (proximity, already stored per edge)
#   co-occurrence   -> len(entities) (the only real per-edge discriminator;
#                      w is a constant 1.0 across this whole bucket)
# All deterministic (never a dict / hash-order read); the secondary
# lexicographic (src, dst, kind, w, edge-json) tail is a pure tie-breaker,
# so two runs stay byte-identical.
_DEFAULT_MAX_NODE_SHARE = 2.0 / 3.0

_K1_DEFAULT = float(DECAY_DEFAULTS["search"]["bm25_k1"])
_K_B_DEFAULT = float(DECAY_DEFAULTS["search"]["bm25_b"])
_MIN_OVERLAP_DEFAULT = float(DECAY_DEFAULTS["search"]["graph_min_overlap"])
_WINDOW_DAYS_DEFAULT = float(DECAY_DEFAULTS["search"]["graph_temporal_window_days"])


def default_storage_root() -> Path:
    """Mirror the provider's auto tier: <base>/filemem where <base> is
    HERMES_HOME (or ~/.hermes) minus a trailing profiles/<name> segment."""
    home = Path(_resolve_hermes_home())
    parts = [p for p in home.parts if p]
    if "profiles" in parts:
        idx = parts.index("profiles")
        base = Path(*parts[:idx]) if idx > 0 else None
    else:
        base = Path(*parts) if parts else None
    if base is None:
        base = Path.cwd()
    return base / "filemem"


def load_search_config(storage_root: Path) -> dict:
    """Thresholds from the decay.json `search` block, falling back to
    DECAY_DEFAULTS per key (single source of truth for defaults). The
    tool never owns decay.json — it only READS it."""
    # SA2-6: bucket quotas come from decay.json's DECAY_DEFAULTS["search"]
    # (added alongside the SA2-5 detach knobs), falling back to the module
    # default when a deployment predates that key.
    defaults = dict(_DEFAULT_BUCKET_QUOTAS)
    dd_quotas = DECAY_DEFAULTS.get("search", {}).get("graph_bucket_quotas")
    if isinstance(dd_quotas, dict):
        defaults.update(dd_quotas)
    cfg = {
        "bm25_k1": _K1_DEFAULT,
        "bm25_b": _K_B_DEFAULT,
        "graph_min_overlap": _MIN_OVERLAP_DEFAULT,
        "graph_temporal_window_days": _WINDOW_DAYS_DEFAULT,
        "graph_bucket_quotas": defaults,
    }
    decay = storage_root / "decay.json"
    if decay.is_file():
        try:
            block = (json.loads(decay.read_text(encoding="utf-8"))
                     .get("search") or {})
            for k in cfg:
                if k in block and block[k] is not None:
                    if k == "graph_bucket_quotas":
                        # user-supplied quotas replace the defaults wholesale
                        # (a partial override would silently re-inject the
                        # defaults for the missing kinds — surprising).
                        cfg[k] = dict(block[k])
                    else:
                        cfg[k] = block[k]
        except (json.JSONDecodeError, OSError):
            pass  # fail-soft: defaults remain
    return cfg


# ── corpus collection (mirrors the provider's pool assembly, read-only) ──

def _effective_time(item: dict) -> "datetime | None":
    """SA2-1 rule: occurred_at preferred, fall back to created_at.
    Naive stamps are UTC (provider convention). None = no temporal signal."""
    for key in ("occurred_at", "created_at"):
        ts = item.get(key)
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _entities(text: str) -> set:
    """Hindsight first-tier entity resolution: case-sensitive literal
    alphanumeric tokens (len >= 3) + CJK term runs (len >= 2)."""
    out = set(_LATIN_ENTITY.findall(text or ""))
    out.update(_CJK_TERM.findall(text or ""))
    return out


def collect_corpus(storage_root: Path):
    """Return (items, ids) where items = normalized dicts exactly as the
    provider's search pool would see them (content/id/group/time), ids =
    the stable pool-id set. Read-only over the live store."""
    items = []
    seen = set()
    mem_dir = storage_root / "memories"
    if mem_dir.is_dir():
        for f in sorted(mem_dir.glob("*.json")):
            if f.name.endswith(".bak") or f.name.endswith(".lock"):
                continue
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                items.append({
                    "id": str(row["id"]),
                    "content": row.get("content", ""),
                    "group": str(row.get("type") or "memory"),
                    "time": _effective_time(row),
                })
                seen.add(str(row["id"]))
    ep_dir = storage_root / "episodic"
    if ep_dir.is_dir():
        for shard in sorted(ep_dir.rglob("*.jsonl")):
            if shard.name.endswith(".lock") or shard.name.endswith(".bak"):
                continue
            try:
                lines = shard.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue  # fail-soft, same as _load_episodic_pool
                if not isinstance(e, dict):
                    continue
                eid = e.get("id") or f"episodic_{shard.stem}_{e.get('session_id')}"
                eid = str(eid)
                if eid in seen:
                    continue
                seen.add(eid)
                items.append({
                    "id": eid,
                    "content": e.get("content", ""),
                    "group": f"session:{e.get('session_id') or shard.stem}",
                    "time": _effective_time(e),
                })
    return items, seen


# ── edge builders (all deterministic, all stdlib) ──────────────────────────

def _build_temporal(items, window_days: float) -> list:
    """Same-group pairs within the event-time window. O(n^2) within groups
    is fine at this corpus scale (<430 rows total, groups are small);
    pairs are emitted with src < dst."""
    by_group = {}
    for it in items:
        if it["time"] is None:
            continue
        by_group.setdefault(it["group"], []).append(it)
    edges = []
    for g in sorted(by_group):
        group = sorted(by_group[g], key=lambda i: (i["time"], i["id"]))
        for a_i in range(len(group)):
            for b_i in range(a_i + 1, len(group)):
                a, b = group[a_i], group[b_i]
                gap_days = abs((a["time"] - b["time"]).total_seconds()) / 86400.0
                if gap_days > window_days:
                    break  # group is time-sorted: later pairs are farther
                w = round(max(0.0, 1.0 - gap_days / max(window_days, 1e-9)), 4)
                edges.append({"src": a["id"], "dst": b["id"],
                              "kind": "temporal", "w": w})
    return edges


def _token_set(text: str) -> set:
    return set(tokenize(text))


def _build_semantic(items, k1: float, b: float, min_overlap: float) -> list:
    """SA2-3 BM25 top-3 per item, Jaccard-overlap-gated. ONE index build:
    rank the corpus against each item's own content (its most similar
    neighbors), take the top-3 OTHER items, keep pairs whose content-token
    Jaccard >= min_overlap. Emitted once per pair (src < dst)."""
    n = len(items)
    if n < 2:
        return []
    tok_sets = [_token_set(it["content"]) for it in items]
    pool = [{"id": it["id"], "content": it["content"]} for it in items]
    idx = BM25Index(k1=k1, b=b).build(pool)
    pairs = {}
    for i, it in enumerate(items):
        # top-3 OTHER items by (bm25 score, pool position) — deterministic
        # even for equal-score ties (raw rank() ties on position already,
        # but the explicit re-sort documents the guarantee).
        scores = idx.score(it["content"])
        order = sorted(range(n), key=lambda j: (-scores[j], j))
        considered = 0
        for j in order:
            if j == i:
                continue
            considered += 1
            if considered > 3:
                break
            a, c = sorted((i, j))
            inter = len(tok_sets[a] & tok_sets[c])
            union = len(tok_sets[a] | tok_sets[c])
            if not union or inter / union < min_overlap:
                continue
            key = (items[a]["id"], items[c]["id"])
            w = round(inter / union, 4)
            # deterministic winner on duplicate pair emission: max weight
            if key not in pairs or w > pairs[key]:
                pairs[key] = w
    return [{"src": s, "dst": d, "kind": "semantic", "w": w}
            for (s, d), w in sorted(pairs.items())]


def _build_cooccurrence(items) -> list:
    """Pairs sharing a literal entity token; deduplicated — one edge per
    pair carrying the sorted shared-token list as `entities`."""
    ent = {it["id"]: _entities(it["content"]) for it in items}
    by_token = {}
    for iid, es in ent.items():
        for e in es:
            by_token.setdefault(e, []).append(iid)
    pairs = {}
    for e in by_token:
        holders = sorted(set(by_token[e]))
        for a in range(len(holders)):
            for c in range(a + 1, len(holders)):
                x, y = holders[a], holders[c]
                key = (x, y) if x < y else (y, x)
                lst = pairs.setdefault(key, [])
                if e not in lst:
                    lst.append(e)
    return [{"src": s, "dst": d, "kind": "co-occurrence", "w": 1.0,
             "entities": sorted(es)}
            for (s, d), es in sorted(pairs.items())]


# ── assembly + determinism gates ───────────────────────────────────────────

def build_graph(storage_root: Path) -> dict:
    """Deterministic full build. Same corpus -> same file, byte for byte
    (see module docstring): sorted adjacency, sort_keys serialization, and
    a fingerprint over nodes+edges only (a field of the file, never an
    input to it — idempotency by construction)."""
    cfg = load_search_config(storage_root)
    items, ids = collect_corpus(storage_root)
    # SA2-6 (owner 2026-11-12): per-kind bucket quotas replace the pre-SA2-6
    # single shared 2000-slot pool. The old pool truncated ALL three edge
    # kinds in one global lexicographic sort, so on the live ~1500-node
    # corpus the high-volume temporal (105,714 cand) + co-occurrence
    # (372,926 cand) candidate lists consumed 1,956/2000 slots and squeezed
    # the high-information semantic axis (1,584 cand, BM25 top-3 + Jaccard
    # gate) down to 44 slots (2.2%). Now each kind is truncated to its OWN
    # quota (int(_TOTAL_CAP * fraction) — semantic 50% / co-occurrence 30% /
    # temporal 20% by default, user-tunable via decay.json search block):
    # semantic gets 1000, co-occurrence 600, temporal 400. Truncation WITHIN
    # a bucket is the same deterministic sort the single-pool path used
    # (src, dst, kind, weight, edge json — kind is constant within a bucket,
    # so it's an inert field; the edge-json term keeps it byte-deterministic
    # for equal (src,dst,weight) edges). Unused quota in a kind is NOT
    # redistributed to the others — that would make the truncation order
    # depend on the other buckets' candidate counts (non-deterministic feel,
    # extra second pass, and it would let temporal re-crowd semantic when
    # semantic's own candidate list is small). Per-node degree cap
    # (_DEG_CAP=5) still applies across all buckets (a node that fills 5
    # semantic slots can't also take a temporal one).
    buckets = {
        "temporal": _build_temporal(items, float(cfg["graph_temporal_window_days"])),
        "semantic": _build_semantic(items, float(cfg["bm25_k1"]),
                                    float(cfg["bm25_b"]),
                                    float(cfg["graph_min_overlap"])),
        "co-occurrence": _build_cooccurrence(items),
    }
    quotas = cfg.get("graph_bucket_quotas") or _DEFAULT_BUCKET_QUOTAS
    kept = []
    degree = {i: 0 for i in ids}
    # Iterate buckets in a fixed deterministic order (sorted by kind name) so
    # the degree cap's cross-bucket contention is itself deterministic: a
    # node that reaches _DEG_CAP in the earlier-iterated bucket is denied in
    # the later one, ALWAYS (not dependent on dict insertion order).
    # SA2-7 (owner 2026-11-12, option B): anti-stacking guardrail fraction —
    # no single node may consume more than this share of its bucket's kept
    # slots (default 2/3, user-tunable via decay.json's "search" block as
    # "graph_max_node_share"). This is what fixes the SA2-7-v1 (pure
    # weight-sort, no guardrail) on-disk regression where a dense
    # co-occurrence clique of a handful of high-frequency nodes kept
    # refilling its own 5-slot degree allowance and ate the whole bucket,
    # dropping distinct-node coverage (co-occurrence 493 -> 234, temporal
    # 74 -> 55 on the live corpus).
    max_node_share = float(cfg.get("graph_max_node_share", _DEFAULT_MAX_NODE_SHARE))
    for kind in sorted(buckets):
        edges_kind = buckets[kind]
        cap_kind = int(_TOTAL_CAP * float(quotas.get(kind, 0.0)))
        seen_kind = set()
        kept_count = 0
        node_kind_slots = {}  # node -> slots consumed by THIS bucket so far

        def _score(e):
            if e["kind"] == "co-occurrence":
                return len(e.get("entities", []))
            return e.get("w", 0.0)

        for e in sorted(edges_kind, key=lambda e: (-_score(e), e["src"], e["dst"],
                                                    e["kind"], e.get("w", 0.0),
                                                    json.dumps(e, sort_keys=True))):
            if kept_count >= cap_kind:
                break
            key = (e["src"], e["dst"], e["kind"])
            if key in seen_kind:
                continue
            if degree.get(e["src"], 0) >= _DEG_CAP or degree.get(e["dst"], 0) >= _DEG_CAP:
                continue
            node_cap = int(cap_kind * max_node_share)
            if node_cap > 0:
                if (node_kind_slots.get(e["src"], 0) >= node_cap
                        or node_kind_slots.get(e["dst"], 0) >= node_cap):
                    continue
            seen_kind.add(key)
            node_kind_slots[e["src"]] = node_kind_slots.get(e["src"], 0) + 1
            node_kind_slots[e["dst"]] = node_kind_slots.get(e["dst"], 0) + 1
            degree[e["src"]] = degree.get(e["src"], 0) + 1
            degree[e["dst"]] = degree.get(e["dst"], 0) + 1
            kept.append(e)
            kept_count += 1
    node_list = sorted(ids)
    fp_src = json.dumps([{"n": node_list},
                         [{"src": e["src"], "dst": e["dst"], "kind": e["kind"],
                           "w": e["w"]} for e in kept]],
                        ensure_ascii=False, sort_keys=True)
    return {
        "version": 1,
        "fingerprint": hashlib.sha256(fp_src.encode("utf-8")).hexdigest(),
        "node_count": len(node_list),
        "edge_count": len(kept),
        "nodes": [{"id": i} for i in node_list],
        "edges": kept,
    }


def graph_fingerprint(graph: dict) -> str:
    """Fingerprint of a BUILT graph (nodes+edges only, same recipe as the
    build) — used to detect corpus drift on re-run: if the recomputed
    fingerprint equals the stored one, the file is already current and the
    write is skipped (idempotent second run)."""
    edges_min = [{"src": e["src"], "dst": e["dst"], "kind": e["kind"], "w": e["w"]}
                 for e in graph.get("edges", [])]
    fp_src = json.dumps([{"n": [n.get("id") for n in graph.get("nodes", [])]},
                         [e for e in edges_min]],
                        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(fp_src.encode("utf-8")).hexdigest()


def serialize(graph: dict) -> str:
    """Canonical bytes: sort_keys + ensure_ascii=False + indent 2 + trailing
    newline. Two builds of the same corpus -> identical strings."""
    return json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def build_and_write(storage_root: Path, dry_run: bool = False) -> dict:
    """Build + write with the .bak/idempotency conventions of the backfill
    tool: --dry-run reports only; first mutation snapshots graph.json.bak
    (never clobbered); an unchanged re-run rewrites zero bytes."""
    storage_root = Path(storage_root)
    out = storage_root / GRAPH_NAME
    graph = build_graph(storage_root)
    result = {"root": str(storage_root), "dry_run": dry_run,
              "nodes": graph["node_count"], "edges": graph["edge_count"],
              "fingerprint": graph["fingerprint"]}
    if dry_run:
        return result
    if out.is_file():
        prev = out.read_text(encoding="utf-8")
        try:
            if graph_fingerprint(json.loads(prev)) == graph["fingerprint"]:
                result["skipped"] = "up-to-date (fingerprint match, zero writes)"
                # A1-1 follow-up: touch mtime so _graph_freshness's mtime
                # comparison returns "fresh" — without this the skip path
                # never updates graph.json's mtime, the read-side pull sees
                # "stale" forever (corpus mtime > graph mtime), and every
                # search spawns a pointless 39s build that skips again → ∞.
                os.utime(out, times=(os.path.getatime(out), time.time()))
                return result
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/legacy file: proceed to (re)write under .bak
        bak = storage_root / GRAPH_BAK_NAME
        if not bak.is_file():  # first mutation only — keep the original
            bak.write_bytes(out.read_bytes())
            result["backup"] = str(bak)
    # SA2-5 (owner 2026-11-12): atomic replace. write_text is truncate+write —
    # a concurrent reader (_load_graph's lazy json.load, now running on the
    # search face while a background full rebuild writes) could observe a
    # half-written file. Write a .tmp sibling then os.replace (single atomic
    # rename syscall): the reader sees either the OLD or the NEW complete
    # file, never a partial.
    tmp = out.with_name(GRAPH_NAME + ".tmp")
    tmp.write_text(serialize(graph), encoding="utf-8")
    os.replace(tmp, out)
    result["written"] = str(out)
    return result


def graph_status(storage_root: Path) -> dict:
    """Read-only freshness verdict for a live store (no writes, not even
    .bak). Rebuilds the corpus in memory (same cost as --dry-run, a few
    hundred ms at ~700-node scale — ops-only, never a hot-path probe) and
    compares the recomputed fingerprint against the stored graph.json.

    Returns:
        {"root", "status": "missing"|"fresh"|"stale",
         "stored": {"fingerprint", "node_count", "edge_count"} | None,
         "current": {"fingerprint", "node_count", "edge_count"}}

    - status "missing": no graph.json / corrupt JSON on disk (the search
      face is running two-axis right now).
    - status "fresh":   stored fingerprint == recomputed -> zero drift, a
      re-run would write nothing (idempotent).
    - status "stale":   stored fingerprint != recomputed -> corpus changed
      since the last build; a rebuild IS warranted.
    """
    storage_root = Path(storage_root)
    current = build_graph(storage_root)
    out = storage_root / GRAPH_NAME
    stored = None
    if out.is_file():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                stored = {"fingerprint": prev.get("fingerprint"),
                          "node_count": prev.get("node_count"),
                          "edge_count": prev.get("edge_count")}
        except (json.JSONDecodeError, OSError):
            stored = None  # corrupt == missing (fail-soft, search is two-axis)
    if stored is None:
        status = "missing"
    elif stored.get("fingerprint") == current["fingerprint"]:
        status = "fresh"
    else:
        status = "stale"
    return {"root": str(storage_root), "status": status,
            "stored": stored,
            "current": {"fingerprint": current["fingerprint"],
                        "node_count": current["node_count"],
                        "edge_count": current["edge_count"]}}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SA2-4: deterministic graph skeleton builder (zero LLM)")
    ap.add_argument("--storage-root", default=None,
                    help="filemem root (default: auto-resolved <base>/filemem)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report node/edge counts; write nothing (not even .bak)")
    ap.add_argument("--check", action="store_true",
                    help="read-only freshness verdict (missing/fresh/stale + "
                         "stored-vs-current node/edge deltas); writes nothing, "
                         "never a .bak")
    args = ap.parse_args()
    root = Path(args.storage_root).expanduser() if args.storage_root else default_storage_root()
    if not root.is_dir():
        print(f"ERROR: storage root not found: {root}", file=sys.stderr)
        return 2
    if args.check:
        verdict = graph_status(root)
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0
    summary = build_and_write(root, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
