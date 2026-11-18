#!/usr/bin/env python3
"""EP-2 Observation layer — E2E smoke harness (host-model face).

Purpose: prove the Observation layer's host-model face is not just
mock-passing — a genuine `llm_caller.observe` call routes through the host
main model (PluginLlm default caller -> agent.auxiliary_client.call_llm with
task=None; NO hardcoded model name, NO custom api/provider config block) and
lands a valid `kind=observation` entry whose supported_by refs resolve.

This is a VERIFICATION HARNESS, not plugin runtime code. The plugin itself
(__init__.py / consolidation/) stays zero-external-dependency. Only this
tool lazily imports the host agent face, and ONLY under `--host`.

Two modes (both isolated — the live store is NEVER written):
  default (offline): a deterministic mock caller (zero LLM) exercises the full
    _observe_consolidate -> upsert_observation -> retrieval pool -> audit path
    in a tmp tree.  This is the offline, stdlib-only regression you can run
    anywhere; it is NOT a model-quality proof.
  --host: constructs the real `PluginLlm(plugin_id="filemem")` and lets it
    call the host main model (resolved from host config, never hardcoded).
    Requires the host agent to be importable in the running interpreter
    (i.e. run from the host venv / PYTHONPATH).  This is the end-to-end proof.

  --check-live STORE: read-only probe of a store's knowledge/raw_pending.json
    reporting how many raw topics carry RESOLVABLE memory_ids (i.e. can back a
    supported_by reference and be digested by the observe face) vs. how many
    are quarantined tool-dumps that cannot.  Does not write anything.

All paths come from CLI args / env — no hardcoded absolute paths.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
# Plugin root on sys.path so `import __init__` / `import consolidation` resolve
# no matter how the tool is invoked (python tools/ep2_observe_smoke.py puts
# tools/ — not the plugin root — on sys.path[0]). stdlib-only.
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
from dep_bootstrap import _resolve_hermes_home  # noqa: E402

# Built-in clean-facts example: one topic, three resolvable memory ids. This is
# the "clean observation source" the live raw_pending currently lacks (its 64
# rows are quarantined tool-dumps with 0 resolvable memory_ids).
DEMO_FACTS = {
    "fact_100": "The team deploys to staging on weekdays and to production only after a code review.",
    "fact_101": "Production deploys are gated behind a passing CI pipeline and a documented rollback plan.",
    "fact_102": "Deploy freezes run every Friday afternoon to keep the release train stable.",
}
DEMO_TOPIC = "deploy_policy"


def _build_tree(root: Path, facts, topic):
    """Seed an isolated store tree: memories/*.json (resolvable ids) + a clean
    raw_pending row per fact (CARRYING resolvable memory_ids)."""
    (root / "memories").mkdir(parents=True, exist_ok=True)
    (root / "knowledge").mkdir(parents=True, exist_ok=True)
    (root / "consolidated").mkdir(parents=True, exist_ok=True)
    (root / "memories" / "fact.json").write_text(
        json.dumps([
            {"id": mid, "type": "fact", "content": txt, "salience": 0.5,
             "created_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T00:00:00"}
            for mid, txt in facts.items()
        ], ensure_ascii=False), encoding="utf-8")
    raw_rows = [
        {"topic": topic, "content": txt, "memory_ids": [mid], "origin": "smoke",
         "reason": "", "created_ts": f"2026-10-01T00:00:0{i}"}
        for i, (mid, txt) in enumerate(facts.items())
    ]
    (root / "knowledge" / "raw_pending.json").write_text(
        json.dumps(raw_rows, ensure_ascii=False), encoding="utf-8")


def _provider(root: Path, llm_ctx_holder):
    """Minimal provider shim (__new__ skips initialize())."""
    import __init__ as fm  # local plugin module — allowed
    p = fm.FileMemProvider.__new__(fm.FileMemProvider)
    p._lock = threading.Lock()
    p._decay = {"knowledge": {"stale_min_overlap": 0.4, "observe_max_per_tick": 3}}
    p.storage_dir = root
    p.memories_dir = root / "memories"
    p.knowledge_dir = root / "knowledge"
    p.knowledge_entries_path = root / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = root / "knowledge" / "raw_pending.json"
    p.pending_queue_path = root / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "smoke"
    p.session_id = "smoke-session"
    p._observing_in_flight = False
    p._llm_ctx = llm_ctx_holder
    return p


def _mock_llm_ctx(facts):
    """Deterministic mock caller (zero LLM): returns a fixed observation whose
    supported_by quotes are literal substrings of the seeded facts. Offline
    regression only — NOT a model-quality proof."""
    class _Ctx:
        pass
    ctx = _Ctx()

    class _LLM:
        def complete_structured(self, *a, **k):
            class _R:
                pass
            r = _R()
            quotes = [t.strip().split(". ")[0][:60] for t in list(facts.values())][:3]
            r.parsed = {
                "observation": " ".join(quotes),
                "value": "high",
                "supported": [
                    {"id": mid, "quote": q} for mid, q in zip(list(facts)[:3], quotes)
                ],
            }
            r.parsed["model_used"] = True
            return r
    ctx.llm = _LLM()
    return ctx


def _host_llm_ctx():
    """REAL host main-model caller: PluginLlm(plugin_id='filemem') with the
    default caller (None) -> routes through the host main model (task=None, no
    hardcoded model name, no custom api/provider block). Lazily imported so
    the offline mode never drags the host agent in."""
    try:
        from agent.plugin_llm import PluginLlm
    except Exception as exc:
        raise SystemExit(
            "host agent not importable in this interpreter; run --host from the "
            f"host venv / with the agent on PYTHONPATH. (import error: {exc})"
        )
    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.llm = PluginLlm(plugin_id="filemem")
    return ctx


def _verify(root: Path, p, facts):
    print("\n--- VERIFY ---")
    ep = root / "knowledge" / "entries.json"
    ents = json.load(open(ep, encoding="utf-8")) if ep.exists() else []
    obs = [e for e in ents if e.get("kind") == "observation"]
    print(f"entries.json observations: {len(obs)}")
    mem_ids = set(facts)
    ok_refs = ok_substring = False
    if obs:
        o = obs[0]
        supported = o.get("supported_by") or []
        resolved = [s for s in supported if s.get("id") in mem_ids]
        print(f"observation id={o.get('id')} value={o.get('value')} "
              f"stale={o.get('stale')} proof_count={o.get('proof_count')}")
        print(f"supported_by resolved vs seed ids: {len(resolved)}/{len(supported)}")
        print(f"observation text: {o.get('content')}")
        ok_refs = bool(resolved) and len(resolved) == len(supported)
        ok_substring = all((s.get("quote") or "") in facts.get(s.get("id"), "")
                           for s in supported)
        pool = p._load_knowledge_pool()
        print(f"retrieval pool: {[(e['id'], e['salience']) for e in pool]} "
              f"(expect obs salience 0.9)")
    else:
        print("NO observation landed (empty model reply -> fail-soft kept raw pending).")

    log = root / "consolidation_log.jsonl"
    if log.exists():
        rows = [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
        print(f"audit observe rows: {[r for r in rows if r.get('action') == 'observe']}")
    else:
        print("no consolidation_log.jsonl written")

    return {"observations": len(obs), "refs_resolved": ok_refs,
            "quotes_are_substrings": ok_substring}


def _check_live(store: Path):
    """Read-only: report how many raw topics carry resolvable memory_ids."""
    rp = store / "knowledge" / "raw_pending.json"
    if not rp.exists():
        print(f"no raw_pending.json at {rp}")
        return
    rows = json.load(open(rp, encoding="utf-8"))
    by_topic = {}
    for r in rows:
        by_topic.setdefault(r.get("topic", ""), []).append(r)
    # resolvable ids = ids that exist under memories/*.json
    known = set()
    memdir = store / "memories"
    if memdir.exists():
        for f in memdir.glob("*.json"):
            try:
                known.update(i.get("id") for i in json.load(open(f, encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
    digested = 0
    for t, rs in sorted(by_topic.items()):
        resolvable = [r for r in rs
                     if any(m in known for m in (r.get("memory_ids") or []))]
        print(f"  topic={t!r:24} rows={len(rs):3} resolvable={len(resolvable):3} "
              f"{'DIGESTIBLE' if resolvable else 'quarantined (no resolvable memory_ids)'}")
        if resolvable:
            digested += 1
    print(f"\n{digested}/{len(by_topic)} topics are digestible by the observe face; "
          f"the rest are quarantined raw (kept pending, zero LLM cost).")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", help="isolated store dir (default: a fresh tmp dir)")
    ap.add_argument("--host", action="store_true",
                    help="call the REAL host main model (default: offline mock)")
    ap.add_argument("--check-live", metavar="STORE",
                    help="read-only probe of a store's raw_pending digestibility")
    args = ap.parse_args()

    if args.check_live:
        _check_live(Path(_resolve_hermes_home())
                    / "filemem" if args.check_live == "-" else Path(args.check_live))
        return 0

    root = Path(args.store) if args.store else Path(tempfile.mkdtemp(prefix="ep2_smoke_"))
    _build_tree(root, DEMO_FACTS, DEMO_TOPIC)

    if args.host:
        ctx = _host_llm_ctx()
        print(f"=== store: {root}")
        print(f"=== caller: REAL host main-model ({ctx.llm!r})")
    else:
        ctx = _mock_llm_ctx(DEMO_FACTS)
        print(f"=== store: {root}")
        print(f"=== caller: OFFLINE deterministic mock (zero LLM; regression only)")

    p = _provider(root, ctx)
    try:
        p._observe_consolidate()
    except Exception as exc:  # pragma: no cover — fail-soft should not raise
        import traceback
        print(f"observe raised unexpectedly: {exc}")
        traceback.print_exc()

    result = _verify(root, p, DEMO_FACTS)
    print("\n--- RESULT ---")
    print(json.dumps(result, ensure_ascii=False))
    if args.host:
        ok = result["observations"] >= 1 and result["refs_resolved"] and result["quotes_are_substrings"]
        print(f"HOST E2E: {'PASS — valid observation landed, refs resolved, quotes grounded' if ok else 'partial'}")
        return 0 if ok else 1
    print("offline regression (code path exercised): "
          f"{'PASS' if result['observations'] >= 1 else 'see verify'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
