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

# SA2-5 (owner 2026-11-12): the full ~39s build is DETACHED from the tick
# thread's 10s join budget — it now runs on a dedicated daemon thread spawned
# by the provider (see FileMemProvider._graph_maybe_spawn_bg_build in
# __init__.py). ensure_fresh's old "graph_status full-rescan inside an 8s
# soft deadline -> permanent defer" dead loop is gone: needs_rebuild is an
# O(1) mtime gate only, and build_background carries the full build with no
# wall-time cap. Rebuilds are further throttled by the provider-side
# water-level + cooldown knobs (DECAY_DEFAULTS["search"].graph_rebuild_*).


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


def _mtime_verdict(root: Path) -> str:
    """O(1) mtime-only stale verdict (same recipe as the provider's
    _graph_freshness): graph.json mtime vs newest corpus file under
    memories/ + episodic/. Pure stat, zero corpus rescan, zero fingerprint.
    Returns 'missing' | 'fresh' | 'stale'. Fail-soft -> 'missing'."""
    graph_path = root / "graph.json"
    try:
        if not graph_path.is_file():
            return "missing"
        graph_mtime = graph_path.stat().st_mtime
    except OSError:
        return "missing"
    newest = 0.0
    for sub in ("memories", "episodic"):
        d = root / sub
        if not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    # SA2-5: use >= not >: a corpus write that lands in the SAME mtime
    # resolution window as a graph write (e.g. a test fixture, or the rare
    # real-world same-instant write) must still count as "stale" — err on
    # the side of triggering a background full rebuild (idempotent +
    # detached, no tick-thread cost) rather than betting on sub-microsecond
    # mtime ordering to catch every corpus drift.
    return "stale" if newest >= graph_mtime else "fresh"


def needs_rebuild(storage_root=None,
                  min_corpus_rows: int = MIN_CORPUS_ROWS) -> bool:
    """SA2-5: O(1) staleness gate for the tick thread. True iff a background
    full rebuild is warranted (corpus newer than graph.json, or graph.json
    missing) AND the store is large enough to warrant a graph. NEVER does a
    corpus rescan or fingerprint recompute — that moved to build_background
    on a dedicated daemon thread, so the tick thread's 10s join budget is
    intact and the old "39s graph_status inside an 8s soft deadline"
    dead loop is gone. Fail-soft: any probe hiccup -> False (no rebuild
    this tick; the next tick retries)."""
    try:
        build_graph = _load_build_graph()
        root = Path(storage_root) if storage_root else build_graph.default_storage_root()
        if _count_corpus_rows(root) < min_corpus_rows:
            return False
        return _mtime_verdict(root) in ("stale", "missing")
    except Exception:
        return False


def build_background(storage_root=None) -> bool:
    """SA2-5: the FULL ~39s build_graph + atomic build_and_write, meant to
    run on a DEDICATED daemon thread with NO wall-time budget (detached from
    the tick thread's join). Returns True iff graph.json was actually
    written (a fingerprint-idempotent re-run that skips the write is
    reported as a log line and returns False, not an error). Re-entrancy is
    the CALLER's job — the provider guards it with a per-instance
    _graph_building flag + cooldown + water-level (see __init__.py)."""
    try:
        build_graph = _load_build_graph()
        root = Path(storage_root) if storage_root else build_graph.default_storage_root()
        result = build_graph.build_and_write(root)
        if result.get("skipped"):
            logger.info(f"FileMem: graph background build skipped ({result['skipped']})")
        return bool(result.get("written"))
    except Exception as e:
        logger.warning(f"FileMem: graph background build failed (graph.json intact): {e}")
        return False
