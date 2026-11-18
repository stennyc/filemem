#!/usr/bin/env python3
"""SESSION_ISOLATION_V2 · E (owner 2026-10-10): one-off 存量 sweep that
physically removes every memories/*.json row that is NOT user-confirmed.

Companion to the read-side confirmed gate in the plugin root __init__.py
(``_auto_inject_eligible``): that gate stops unconfirmed rows from the
auto-inject face going forward; THIS tool cleans the historical 存量 that
already landed before the gate went live.

What it removes (owner decision ④ "现有数据不重要，可以清理删除"):
  - EVERY memories/<type>.json row whose metadata.confirmed is NOT True
    (the ~1400 unconfirmed auto-catch rows: dirty debug thought-streams,
    raw tracebacks, LLM tool-dumps, old cross-session task noise).

Deliberately KEPT:
  - metadata.confirmed=True rows (the per-profile global skeleton: the 396
    user-validated rows + the 1 factory-seeded L2 meta-rule, which is
    confirmed=True by construction).

Guarantees (the sweep_dirty_injections / sweep_low_value precedent):
  - 留痕非删: every removed row is appended as ONE JSONL line to
    knowledge/archive/unconfirmed_sweep_<ts>.jsonl (recoverable), and one
    audit row per removed row is appended to the EXISTING
    consolidation_log.jsonl (action=unconfirmed_sweep). Nothing is
    hard-deleted without a trace.
  - dry-run is the DEFAULT: it reports the judged count and writes NOTHING
    (no archive, no audit, no shard rewrite). `--yes` lifts to apply.
  - Idempotent: a re-apply after an apply finds 0 unconfirmed rows left and
    writes nothing (no new trace file — the raw_pending C-slice posture).
  - Cross-process lock: exclusive fcntl.flock on the sibling <file>.lock
    (same protocol as the plugin's _shard_lock) around each rewrite.
  - DYNAMIC selection (not a pinned id manifest): a row is swept iff its
    live metadata.confirmed is not True. The pool keeps growing between runs
    (auto-catch), so a pinned-id manifest would miss new rows; the
    confirmed-flag predicate is the stable, reviewable selection rule.

Zero LLM, pure stdlib. The plugin root __init__.py is NOT imported (it
runs host-hook setup on import); the selection is a one-line flag read so a
re-run is deterministic and reviewable.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
PLUGIN_DIR_DEFAULT = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR_DEFAULT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR_DEFAULT))
from dep_bootstrap import _resolve_hermes_home  # noqa: E402
from typing import Any, Dict, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux
    msvcrt = None

MEMORY_SHARDS = ("preference", "fact", "decision", "task")
ARCHIVE_PREFIX = "unconfirmed_sweep_"
AUDIT_ACTION = "unconfirmed_sweep"


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
            # type: ignore[attr-defined]  (msvcrt has no type stubs)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            return fn()
        finally:
            try:
                # type: ignore[attr-defined]
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - already released
                pass
            fh.close()
    else:  # pragma: no cover - exotic host
        sys.stderr.write(
            "sweep_unconfirmed: WARNING: no cross-process lock primitive "
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


def _is_confirmed(row: Dict[str, Any]) -> bool:
    meta = row.get("metadata")
    return bool(isinstance(meta, dict) and meta.get("confirmed"))


def _sweep_memories(store: Path, dry_run: bool, ts: str) -> Dict[str, Any]:
    """Move every UNCONFIRMED memory row into the archive + audit; rewrite
    the shard with its surviving (confirmed) rows, under flock."""
    arch_dir = store / "knowledge" / "archive"
    arch_path = arch_dir / f"{ARCHIVE_PREFIX}{ts}.jsonl"
    log_path = store / "consolidation_log.jsonl"
    stats: Dict[str, Any] = {"shards": 0, "rows_swept": 0, "rows_kept": 0,
                             "by_type": {}}
    for shard in MEMORY_SHARDS:
        mf = store / "memories" / f"{shard}.json"
        if not mf.exists():
            continue
        stats["shards"] += 1
        rows = _read_rows(mf)
        removed = [r for r in rows if not _is_confirmed(r)]
        kept = [r for r in rows if _is_confirmed(r)]
        if not removed:
            stats["rows_kept"] += len(kept)
            continue
        stats["by_type"][shard] = len(removed)
        stats["rows_swept"] += len(removed)
        stats["rows_kept"] += len(kept)
        if dry_run:
            continue
        # 1. archive the swept rows FIRST (留痕非删, recoverable).
        arch_dir.mkdir(parents=True, exist_ok=True)
        with open(str(arch_path), "a", encoding="utf-8") as af:
            for r in removed:
                af.write(json.dumps(
                    {"row": r, "shard": shard, "sweep_ts": _utcnow_iso()},
                    ensure_ascii=False, sort_keys=True) + "\n")

        def _apply(target=mf, keep=kept, removed_rows=removed, sh=shard):
            with open(str(log_path), "a", encoding="utf-8") as lf:
                for r in removed_rows:
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
    return stats


def sweep(store: Path, *, dry_run: bool = True, yes: bool = False) -> Dict[str, Any]:
    """One sweep pass. `yes=True` applies (waives the dry-run default).
    Idempotent: a re-apply after an apply finds 0 unconfirmed rows."""
    if yes:
        dry_run = False
    ts = _ts_tag()
    mem = _sweep_memories(store, dry_run=dry_run, ts=ts)
    return {"dry_run": dry_run, "ts_tag": ts, "memories": mem}


def _default_store() -> Path:
    from dep_bootstrap import _resolve_hermes_home
    return Path(_resolve_hermes_home()) / "filemem"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="SESSION_ISOLATION_V2 E: one-shot 存量 sweep that "
                    "physically removes every UNCONFIRMED memories/*.json row "
                    "(deterministic, zero LLM, idempotent; dry-run default, "
                    "--yes applies).")
    ap.add_argument("--store", default=None,
                    help="the filemem store root (default: <HERMES_HOME or "
                         "~/.hermes>/filemem)")
    ap.add_argument("--yes", action="store_true",
                    help="apply (default is dry-run: report, write NOTHING)")
    args = ap.parse_args(argv)
    store = Path(args.store) if args.store else _default_store()
    result = sweep(store, dry_run=True, yes=args.yes)
    mem = result["memories"]
    # stdout = PURE JSON (machine face); the human-readable summary goes to
    # stderr so a `| jq` / json.loads(stdout) consumer never sees "Extra data".
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result["dry_run"]:
        sys.stderr.write(
            f"sweep_unconfirmed: DRY-RUN — {mem['rows_swept']} unconfirmed row(s) "
            f"judged (by_type={mem['by_type']}), {mem['rows_kept']} confirmed "
            f"row(s) kept, NOTHING written (store={store}); re-run with --yes.")
    else:
        sys.stderr.write(
            f"sweep_unconfirmed: applied — {mem['rows_swept']} unconfirmed row(s) "
            f"swept to the append-only archive, {mem['rows_kept']} confirmed "
            f"row(s) kept (store={store}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
