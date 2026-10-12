#!/usr/bin/env python
r"""P2 v2 M2: tag the self-test residue set (G3, recomputed at M2 exec time).

Design (locked by PM lock + DE caliber fix + Reviewer evidence chain):
- The tagging set is NOT a frozen list ("11 items" was a C4-time snapshot;
  the self-referential spiral kept growing). At M2 exec time the script
  RECOMPUTES the full set from live storage using the exec1 manifest's
  `anchor_rules` selectors + window, exactly like exec1 did (time-of-exec
  re-delimiting; tagged counts drifting with exec time is known behavior,
  not a defect).
- Tag = metadata.self_test = true IN PLACE (no deletion — the v2 design is
  a surgical cut: data stays on disk, only the prefetch egress face is cut).
- Whitelist fact_342_1790326138 / fact_344_1790326539 (real production
  negative examples) are NEVER touched — the ids are read from the exec1
  manifest (single source of truth), intersection with the tagging set must
  be empty (hard abort otherwise, same as Reviewer's C3 diff).
- Result: an APPEND-ONLY second manifest run-record appended to
  storage/archive/p2-selftest-20260925/manifest.json (path+index+created+sha1
  anchor form, recompute rule, result count) so the Reviewer's independent
  chain can verify it the same way as exec1.

Usage:
  python tag_v2_selftest.py --dry-run   # recompute + print, no writes
  python tag_v2_selftest.py             # M2 exec: tag + append manifest record

Run with the host venv python (the interpreter the backend loads the plugin
with).
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- topology (N=1, resolved physical path — Reviewer path-resolution rule) ---
# Mirror __init__.py:DEFAULT_MEMORY_DIR (dynamic per host, never hardcoded —
# the old C:\Users\user\... pin broke on non-Windows hosts).
STORAGE_ROOT = Path.home() / ".hermes" / "plugins" / "filemem" / "storage"
MEMORIES_DIR = STORAGE_ROOT / "memories"
ARCHIVE_DIR = STORAGE_ROOT / "archive" / "p2-selftest-20260925"
MANIFEST = ARCHIVE_DIR / "manifest.json"
MEMORY_FILES = ["fact.json", "preference.json", "decision.json", "task.json"]

# exec1 window lower bound: manifest anchor_rules says
# "created_at >= 2026-09-24T23:50:00 (local naive; room discussion window)".
# This host is MSL UTC+8, and item created_at is written naive-UTC by the
# provider (_utcnow() in __init__.py: aware-UTC stripped to naive), so the
# bound in created_at format:
WINDOW_START_LOCAL = "2026-09-24T23:50:00"
WINDOW_START_UTC = "2026-09-24T15:50:00"
LOCAL_TZ_OFFSET_H = 8

CANONICAL_SHA1 = "sha1(json.dumps(item, ensure_ascii=False, sort_keys=True).encode('utf-8'))"

# M1/M3 shared META_MARKERS head table (single source of truth, mirroring
# __init__.py — the room/meta skeleton shapes observed in stored items).
HEAD_MARKERS = ("Rules for this room", "Group chat:", "[System note",
                "new messages in the room", "[OUT-OF-BAND USER MESSAGE",
                "You are @", "(pass)", "One entry per")


def item_sha1(item: dict) -> str:
    """exec1's canonical item-level sha1 form (manifest-declared)."""
    return hashlib.sha1(
        json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def file_sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def selectors_hit(item: dict) -> list:
    """exec1 anchor_rules selectors applied to one live item (DE #2):
    room_marker in content | warn_marker in content | metadata.failure_type.
    Whitelist exclusion is done by the caller via id, never in-predicate."""
    hits = []
    t = (item.get("content") or "").strip()
    head = t[:120]
    # room_marker: room/meta skeleton shape (M3-shaped: structural opener
    # {/[ OR a META_MARKERS substring in the head)
    if t[:1] in ("{", "[") or any(m in head for m in HEAD_MARKERS):
        hits.append("room_marker")
    # warn_marker
    if "\u26a0\ufe0f" in t:
        hits.append("warn_marker")
    # failure_type metadata selector
    if (item.get("metadata") or {}).get("failure_type"):
        hits.append("failure_type")
    return hits


def load_exec1_manifest() -> dict:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    runs = data if isinstance(data, list) else [data]
    exec1 = next(r for r in runs if r.get("run_id") == "p2-selftest-20260925-exec1")
    return exec1


def main() -> int:
    ap = argparse.ArgumentParser(description="P2 v2 M2 self-test tagging (G3 recomputed set)")
    ap.add_argument("--dry-run", action="store_true",
                    help="recompute the tagging set and print; write nothing")
    args = ap.parse_args()

    exec1 = load_exec1_manifest()
    whitelist_ids = {w["id"] for w in exec1.get("whitelist", [])}
    print(f"whitelist (from exec1 manifest, untouchable): {sorted(whitelist_ids)}")

    # Phase 1: read + recompute + hash BEFORE any mutation.
    pre_file_sha = {m: file_sha1(MEMORIES_DIR / m) for m in MEMORY_FILES
                    if (MEMORIES_DIR / m).exists()}
    tag_set = []   # (file, index, item, selectors)
    for fname in MEMORY_FILES:
        fpath = MEMORIES_DIR / fname
        if not fpath.exists():
            continue
        rows = json.loads(fpath.read_text(encoding="utf-8"))
        for idx, item in enumerate(rows):
            if item.get("id") in whitelist_ids:
                continue
            if (item.get("created_at") or "") < WINDOW_START_UTC:
                continue  # outside the exec1 room window (created_at is UTC)
            hits = selectors_hit(item)
            if not hits:
                continue
            if item.get("metadata", {}).get("self_test"):
                continue  # already tagged (idempotent re-runs)
            tag_set.append((fname, idx, item, hits))

    # Whitelist safety (Reviewer C3 shape): intersection must be empty.
    hit_ids = {it.get("id") for _, _, it, _ in tag_set}
    overlap = hit_ids & whitelist_ids
    if overlap:
        print(f"ABORT: whitelist intersection non-empty: {sorted(overlap)}")
        return 2

    dist = {}
    for fname, _, _, hits in tag_set:
        for h in hits:
            dist[h] = dist.get(h, 0) + 1
    print(f"recomputed tagging set: {len(tag_set)} items "
          f"(window created_at >= {WINDOW_START_UTC} UTC "
          f"= {WINDOW_START_LOCAL} local MSL)")
    print(f"selector distribution: {dist}")

    if args.dry_run:
        for fname, idx, item, hits in tag_set:
            print(f"  would tag {fname}[{idx}] {item.get('id')} "
                  f"created={item.get('created_at')} sel={hits}")
        print("dry-run: no writes")
        return 0

    # Phase 2: mutate in place (metadata only — content/ids untouched).
    tagged_ids = []
    post_file_sha = {}
    for fname in MEMORY_FILES:
        fpath = MEMORIES_DIR / fname
        if not fpath.exists():
            continue
        target_idx = {idx for f2, idx, it, h in tag_set if f2 == fname}
        if not target_idx:
            continue
        rows = json.loads(fpath.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        for idx, item in enumerate(rows):
            if idx not in target_idx:
                continue
            item.setdefault("metadata", {})["self_test"] = True
            item["metadata"]["self_test_tagged_at"] = now
            item["metadata"]["self_test_tag"] = "p2-v2-m2"
            item["updated_at"] = now
            tagged_ids.append(item.get("id"))
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        post_file_sha[fname] = file_sha1(fpath)

    # Phase 3: append-only second manifest run-record (Reviewer evidence chain).
    # post_item_sha1 is computed from the ACTUAL post-mutation rows (re-read),
    # never guessed — same "self-hash recipe" discipline as exec1.
    post_rows = {}
    for fname in MEMORY_FILES:
        fpath = MEMORIES_DIR / fname
        if fpath.exists():
            post_rows[fname] = {r.get("id"): r for r in
                                json.loads(fpath.read_text(encoding="utf-8"))}
    tagged_items = []
    for fname, idx, pre_item, hits in tag_set:
        post_item = post_rows.get(fname, {}).get(pre_item.get("id"), {})
        tagged_items.append({
            "path": f"memories/{fname}",
            "id": pre_item.get("id"),
            "index": idx,
            "created": pre_item.get("created_at"),
            "selectors": hits,
            "pre_item_sha1": item_sha1(pre_item),
            "post_item_sha1": item_sha1(post_item) if post_item else None,
        })
    record = {
        "run_id": "p2-selftest-20260925-v2-tag-m2",
        "executed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "executed_by": "coder (v2 tag script, M2 exec window)",
        "recompute_rule": {
            "window_local": WINDOW_START_LOCAL,
            "window_utc": WINDOW_START_UTC,
            "tz_note": "host MSL UTC+8; item created_at is naive-UTC ISO",
            "selectors": ["room_marker (M3-shaped structural opener or META_MARKERS head)",
                           "warn_marker (content contains U+26A0)",
                           "metadata.failure_type present"],
            "exclusions": ["whitelist ids from exec1 manifest (id-anchored)",
                           "items already carrying metadata.self_test"],
        },
        "item_sha1_canonical_form": CANONICAL_SHA1,
        "tagged_items": tagged_items,
        "tagged_count": len(tagged_ids),
        "whitelist_intersection": sorted(overlap),
        "files": {
            f: {"pre_tag_sha1": pre_file_sha.get(f),
                "post_tag_sha1": post_file_sha.get(f)}
            for f in MEMORY_FILES if pre_file_sha.get(f)
        },
    }
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if isinstance(data, list):
        data.append(record)
    else:
        data = [data, record]
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"M2 tagging done: {len(tagged_ids)} items tagged; "
          f"second manifest run-record appended to {MANIFEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
