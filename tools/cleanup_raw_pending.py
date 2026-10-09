#!/usr/bin/env python3
"""raw_pending cleanup tool - archives overflowed items to quarantine.

Profile attribution (cull-face ownership closure): the cull face only ever
reads/culls rows stamped with the caller's own HERMES_PROFILE. Rows without
the stamp are one-off swept into a traceable archive (--sweep-unstamped for
pool + auto-archive, --sweep-quarantine-unstamped for the quarantine face),
never physically deleted; --sweep-quarantine-unstamped additionally offers
a physical-delete switch (--hard, requires a two-step --dry-run count
confirmation) for the one-off legacy residue. Fail-closed: cull actions
without a recognized profile identity are refused."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

def clean(raw_pending: Path, quarantine: Path, dry_run: bool) -> dict:
    """Archive queue_overflow entries to quarantine, keep catch_filter audit."""
    quarantine.mkdir(parents=True, exist_ok=True)
    rows = []
    lock_path = raw_pending.with_name(raw_pending.name + ".lock")
    with open(lock_path, "a+") as lock_fh:
        import fcntl
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        try:
            if raw_pending.exists():
                with open(raw_pending, "r", encoding="utf-8") as f:
                    rows = json.load(f)
            overflow_rows = [r for r in rows if r.get("origin") == "queue_overflow"]
            catch_rows = [r for r in rows if r.get("origin") != "queue_overflow"]
            if not dry_run and overflow_rows:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                archive_path = quarantine / f"raw_pending_queue_overflow_{ts}.jsonl"
                with open(archive_path, "w", encoding="utf-8") as f:
                    for r in overflow_rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                import os as _os
                _tmp = raw_pending.with_suffix(".tmp")
                with open(_tmp, "w", encoding="utf-8") as f:
                    json.dump(catch_rows, f, ensure_ascii=False, indent=2)
                _os.replace(_tmp, raw_pending)
                return {"archived": len(overflow_rows), "kept": len(catch_rows), "archive": str(archive_path)}
            return {"dry_run": True, "overflow_rows": len(overflow_rows), "catch_rows": len(catch_rows)}
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)

# ── Path B: recycle-bin human face (--list / --promote) + prune layer ─────
# Three raw_pending sources, fixed order pool -> quarantine -> auto-archive.
# --list is a pure read (no state, no writes). --promote moves a row back
# into the pool with origin=manual_promote + an append-only audit row. The
# pool RMW takes an exclusive fcntl.flock on raw_pending.json.lock (the same
# file + direction as the plugin C-slice _shard_lock, so no lock inversion);
# non-POSIX degrades to a no-op lock (documented, same as clean()).
# The prune layer (knowledge.archive_prune_days) is on the plugin side
# (__init__._auto_prune_raw_pending_archive), NOT here — this tool only
# reads the C-slice archive files as a --list/--promote source.

def _read_pool_file(pool: Path) -> list:
    """Pool is a JSON array; a missing file = an empty pool."""
    if not pool.exists():
        return []
    with open(pool, "r", encoding="utf-8") as f:
        return json.load(f)


def _flock_lock(pool: Path):
    """Exclusive flock on raw_pending.json.lock (innermost, same file and
    direction as the plugin C-slice _shard_lock). Non-POSIX: no lock — the
    context degrades to a no-op (documented; mirrors clean()'s behavior)."""
    lock_path = pool.with_name(pool.name + ".lock")
    fh = open(lock_path, "a+")
    try:
        import fcntl
    except ImportError:
        fcntl = None

    class _Lock:
        def __enter__(self_inner):
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            return self_inner

        def __exit__(self_inner, *exc):
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            return False

    return _Lock()


def _caller_profile() -> str | None:
    """Caller's profile identity for the cull face: HERMES_PROFILE from env.

    TUI/kanban sessions both carry it; a bare shell does not. None means
    "unidentified" — read actions stay allowed, cull actions fail closed."""
    p = os.environ.get("HERMES_PROFILE", "").strip()
    return p or None


def _scope_rows(rows: list, profile: str | None) -> list:
    """Keep only rows stamped with exactly `profile` (None = keep all).

    A missing/empty stamp is NEVER a wildcard: only a positive,
    exact-equality match survives, so unstamped rows (the one-off legacy
    residue, pending sweep) are hidden from culls until swept."""
    if profile is None:
        return rows
    return [r for r in rows if r.get("source_profile") == profile]


def list_rows(knowledge_dir: Path, quarantine_dir: Path,
              profile: str | None = None):
    """Enumerate the 3 raw_pending sources with global row numbering.

    Fixed source order: pool knowledge/raw_pending.json, then quarantine
    raw_pending_queue_overflow_*.jsonl (name-sorted), then
    knowledge/archive/raw_pending_auto_*.jsonl (name-sorted). Within a source
    rows sort by created_ts ascending. A missing/corrupt source is skipped
    with a stderr warning (not fatal). Pure read: writes nothing.

    profile: when set (the caller's HERMES_PROFILE), only rows stamped with
    exactly that source_profile are returned; when None, all rows (read-only
    listing stays harmlessly global)."""
    pool = knowledge_dir / "raw_pending.json"
    archive = knowledge_dir / "archive"
    sources = [("pool", pool)]
    for p in sorted(quarantine_dir.glob("raw_pending_queue_overflow_*.jsonl")):
        sources.append(("quarantine", p))
    for p in sorted(archive.glob("raw_pending_auto_*.jsonl")):
        sources.append(("auto-archive", p))
    rows = []
    for label, p in sources:
        try:
            if label == "pool":
                if not p.exists():
                    print(f"warning: {p} missing, skipping source", file=sys.stderr)
                    continue
                data = _read_pool_file(p)
                assert isinstance(data, list)
            else:
                data = [json.loads(line) for line in
                        p.read_text(encoding="utf-8").splitlines() if line.strip()]
            data.sort(key=lambda r: str(r.get("created_ts", "")))
            for r in _scope_rows(data, profile):
                rows.append((label, r))
        except Exception as e:
            print(f"warning: skipping corrupt/unreadable source {p}: {e}", file=sys.stderr)
    return rows


def _list_row_line(n: int, label: str, row: dict) -> str:
    origin = str(row.get("origin", ""))
    reason = str(row.get("reason", ""))[:40]
    ts = str(row.get("created_ts", ""))
    content = str(row.get("content", ""))[:80]
    return (f"#{n}  [{label}]  origin={origin}  reason={reason}  {ts}  {content}")


def promote(root: Path, row_num: int, dry_run: bool = False,
            profile: str | None = None) -> dict:
    """Promote global row <row_num> (as printed by --list) back into the pool
    with origin=manual_promote + an append-only audit row.

    pool rows: in-place origin mark only (no duplicate insert). archive rows
    (quarantine / auto-archive): a copy is appended to the pool, idempotent
    by content+topic (already present -> mark only, no re-insert). An audit
    row is appended to knowledge/archive/manual_promote_<ts>.jsonl. An
    out-of-range row returns {'error','valid_range'} and touches nothing.
    --dry-run previews without writing anything (no lock file, no pool /
    audit write). The pool read-modify-write happens under the exclusive
    flock, and the pool is re-read INSIDE the lock so a concurrent writer's
    rows are not clobbered (no lost update).

    profile (cull-face ownership closure): a cull action only addresses rows
    stamped with exactly the caller's source_profile. profile=None is
    fail-closed: refuse with an error dict and touch nothing (no preview
    either — the cull face does not act without a recognized identity).
    With a profile, the row space is the caller's OWN rows (scoped
    --list numbering), and the target row's stamp is re-verified against the
    profile before execution; a mismatch refuses with the row number and the
    row's attribution. Foreign rows are simply unaddressable: they do not
    appear in the scoped numbering."""
    if profile is None:
        return {"error": "profile identity unrecognized, cull refused",
                "hint": "run with HERMES_PROFILE set to this profile's name",
                "row": row_num, "dry_run": dry_run}
    knowledge = root / "knowledge"
    pool = knowledge / "raw_pending.json"
    quarantine = root / "quarantine"
    archive = knowledge / "archive"
    rows = list_rows(knowledge, quarantine, profile)
    if not 1 <= row_num <= len(rows):
        return {"error": f"row {row_num} out of range",
                "valid_range": f"1..{len(rows)}", "dry_run": dry_run,
                "profile": profile}
    label, row = rows[row_num - 1]
    if row.get("source_profile") != profile:
        # Belt-and-braces: scoped listing already excludes this, but the
        # cull face verifies attribution at the gate, never trusts it.
        return {"error": (f"row {row_num} belongs to "
                          f"'{row.get('source_profile')}', cull refused"),
                "row": row_num, "row_profile": row.get("source_profile"),
                "caller_profile": profile, "dry_run": dry_run}
    key = (row.get("content"), row.get("topic"))
    if dry_run:
        # Read-only preview: never open the lock file, never write anything
        # (the scoped enumeration above is itself pure read; the caller's
        # own rows are what the preview describes — a foreign row is simply
        # unaddressable, not a previewable target).
        pool_rows = _read_pool_file(pool)
        matched = any((r.get("content"), r.get("topic")) == key for r in pool_rows)
        action = "mark" if matched else "insert+mark"
        return {"dry_run": True, "row": row_num, "source": label,
                "action": action, "target_pool": str(pool),
                "row_data": {**row, "origin": "manual_promote"}}
    # Write path: re-read the pool INSIDE the exclusive lock so concurrent
    # writers' rows survive (no lost update); RMW + audit under the lock.
    with _flock_lock(pool):
        pool_rows = _read_pool_file(pool)
        matched = next((r for r in pool_rows
                        if (r.get("content"), r.get("topic")) == key), None)
        inserted = False
        if matched is not None:
            target = matched
        else:
            target = dict(row)
            pool_rows.append(target)
            inserted = True
        target["origin"] = "manual_promote"
        action = "insert+mark" if inserted else "mark"
        tmp = pool.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pool_rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, pool)
        archive.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        audit_path = archive / f"manual_promote_{ts}.jsonl"
        audit = {"row": row_num, "source": label, "origin": row.get("origin"),
                 "created_ts": row.get("created_ts"),
                 "promoted_ts": datetime.now().isoformat()}
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit, ensure_ascii=False) + "\n")
        return {"promoted": row_num, "source": label, "action": action,
                "audit": str(audit_path)}


