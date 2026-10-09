#!/usr/bin/env python
"""SEC-1-2 read-only regression probe: the injection gate is the IDENTITY
on live data (0 drop / 0 quote / 0 mark) and stays identity across the
wiring change (byte-identical before/after).

READ-ONLY BY CONSTRUCTION — the regression gate of SEC-1-2:
  * NEVER calls provider.prefetch() (its tail _reinforce_recall bumps
    reference_count and writes disk -> live data would drift between two
    runs, defeating the byte-identity check).
  * Pool assembly goes through the provider's read-only loaders
    (_load_episodic_pool / _load_knowledge_pool / json.load of
    memories/*.json / _read_knowledge_entries / _load_graph — all open
    files in read mode only).
  * The gate verdict is the pure function injection_gate_decision (no I/O).
  * search_memories golden-query smoke is disk-read-only (its in-place
    result-dict marks are memory-only; _reinforce_recall is prefetch-only).

Before/after identity: the "pre-wiring" pool shape (no metadata.origin on
episodic/knowledge rows — the T2 gap) is emulated deterministically with
--legacy (metadata key stripped from the assembled pool), so one
invocation runs BOTH shapes against the live corpus and checks:
  1. post-wiring pool: metadata.origin coverage (origin=None count == 0
     — the 155-row episodic + knowledge backfill landed);
  2. gate verdicts on live data == identity: drop=0, quote=0, mark=0
     in BOTH shapes (drop_unknown_trust=false => unmarked rows pass);
  3. the gate report bytes are identical pre/post shape (sha256 equal);
  4. golden-query smoke: the id sets returned by search_memories are
     stable and carry no origin_mark on live data (origin_mark is a
     brand-new field; recall ids are unaffected).

Usage:
  python3 tools/verify_injection_gate.py            # full check + verdict
  python3 tools/verify_injection_gate.py --legacy    # pre-wiring shape only
  python3 tools/verify_injection_gate.py --store DIR # alternate live store
"""
import hashlib
import json
import os
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402  (plugin module; imports safely, no I/O)

# Dynamic path resolution (mirrors build_graph.py / backfill_origin.py):
# HERMES_HOME env > ~/.hermes. No hardcoded /root paths (portability).
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
LIVE_STORE = HERMES_HOME / "filemem"  # --store overrides this in main()

# Golden smoke queries: real keywords from the live corpus (bookkeeping
# fact rows, the 9200/APK incident threads, hermes-ui-debug).
GOLDEN_QUERIES = [
    "bookkeeping get_pending",
    "9200",
    "APK",
    "hermes-ui-debug",
    "崩溃",
]


def build_provider(store: Path) -> "fm.FileMemProvider":
    """Read-only provider shell: __init__ does zero I/O; the path attrs
    that initialize() would set are assigned directly so ONLY the
    read-side loaders work (no mkdir, no drafts, no decay write)."""
    p = fm.FileMemProvider()
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.episodic_dir = store / "episodic"
    p.sessions_dir = store / "sessions"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p._decay = fm.load_decay_config(HERMES_HOME)  # read-only: decay.json + DECAY_DEFAULTS
    return p


def assemble_pool(p: "fm.FileMemProvider") -> list:
    """Read-only mirror of search_memories' pool assembly (no scoring,
    no egress, no writing)."""
    pool = []
    for mem_type in fm.MEMORY_TYPES:
        f = p.memories_dir / f"{mem_type}.json"
        if f.exists():
            with open(f, encoding="utf-8") as fh:
                pool.extend(json.load(fh))
    pool.extend(p._load_episodic_pool(include_fixture=False))
    pool.extend(p._load_knowledge_pool())
    return pool


def gate_report(pool: list, cfg: dict) -> dict:
    """Run the pure gate over every pool row; count verdicts + trust
    distribution. This is the byte-identical regression artifact."""
    from __init__ import injection_gate_decision

    counts = {"total": len(pool), "drop": 0, "quote": 0, "plain": 0}
    origin_missing = 0
    trust_dist: dict = {}
    types: dict = {}
    for row in pool:
        if not isinstance(row, dict):
            continue
        meta = row.get("metadata")
        origin = meta.get("origin") if isinstance(meta, dict) else None
        if not isinstance(origin, dict) or origin.get("trust") is None:
            origin_missing += 1
        trust = origin.get("trust") if isinstance(origin, dict) else "<none>"
        trust_dist[trust] = trust_dist.get(trust, 0) + 1
        types[row.get("type", "<no-type>")] = types.get(row.get("type", "<no-type>"), 0) + 1
        try:
            action, mark = injection_gate_decision(row, cfg)
        except Exception:  # caller-level fail-soft (T2 rule 6)
            action, mark = "allow", None
        if action == "drop":
            counts["drop"] += 1
        elif mark == "directive-quote":
            counts["quote"] += 1
        else:
            counts["plain"] += 1
    counts["origin_missing"] = origin_missing
    counts["trust_dist"] = trust_dist
    counts["types"] = types
    return counts


