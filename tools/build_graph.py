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
import sys
from datetime import datetime, timezone
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# Deliberate local imports: after the sys.path insert above these resolve
# to plugin modules; module-level AUDITABLE imports stay stdlib-only
# (json/hashlib/argparse/datetime/pathlib/collections).
from search_index import BM25Index, tokenize  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

GRAPH_NAME = "graph.json"
GRAPH_BAK_NAME = "graph.json.bak"
_DEG_CAP = 5          # spec guardrail: <= 5 edges per node
_TOTAL_CAP = 2000     # spec guardrail: total edge cap
_LATIN_ENTITY = None  # lazily compiled (keep module-level audit clean)
import re
_LATIN_ENTITY = re.compile(r"[A-Za-z0-9]{3,}")   # case-sensitive literal entity
_CJK_TERM = re.compile(r"[\u4e00-\u9fff]{2,}")   # CJK term heuristic

_K1_DEFAULT = float(DECAY_DEFAULTS["search"]["bm25_k1"])
_K_B_DEFAULT = float(DECAY_DEFAULTS["search"]["bm25_b"])
_MIN_OVERLAP_DEFAULT = float(DECAY_DEFAULTS["search"]["graph_min_overlap"])
_WINDOW_DAYS_DEFAULT = float(DECAY_DEFAULTS["search"]["graph_temporal_window_days"])


def default_storage_root() -> Path:
    """Mirror the provider's auto tier: <base>/filemem where <base> is
    HERMES_HOME (or ~/.hermes) minus a trailing profiles/<name> segment."""
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
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
    cfg = {
        "bm25_k1": _K1_DEFAULT,
        "bm25_b": _K_B_DEFAULT,
        "graph_min_overlap": _MIN_OVERLAP_DEFAULT,
        "graph_temporal_window_days": _WINDOW_DAYS_DEFAULT,
    }
    decay = storage_root / "decay.json"
    if decay.is_file():
        try:
            block = (json.loads(decay.read_text(encoding="utf-8"))
                     .get("search") or {})
            for k in cfg:
                if k in block and block[k] is not None:
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
    edges = []
    edges += _build_temporal(items, float(cfg["graph_temporal_window_days"]))
    edges += _build_semantic(items, float(cfg["bm25_k1"]),
                             float(cfg["bm25_b"]),
                             float(cfg["graph_min_overlap"]))
    edges += _build_cooccurrence(items)
    # Undirected dedupe + guardrails, in deterministic order.
    seen_edges, kept = set(), []
    degree = {i: 0 for i in ids}
    for e in sorted(edges, key=lambda e: (e["src"], e["dst"], e["kind"],
                                          e.get("w", 0.0),
                                          json.dumps(e, sort_keys=True))):
        key = (e["src"], e["dst"], e["kind"])
        if key in seen_edges:
            continue
        if len(kept) >= _TOTAL_CAP:
            break
        if degree.get(e["src"], 0) >= _DEG_CAP or degree.get(e["dst"], 0) >= _DEG_CAP:
            continue
        seen_edges.add(key)
        degree[e["src"]] = degree.get(e["src"], 0) + 1
        degree[e["dst"]] = degree.get(e["dst"], 0) + 1
        kept.append(e)
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
                return result
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/legacy file: proceed to (re)write under .bak
        bak = storage_root / GRAPH_BAK_NAME
        if not bak.is_file():  # first mutation only — keep the original
            bak.write_bytes(out.read_bytes())
            result["backup"] = str(bak)
    out.write_text(serialize(graph), encoding="utf-8")
    result["written"] = str(out)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SA2-4: deterministic graph skeleton builder (zero LLM)")
    ap.add_argument("--storage-root", default=None,
                    help="filemem root (default: auto-resolved <base>/filemem)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report node/edge counts; write nothing (not even .bak)")
    args = ap.parse_args()
    root = Path(args.storage_root).expanduser() if args.storage_root else default_storage_root()
    if not root.is_dir():
        print(f"ERROR: storage root not found: {root}", file=sys.stderr)
        return 2
    summary = build_and_write(root, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