def restore(raw_pending: Path, quarantine: Path, profile: str | None = None) -> dict:
    """Restore rows stamped with `profile` from the newest quarantine archive.

    Ownership closure: `restore` is a cull-face write action, so it refuses
    with profile=None (fail-closed). With a profile, only rows stamped with
    exactly `profile` are merged back into the pool; foreign and unstamped
    rows are left in the archive. Stamped rows keep their source_profile
    verbatim (no re-stamp).

    Idempotent: the merge dedupes by content+topic (same key as promote's
    `matched -> mark only`), so a repeated restore never re-dumps a row
    that is already in the pool. The pool read-modify-write runs under the
    exclusive flock on raw_pending.json.lock (same file + direction as
    promote/sweep/_shard_lock); the pool is re-read INSIDE the lock so a
    concurrent writer's rows are not clobbered (no lost update). When rows
    actually move, ONE audit row {action:restore_quarantine, profile,
    restored_n, archive, ts} is appended to the root
    consolidation_log.jsonl (sort_keys, append-only, under the audit flock)
    — same discipline as sweep_unstamped; a zero-move re-run writes nothing
    (no pool write, no audit row)."""
    if profile is None:
        print("profile identity unrecognized, cull action refused "
              "(restore); set HERMES_PROFILE", file=sys.stderr)
        return {"error": "profile identity unrecognized, cull refused"}
    archives = sorted(quarantine.glob("raw_pending_queue_overflow_*.jsonl"),
                      reverse=True)
    if not archives:
        print("No archives found")
        return {"restored_n": 0, "archive": None}
    archive = archives[0]
    with open(archive, "r", encoding="utf-8") as f:
        archived = [json.loads(line) for line in f if line.strip()]
    archived = [r for r in archived if r.get("source_profile") == profile]
    moved = 0
    with _flock_lock(raw_pending):
        # re-read the pool INSIDE the exclusive lock (no lost update)
        existing = _read_pool_file(raw_pending)
        seen = {(r.get("content"), r.get("topic")) for r in existing}
        for r in archived:
            key = (r.get("content"), r.get("topic"))
            if key in seen:
                continue  # dedupe: already in pool (or an earlier archive row)
            seen.add(key)
            existing.append(r)
            moved += 1
        if moved:
            _tmp = raw_pending.with_suffix(".tmp")
            with open(_tmp, "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, raw_pending)
    if moved:
        log_path = raw_pending.parent.parent / "consolidation_log.jsonl"
        log_lock = log_path.with_name(log_path.name + ".lock")
        with _flock_lock_for(log_lock):
            audit = {"ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                     "action": "restore_quarantine",
                     "profile": profile,
                     "restored_n": moved,
                     "archive": str(archive)}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")
    skipped = len(archived) - moved
    print(f"Restored {moved} rows for profile '{profile}' from {archive}"
          + (f" ({skipped} already present, skipped)" if skipped else ""))
    return {"restored_n": moved, "archive": str(archive)}


def sweep_unstamped(root: Path, dry_run: bool = False) -> dict:
    """One-off sweep: rows WITHOUT a source_profile stamp leave the cullable
    face. Sources scanned (fixed order): pool knowledge/raw_pending.json,
    then knowledge/archive/raw_pending_auto_*.jsonl (name-sorted). Every
    unstamped row is MOVED into a single traceable archive
    knowledge/archive/raw_pending_unstamped_sweep_<UTCts>.jsonl — never a
    physical rm; the sweep stays auditable and reversible.

    Idempotent: after one sweep no unstamped rows remain in the scanned
    sources, so a re-run moves 0 and writes nothing (no archive, no audit
    row). ONE sweep audit row (action=sweep_unstamped) is appended to
    root/consolidation_log.jsonl per run that actually moved rows, under
    the consolidation_log flock (same direction as the plugin's _shard_lock
    on that file), so the human governance run is traceable. The whole
    operation runs under the exclusive flock on raw_pending.json.lock
    (same file + direction as the plugin C-slice _shard_lock, so no lock
    inversion). --dry-run counts without writing anything."""
    knowledge = root / "knowledge"
    pool = knowledge / "raw_pending.json"
    archive_dir = knowledge / "archive"

    def _scan_jsonl(p: Path):
        """(kept, unstamped, error) for one JSONL source."""
        try:
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
                    if l.strip()]
        except Exception as e:
            return [], [], e
        kept = [r for r in rows if r.get("source_profile")]
        unstamped = [r for r in rows if not r.get("source_profile")]
        return kept, unstamped, None

    auto_files = (sorted(archive_dir.glob("raw_pending_auto_*.jsonl"))
                  if archive_dir.exists() else [])

    if dry_run:
        # Pure-read preview: no lock file, no writes (mirrors promote's
        # dry-run discipline).
        pool_loaded = None
        if pool.exists():
            try:
                pool_loaded = _read_pool_file(pool)
                assert isinstance(pool_loaded, list)
            except Exception as e:
                print(f"warning: skipping unreadable source {pool}: {e}",
                      file=sys.stderr)
                pool_loaded = None
        planned = []
        if pool_loaded is not None:
            planned.append(("pool", [r for r in pool_loaded
                                     if not r.get("source_profile")]))
        for f in auto_files:
            kept, unstamped, err = _scan_jsonl(f)
            if err is not None:
                print(f"warning: skipping unreadable source {f}: {err}",
                      file=sys.stderr)
                continue
            planned.append((f.name, unstamped))
        total = sum(len(u) for _, u in planned)
        return {"dry_run": True, "would_move": total,
                "would_move_per_source": {n: len(u) for n, u in planned}}
    with _flock_lock(pool):
        pool_loaded = None
        pool_kept = []
        if pool.exists():
            try:
                pool_loaded = _read_pool_file(pool)
                assert isinstance(pool_loaded, list)
                pool_kept = [r for r in pool_loaded if r.get("source_profile")]
            except Exception as e:
                print(f"warning: skipping unreadable source {pool}: {e}",
                      file=sys.stderr)
                pool_loaded = None
        planned = []  # (source_name, unstamped_rows, kept_rows, is_pool)
        if pool_loaded is not None:
            unstamped = [r for r in pool_loaded if not r.get("source_profile")]
            planned.append(("pool", unstamped, pool_kept, True))
        for f in auto_files:
            kept, unstamped, err = _scan_jsonl(f)
            if err is not None:
                print(f"warning: skipping unreadable source {f}: {err}",
                      file=sys.stderr)
                continue
            planned.append((f.name, unstamped, kept, False))
        total = sum(len(u) for _, u, _, _ in planned)
        if total == 0:
            # Nothing to move: fully idempotent re-run — no archive, no audit
            # row, no file touched.
            return {"moved_total": 0, "archive_file": None,
                    "moved_per_source": {}}
        ts_tag = datetime.now(timezone.utc).replace(tzinfo=None).strftime(
            "%Y%m%dT%H%M%SZ")
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f"raw_pending_unstamped_sweep_{ts_tag}.jsonl"
        out = open(dest, "a", encoding="utf-8")
        try:
            for name, unstamped, kept, is_pool in planned:
                if not unstamped:
                    continue
                for r in unstamped:
                    out.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
                out.flush()
                if is_pool:
                    _tmp = pool.with_suffix(".tmp")
                    with open(_tmp, "w", encoding="utf-8") as f:
                        json.dump(kept, f, ensure_ascii=False, indent=2)
                    os.replace(_tmp, pool)
                else:
                    src_path = archive_dir / name
                    if kept:
                        _tmp = src_path.with_name(src_path.name + ".tmp")
                        with open(_tmp, "w", encoding="utf-8") as f:
                            for r in kept:
                                f.write(json.dumps(r, ensure_ascii=False,
                                                   sort_keys=True) + "\n")
                        os.replace(_tmp, src_path)
                    else:
                        # Fully-unstamped file: every row moved out — rewrite
                        # as empty rather than unlink (the archive stays
                        # readable by any concurrent reader; the sweep
                        # archive holds the rows).
                        src_path.write_text("", encoding="utf-8")
        finally:
            out.close()
        log_path = root / "consolidation_log.jsonl"
        log_lock = log_path.with_name(log_path.name + ".lock")
        with _flock_lock_for(log_lock):
            # ts is naive UTC (the store's _utcnow() convention, matching
            # every other consolidation_log row).
            audit = {"ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                     "action": "sweep_unstamped",
                     "moved_total": total,
                     "moved_per_source": {n: len(u) for n, u, _, _ in planned
                                           if u},
                     "archive_file": str(dest)}
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")
        return {"moved_total": total, "archive_file": str(dest),
                "moved_per_source": {n: len(u) for n, u, _, _ in planned if u},
                "ts_tag": ts_tag}


