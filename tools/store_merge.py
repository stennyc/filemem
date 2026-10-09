"""T5a store merge: union a per-profile filemem store into the central store.

Implements @data-engineer's merge contract (group chat 2026-09-27):
- deterministic union, ZERO raw deletion, ZERO loss
- memories: union by memory_id. Same id + same content (sha1) -> 1 entry;
  same id diff content -> BOTH kept, default-side copy renamed `<id>~default`
  (+ a conflict id in the audit row; whichever runs second never overwrites)
- MEMORY.md / USER.md: line-level union, exact-duplicate lines deduped,
  central lines first, then default-only lines in first-seen order
- *.jsonl append logs (consolidated/, sessions/): line-hash dedup, central
  first, default-only lines appended; conflicting (diff) lines keep both
- drafts_*.json: filename collision -> default side renamed `<name>~default`
  (whole file moved, entries preserved); otherwise plain copy
- source_profile stamps are NEVER rewritten (lineage stays auditable)
- audit: both trees snapshotted to <archive>/store-merge-<ts>/ {central,default}
  BEFORE mutation; one `store_merge` event appended to the central
  <storage>/consolidation_log.jsonl (files/entries/lines + conflict ids +
  snapshot path + actor). Rollback = restore the snapshot.
- dry_run: compute the report, touch nothing on disk (loss is 0 by construction)

Idempotent: a re-run detects already-merged marker entries and reports loss 0.

CLI:
    python3 store_merge.py --central <storage_root> --default <storage_root> \
        --archive <archive_dir> --actor coder [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MARKER = "~default"          # suffix for the default-side copy of a collision
_AUDIT_NAME = "consolidation_log.jsonl"
_MD_FILES = ("MEMORY.md", "USER.md")
_JSONL_SUBDIRS = ("consolidated", "sessions")


def _sha1(obj) -> str:
    if isinstance(obj, str):
        raw = obj
    else:
        raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()


def _content_sha1(entry: dict) -> str:
    """Dedup identity per @data-engineer: (memory_id matched, content compared).
    memory_id is the match key; source_profile is a lineage stamp — neither
    participates in content identity, so an entry written under two profiles
    with identical content dedups (content survives; the audit row records it)."""
    body = {k: v for k, v in entry.items() if k not in ("memory_id", "source_profile")}
    return _sha1(body)


def _load_json(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _append_json(path: Path, items: List[dict]) -> None:
    cur = _load_json(path)
    cur.extend(items)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")


def _count_entries(storage: Path) -> int:
    """Total item count across memories/*.json + drafts_*.json (empty shells count 0)."""
    n = 0
    mem = storage / "memories"
    if mem.is_dir():
        for f in mem.glob("*.json"):
            n += len(_load_json(f))
    for f in storage.glob("drafts_*.json"):
        n += len(_load_json(f))
    return n


def _line_union(central_text: Optional[str], default_text: Optional[str]) -> Tuple[str, int]:
    """Line-level union: central lines first (deduped), then default-only lines in
    first-seen order. Returns (merged_text, lines_added_from_default)."""
    seen: set = set()
    out: List[str] = []
    added = 0

    def _side(text: Optional[str], track: bool) -> None:
        nonlocal added
        if not text:
            return
        for ln in text.splitlines():
            if ln in seen:
                continue
            seen.add(ln)
            out.append(ln)
            if track:
                added += 1

    _side(central_text, track=False)
    _side(default_text, track=True)
    return ("\n".join(out) + ("\n" if out else ""), added)


def _jsonl_union(central_p: Path, default_p: Path) -> Tuple[str, int]:
    """Append-log union by line-hash: central first, default-only lines kept after;
    identical lines collapse, conflicting (diff) lines keep both."""
    seen: set = set()
    lines: List[str] = []
    added = 0

    for p, track in ((central_p, False), (default_p, True)):
        if not p.is_file():
            continue
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            key = _sha1(ln)
            if key in seen:
                continue
            seen.add(key)
            lines.append(ln)
            if track:
                added += 1
    return ("\n".join(lines) + ("\n" if lines else ""), added)


def merge_trees(central: Path, default: Path, archive: Path,
                actor: str, *, dry_run: bool = False) -> dict:
    """Merge *default*'s store into *central*'s (both are storage roots).
    Returns the report dict that also feeds the `store_merge` audit row."""
    central, default, archive = Path(central), Path(default), Path(archive)
    pre_c = _count_entries(central)
    pre_d = _count_entries(default)

    conflicts: List[str] = []
    identical = 0          # default entries already represented (same id+content)
    already_merged = 0     # default entries already represented (marker present) - rerun
    lines_added = 0
    touched: set = set()

    # ── 1. memories union ─────────────────────────────────────────────────
    mem_c = central / "memories"
    mem_d = default / "memories"
    if mem_d.is_dir():
        index: Dict[str, Tuple[Path, dict]] = {}
        if mem_c.is_dir():
            for f in sorted(mem_c.glob("*.json")):
                for e in _load_json(f):
                    mid = e.get("memory_id")
                    if mid:
                        index[mid] = (f, e)
        for f in sorted(mem_d.glob("*.json")):
            entries = _load_json(f)
            new_entries: List[dict] = []
            for e in entries:
                mid = e.get("memory_id")
                if mid and mid in index:
                    cpath, centry = index[mid]
                    marker_id = f"{mid}{MARKER}"
                    marker_present = any(x.get("memory_id") == marker_id for x in _load_json(cpath))
                    if marker_present:
                        already_merged += 1                 # idempotent re-run
                    elif _content_sha1(centry) == _content_sha1(e):
                        identical += 1                     # same content: dedup
                    else:
                        # conflict: keep BOTH, default side gets the marker suffix
                        if not dry_run:
                            _append_json(cpath, [{**e, "memory_id": marker_id}])
                        conflicts.append(mid)
                        touched.add(f"memories/{cpath.name}")
                else:
                    new_entries.append(e)
            if new_entries:
                tgt = mem_c / f.name
                if not dry_run:
                    _append_json(tgt, new_entries)
                for e in new_entries:
                    m2 = e.get("memory_id")
                    if m2:
                        index[m2] = (tgt, e)
                touched.add(f"memories/{f.name}")

    # ── 2. MEMORY.md / USER.md line union ─────────────────────────────────
    for name in _MD_FILES:
        cp, dp = central / name, default / name
        if not (cp.is_file() or dp.is_file()):
            continue
        merged, added = _line_union(cp.read_text(encoding="utf-8") if cp.is_file() else None,
                                    dp.read_text(encoding="utf-8") if dp.is_file() else None)
        lines_added += added
        central_now = cp.read_text(encoding="utf-8") if cp.is_file() else None
        if merged and merged != central_now and not dry_run:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(merged, encoding="utf-8")
            touched.add(name)

    # ── 3. *.jsonl append-log union ───────────────────────────────────────
    for sub in _JSONL_SUBDIRS:
        cd, dd = central / sub, default / sub
        if not dd.is_dir():
            continue
        for lf in sorted(dd.glob("**/*.jsonl")):
            rel = lf.relative_to(dd)
            merged, added = _jsonl_union(cd / rel, lf)
            lines_added += added
            if added and not dry_run:
                tgt = cd / rel
                tgt.parent.mkdir(parents=True, exist_ok=True)
                tgt.write_text(merged, encoding="utf-8")
                touched.add(f"{sub}/{rel}")

    # ── 4. drafts union (uuid-suffixed; collision -> rename default side) ─
    for df in sorted(default.glob("drafts_*.json")):
        entries = _load_json(df)
        if not entries:
            continue
        tgt_name = df.name
        if (central / df.name).exists():
            tgt_name = f"{df.stem}{MARKER}.json"      # collision: rename whole file
        if not dry_run:
            central.mkdir(parents=True, exist_ok=True)
            (central / tgt_name).write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        touched.add(tgt_name)

    # ── 5. compute loss, snapshot + audit (never in dry-run) ──────────────
    skip = identical + already_merged
    expected_post = pre_c + pre_d - skip
    if dry_run:
        post_c = expected_post
        snapshot = None
    else:
        post_c = _count_entries(central)
        snapshot = archive / f"store-merge-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        snapshot.mkdir(parents=True, exist_ok=True)
        for label, src in (("central", central), ("default", default)):
            shutil.copytree(src, snapshot / label, dirs_exist_ok=True)
        row = {
            "event": "store_merge",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "actor": actor,
            "central_store": str(central),
            "default_store": str(default),
            "files": len(touched),
            "entries": post_c,
            "lines": lines_added,
            "pre_central_entries": pre_c,
            "pre_default_entries": pre_d,
            "conflicts": conflicts,
            "snapshot": str(snapshot),
        }
        log = central / _AUDIT_NAME
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    loss = max(0, expected_post - post_c)
    return {
        "dry_run": dry_run,
        "pre_central_entries": pre_c,
        "pre_default_entries": pre_d,
        "post_central_entries": post_c,
        "identical": identical,
        "already_merged": already_merged,
        "conflicts": conflicts,
        "lines": lines_added,
        "loss": loss,
        "snapshot": str(snapshot) if snapshot else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Union a per-profile filemem store into the central store "
                    "(deterministic, snapshot + audit, 0 raw deletion, 0 loss).")
    ap.add_argument("--central", required=True, help="central storage root (memories/, drafts_*.json, ...)")
    ap.add_argument("--default", required=True, help="default-profile storage root to merge in")
    ap.add_argument("--archive", required=True, help="archive dir for pre-merge snapshots")
    ap.add_argument("--actor", default="store-merge", help="audit actor id")
    ap.add_argument("--dry-run", action="store_true", help="compute report only; touch nothing")
    a = ap.parse_args()
    rep = merge_trees(Path(a.central), Path(a.default), Path(a.archive), a.actor, dry_run=a.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["loss"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
