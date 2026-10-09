#!/usr/bin/env python3
"""P3-1: one-off historical 存量 sweep for low-value draft / L0-auto memory
rows (deterministic, zero LLM, idempotent, dry-run default).

The data-face companion of the P3-1 pre-queue no-popup gate
(`_no_popup_silent_discard` in the plugin root __init__.py): the gate stops
NEW low-value rows from ever reaching the user-visible [记忆报告] /
[未决草稿] faces; this sweep cleans the historical 存量 that already
landed before the gate went live (run by @devops on the RESTARTED gateway
so the judgment runs on the new code's semantics):

  1. drafts_*.json across the store root (活根, e.g. <HERMES_HOME or ~/.hermes>/filemem):
     row-level conservative judgment (the same 3-leg formula as the live
     gate). A file whose rows are ALL low-value moves whole into
     `drafts_discarded_sweep_<ts>.jsonl` (留痕非删 — every original row is
     appended as one JSONL line, then the .json is unlinked); a MIXED file
     has its low-value rows appended to the same trace jsonl and the file
     is REWRITTEN with only the surviving high-value rows (under the
     sibling-flock protocol, same as the plugin's _shard_lock).
  2. memories/*.json: the OLD L0 auto rows (level=L0 AND source=auto AND an
     egress-noise-table hit — the sweep face is deliberately NARROWER than
     the live gate: short fragments in the pool stay, left to the panel /
     human review) move into knowledge/archive/ (reuse of the P1-1
     archive precedent: append-only JSONL, recoverable) + one
     consolidation_log.jsonl audit row per row (action=no_popup_sweep,
     source=sweep, carrying the ORIGINAL content's first 64 chars).
  3. High-value rows (the ones no leg can judge) are NEVER touched —
     they stay for the 弹窗 / human review.

--dry-run is the DEFAULT: it reports per-file + total judgment counts and
writes NOTHING (no trace file, no archive, no audit row, no unlink).
--yes lifts it. Idempotent: after an apply run, a second run finds 0
candidates and writes nothing (0 candidates -> no new trace file, the
raw_pending C-slice fail-soft posture).

Single source: `_noise_table_hit` and `_no_popup_silent_discard` are
COPIED VERBATIM from the plugin root __init__.py (the plugin root executes
host-hook setup on import — `from agent.memory_provider import ...` —
not safe for a standalone stdlib tool; the same precedent as
tools/govern_memories.py's _looks_like_tool_dump copy).
tests/test_no_popup_gate.py asserts AST source-segment identity for both
copies so the sweep face and the live gate can NEVER drift apart.

Locking: same protocol as the plugin's _shard_lock / govern_memories —
exclusive fcntl.flock on the sibling <file>.lock (O_CREAT|O_RDWR, 0o644;
flock(LOCK_EX) around the read/rewrite, LOCK_UN + close at the end;
Windows msvcrt mirror available; exotic hosts degrade to a loud no-op).

Config: decay.json from the plugin dir (locate via --plugin-dir, default
= <plugin>/../decay.json relative to this file; overridable per-run with
--decay PATH). Every fingerprint/length/salience number comes from that
config — 改表不改码, this tool carries zero hardcoded fingerprints.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── cross-process lock (same protocol as the plugin's _shard_lock) ─────────
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - Linux
    msvcrt = None

# ── default decay config location (plugin dir next to this tools/ dir) ─────
PLUGIN_DIR_DEFAULT = Path(__file__).resolve().parents[1]

# ── SINGLE SOURCE (copies): plugin root __init__.py `_noise_table_hit` +
# `_no_popup_silent_discard`. COPIED VERBATIM from the plugin (see module
# docstring "single source" note); AST byte-identity of both copies is
# asserted by tests/test_no_popup_gate.py so the sweep face and the live
# gate can't drift silently.
def _noise_table_hit(s: str, decay_cfg: Optional[Dict[str, Any]]) -> bool:
    """P0-3: the shared egress-noise OPENER tables — one table, wired into
    BOTH faces so they can never drift apart (写侧 ingress 切同表):

      - `egress_noise.tool_dump_openers` (leg ③ of _egress_noise_cut)
      - `egress_noise.fragment_prefixes` (leg ⑤, P0-3 new)

    Both are STARTSWITH semantics (a row is hit only when it OPENS the
    anchor — substring matching is deliberately NOT done, 零误杀: a normal
    fact that merely contains the anchor mid-text is never caught). The
    write-side ingress gate uses this helper in its raw_tool_output leg,
    OR-ed with the always-live 3-key code default
    (`_looks_like_tool_dump` + the `<untrusted_tool_result` prefix —
    superset gate: every P0-1 reject stays a reject, zero regression).

    Fail-soft: a missing/malformed block, or a malformed leg shape
    (non-dict table, non-list language row, non-str entry), degrades to
    "no hit" for that leg only — it never raises, never takes down the
    sibling leg. decay_cfg=None behaves exactly like the P0-1 code-only
    gate (no tables consulted). Zero LLM, pure stdlib startswith/len."""
    try:
        en = (decay_cfg or {}).get("egress_noise")
        if not isinstance(en, dict):
            return False
        for table in ("tool_dump_openers", "fragment_prefixes"):
            block = en.get(table)
            if not isinstance(block, dict):
                continue  # malformed leg shape -> fail-soft off for this leg
            for pats in block.values():
                if not isinstance(pats, (list, tuple)):
                    continue
                for pat in pats:
                    if pat and s.startswith(str(pat)):
                        return True
    except Exception:
        return False
    return False


def _no_popup_silent_discard(content: str, decay_cfg: Optional[Dict[str, Any]],
                             salience: Optional[float] = None) -> bool:
    """P3-1: the pre-queue conservative low-value DROP decision (1A 保守档,
    钉死, 勿扩大). A row is judged "low-value, drop directly (never let it
    enter the user-visible pending-confirm queue)" if and only if it hits ANY
    ONE of the three config-driven legs:

      1. **egress_noise OPENER table hit** — reuses the P0-3 shared
         `_noise_table_hit` (tool_dump_openers + fragment_prefixes, all
         config-driven). This is the SAME table the egress cut and the
         write-side ingress gate read, so the drop face can never drift apart.
      2. **too short** — `len(normalized content) < no_popup.min_chars`
         (default 40; a new config knob, not a hardcoded constant).
      3. **low salience** — an EXPLICIT `salience` value `< no_popup.
         salience_floor` (default 0.5). A MISSING salience (L0-auto / draft
         rows default to 0.5, i.e. AT the floor) is NOT a hit — the
         conservative posture deliberately does not mis-kill on salience;
         the noise table + short fragment are the main legs.

    No leg hit = high-value → the caller queues the row exactly as today
    (behavior unchanged). 改表不改码: every number comes from the `no_popup`
    decay block — `enable=false` (or a missing/malformed block) turns the gate
    OFF and every row is queued (full legacy rollback). Fail-soft: an
    exception inside the judgment degrades to "not low-value" (queue — 宁多弹
    不可崩, a crashed gate must never take down the host turn). Zero LLM,
    pure stdlib (the P0-3 startswith table + len + float)."""
    try:
        en = (decay_cfg or {}).get("no_popup")
        if not isinstance(en, dict):
            return False  # missing block -> gate off (exact legacy behavior)
        if not en.get("enable", True):
            return False  # enable=false -> fully roll back to legacy queueing
        s = (content or "").strip()
        # leg ①: the shared P0-3 egress-noise OPENER tables (config-driven).
        if _noise_table_hit(s, decay_cfg):
            return True
        # leg ②: too short (normalized length below the config floor).
        try:
            min_chars = int(en.get("min_chars", 40))
        except (TypeError, ValueError):
            min_chars = 40
        if min_chars > 0 and len(s) < min_chars:
            return True
        # leg ③: an EXPLICIT low salience below the config floor (a missing
        # salience is NOT a hit — no salience mis-kill by default).
        if salience is not None:
            try:
                floor = float(en.get("salience_floor", 0.5))
            except (TypeError, ValueError):
                floor = 0.5
            if salience < floor:
                return True
    except Exception:
        return False  # fail-soft: a judgment exception = not low-value (queue)
    return False
# ── end single-source copies ────────────────────────────────────────────────

MEMORY_TYPES = ["preference", "fact", "decision", "task"]
DRAFTS_GLOB = "drafts_*.json"
DISCARDED_TRACE_PREFIX = "drafts_discarded_sweep_"
ARCHIVE_SWEEP_PREFIX = "sweep_low_value_"


def _utcnow_iso() -> str:
    # naive-UTC ISO, the plugin's _utcnow() convention (one source of truth).
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _ts_tag() -> str:
    """The %Y%m%dT%H%M%SZ tag shared with the raw_pending C-slice archive
    filenames (the P1-1 precedent) — deterministic per run, never per row."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_decay_cfg(path: Path) -> Dict[str, Any]:
    """Load the decay config (file, or {} baked in for a missing file —
    fail-soft, same posture as the plugin's load_decay_config). The full
    merged block is consumed: the judgment reads no_popup.* while its leg ①
    reads egress_noise.* (the shared P0-3 tables)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _row_salience(row: Dict[str, Any]) -> Optional[float]:
    """The row's EXPLICIT salience, or None (absent/undecodable = NOT a
    salience hit — the conservative posture, same as the live gate)."""
    sal = row.get("salience")
    try:
        return float(sal)
    except (TypeError, ValueError):
        return None


def judge_draft_row(row: Dict[str, Any], decay: Dict[str, Any]) -> bool:
    """Row-level conservative judgment (the FULL 3-leg formula) over one
    drafts_*.json row (a MemoryItem.to_dict shape: content + salience)."""
    content = str(row.get("content") or "")
    return _no_popup_silent_discard(
        content, decay, salience=_row_salience(row))


def judge_l0_auto_row(row: Dict[str, Any], decay: Dict[str, Any]) -> bool:
    """The NARROW sweep face over memories/*.json: only OLD L0 auto rows
    (level=L0 AND source=auto) with an egress-noise-table hit are swept —
    the spec's 旧 L0 auto 行 class. Short fragments / sub-floor salience
    rows in the pool are NOT swept (left to the panel / human, 零误杀)."""
    if row.get("level") != "L0" or row.get("source") != "auto":
        return False
    return _noise_table_hit(str(row.get("content") or ""), decay)


def _flock(path: Path, fn):
    """Exclusive flock on <path>.lock (same protocol as the plugin's
    _shard_lock: sibling .lock file, O_CREAT|O_RDWR 0o644, LOCK_EX;
    Windows msvcrt mirror; exotic host = loud no-op, exactly as the
    plugin documents)."""
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
            "sweep_low_value: WARNING: no cross-process lock primitive "
            f"(fcntl/msvcrt) for {lock_path.name} — running unlocked\n")
        return fn()


def _read_rows(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def sweep_drafts(store: Path, decay: Dict[str, Any], *,
                 dry_run: bool, ts: str) -> Dict[str, Any]:
    """Face 1: the drafts_*.json 存量. ALL-low-value files move whole into
    the run's trace jsonl (留痕非删 — then the .json is unlinked); mixed
    files are rewritten with their surviving rows only (low-value rows
    appended to the trace jsonl). High-value rows are NEVER touched.
    dry_run: count only, write NOTHING."""
    trace_path = store / f"{DISCARDED_TRACE_PREFIX}{ts}.jsonl"
    stats: Dict[str, Any] = {"files": 0, "files_moved": 0,
                            "files_rewritten": 0, "rows_discarded": 0,
                            "rows_kept": 0}
    for df in sorted(store.glob(DRAFTS_GLOB)):
        if not df.is_file():
            continue
        rows = _read_rows(df)
        if not rows:
            continue  # an empty-shell drafts file: nothing to judge
        low = [r for r in rows if judge_draft_row(r, decay)]
        if not low:
            stats["files"] += 1
            stats["rows_kept"] += len(rows)
            continue  # a file with 0 low-value rows is never rewritten
        kept = [r for r in rows if r not in low]
        stats["files"] += 1
        stats["rows_discarded"] += len(low)
        stats["rows_kept"] += len(kept)
        # THIS file's trace lines only (留痕: the discarded rows, each with
        # its provenance source_file).
        trace_lines = [
            json.dumps({"source_file": df.name, "row": r},
                       ensure_ascii=False, sort_keys=True)
            for r in low
        ]
        if dry_run:
            continue
        if kept:
            # MIXED file: append this file's low rows to the trace, rewrite
            # the original with only the survivors (sibling flock protocol).
            with open(trace_path, "a", encoding="utf-8") as tf:
                for line in trace_lines:
                    tf.write(line + "\n")

            def _rewrite(target=df, survivors=kept):
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(survivors, f, ensure_ascii=False, indent=2)

            _flock(df, _rewrite)
            stats["files_rewritten"] += 1
        else:
            # ALL-low-value file: 整文件移入 trace jsonl (留痕非删) — each
            # original row lands as one JSONL line, then the .json is
            # unlinked (its content lives on in the trace, never deleted).
            with open(trace_path, "a", encoding="utf-8") as tf:
                for line in trace_lines:
                    tf.write(line + "\n")
            _flock(df, df.unlink)
            stats["files_moved"] += 1
    return stats


def sweep_memories(store: Path, decay: Dict[str, Any], *,
                   dry_run: bool, ts: str) -> Dict[str, Any]:
    """Face 2: the OLD L0 auto rows in memories/*.json (level=L0 +
    source=auto + noise-table hit). Apply: the rows move to
    knowledge/archive/sweep_low_value_<ts>.jsonl (P1-1 archive precedent —
    append-only, recoverable) and ONE consolidation_log.jsonl audit row
    per row is appended (action=no_popup_sweep, source=sweep, the original
    content's first 64 chars). dry_run: count only, write NOTHING."""
    arch_dir = store / "knowledge" / "archive"
    arch_path = arch_dir / f"{ARCHIVE_SWEEP_PREFIX}{ts}.jsonl"
    log_path = store / "consolidation_log.jsonl"
    stats: Dict[str, Any] = {"files": 0, "rows_swept": 0, "rows_kept": 0}
    for mem_type in MEMORY_TYPES:
        mf = store / "memories" / f"{mem_type}.json"
        if not mf.exists():
            continue
        rows = _read_rows(mf)
        low = [r for r in rows if judge_l0_auto_row(r, decay)]
        if not low:
            if rows:
                stats["files"] += 1
            stats["rows_kept"] += len(rows)
            continue
        kept = [r for r in rows if r not in low]
        stats["files"] += 1
        stats["rows_swept"] += len(low)
        stats["rows_kept"] += len(kept)
        if dry_run:
            continue
        # 1. archive the swept rows FIRST (append-only, the P1-1 precedent
        #    — "忘掉≠抹掉": the rows are recoverable from the archive).
        arch_dir.mkdir(parents=True, exist_ok=True)
        with open(arch_path, "a", encoding="utf-8") as af:
            for r in low:
                af.write(json.dumps({
                    "row": r,
                    "mem_type": mem_type,
                    "sweep_ts": _utcnow_iso(),
                }, ensure_ascii=False, sort_keys=True) + "\n")
        # 2. one audit row per swept row into the EXISTING
        #    consolidation_log.jsonl (source=sweep, content first 64 chars),
        #    then rewrite the memories file with only the surviving rows —
        #    both under the memory file's sibling-flock protocol.
        def _append_log(target=mf, survivors=kept, low_rows=low, mt=mem_type):
            with open(log_path, "a", encoding="utf-8") as lf:
                for r in low_rows:
                    lf.write(json.dumps({
                        "ts": _utcnow_iso(),
                        "action": "no_popup_sweep",
                        "source": "sweep",
                        "mem_type": mt,
                        "row_id": r.get("id"),
                        "content_preview": str(r.get("content") or "")[:64],
                        "source_file": target.name,
                        "archived_to": arch_path.name,
                    }, ensure_ascii=False, sort_keys=True) + "\n")
            with open(target, "w", encoding="utf-8") as f:
                json.dump(survivors, f, ensure_ascii=False, indent=2)
        _flock(mf, _append_log)
    return stats


def sweep(store: Path, decay: Dict[str, Any], *,
          dry_run: bool = True, yes: bool = False) -> Dict[str, Any]:
    """One sweep pass. dry_run (the DEFAULT) reports judgment counts and
    writes NOTHING; yes=True lifts it to an apply pass (a --yes run is
    ALWAYS an apply: the flag explicitly waives the dry-run default, the
    govern_memories --yes precedent). Idempotent: a re-run after an
    apply finds 0 candidates and writes nothing."""
    if yes:
        dry_run = False
    else:
        dry_run = True
    ts = _ts_tag()
    drafts = sweep_drafts(store, decay, dry_run=dry_run, ts=ts)
    memories = sweep_memories(store, decay, dry_run=dry_run, ts=ts)
    return {
        "dry_run": dry_run,
        "ts_tag": ts,
        "drafts": drafts,
        "memories": memories,
        "judged_low_value": (drafts["rows_discarded"]
                             + memories["rows_swept"]),
    }


def _default_store() -> Path:
    """The shared neutral store root (<hermes home>/filemem — the T5b 'auto'
    root), resolved without hardcoding a host path: HERMES_HOME env when
    set, else ~/.hermes."""
    home = os.environ.get("HERMES_HOME") or str(Path.home())
    return Path(home) / "filemem"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="P3-1 low-value 存量 sweep (deterministic, zero LLM, "
                    "idempotent; dry-run default, --yes applies).")
    ap.add_argument("--store", default=None,
                    help="the filemem store root (default: <HERMES_HOME or "
                         "~/.hermes>/filemem)")
    ap.add_argument("--decay", default=None,
                    help="explicit decay.json path (default: the plugin's)")
    ap.add_argument("--plugin-dir", default=str(PLUGIN_DIR_DEFAULT),
                    help="plugin dir holding decay.json (default next to "
                         "this tools/ dir)")
    ap.add_argument("--yes", action="store_true",
                    help="apply (default is dry-run: report, write NOTHING)")
    args = ap.parse_args(argv)

    store = Path(args.store) if args.store else _default_store()
    decay_path = (Path(args.decay) if args.decay
                  else Path(args.plugin_dir) / "decay.json")
    decay = load_decay_cfg(decay_path)

    result = sweep(store, decay, dry_run=True, yes=args.yes)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    if result["dry_run"]:
        print(f"sweep_low_value: DRY-RUN — {result['judged_low_value']} "
              f"low-value rows judged, NOTHING written "
              f"(store={store}, decay={decay_path}); re-run with --yes to "
              f"apply.")
    else:
        print(f"sweep_low_value: applied — {result['judged_low_value']} "
              f"low-value rows swept (store={store}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