def sweep_quarantine_unstamped(root: Path, dry_run: bool = False,
                               hard: bool = False, confirm=None) -> dict:
    """One-off cull of the quarantine face: rows in
    quarantine/raw_pending_queue_overflow_*.jsonl WITHOUT a source_profile
    stamp (the legacy unstamped residue that sweep_unstamped never scans and
    restore can only 0-match) get a terminal home. GLOBAL face: unlike
    promote/restore this command needs no HERMES_PROFILE — unstamped rows
    belong to no profile, and an unidentified caller is exactly who should
    be able to run this one-off legacy cull.

    Default mode (archive-retain): every unstamped row is MOVED into
    knowledge/archive/raw_pending_quarantine_unstamped_<UTCts>.jsonl
    (terminal, self-describing); the source quarantine files are reduced by
    exactly those rows (stamped rows, if any, stay put). Before the move, a
    per-row content-hash manifest
    (knowledge/archive/raw_pending_quarantine_unstamped_<UTCts>_archive_manifest.
    json: source files / row counts / sha256 of each canonical row) is
    landed first — the retain-as-a-trail discipline: line content is
    never rm'd.

    hard mode (--hard): the SAME manifest is landed first (retain trail
    bottoming out), then the rows are PHYSICALLY DELETED from the
    quarantine files — IRREVERSIBLE, no archive file is written. A hard
    run requires the two-step gate: --dry-run first (returns would_move),
    then execution with --confirm <count> exactly matching that count;
    --hard without a matching --confirm is refused and touches nothing.

    Serialization + audit: each quarantine file touched is rewritten under
    its own exclusive flock (<file>.lock, same direction as the existing
    pool/log locks); one append-only audit row
    {action:quarantine_unstamped_sweep, mode:archive|hard, moved_n,
    manifest, ts} is appended to root/consolidation_log.jsonl under its
    flock. Idempotent: a re-run with 0 unstamped rows moves 0 and writes
    nothing (no manifest, no archive, no audit row). --dry-run counts
    without writing anything (no lock file, no manifest)."""
    quarantine = root / "quarantine"
    archive_dir = root / "knowledge" / "archive"
    qfiles = (sorted(quarantine.glob("raw_pending_queue_overflow_*.jsonl"))
              if quarantine.exists() else [])

    def _scan(p: Path):
        """(rows, error) for one quarantine JSONL source."""
        try:
            rows = [json.loads(l) for l in
                    p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except Exception as e:
            return [], e
        return rows, None

    planned = []  # (path, unstamped, kept)
    for p in qfiles:
        rows, err = _scan(p)
        if err is not None:
            print(f"warning: skipping unreadable quarantine source {p}: {err}",
                  file=sys.stderr)
            continue
        unstamped = [r for r in rows if not r.get("source_profile")]
        kept = [r for r in rows if r.get("source_profile")]
        planned.append((p, unstamped, kept))
    total = sum(len(u) for _, u, _ in planned)

    if dry_run:
        # Pure-read preview: no lock file, no manifest, no audit (mirrors
        # sweep_unstamped's dry-run discipline).
        return {"dry_run": True, "mode": "hard" if hard else "archive",
                "would_move": total,
                "would_move_per_source": {p.name: len(u) for p, u, _ in planned if u}}

    if hard:
        # Two-step gate: --hard executes only with an explicit confirm of
        # the live count (obtained from a prior --dry-run). A missing or
        # mismatched confirm refuses and touches nothing.
        if confirm is None:
            return {"error": ("--hard is refused without --confirm: run "
                               "--dry-run first, then re-run with --hard "
                               "--confirm <would_move> "
                               "(physical delete is irreversible)")}
        if confirm != total:
            return {"error": (f"--confirm {confirm} does not match the "
                              f"{total} live unstamped quarantine rows; "
                              "re-run --dry-run and confirm the live count")}

    if total == 0:
        # Nothing to move: fully idempotent re-run — no manifest, no
        # archive, no audit row, no file touched.
        return {"moved_total": 0, "archive_file": None, "manifest": None,
                "moved_per_source": {}, "mode": "hard" if hard else "archive"}

    ts_tag = datetime.now(timezone.utc).replace(tzinfo=None).strftime(
        "%Y%m%dT%H%M%SZ")
    manifest_path = archive_dir / (
        f"raw_pending_quarantine_unstamped_{ts_tag}_archive_manifest.json")

    # Retain trail FIRST: the manifest (source files / row counts / per-row
    # sha256 of the canonical JSONL row) is landed to disk BEFORE any row
    # is moved or physically deleted.
    src_entries = []
    for p, unstamped, _ in planned:
        if not unstamped:
            continue
        src_entries.append({
            "source_file": str(p.relative_to(root)),
            "moved_n": len(unstamped),
            "hashes": [hashlib.sha256(
                json.dumps(r, ensure_ascii=False, sort_keys=True)
                .encode("utf-8")).hexdigest() for r in unstamped],
        })
    manifest = {"action": "quarantine_unstamped_sweep",
                 "mode": "hard" if hard else "archive",
                 "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                 "moved_n": total, "source_files": src_entries}
    archive_dir.mkdir(parents=True, exist_ok=True)
    m_tmp = manifest_path.with_suffix(".json.tmp")
    with open(m_tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, sort_keys=True, indent=2)
    os.replace(m_tmp, manifest_path)

    def _reduce_source(p: Path, kept: list):
        # Rewrite the quarantine file down to its stamped rows; a fully
        # unstamped file is rewritten EMPTY, never unlinked (the file stays
        # readable by any concurrent reader — mirrors sweep_unstamped).
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(tmp, p)

    if not hard:
        archive_path = archive_dir / f"raw_pending_quarantine_unstamped_{ts_tag}.jsonl"
        out = open(archive_path, "a", encoding="utf-8")
        try:
            for p, unstamped, kept in planned:
                if not unstamped:
                    continue
                with _flock_lock(p):
                    for r in unstamped:
                        out.write(json.dumps(r, ensure_ascii=False,
                                              sort_keys=True) + "\n")
                    out.flush()
                    _reduce_source(p, kept)
        finally:
            out.close()
    else:
        # hard: manifest already on disk (trail); physically delete the
        # rows by rewriting each source down to its stamped rows only.
        archive_path = None
        for p, unstamped, kept in planned:
            if not unstamped:
                continue
            with _flock_lock(p):
                _reduce_source(p, kept)

    log_path = root / "consolidation_log.jsonl"
    log_lock = log_path.with_name(log_path.name + ".lock")
    with _flock_lock_for(log_lock):
        audit = {"ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                 "action": "quarantine_unstamped_sweep",
                 "mode": "hard" if hard else "archive",
                 "moved_n": total,
                 "manifest": str(manifest_path),
                 "moved_per_source": {p.name: len(u) for p, u, _ in planned if u}}
        if not hard:
            audit["archive_file"] = str(archive_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(audit, ensure_ascii=False, sort_keys=True) + "\n")

    result = {"moved_total": total,
              "archive_file": None if hard else str(archive_path),
              "manifest": str(manifest_path),
              "moved_per_source": {p.name: len(u) for p, u, _ in planned if u},
              "ts_tag": ts_tag, "mode": "hard" if hard else "archive"}
    return result


def _flock_lock_for(lock_path: Path):
    """Flock context on an explicit lock FILE path (for the audit log, whose
    lock file differs from the one _flock_lock derives from a data file).
    Same LOCK_EX + no-op-degrade semantics as _flock_lock."""
    fh = open(lock_path, "a+")
    try:
        import fcntl
    except ImportError:
        fcntl = None

    class _L:
        def __enter__(self_i):
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            return self_i

        def __exit__(self_i, *exc):
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            return False

    return _L()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Clean up raw_pending.json")
    parser.add_argument("--root", required=True, help="Path to filemem directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    parser.add_argument("--restore", action="store_true", help="Restore stamped rows from archive")
    parser.add_argument("--list", action="store_true",
                        help="List raw_pending rows across pool/quarantine/auto-archive "
                             "(pure read, no writes; when HERMES_PROFILE is set, "
                             "only rows stamped with that profile)")
    parser.add_argument("--promote", type=int, metavar="ROW",
                        help="Promote global row ROW (as printed by --list) back into the pool "
                             "with origin=manual_promote + audit row. Owns the cull face: "
                             "refused when HERMES_PROFILE is unset (fail-closed), and the "
                             "target row must be stamped with exactly HERMES_PROFILE.")
    parser.add_argument("--sweep-unstamped", action="store_true",
                        help="One-off sweep: move rows WITHOUT a source_profile stamp "
                             "(pool + auto-archive) into a traceable archive "
                             "(raw_pending_unstamped_sweep_<ts>.jsonl) + a consolidation_log "
                             "audit row. Idempotent; never a physical rm. Use --dry-run to "
                             "count without writing.")
    parser.add_argument("--sweep-quarantine-unstamped", action="store_true",
                        help="One-off quarantine-face cull: move rows WITHOUT a "
                             "source_profile stamp from quarantine/raw_pending_queue_overflow_*.jsonl "
                             "into a terminal archive "
                             "(knowledge/archive/raw_pending_quarantine_unstamped_<UTCts>.jsonl) "
                             "with a landed per-row sha256 manifest + consolidation_log audit row. "
                             "GLOBAL face (no HERMES_PROFILE needed — unstamped rows belong to no "
                             "profile). Idempotent; default mode never physically rm's. "
                             "Use --dry-run to count without writing; add --hard for the "
                             "irreversible physical-delete variant (requires --confirm <count>).")
    parser.add_argument("--hard", action="store_true",
                        help="With --sweep-quarantine-unstamped only: physically delete the "
                             "unstamped rows after the manifest is landed (retain trail, "
                             "irreversible). Refused unless --confirm carries the live count "
                             "obtained from a prior --dry-run.")
    parser.add_argument("--confirm", type=int, metavar="N", default=None,
                        help="Second-confirmation row count for --hard: must exactly equal "
                             "the live unstamped count from the prior --dry-run.")
    args = parser.parse_args()

    root = Path(args.root)
    knowledge = root / "knowledge"
    quarantine = root / "quarantine"
    profile = _caller_profile()

    if args.list:
        for n, (label, row) in enumerate(list_rows(knowledge, quarantine, profile), start=1):
            print(_list_row_line(n, label, row))
    elif args.promote is not None:
        result = promote(root, args.promote, dry_run=args.dry_run, profile=profile)
        print(json.dumps(result, indent=2))
        if "error" in result:
            detail = (f" (valid row range: {result['valid_range']})"
                      if "valid_range" in result else "")
            print(f"error: {result['error']}{detail}", file=sys.stderr)
            if "hint" in result:
                print(f"hint: {result['hint']}", file=sys.stderr)
            sys.exit(2)
    elif args.sweep_unstamped:
        result = sweep_unstamped(root, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
    elif args.sweep_quarantine_unstamped:
        if args.hard:
            # --hard only governs this subcommand (documented on the flag).
            result = sweep_quarantine_unstamped(
                root, dry_run=args.dry_run, hard=True, confirm=args.confirm)
        else:
            result = sweep_quarantine_unstamped(root, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        if "error" in result:
            print(f"error: {result['error']}", file=sys.stderr)
            sys.exit(2)
    elif args.restore:
        if profile is None:
            print("profile identity unrecognized, cull action refused "
                  "(restore); set HERMES_PROFILE", file=sys.stderr)
            sys.exit(2)
        restore(knowledge / "raw_pending.json", quarantine, profile)
    else:
        result = clean(knowledge / "raw_pending.json", quarantine, args.dry_run)
        print(json.dumps(result, indent=2))
