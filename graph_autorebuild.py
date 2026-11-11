"""SA2-4 graph auto-rebuild hook: rides the consolidation tick (dream
mechanism) to keep graph.json fresh. Zero LLM, zero new concurrency —
just "is the deterministic fingerprint stale, rebuild if so" using the
existing tools/build_graph.py primitives (imported by path, same
convention as consolidation/llm_caller.py's own loader, so there is no
new cross-module coupling beyond the one the provider already has).

Never raises: any failure (missing module, IO error, tool crash) is
swallowed to a warning and left for the next tick — the search face
keeps running two-axis (keyword+recency) exactly as it does today when
graph.json is absent/stale; this hook only ever makes it fresher, never
the other way round.

Throttling guarantees (SA2-4.1, 2026-11-11): the tick thread has a
bounded join budget (test_s1b_tick_wiring's 10s `th.join(timeout=10.0)`
asserts the thread is dead by then). A full live-store fingerprint
recompute (~1407 rows + episodic) can exceed that budget on a cold
cache, so two guards keep the hook inside the budget without touching
the search face or the tokenizer:
  1. min_corpus_rows: stores smaller than this skip the graph build
     entirely — tiny fixtures (and genuinely tiny live stores) never
     pay the cost of building a near-empty graph.
  2. max_wall_time_s: a soft deadline measured from THIS call's entry,
     checked between graph_status (the expensive stale-decision pass)
     and build_and_write (the expensive write pass); if the first
     pass alone already blew the budget, the second pass is skipped
     and the rebuild is deferred to the next tick. The deadline bounds
     only this hook's own wall time; it is NOT a proxy for the whole
     tick body's 4 prior LLM faces (those have their own fail-soft
     semantics and never block the join budget on their own — they
     self-skip cleanly when no host ctx is available).
"""
import importlib.util
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).resolve().parent
_MODULE_NAME = "_fm_build_graph"

MIN_CORPUS_ROWS = 100   # below this, a graph adds no retrieval value
MAX_WALL_TIME_S = 8.0   # stay well under the tick thread's 10s join budget


def _load_build_graph():
    """Load tools/build_graph.py by path (same convention as the provider's
    own _load_llm_caller: plugins load outside package context, so
    spec_from_file_location is the established, auditable loader here —
    no runtime sys.path mutation, no dependency on tools/ being a package)."""
    p = _PLUGIN_DIR / "tools" / "build_graph.py"
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _count_corpus_rows(root: Path) -> int:
    """Cheap O(n) row count over the memories/ shards only (no BM25, no
    fingerprint) — used to gate the min_corpus_rows check WITHOUT paying
    the expensive graph_status build cost on a tiny/empty store."""
    n = 0
    mem_dir = root / "memories"
    if mem_dir.is_dir():
        for f in mem_dir.glob("*.json"):
            if f.name.endswith(".bak") or f.name.endswith(".lock"):
                continue
            try:
                rows = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(rows, list):
                n += sum(1 for r in rows if isinstance(r, dict) and r.get("id"))
    ep_dir = root / "episodic"
    if ep_dir.is_dir():
        for shard in ep_dir.rglob("*.jsonl"):
            if shard.name.endswith(".lock") or shard.name.endswith(".bak"):
                continue
            try:
                lines = shard.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            n += sum(1 for ln in lines if ln.strip())
    return n


def ensure_fresh(storage_root=None,
                 min_corpus_rows: int = MIN_CORPUS_ROWS,
                 max_wall_time_s: float = MAX_WALL_TIME_S) -> bool:
    """Rebuild graph.json iff it is stale AND the store is large enough to
    warrant a graph; return True if a write happened, False if it was
    already fresh / missing / under the min-corpus threshold / over the
    soft deadline / or anything went wrong."""
    t0 = time.monotonic()
    try:
        build_graph = _load_build_graph()
        root = Path(storage_root) if storage_root else build_graph.default_storage_root()

        if _count_corpus_rows(root) < min_corpus_rows:
            return False

        verdict = build_graph.graph_status(root)
        if verdict["status"] != "stale":
            # "fresh": zero work. "missing": no corpus -> building an empty
            # graph adds nothing (search already degrades to two-axis in
            # that case, matching today's behavior — do NOT change it).
            return False

        if time.monotonic() - t0 > max_wall_time_s:
            # graph_status itself (the expensive stale-decision pass)
            # already blew the budget; skip the second expensive pass
            # (build_and_write) and let the next tick retry from scratch.
            logger.info("FileMem: graph auto-rebuild deferred (soft deadline "
                        f"{max_wall_time_s}s hit after graph_status)")
            return False

        result = build_graph.build_and_write(root)
        return bool(result.get("written"))
    except Exception as e:
        logger.warning(f"FileMem: graph auto-rebuild tick skipped: {e}")
        return False
