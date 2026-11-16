"""Adjudication reclassify subprocess entry (owner 2026-11-17, code_map parity).

A fresh python runs the budget-gated LLM reclassify + advisory stamp for one
already-landed confirmed_draft row, isolated from the host gateway process
(GIL / CPU / LLM-call blast radius — the same posture as the code_map
subprocess build, 666fdbf).

Protocol (parent -> child):
  argv : --shard <type> --row <id>
  stdin: ONE JSON line {storage_dir, shard, row_id, session_id,
         source_profile, content, main_runtime (dict snapshot), host_tree}
  stdout: DEVNULL (parent-side); stderr: inherited (fail-visible)

The child bootstraps sys.path (the host tree first, so the on-disk
agent.* imports resolve; then the plugin root), loads the ON-DISK
consolidation/llm_caller (fresh code — the same owner-build posture as the
code_map child), runs classify_type on the carried main_runtime snapshot,
and on an upgrade verdict advisory-stamps the row through the EXISTING
on-disk provider method (single-shard RMW under the cross-process
<shard>.json.lock, the audit append after lock release, plus the llm_call
telemetry row) — one source of truth, no duplicated RMW logic.

Exit codes: 0 = done (the stamp landed, or the LLM verdict agreed with the
regex fact / the host LLM surface was unavailable — a clean no-op);
non-zero = a genuine failure (traceback on stderr, fail-visible; the row
keeps the regex verdict — the stamp is advisory audit-trail, never
load-bearing, the row was durably landed before the spawn).
"""
import importlib.util
import json
import sys
import traceback
from pathlib import Path

_PLUGIN_ROOT = str(Path(__file__).resolve().parent)
_UPGRADE_VERDICTS = ("preference", "decision", "task")


def _parse_args(argv):
    shard = argv[argv.index("--shard") + 1] if "--shard" in argv else ""
    row_id = argv[argv.index("--row") + 1] if "--row" in argv else ""
    return shard, row_id


def _load_on_disk_llm_caller(plugin_root: str):
    p = Path(plugin_root) / "consolidation" / "llm_caller.py"
    spec = importlib.util.spec_from_file_location("_fm_llm_caller_sp", p)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the on-disk llm_caller from {p}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp_in_child(payload: dict, shard: str, row_id: str, verdict: str,
                    llm_module) -> None:
    """Reuse the ON-DISK provider method (single source of truth for the
    RMW + lock + audit ordering): a partial provider instance carries just
    the fields _advisory_stamp_suggested_type touches."""
    import __init__ as fm
    import threading
    prov = fm.FileMemProvider.__new__(fm.FileMemProvider)
    prov._lock = threading.Lock()
    prov.storage_dir = Path(payload.get("storage_dir") or _PLUGIN_ROOT)
    prov.memories_dir = prov.storage_dir / "memories"
    prov.source_profile = payload.get("source_profile")
    prov.session_id = payload.get("session_id")
    try:
        prov._append_llm_call_telemetry(llm_module, "classify_type")
    except Exception:
        pass  # telemetry is audit-only, never blocks the stamp
    prov._advisory_stamp_suggested_type(shard, row_id, verdict)


def main() -> int:
    shard, row_id = _parse_args(sys.argv[1:])
    payload = json.loads(sys.stdin.read() or "{}")  # protocol violation -> raise
    host_tree = payload.get("host_tree") or ""
    if host_tree:
        sys.path.insert(0, host_tree)
    if _PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, _PLUGIN_ROOT)
    llm = _load_on_disk_llm_caller(_PLUGIN_ROOT)
    verdict = None
    if llm._HOST_CALL_LLM is not None:
        res = llm.classify_type(payload.get("main_runtime"),
                                payload.get("content") or "")
        verdict = res.get("type")
    if verdict in _UPGRADE_VERDICTS:
        _stamp_in_child(payload, shard, row_id, verdict, llm)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()  # fail-visible on the inherited host stderr
        sys.exit(1)
