#!/usr/bin/env python3
"""P4: one-off 存量 structural sweep (owner 2026-10-15 源头过滤, data-face).

Companion to the P4 code change (ingress structural leg (b2) + egress
structural leg ⑥ in the plugin root __init__.py): the LIVE gates stop
NEW structural garbage (JSON-dump / room-skeleton / kanban-relay rows)
from ever landing; THIS sweep cleans the historical 存量 that already
landed in memories/*.json before the gates went live (the 2026-10-15
owner census: 71 live room-skeleton rows + ~30 multi-key JSON dumps +
41 kanban-relay rows, all L0, many confirmed=True, all re-injecting
on the auto-inject face).

Two candidate classes, one predicate each (deterministic, zero LLM):

  1. structural_opener — content.lstrip()[:1] in '{['
     (the SAME primitive the P4 ingress (b2) leg + egress ⑥ leg use:
     _NONPROSE_OPENERS. A JSON-dump / [Group chat …] skeleton / system
     scaffolding row — M1 doctrine "never a durable user fact". This
     leg is a CODE DEFAULT, not a config table: it runs even when the
     decay block is missing (the sweep face is deliberately NARROWER
     than the live gate: only the structural class + the kanban table
     class; short prose fragments in the pool stay, left to the panel
     / human review, same posture as sweep_low_value.py).

  2. kanban_relay — content starts with any of the kanban relay
     prefixes in egress_noise.tool_dump_openers (`⏸ [`, `✔ [`, `⏱ [`).
     The shared P0-3 table (改表不改码): the sweep reads the SAME
     decay.json the live faces read, so if the owner extends the table
     the sweep follows without a code change.

Apply pass (per memories/*.json, the sweep_low_value.py protocol):
  1. ARCHIVE first — candidate rows move into an append-only JSONL
     (knowledge/archive/structural_sweep_<ts>.jsonl, one line per row
     carrying the FULL original row + class + mem_type + sweep ts —
     "忘掉≠抹掉", the P1-1 archive precedent, recoverable);
  2. AUDIT — one consolidation_log.jsonl row per swept row
     (action=structural_sweep, source=sweep, class, row_id, the
     original content's first 64 chars, source_file, archived_to);
  3. REWRITE the memories file with only the surviving rows — all
     under the memory file's sibling-flock protocol (the same exclusive
     flock as sweep_low_value / govern_memories / the plugin's
     _shard_lock: one writer at a time, no torn JSON).

Idempotent: after an apply run a second run finds 0 candidates (the
rows are archived, not re-judged in place) and writes nothing
(0 candidates -> no new trace file, the raw_pending C-slice
fail-soft posture). --dry-run is the DEFAULT: it reports per-file +
total counts and writes NOTHING.

Config: decay.json from the plugin dir (locate via --plugin-dir,
default = <plugin>/../decay.json relative to this file; overridable
per-run with --decay PATH). The kanban relay class reads the live
table; the structural class is code (no config dependency).
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

ARCHIVE_SWEEP_PREFIX = "structural_sweep_"

# the P4 structural primitive — SAME frozenset the plugin root
# _egress_noise_cut leg ⑥ + ingress_reject_class leg (b2) use
# (_NONPROSE_OPENERS). Inlined here (the plugin root executes host-hook
# setup on import — not safe for a standalone stdlib tool; the same
# precedent as tools/govern_memories.py / sweep_low_value.py copies).
_NONPROSE_OPENERS = frozenset("{[")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _flock(path: Path, fn):
    """Exclusive flock on <path>.lock (same protocol as the plugin's
    _shard_lock: sibling .lock file, O_CREAT|O_RDWR 0o644, LOCK_EX;
    Windows msvcrt mirror; exotic host = loud no-op, exactly as the
    plugin documents). The sweep_low_value.py proven shape, reused
    verbatim."""
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
        fh = open(lock_path, "a+b")
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
            "sweep_structural_noise: WARNING: no cross-process lock "
            f"primitive (fcntl/msvcrt) for {lock_path.name} — "
            "running unlocked\n")
        return fn()


def _load_decay(decay_path: Optional[str]) -> Dict[str, Any]:
    """Load the plugin's decay.json (the live egress-noise table source).
    A missing/corrupt file degrades to an EMPTY block — the kanban relay
    class then simply finds no table entries (the structural class is
    code-default and unaffected; the sweep still runs, fail-soft)."""
    if decay_path:
        p = Path(decay_path)
    else:
        p = PLUGIN_DIR_DEFAULT / "decay.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[warn] decay config unreadable ({e}); "
              f"kanban-relay class runs with an empty table "
              f"(structural class unaffected — code default)")
        return {}


def _kanban_prefixes(decay: Dict[str, Any]) -> List[str]:
    """The kanban relay startswith anchors — the LIVE shared table
    (egress_noise.tool_dump_openers), so 改表不改码: extending the
    table in decay.json extends the sweep automatically."""
    en = ((decay.get("egress_noise") or {}).get("tool_dump_openers")
          or {}).get("en") or []
    return [str(p) for p in en if isinstance(p, str) and p]


def classify_row(content: str, prefixes: List[str]) -> Optional[str]:
    """The P4 sweep verdict for one row's content: 'structural_opener',
    'kanban_relay', or None (keep). Pure rule + stdlib, zero LLM; never
    raises on any input (a malformed row is simply kept)."""
    s = (content or "").strip()
    if not s:
        return None
    if s.lstrip()[:1] in _NONPROSE_OPENERS:
        return "structural_opener"
    for p in prefixes:
        if s.startswith(p):
            return "kanban_relay"
    return None


def sweep_memories(store: Path, decay: Dict[str, Any], *,
                   dry_run: bool, ts: str,
                   memory_types: Optional[List[str]] = None) -> Dict[str, Any]:
    """The memories/*.json face (same protocol as sweep_low_value):
    candidate rows are archived to knowledge/archive/
    structural_sweep_<ts>.jsonl, one consolidation_log.jsonl audit row
    per row is appended, then the file is rewritten with survivors —
    all under the memory file's sibling-flock. dry_run: count only,
    write NOTHING."""
    mem_types = memory_types or ["fact", "preference", "decision", "task",
                                 "plan"]
    prefixes = _kanban_prefixes(decay)
    arch_dir = store / "knowledge" / "archive"
    arch_path = arch_dir / f"{ARCHIVE_SWEEP_PREFIX}{ts}.jsonl"
    log_path = store / "consolidation_log.jsonl"
    stats: Dict[str, Any] = {
        "files": 0, "rows_swept": 0, "rows_kept": 0,
        "by_class": {"structural_opener": 0, "kanban_relay": 0},
    }
    for mem_type in mem_types:
        mf = store / "memories" / f"{mem_type}.json"
        if not mf.exists():
            continue
        try:
            rows = json.loads(mf.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except Exception as e:
            print(f"[warn] {mf.name}: unreadable ({e}); skipped (the pool "
                  f"file self-heals on the plugin's next RMW)")
            continue
        verdicts = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            verdicts[id(r)] = classify_row(str(r.get("content") or ""),
                                            prefixes)
        swept = [r for r in rows if isinstance(r, dict)
                  and verdicts.get(id(r))]
        kept = [r for r in rows if isinstance(r, dict)
                and not verdicts.get(id(r))]
        if not swept:
            stats["files"] += 1
            stats["rows_kept"] += len(rows)
            continue
        stats["files"] += 1
        stats["rows_swept"] += len(swept)
        stats["rows_kept"] += len(kept)
        for r in swept:
            stats["by_class"][verdicts[id(r)]] += 1
        if dry_run:
            for r in swept:
                c = (r.get("content") or "").strip()
                print(f"  [dry] {mem_type}/{r.get('id')}: "
                      f"{verdicts[id(r)]} — {c[:60]}")
            continue
        # 1. archive the swept rows FIRST (append-only, P1-1 precedent).
        arch_dir.mkdir(parents=True, exist_ok=True)

        def _apply(target=mf, survivors=kept, swept_rows=swept,
                   mt=mem_type, verdicts_map=verdicts):
            with open(arch_path, "a", encoding="utf-8") as af:
                for r in swept_rows:
                    af.write(json.dumps({
                        "row": r,
                        "mem_type": mt,
                        "class": verdicts_map[id(r)],
                        "sweep_ts": _utcnow_iso(),
                    }, ensure_ascii=False, sort_keys=True) + "\n")
            with open(log_path, "a", encoding="utf-8") as lf:
                for r in swept_rows:
                    lf.write(json.dumps({
                        "ts": _utcnow_iso(),
                        "action": "structural_sweep",
                        "source": "sweep",
                        "class": verdicts_map[id(r)],
                        "mem_type": mt,
                        "row_id": r.get("id"),
                        "content_preview":
                            str(r.get("content") or "")[:64],
                        "source_file": target.name,
                        "archived_to": arch_path.name,
                    }, ensure_ascii=False, sort_keys=True) + "\n")
            with open(target, "w", encoding="utf-8") as f:
                json.dump(survivors, f, ensure_ascii=False, indent=2)

        _flock(mf, _apply)
        print(f"  {mem_type}: swept {len(swept)} -> {arch_path.name}, "
              f"kept {len(kept)}")
    return stats


def sweep(store: Path, decay: Dict[str, Any], *,
          dry_run: bool = True, yes: bool = False) -> Dict[str, Any]:
    """One sweep pass. dry_run (the DEFAULT) reports candidate counts and
    writes NOTHING; yes=True lifts it to an apply pass. Idempotent: a
    re-run after an apply finds 0 candidates and writes nothing."""
    if yes:
        dry_run = False
    else:
        dry_run = True
    ts = _ts_tag()
    memories = sweep_memories(store, decay, dry_run=dry_run, ts=ts)
    return {
        "dry_run": dry_run,
        "ts_tag": ts,
        "memories": memories,
        "rows_swept": memories["rows_swept"],
    }


def _default_store() -> Path:
    """The shared neutral store root (<hermes home>/filemem — the T5b
    'auto' root), resolved without hardcoding a host path: HERMES_HOME
    env when set, else ~/.hermes."""
    from dep_bootstrap import _resolve_hermes_home
    return Path(_resolve_hermes_home()) / "filemem"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store-root", default=None,
                    help="the filemem store root (default: "
                         "$HERMES_HOME/filemem or ~/.hermes/filemem)")
    ap.add_argument("--decay", default=None,
                    help="override the decay.json path (default: the "
                         "plugin's shipped decay.json)")
    ap.add_argument("--yes", action="store_true",
                    help="APPLY the sweep (default is dry-run)")
    args = ap.parse_args()

    store = Path(args.store_root) if args.store_root else _default_store()
    if not (store / "memories").is_dir():
        print(f"error: {store / 'memories'} is not a directory")
        return 2
    decay = _load_decay(args.decay)
    res = sweep(store, decay, yes=args.yes)
    m = res["memories"]
    mode = "APPLY" if res["rows_swept"] and not res["dry_run"] else \
        ("DRY-RUN" if res["dry_run"] else "APPLY (0 candidates)")
    print(f"\n[{mode}] store={store}")
    print(f"  files scanned : {m['files']}")
    print(f"  rows swept    : {m['rows_swept']} "
          f"(structural_opener={m['by_class']['structural_opener']}, "
          f"kanban_relay={m['by_class']['kanban_relay']})")
    print(f"  rows kept     : {m['rows_kept']}")
    if res["dry_run"] and res["rows_swept"]:
        print(f"  re-run with --yes to apply (idempotent; trace jsonl + "
              f"audit rows + rewrite under the sibling-flock protocol)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
