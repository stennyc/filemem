#!/usr/bin/env python3
"""P4 (20261007 清染): one-shot 存量 sweep of the evidence-vetted dirty
injection rows out of the shared filemem store.

Companion to the write-side role gate in the plugin root __init__.py
(``P4 role gate``): that gate stops NEW model-replies / tool-dumps from
being mis-stamped as user facts; THIS tool cleans the historical 存量
that already landed before the gate went live.

What it removes (the 17 trace-vetted rows, pinned by stable row id):
  - 8 model-reply PROSE rows mis-stamped `origin {kind: user, trust:
    medium}` (the 2026-10-07 session-trace cross-check proved each is a
    model reply, not a user utterance):
        fact_414_1791196731, fact_443_1791226607, fact_496_1791232242,
        fact_666_1791330031, fact_670_1791331803, preference_140_1791324303,
        task_148_1791230039, task_256_1791313313
  - 6 decision.json LLM-structured JSON rows stored verbatim
    (`{"responses": [...]}` / `{"py_compile": ...}` consolidation
    verdicts — never a durable user decision):
        decision_4_1791153827, decision_6_1791168800, decision_7_1791193514,
        decision_15_1791231949, decision_33_1791251050, decision_37_1791271073
  - 3 pending-draft JSON-dump rows (raw cronjob / tool_search dumps queued
    as L1 confirm candidates that pester the user at the [未决草稿] face):
        draft_1791282035882371_2, draft_1791283506313959_4,
        draft_1791338848463817_9

Deliberately KEPT (the trace proved these are legitimate USER utterances
or v2 canary/audit assets — never swept):
  - fact_309 / fact_417 / fact_560 / fact_614 / fact_635 / decision_48
    ("好的，请开始…" — real user instructions, a durable fact)
  - self_test-stamped `[Group chat` room-meta rows + failure_type lesson
    rows (the v2 audit/canary design keeps them on disk for rollback).

Guarantees (the sweep_low_value precedent):
  - 留痕非删: every removed row is appended as ONE JSONL line to
    knowledge/archive/dirty_injection_sweep_<ts>.jsonl (recoverable),
    and one audit row is appended to the EXISTING consolidation_log.jsonl
    (action=dirty_injection_sweep). Nothing is hard-deleted.
  - dry-run is the DEFAULT: it reports the judged count and writes NOTHING
    (no archive, no audit, no shard/draft rewrite). `--yes` lifts to apply.
  - Idempotent: a re-apply after an apply finds 0 manifest rows left and
    writes nothing (no new trace file, the raw_pending C-slice posture).
  - Cross-process lock: exclusive fcntl.flock on the sibling <file>.lock
    (same protocol as the plugin's _shard_lock) around each rewrite.

Zero LLM, pure stdlib. The plugin root __init__.py is NOT imported (it
runs host-hook setup); the manifest is pinned here as data so a re-run is
deterministic and reviewable.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── cross-process lock (verbatim copy of the plugin's _shard_lock protocol
#    and tools/sweep_low_value.py's _flock — sibling <file>.lock, O_CREAT|
#    O_RDWR 0o644, flock LOCK_EX; Windows msvcrt mirror; exotic host degrades
#    to a loud no-op, never blocks a rewrite). ─────────────────────────────
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux
    msvcrt = None

PLUGIN_DIR_DEFAULT = Path(__file__).resolve().parents[1]

# ── the evidence-vetted manifest (pin it as data; 改 evidence 不改 code) ───
# shard name -> [row ids to sweep]. A row is swept ONLY if its id is in this
# manifest AND it is present in the live store (a missing id = already swept
# or never landed -> not judged, keeps the sweep idempotent).
MEMORY_MANIFEST: Dict[str, List[str]] = {
    "fact": [
        "fact_414_1791196731",
        "fact_443_1791226607",
        "fact_496_1791232242",
        "fact_666_1791330031",
        "fact_670_1791331803",
    ],
    "preference": [
        "preference_140_1791324303",
    ],
    "task": [
        "task_148_1791230039",
        "task_256_1791313313",
    ],
    "decision": [
        "decision_4_1791153827",
        "decision_6_1791168800",
        "decision_7_1791193514",
        "decision_15_1791231949",
        "decision_33_1791251050",
        "decision_37_1791271073",
    ],
}
# pending-draft row ids swept from ANY drafts_*.json (globally unique ids).
DRAFT_MANIFEST: List[str] = [
    "draft_1791282035882371_2",
    "draft_1791283506313959_4",
    "draft_1791338848463817_9",
]
ARCHIVE_PREFIX = "dirty_injection_sweep_"
AUDIT_ACTION = "dirty_injection_sweep"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _ts_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _flock(path: Path, fn):
    """Exclusive flock on <path>.lock (verbatim sweep_low_value protocol)."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return fn()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    elif msvcrt is not None:
        lock_path.touch(exist_ok=True)
        fh = open(str(lock_path), "a+b")
        try:
            fh.seek(0)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            return fn()
        finally:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:  # pragma: no cover - already released
                pass
            fh.close()
    else:  # pragma: no cover - exotic host
        sys.stderr.write(
            "sweep_dirty_injections: WARNING: no cross-process lock primitive "
            f"(fcntl/msvcrt) for {lock_path.name} — running unlocked\n")
        return fn()


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(str(path), "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _sweep_memories(store: Path, dry_run: bool, ts: str) -> Dict[str, Any]:
    """Move every manifest memory row that is present into the archive +
    audit; rewrite the shard with its surviving rows (under flock)."""
    arch_dir = store / "knowledge" / "archive"
    arch_path = arch_dir / f"{ARCHIVE_PREFIX}{ts}.jsonl"
    log_path = store / "consolidation_log.jsonl"
    stats: Dict[str, Any] = {"shards": 0, "rows_swept": 0, "rows_kept": 0}
    for shard, ids in MEMORY_MANIFEST.items():
        mf = store / "memories" / f"{shard}.json"
        if not mf.exists():
            continue
        stats["shards"] += 1
        rows = _read_rows(mf)
        wanted = set(ids)
        present = [r for r in rows if r.get("id") in wanted]
        if not present:
            stats["rows_kept"] += len(rows)
            continue
        kept = [r for r in rows if r.get("id") not in wanted]
        stats["rows_kept"] += len(kept)
        if dry_run:
            stats["rows_swept"] += len(present)
            continue
        # 1. archive the swept rows FIRST (留痕非删, recoverable).
        arch_dir.mkdir(parents=True, exist_ok=True)
        with open(str(arch_path), "a", encoding="utf-8") as af:
            for r in present:
                af.write(json.dumps(
                    {"row": r, "shard": shard, "sweep_ts": _utcnow_iso()},
                    ensure_ascii=False, sort_keys=True) + "\n")
        # 2. audit + rewrite, both under the shard's sibling-flock.
        def _apply(target=mf, keep=kept, removed=present, sh=shard):
            with open(str(log_path), "a", encoding="utf-8") as lf:
                for r in removed:
                    lf.write(json.dumps({
                        "ts": _utcnow_iso(),
                        "action": AUDIT_ACTION,
                        "source": "sweep",
                        "shard": sh,
                        "row_id": r.get("id"),
                        "content_preview": str(r.get("content") or "")[:64],
                        "archived_to": arch_path.name,
                    }, ensure_ascii=False, sort_keys=True) + "\n")
            with open(str(target), "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=2)
        _flock(mf, _apply)
        stats["rows_swept"] += len(present)
    return stats


def _sweep_drafts(store: Path, dry_run: bool, ts: str) -> Dict[str, Any]:
    """Move every manifest draft row that is present in ANY drafts_*.json
    into the archive + audit; rewrite each affected draft file."""
    arch_dir = store / "knowledge" / "archive"
    arch_path = arch_dir / f"{ARCHIVE_PREFIX}{ts}.jsonl"
    log_path = store / "consolidation_log.jsonl"
    wanted = set(DRAFT_MANIFEST)
    stats: Dict[str, Any] = {"files": 0, "rows_swept": 0, "rows_kept": 0}
    for df in sorted(store.glob("drafts_*.json")):
        if not df.is_file():
            continue
        rows = _read_rows(df)
        present = [r for r in rows if r.get("id") in wanted]
        if not present:
            stats["rows_kept"] += len(rows)
            continue
        stats["files"] += 1
        kept = [r for r in rows if r.get("id") not in wanted]
        stats["rows_kept"] += len(kept)
        if dry_run:
            stats["rows_swept"] += len(present)
            continue
        arch_dir.mkdir(parents=True, exist_ok=True)
        with open(str(arch_path), "a", encoding="utf-8") as af:
            for r in present:
                af.write(json.dumps(
                    {"row": r, "source_file": df.name, "sweep_ts": _utcnow_iso()},
                    ensure_ascii=False, sort_keys=True) + "\n")
        def _apply(target=df, keep=kept, removed=present):
            with open(str(log_path), "a", encoding="utf-8") as lf:
                for r in removed:
                    lf.write(json.dumps({
                        "ts": _utcnow_iso(),
                        "action": AUDIT_ACTION,
                        "source": "sweep",
                        "shard": "draft",
                        "row_id": r.get("id"),
                        "content_preview": str(r.get("content") or "")[:64],
                        "source_file": target.name,
                        "archived_to": arch_path.name,
                    }, ensure_ascii=False, sort_keys=True) + "\n")
            with open(str(target), "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=2)
        _flock(df, _apply)
        stats["rows_swept"] += len(present)
    return stats


def sweep(store: Path, *, dry_run: bool = True, yes: bool = False,
          judged_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """One sweep pass. `yes=True` applies (waives the dry-run default).
    Idempotent: a re-apply after an apply finds 0 present manifest rows."""
    if yes:
        dry_run = False
    else:
        dry_run = True
    ts = _ts_tag()
    mem = _sweep_memories(store, dry_run=dry_run, ts=ts)
    drf = _sweep_drafts(store, dry_run=dry_run, ts=ts)
    return {
        "dry_run": dry_run,
        "ts_tag": ts,
        "memories": mem,
        "drafts": drf,
        # "judged" = the manifest rows actually PRESENT in the store
        # (a re-run after an apply reports 0 -> idempotent).
        "judged": mem["rows_swept"] + drf["rows_swept"],
    }


def _default_store() -> Path:
    home = os.environ.get("HERMES_HOME") or str(Path.home())
    return Path(home) / "filemem"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="P4 one-shot 存量 sweep of the 17 evidence-vetted dirty "
                    "injection rows (deterministic, zero LLM, idempotent; "
                    "dry-run default, --yes applies).")
    ap.add_argument("--store", default=None,
                    help="the filemem store root (default: <HERMES_HOME or "
                         "~/.hermes>/filemem)")
    ap.add_argument("--yes", action="store_true",
                    help="apply (default is dry-run: report, write NOTHING)")
    args = ap.parse_args(argv)
    store = Path(args.store) if args.store else _default_store()
    result = sweep(store, dry_run=True, yes=args.yes)
    # stdout = PURE JSON (machine face); the human-readable summary line goes to
    # stderr so a `... | jq` / `json.loads(stdout)` consumer never sees "Extra
    # data".
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result["dry_run"]:
        sys.stderr.write(
            f"sweep_dirty_injections: DRY-RUN — {result['judged']} "
            f"manifest row(s) judged present, NOTHING written "
            f"(store={store}); re-run with --yes to apply.\n")
    else:
        sys.stderr.write(
            f"sweep_dirty_injections: applied — {result['judged']} "
            f"manifest row(s) swept to the append-only archive "
            f"(store={store}).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