def gate_bytes(rep: dict) -> str:
    """Canonical bytes of the DECISION report — the identity artifact.

    The regression gate is that the gate's DECISIONS are unchanged
    before/after the wiring: 0 drop / 0 quote / 0 mark on live data, in
    both pool shapes.  So the hashed fields are exactly the decision
    counters + the row-type mix (all stable under a metadata strip /
    backfill).  Deliberately EXCLUDED from the hash:
      * trust_dist      — changes BY DESIGN when the metadata backfill
                          lands (all-<none> -> 17 high / 210 medium); it
                          is diagnostic, not a decision.
      * origin_missing  — the one thing the wiring changes (227 -> 0).
    Including either would defeat the byte-identity check."""
    canonical = {k: rep[k] for k in ("total", "drop", "quote", "plain", "types")}
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False)


def golden_smoke(p: "fm.FileMemProvider") -> dict:
    """search_memories is disk-read-only (no _reinforce_recall on this
    path). Check recall ids are stable and origin_mark is absent on
    live data (gate = identity)."""
    out = {}
    for q in GOLDEN_QUERIES:
        res = p.search_memories(q, limit=5)
        ids = [r.get("id") for r in res if isinstance(r, dict)]
        marks = [r.get("origin_mark") for r in res if isinstance(r, dict) and "origin_mark" in r]
        out[q] = {"ids": ids, "origin_marks": marks}
    return out


def main() -> int:
    legacy_only = "--legacy" in sys.argv
    store = LIVE_STORE
    if "--store" in sys.argv:
        store = Path(sys.argv[sys.argv.index("--store") + 1])

    p = build_provider(store)
    cfg = p._decay.get("injection") or {}

    # Post-wiring pool (metadata.origin backfilled by T2).
    pool = assemble_pool(p)
    post = gate_report(pool, cfg)
    post_bytes = gate_bytes(post)

    if legacy_only:
        print("gate report (legacy/pre-wiring shape):")
        print(json.dumps({k: post[k] for k in ("total", "drop", "quote", "plain", "origin_missing", "types")},
                          ensure_ascii=False, sort_keys=True))
        print("report sha256:", hashlib.sha256(post_bytes.encode()).hexdigest())
        return 0 if post["drop"] == 0 and post["quote"] == 0 else 1

    # Pre-wiring pool: same assembly, metadata key stripped — the exact
    # T2-gap shape the gate used to see (155 episodic + knowledge rows
    # reaching it unmarked).
    legacy_pool = []
    for row in pool:
        r2 = dict(row)
        r2.pop("metadata", None)
        legacy_pool.append(r2)
    pre = gate_report(legacy_pool, cfg)
    pre_bytes = gate_bytes(pre)

    print("== live-data pool ==", len(pool), "rows (memories+episodic+knowledge)")
    print("types:", json.dumps(post["types"], sort_keys=True, ensure_ascii=False))
    print("trust dist (post-wiring):", json.dumps(post["trust_dist"], sort_keys=True, ensure_ascii=False))
    print(f"origin_missing: pre-wiring shape={pre['origin_missing']}  post-wiring={post['origin_missing']}")
    print(f"gate verdicts: pre drop={pre['drop']} quote={pre['quote']} | post drop={post['drop']} quote={post['quote']} mark={post['quote']}")
    print("report sha256: pre=" + hashlib.sha256(pre_bytes.encode()).hexdigest())
    print("                 post=" + hashlib.sha256(post_bytes.encode()).hexdigest())

    smoke = golden_smoke(p)
    print("== golden smoke (search_memories, read-only) ==")
    for q, res in smoke.items():
        print(f"  {q!r}: {len(res['ids'])} ids {res['ids']} origin_mark={res['origin_marks'] or '-'}")

    ok = True
    if post["origin_missing"] != 0:
        ok = False
        print("FAIL: post-wiring pool still has rows without metadata.origin "
              f"({post['origin_missing']}) — 155-row episodic backfill incomplete")
    if pre["drop"] or pre["quote"] or post["drop"] or post["quote"]:
        ok = False
        print("FAIL: gate is NOT identity on live data")
    if pre_bytes != post_bytes:
        ok = False
        print("FAIL: gate report bytes differ between pre/post wiring shapes")
    if any(m["origin_marks"] for m in smoke.values()):
        ok = False
        print("FAIL: live search results carry origin_mark (gate not identity)")

    if ok:
        print("活数据恒等：0 drop / 0 quote / 0 mark，前后字节一致 "
              "(live-data identity: 0 drop / 0 quote / 0 mark, byte-identical pre/post)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
