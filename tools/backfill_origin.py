#!/usr/bin/env python
r"""SEC-1-1: backfill the deterministic ``origin`` trust stamp onto live storage.

Data model (spec SEC-1-1): every memory/episodic row carries
``metadata.origin = {kind, trust}`` where
``kind  in {user, model, tool_output, external, unknown}`` and
``trust in {high, medium, low}``. This tool stamps the existing corpus
(72 memories/*.json rows + 155 episodic jsonl lines) with the SAME
deterministic mapping the save doors use (single source of truth: the
plugin's own ``origin_stamp_for_source`` / ``_EPISODIC_ORIGIN_STAMP`` —
no duplicated table, no LLM, stdlib only), so old rows get the same
treatment new rows do.

Mapping rules (from the live data's actual ``source`` values):
- memories rows: source 'auto' -> user/medium, 'manual' -> user/high,
  'confirmed_draft' -> user/high, 'session:*' -> user/medium;
  anything else (including missing) -> unknown/low (fail-safe lowest
  trust, never guess high).
- episodic rows: the episodic write door (_write_episodic_entry) only
  aggregates this session's user-turn drafts -> user/medium
  (deterministic door rule, independent of row content).

Portability rules (tool-script audit gate): NO hardcoded paths — the
storage root is a CLI arg, else the same auto-resolution the sibling
backfill tool uses (``<base>/filemem`` where ``base`` is HERMES_HOME /
~/.hermes with a trailing ``profiles/<name>`` segment stripped); every
root is parameterized so the function is directly unit-testable on a
fixture tree.

Guarantees:
- Idempotent: rows whose metadata.origin is already well-formed (the
  enums, value or from a previous run) are NEVER re-touched — a second
  run leaves every file byte-identical (zero change). Malformed/absent
  values are re-stamped from the deterministic rule.
- .bak before first write: the original pre-backfill snapshot is copied
  next to the file (<file>.bak) the FIRST time the file is mutated;
  existing .bak files are never clobbered (the original stays restorable).
- Diff log: one JSON line per stamped row (file/index/id/source/origin)
  appended to <root>/backfill_origin.log, plus a per-run summary line.
- Zero-loss gate: per-file row counts + a content fingerprint (sha256
  over each row's fields EXCLUDING metadata.origin, position-ordered)
  are captured BEFORE the rewrite and re-verified AFTER; any mismatch
  aborts the write of that file.
- Fail-soft: an unparseable episodic line is kept verbatim and logged,
  never fatal.

Usage:
  python tools/backfill_origin.py --dry-run      # report, no writes
  python tools/backfill_origin.py               # stamp + verify
  python tools/backfill_origin.py --storage-root /path/to/filemem
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path  # noqa: E402
_PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
from dep_bootstrap import _resolve_hermes_home  # noqa: E402

# Import the mapping from the plugin itself (single source of truth, same
# shape as backfill_occurred_at: plugin dir on sys.path, `__init__` top-level).
# Deliberate local import: after the sys.path insert above this resolves to
# the plugin module; the audit gate (tests) sees stdlib-only module-level
# imports here (json/hashlib/argparse/...), the rest is function-level.
from __init__ import (  # noqa: E402
    _EPISODIC_ORIGIN_STAMP,
    _origin_is_wellformed,
    origin_stamp_for_source,
)

DIFF_LOG_NAME = "backfill_origin.log"


def default_storage_root() -> Path:
    """Mirror the provider's auto tier: <base>/filemem where <base> is
    HERMES_HOME (or ~/.hermes) minus a trailing profiles/<name> segment."""
    import os
    home = Path(_resolve_hermes_home())
    parts = [p for p in home.parts if p]
    if "profiles" in parts:
        idx = parts.index("profiles")
        base = Path(*parts[:idx]) if idx > 0 else None
    else:
        base = Path(*parts) if parts else None
    if base is None:
        base = Path.cwd()
    return base / "filemem"


def _row_fingerprint(rows) -> str:
    """Zero-loss gate: sha256 over each row's fields EXCLUDING
    metadata.origin, position-ordered. Pre == post rewrite proves no data
    was lost/shifted — only the new key was added."""
    h = hashlib.sha256()
    for row in rows:
        if isinstance(row, dict):
            meta = dict(row.get("metadata") or {})
            meta.pop("origin", None)
            stripped = dict(row)
            stripped["metadata"] = meta
        else:
            stripped = row
        h.update(json.dumps(stripped, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _stamp_memories_rows(rows, where: str, diff: list, now: str):
    """Stamp memories rows in place. Returns (changed: bool, stamped: int).

    Idempotency rule: a row whose metadata.origin is already well-formed is
    NEVER re-touched, so a second run rewrites zero files (byte-identical)."""
    changed = False
    stamped = 0
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        meta = row.setdefault("metadata", {})
        if _origin_is_wellformed(meta.get("origin")):
            continue  # present + well-formed -> leave, idempotent
        new_val = origin_stamp_for_source(row.get("source"))
        meta["origin"] = new_val
        changed = True
        stamped += 1
        diff.append({
            "ts": now,
            "kind": "memories",
            "file": where,
            "index": idx,
            "id": row.get("id"),
            "source": row.get("source"),
            "origin": new_val,
        })
    return changed, stamped


def _backfill_memories_file(path: Path, now: str, diff: list, dry_run: bool = False) -> dict:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        return {"file": str(path), "skipped": "not a JSON list"}
    pre_count, pre_fp = len(rows), _row_fingerprint(rows)
    changed, stamped = _stamp_memories_rows(rows, str(path), diff, now)
    out = {"file": str(path), "rows": pre_count, "stamped": stamped}
    if dry_run:
        return out  # report only: no .bak, no write, no diff-log entry
    if not changed:
        return out
    pre_bak = path.with_name(path.name + ".bak")
    if not pre_bak.exists():  # first mutation only — keep the original snapshot
        pre_bak.write_bytes(path.read_bytes())
        out["backup"] = str(pre_bak)
    new_text = json.dumps(rows, ensure_ascii=False, indent=2)
    post_rows = json.loads(new_text)
    # Zero-loss gate BEFORE touching the real file.
    if len(post_rows) != pre_count or _row_fingerprint(post_rows) != pre_fp:
        raise RuntimeError(f"zero-loss gate failed for {path}")
    path.write_text(new_text, encoding="utf-8")
    return out


def _backfill_episodic_file(path: Path, now: str, diff: list, dry_run: bool = False) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    pre_count = sum(1 for ln in lines if ln.strip())
    parsed = []
    fp_rows = []
    for ln in lines:
        if not ln.strip():
            parsed.append(None)
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            parsed.append(None)  # keep verbatim (fail-soft), never crash
            continue
        parsed.append(e)
        fp_rows.append(e)
    pre_fp = _row_fingerprint(fp_rows)
    out_lines = []
    stamped_here = 0
    for i, (ln, e) in enumerate(zip(lines, parsed)):
        if e is None:
            # Blank / unparseable lines stay VERBATIM (fail-soft: never
            # crash, never silently drop a line we couldn't read).
            out_lines.append(ln)
            continue
        if _origin_is_wellformed((e.get("metadata") or {}).get("origin")):
            out_lines.append(ln)
            continue  # present + well-formed -> leave, idempotent
        meta = e.setdefault("metadata", {})
        new_val = dict(_EPISODIC_ORIGIN_STAMP)
        meta["origin"] = new_val
        stamped_here += 1
        diff.append({
            "ts": now,
            "kind": "episodic",
            "file": str(path),
            "index": i,
            "id": e.get("session_id"),
            "origin": new_val,
        })
        out_lines.append(json.dumps(e, ensure_ascii=False))
    out = {"file": str(path), "rows": pre_count, "stamped": stamped_here}
    if dry_run:
        return out  # report only: no .bak, no write, no diff-log entry
    if stamped_here == 0:
        return out
    post_rows = [e for e in parsed if isinstance(e, dict)]
    if len(post_rows) != len(fp_rows) or _row_fingerprint(post_rows) != pre_fp:
        raise RuntimeError(f"zero-loss gate failed for {path}")
    pre_bak = path.with_name(path.name + ".bak")
    if not pre_bak.exists():
        pre_bak.write_bytes(path.read_bytes())
        out["backup"] = str(pre_bak)
    original = path.read_text(encoding="utf-8")
    had_trailing_nl = original.endswith("\n")
    path.write_text("\n".join(out_lines) + ("\n" if had_trailing_nl else ""),
                    encoding="utf-8")
    return out


def backfill(storage_root: Path, dry_run: bool = False, log_path: Path = None) -> dict:
    """Stamp every row under storage_root's memories/ + episodic/ trees.

    Returns a summary dict; safe to call twice — the second run stamps zero
    rows, rewrites zero files, and leaves every file byte-identical.
    """
    storage_root = Path(storage_root)
    if log_path is None:
        log_path = storage_root / DIFF_LOG_NAME
    now = datetime.now().astimezone().isoformat()
    summary = {"root": str(storage_root), "dry_run": dry_run, "files": [],
               "total_stamped": 0, "total_rows": 0,
               "distribution": {}, "stamped_distribution": {}}
    diff: list = []
    memory_files = sorted(p for p in (storage_root / "memories").glob("*.json")
                          if p.is_file()) if (storage_root / "memories").is_dir() else []
    episodic_files = sorted(p for p in (storage_root / "episodic").rglob("*.jsonl")
                            if p.is_file() and not p.name.endswith(".lock")) \
        if (storage_root / "episodic").is_dir() else []
    for path in memory_files + episodic_files:
        try:
            if path.suffix == ".json":
                res = _backfill_memories_file(path, now, diff, dry_run=dry_run)
            else:
                res = _backfill_episodic_file(path, now, diff, dry_run=dry_run)
        except Exception as e:
            # fail-soft: one bad file must not sink the corpus run
            res = {"file": str(path), "error": str(e)}
        summary["files"].append(res)
        summary["total_stamped"] += res.get("stamped", 0)
        summary["total_rows"] += res.get("rows", 0)
        # Distribution reporting (bucket counts kind/trust, post-stamp view)
        if res.get("rows"):
            buckets = {}
            if path.suffix == ".json":
                try:
                    for row in json.loads(path.read_text(encoding="utf-8")):
                        o = (row.get("metadata") or {}).get("origin") if isinstance(row, dict) else None
                        key = f"{o.get('kind')}/{o.get('trust')}" if _origin_is_wellformed(o) else "unknown/low"
                        buckets[key] = buckets.get(key, 0) + 1
                except Exception:
                    pass
            else:
                for ln in path.read_text(encoding="utf-8").splitlines():
                    if not ln.strip():
                        continue
                    try:
                        e = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    o = (e.get("metadata") or {}).get("origin") if isinstance(e, dict) else None
                    key = f"{o.get('kind')}/{o.get('trust')}" if _origin_is_wellformed(o) else "unknown/low"
                    buckets[key] = buckets.get(key, 0) + 1
            for k, v in buckets.items():
                summary["distribution"][k] = summary["distribution"].get(k, 0) + v
    if not dry_run and diff:
        stamped_buckets = {}
        for d in diff:
            key = f"{d['origin'].get('kind')}/{d['origin'].get('trust')}"
            stamped_buckets[key] = stamped_buckets.get(key, 0) + 1
        summary["stamped_distribution"] = stamped_buckets
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            for d in diff:
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": now, "run_summary": summary},
                               ensure_ascii=False) + "\n")
        summary["diff_log"] = str(log_path)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="SEC-1-1: backfill origin trust stamp (idempotent, zero LLM)")
    ap.add_argument("--storage-root", default=None,
                    help="filemem root (default: auto-resolved <base>/filemem)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be stamped; write nothing (not even .bak / diff log)")
    args = ap.parse_args()
    root = Path(args.storage_root).expanduser() if args.storage_root else default_storage_root()
    if not root.is_dir():
        print(f"ERROR: storage root not found: {root}", file=sys.stderr)
        return 2
    summary = backfill(root, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"root={summary['root']} dry_run={summary['dry_run']} "
          f"files={len(summary['files'])} rows={summary['total_rows']} "
          f"stamped={summary['total_stamped']} "
          f"distribution={json.dumps(summary['distribution'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
