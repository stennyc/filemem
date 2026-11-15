#!/usr/bin/env python
"""EP-2 human-confirmation face: deterministically flip a supersedes
candidate's state (candidate -> confirmed | rejected). ZERO LLM.

Companion to the LLM face (consolidation/llm_caller.confirm_supersede,
host main-model path): both land `supersede_state: candidate -> confirmed`,
this one is the deterministic human verdict (人拍板), audited as
supersede_confirm_human.

Usage:
  python3 tools/confirm_supersede.py --list
      read-only: list every supersede_state=="candidate" row awaiting a
      verdict (memory + target ids + content previews). No disk writes.
  python3 tools/confirm_supersede.py --new <id> --confirm
      flip supersede_state -> "confirmed" and stamp superseded_confirmed=True
      on every row in metadata.supersedes (same shard, one RMW under the
      cross-process _shard_lock).
  python3 tools/confirm_supersede.py --new <id> --reject
      flip supersede_state -> "rejected" (candidate dismissed; targets keep
      their superseded=True / superseded_by EP-1 stamps, no confirmed mark).
  python3 tools/confirm_supersede.py --new <id> --confirm --store DIR
      point at an alternate live store (default: $HERMES_HOME/filemem,
      HERMES_HOME defaulting to ~/.hermes — no hardcoded paths).

Every write appends one audit line to <store>/consolidation_log.jsonl:
  {"action": "supersede_confirm_human", "new_id": ..., "target_ids": [...],
   "verdict": "confirmed|rejected", "ts": ...}
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402  (plugin module; import-time is I/O-free)


def _default_live_store() -> Path:
    """Replicate the provider's T5b auto storage-root resolution for the CLI's
    default --store (ZERO I/O, no initialize(), no config read).

    The provider (host) runs with HERMES_HOME pointing at the ACTIVE profile
    (e.g. <$HERMES_HOME>/profiles/coder) and auto-resolves its store to the
    NEUTRAL shared root: strip a trailing ``profiles/<name>`` segment, then
    append ``/filemem``  ->  <$HERMES_HOME>/filemem.  A CLI that naively used
    ``$HERMES_HOME/filemem`` would land on the per-profile path
    (<$HERMES_HOME>/profiles/coder/filemem, typically empty) and silently miss
    every central row.  Mirror the provider's auto branch here so the default
    targets the same tree the host writes.  HERMES_HOME unset -> ~/.hermes.
    """
    home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    parts = [p for p in home.parts if p]
    # strip the trailing "profiles/<name>" tail ONLY when it really ends in
    # exactly that shape (same guard as the provider).
    if len(parts) >= 2 and parts[-2] == "profiles":
        base = Path(*parts[:-2]) if len(parts) > 2 else home
    else:
        base = home
    return base / "filemem"


LIVE_STORE = _default_live_store()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _build_provider(store: Path) -> "fm.FileMemProvider":
    """Provider shell with ONLY path attrs assigned (like
    verify_injection_gate.build_provider): __init__ does zero I/O, so no
    initialize() — no mkdir, no drafts, no decay write. _shard_lock needs
    nothing from self (fcntl/msvcrt + the lock path), so writes work on
    this shell."""
    p = fm.FileMemProvider()
    p.storage_dir = store
    p.memories_dir = store / "memories"
    return p


def _iter_shard_files(p: "fm.FileMemProvider") -> list:
    out = []
    for mem_type in fm.MEMORY_TYPES:
        f = p.memories_dir / f"{mem_type}.json"
        if f.exists():
            out.append(f)
    return out


def _load_shard(f: Path, retries: int = 0, backoff: float = 0.1) -> list:
    """Read one shard. The AUTHORITATIVE RMW read happens under the shard
    lock (a locked reader can never observe a torn file — writers hold the
    same exclusive lock); the UNLOCKED discovery reads (--list, _find_new_row)
    may observe a sibling writer mid-truncate for a few ms, so they retry
    bounded (transient JSONDecodeError = retry; other errors propagate)."""
    last_exc = None
    for _ in range(max(0, retries) + 1):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError as e:
            # transient: a concurrent locked RMW is mid-rewrite; retry.
            last_exc = e
            if _ < retries:
                time.sleep(backoff)
            else:
                raise
        # OSError / IsADirectoryError etc. propagate (genuine errors)
    raise last_exc  # pragma: no cover - loop above always returns/raises


def _find_new_row(p: "fm.FileMemProvider", new_id: str):
    """(shard_file, row) for the row with this id, or (None, None).
    Unlocked discovery read (bounded retry vs a mid-truncate sibling)."""
    for f in _iter_shard_files(p):
        try:
            rows = _load_shard(f, retries=25)
        except OSError:
            continue  # shard vanished between listing and read — keep scanning
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == new_id:
                return f, row
    return None, None


def _append_audit(p: "fm.FileMemProvider", row: dict) -> None:
    """Append-only audit line (O_APPEND single write = safe cross-process)."""
    log_path = p.storage_dir / "consolidation_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def cmd_list(p: "fm.FileMemProvider") -> int:
    """Read-only: print every candidate awaiting a verdict."""
    n = 0
    for f in _iter_shard_files(p):
        for row in _load_shard(f):
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata")
            if not isinstance(meta, dict) or meta.get("supersede_state") != "candidate":
                continue
            n += 1
            target = (meta.get("supersedes") or [None])[0]
            print(f"{row.get('id')}  [{row.get('type')}]  supersedes={target}  "
                  f"content={(row.get('content') or '')[:60]!r}")
    print(f"== {n} candidate(s) awaiting confirmation (read-only; no writes)")
    return 0


def _verify_flip(p: "fm.FileMemProvider", shard: Path, new_id: str, target_state: str) -> bool:
    """Lose-writer guard (write-after-verify): re-read the shard UNDER THE
    SHARD LOCK (the SAME provider `p` the flip was written under, so the
    lock path matches the writer's) and confirm `new_id`'s
    supersede_state is actually `target_state` on disk. A concurrent
    unlocked RMW from a sibling process (consolidation / save-door) may
    have overwritten our write with an older snapshot between our
    read-snapshot and our json.dump — the classic lost-update window that
    `_shard_lock` alone does NOT close (it serializes one RMW, it does not
    CAS against a later writer's stale read). `False` = the flip did not
    survive; the caller re-enters the lock, re-reads fresh, and re-flips
    (bounded retry, never infinite). Fail-soft: any read/lock hiccup
    returns False (the caller treats it as a non-durable flip and retries;
    never raises into the host turn)."""
    try:
        with p._shard_lock(shard):
            items = _load_shard(shard)
    except Exception:
        return False
    for r in items:
        if isinstance(r, dict) and str(r.get("id")) == new_id:
            return (r.get("metadata") or {}).get("supersede_state") == target_state
    return False


def _verdict(p: "fm.FileMemProvider", new_id: str, verdict: str) -> int:
    """Deterministic state flip under the cross-process shard lock.

    Idempotence semantics (one-way decision, deterministic):
      * candidate -> verdict            : flip + audit (the normal path)
      * already in the VERDICT state    : no-op, rc 0, NO double audit
        (a duplicate human confirm must not stack a second audit line)
      * rejected / confirmed against the OTHER verdict, or never stamped :
        refuse, rc 1 (a dismissed candidate is not re-openable by this tool)
    The state re-check happens INSIDE the lock over a fresh read so two
    concurrent workers on the same row serialize cleanly (first writer
    wins, the second is the idempotent no-op).

    Write-after-verify (L8): after the flip is written and the lock is
    released, re-read the shard (again under the lock) to confirm the flip
    survived on disk. A concurrent unlocked RMW from a sibling process
    (consolidation / save-door) can clobber our write with an older
    snapshot (lost update — the lock serializes a single RMW, it does not
    CAS against a later writer's stale read). If the flip did not survive,
    re-enter the lock, re-read fresh, and re-flip — up to 3 total attempts,
    then report the persistent failure (rc 1) instead of silently
    returning 0. `audit` is appended exactly once, only on a confirmed
    durable flip; a retried flip that succeeds appends a single audit line
    (the retry path does not double-stack audit)."""
    target_state = "confirmed" if verdict == "confirm" else "rejected"
    ts = _now_iso()
    max_attempts = 3
    last_err = "not attempted"
    for attempt in range(1, max_attempts + 1):
        shard, _row = _find_new_row(p, new_id)
        if shard is None:
            print(f"ERROR: row {new_id} not found in {p.memories_dir} (memories/*.json)")
            return 1
        old_ids: list = []
        in_lock_changed = False
        with p._shard_lock(shard):  # cross-process RMW (same gate as the save door)
            items = _load_shard(shard)
            row = next((r for r in items if isinstance(r, dict) and str(r.get("id")) == new_id), None)
            if row is None:
                print(f"ERROR: row {new_id} vanished from {shard.name} under the lock (concurrent move?)")
                return 1
            meta = row.get("metadata")
            if not isinstance(meta, dict):
                print(f"ERROR: row {new_id} has no metadata block; not an EP-1 stamped candidate")
                return 1
            state = meta.get("supersede_state")
            if state != "candidate":
                if state == target_state:
                    print(f"OK (idempotent no-op): {new_id} supersede_state already "
                          f"'{target_state}' — no write, no double audit")
                    return 0
                print(f"ERROR: row {new_id} supersede_state={state!r} is not 'candidate' "
                      f"(already decided); refusing to re-flip in the other direction")
                return 1
            old_ids = [str(x) for x in (meta.get("supersedes") or [])]
            changed = False
            for r in items:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id"))
                rm = r.get("metadata")
                if not isinstance(rm, dict):
                    rm = {}
                    r["metadata"] = rm
                if rid == new_id:
                    rm["supersede_state"] = target_state
                    r["updated_at"] = ts
                    changed = True
                elif target_state == "confirmed" and rid in old_ids:
                    rm["superseded_confirmed"] = True
                    r["updated_at"] = ts
                    changed = True
            if changed:
                with open(shard, "w", encoding="utf-8") as fh:
                    json.dump(items, fh, ensure_ascii=False, indent=2)
                in_lock_changed = True
        if not in_lock_changed:
            continue  # idempotent no-op path already returned 0 above
        # Write-after-verify: the lock is released; confirm the flip
        # survived on disk (a concurrent unlocked RMW from a sibling
        # process may have clobbered it between our snapshot and our
        # json.dump — the lost-update window the lock alone does not
        # close).
        if _verify_flip(p, shard, new_id, target_state):
            audit_row = {
                "action": "supersede_confirm_human",
                "new_id": new_id,
                "target_ids": old_ids,
                "verdict": target_state,
                "ts": ts,
                "write_verified": True,
            }
            _append_audit(p, audit_row)
            print(f"{verdict.upper()}: {new_id} supersede_state -> '{target_state}'"
                  + (f"; superseded_confirmed=True on {old_ids}" if target_state == "confirmed" else "")
                  + " (write-verified under lock; audit appended to consolidation_log.jsonl)")
            return 0
        # Flip was clobbered between our write and our verify: retry with a
        # fresh read under the lock.
        last_err = f"flip clobbered by concurrent writer (attempt {attempt}/{max_attempts})"
        if attempt < max_attempts:
            time.sleep(0.1 * attempt)  # bounded backoff before re-read
    print(f"ERROR: {new_id} supersede_state flip did not persist: {last_err} "
          f"(row may be contended by a concurrent writer; re-run after the "
          f"writer settles, or use --store to target a quiescent snapshot)")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--new", metavar="ID", help="new-row id to flip")
    ap.add_argument("--confirm", action="store_true", help="verdict: confirm")
    ap.add_argument("--reject", action="store_true", help="verdict: reject")
    ap.add_argument("--list", action="store_true", help="list candidates (read-only)")
    ap.add_argument("--store", metavar="DIR", help=f"alternate live store (default {LIVE_STORE})")
    args = ap.parse_args()

    store = Path(args.store) if args.store else LIVE_STORE
    p = _build_provider(store)

    if args.list:
        return cmd_list(p)
    if args.new and args.confirm:
        return _verdict(p, args.new, "confirm")
    if args.new and args.reject:
        return _verdict(p, args.new, "reject")
    ap.print_usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
