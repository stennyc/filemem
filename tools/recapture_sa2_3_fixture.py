#!/usr/bin/env python3
"""Re-capture tests/fixtures/sa2_3_golden_queries.json from the live store.

WHY: the golden-query fixture locks the SA2-3 RETRIEVABILITY gate — every
baseline id in `baseline_top5` must remain eligible in the post-RRF pool
(min_score=0.0, full pool). The live store drifts: rows get swept out
(unconfirmed_sweep), consolidated, or superseded, so a baseline captured on
day X stops being the true legacy top-5 on day Y. The gate itself is
correct; the FIXTURE goes stale. This tool re-captures the 11 golden
queries against the CURRENT live store in one command.

RUN (one command, reviewer round-2 runbook):

  cd /root/.hermes/plugins/filemem
  PYTHONPATH=/usr/local/lib/hermes-agent python3 tools/recapture_sa2_3_fixture.py

Behavior:
  * READ-ONLY over the live store (~/.hermes/filemem): builds the provider
    via FileMemProvider.__new__ + load_decay_config — zero initialize(),
    zero writes to the store (identical to the test module's
    _make_live_provider).
  * Keeps the SAME 11 golden queries (the fixture's `queries` block is the
    query list of record; re-capture never adds/removes/renames queries —
    it only refreshes each query's baseline_top5 + n_hits).
  * baseline_top5 = legacy control path: search_memories(q, limit=5,
    ranking="recency") — keyword-qualified, pure updated_at recency order
    (the pre-M4 naive ranking the gate's docstring calls "legacy").
  * n_hits = len(search_memories(q, limit=1000, ranking="recency")) —
    the keyword-qualified pool size for that query.
  * GATE PRE-FLIGHT: before writing, asserts every new baseline id is in
    the min_score=0.0 eligible pool (the test's own assertion, run early)
    so a capture that would itself be red can never land.
  * Updates `captured_at` to today (aware UTC) + a "path b" provenance
    note; preserves the fixture's `note` field verbatim.
  * Idempotent: re-running on an unchanged store rewrites an
    identical fixture (diff-to-zero), like build_graph.py.
"""
import importlib.util
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
FIXTURE = PLUGIN_DIR / "tests" / "fixtures" / "sa2_3_golden_queries.json"
LIVE_STORE = Path.home() / ".hermes" / "filemem"


def _make_live_provider():
    """Read-only provider over the live store (zero initialize(), zero
    writes) — mirrors tests/test_sa2_3_bm25_rrf.py::_make_live_provider so
    the capture path and the gate test path are byte-identical."""
    spec = importlib.util.spec_from_file_location(
        "filemem_init", PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["filemem_init"] = mod
    spec.loader.exec_module(mod)

    p = mod.FileMemProvider.__new__(mod.FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = LIVE_STORE / "memories"
    p.episodic_dir = LIVE_STORE / "episodic"
    p.sessions_dir = LIVE_STORE / "sessions"
    p.storage_dir = LIVE_STORE
    p.knowledge_dir = LIVE_STORE / "knowledge"
    p.knowledge_entries_path = LIVE_STORE / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = LIVE_STORE / "knowledge" / "raw_pending.json"
    p.pending_queue_path = (LIVE_STORE / "consolidated"
                            / "pending_upgrades.jsonl")
    p.source_profile = "sa2-3-test"
    p._decay = mod.load_decay_config()
    p._llm_ctx = None
    return p


def main() -> int:
    p = _make_live_provider()
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))

    recap = {
        "captured_at": (datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        + " (path b: re-captured via tools/recapture_sa2_3_fixture.py)"),
        "note": fix["note"],
        "queries": {},
    }
    gate_failures = 0
    for lang, entries in fix["queries"].items():
        recap_queries = []
        for e in entries:
            q = e["query"]
            # legacy control path: keyword-qualified, recency order
            rows = p.search_memories(q, limit=5, ranking="recency")
            baseline_top5 = [r.get("id") for r in rows if r.get("id")]
            # keyword-qualified pool size
            pool = p.search_memories(q, limit=1000, ranking="recency")
            n_hits = len(pool)
            # gate pre-flight: the test's own assertion, run BEFORE the
            # write so a red capture can never land
            eligible = set(
                r.get("id") for r in
                p.search_memories(q, limit=1000, min_score=0.0))
            missing = [i for i in baseline_top5 if i not in eligible]
            status = "OK" if not missing else f"MISSING: {missing}"
            if missing:
                gate_failures += 1
            print(f"{lang} {q!r}: top5={len(baseline_top5)} "
                  f"n_hits={n_hits} gate={status}")
            recap_queries.append(
                {"query": q, "baseline_top5": baseline_top5, "n_hits": n_hits})
        recap["queries"][lang] = recap_queries

    if gate_failures:
        print(f"\nABORT: {gate_failures} query(ies) fail the retrievability "
              "gate pre-flight — fixture NOT written. Inspect the live "
              "store before re-capturing.")
        return 1

    FIXTURE.write_text(
        json.dumps(recap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"\nwritten: {FIXTURE}")
    print("next: PYTHONPATH=/usr/local/lib/hermes-agent "
          "python3 -m pytest -q "
          "tests/test_sa2_3_bm25_rrf.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
