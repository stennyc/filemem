#!/usr/bin/env python3
"""S2 entry-hygiene one-off governance for memories/*.json (blueprint LTM-fix-epic.md S2b).

Identifies FOUR candidate classes of noise rows (deterministic, zero LLM,
pure stdlib — NO third-party SDK, NO hardcoded model names):

  1. tool-dump fingerprint      — the plugin's single-source-of-truth
                                  `_looks_like_tool_dump` predicate, copied
                                  verbatim below (same source of truth as
                                  the plugin ingress/egress gates; see the
                                  "single source" note under the copy).
  2. tool-result echo           — egress_noise.echo_patterns table from the
                                  decay config (default: the "File unchanged"
                                  class), length-gated by echo_max_chars.
  3. oversized structural row   — single decay key egress_noise.governance
                                  .max_chars: len(content) > N AND first
                                  char in egress_noise.structural_opens.
  4. complaint class (fact_91)  — CONSERVATIVE: a row whose @image refs are
                                  all DANGLING (path missing) AND whose text
                                  (image lines stripped) ends with a question
                                  terminator (?/？) or a complaint marker from
                                  the egress_noise.complaint_markers table.
                                  The script only LISTS these candidates —
                                  it never judges. Default action = salience
                                  downgrade to egress_noise.governance
                                  .complaint_salience (rewrite-of-conclusions
                                  is a human job, done outside this script).

Actions (--yes lifts the dry-run default; dry-run writes NOTHING):
  * classes 1-3: row removed from memories/<type>.json and its FULL original
    dict appended (append-only) to
    knowledge/archive/governance_<ts>.jsonl with governance_reason +
    governed_ts (audit chain; recoverable).
  * class 4: salience downgraded in place (original row recorded in the same
    audit file, governance_reason=downgrade, governed_ts); --remove-
    complaint additionally archives+removes it (off by default).

Idempotent: after a governance run the noise candidates are archived out of
the pool and the downgraded complaint row carries the metadata stamp
`governance_downgraded`, so a re-run lists 0 candidates and writes no audit
file (0 candidates -> no new archive file, matching the raw_pending
C-slice fail-soft posture).

Locking: same protocol as the plugin's _shard_lock — an exclusive
fcntl.flock on <file>.lock (the sibling file, O_CREAT|O_RDWR, 0o644;
flock(LOCK_EX) around the read/rewrite, LOCK_UN + close at the end;
Windows msvcrt mirror available; exotic hosts degrade to a loud no-op,
exactly as the plugin documents).

Config: decay.json from the plugin dir (locate via --plugin-dir, default
= <repo>/../decay.json relative to this file; overridable per-run with
--decay PATH). Every fingerprint/length/salience number comes from that
config — 改表不改码, this tool carries zero hardcoded fingerprints.

Single source note: `_looks_like_tool_dump` is COPIED verbatim from the
plugin root __init__.py (module-level function). The import was preferred
in the blueprint but the plugin root __init__ executes host-hook setup on
import (agent.memory_provider, provider registry) — not safe for a
standalone stdlib tool. The copy is the ONLY second copy in the repo;
keep it byte-identical with the plugin (the plugin test
tests/test_s2_egress_governance.py asserts that) so the two faces can't
drift silently.
"""
import argparse
import json
import os
import re
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

# ── SINGLE SOURCE (copy): plugin root __init__.py `_looks_like_tool_dump`.
# COPIED VERBATIM from the plugin (see module docstring "single source
# note"); byte-identity is asserted by tests/test_s2_egress_governance.py
# so ingress/egress/governance can never drift apart.
def _looks_like_tool_dump(content: str) -> bool:
    """Single source of truth for tool-dump fingerprint."""
    s = (content or "").strip()
    return (s.startswith('{"output":') or s.startswith('{"content":')
            or s.startswith('{"error":'))
# ── end single-source copy ──────────────────────────────────────────────────

MEMORY_TYPES = ["preference", "fact", "decision", "task"]
IMAGE_REF_RE = re.compile(r"@image:(\S+)")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def load_decay_cfg(path: Path) -> Dict[str, Any]:
    """Load the decay config (file, or defaults baked in for a missing
    file — fail-soft, same posture as the plugin's load_decay_config).
    Only the egress_noise block is consumed by this tool."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    en = data.get("egress_noise") or {}
    gov = en.get("governance") or {}
    return {
        "tool_dump_openers": en.get("tool_dump_openers") or {},
        "echo_patterns": en.get("echo_patterns") or {},
        "echo_max_chars": int(en.get("echo_max_chars", 0) or 0),
        "structural_opens": en.get("structural_opens") or "",
        "complaint_markers": en.get("complaint_markers") or {},
        "gov_max_chars": int(gov.get("max_chars", 0) or 0),
        "gov_salience": gov.get("complaint_salience"),
    }


def _row_len(row: Dict[str, Any]) -> int:
    return len(str(row.get("content") or ""))


def classify_row(row: Dict[str, Any], cfg: Dict[str, Any],
                 path_root: Path) -> Optional[str]:
    """Return the FIRST matching governance class for a row, or None.

    Priority: tool-dump (fingerprint OR config opener table) > echo >
    oversized-structural > complaint. The complaint class is the
    conservative one (listed, not judged)."""
    content = str(row.get("content") or "")
    s = content.strip()

    # ① tool-dump: the single-source fingerprint predicate, plus the
    #    config-driven OPENER table (egress_noise.tool_dump_openers, the
    #    'Fetch finished …' task_1 class that misses the brace/JSON class —
    #    same wiring the egress cut uses, so the two faces agree)
    if _looks_like_tool_dump(s):
        return "tool_dump"
    for ops in (cfg["tool_dump_openers"] or {}).values():
        for op in (ops or []):
            if op and s.startswith(op):
                return "tool_dump"

    # ② echo fingerprints (config table, length-gated)
    max_echo = cfg["echo_max_chars"]
    if max_echo and len(s) <= max_echo:
        for pats in (cfg["echo_patterns"] or {}).values():
            for pat in (pats or []):
                if pat and pat in s:
                    return "echo"

    # ③ oversized structural row (single decay key, shared with egress ② rule)
    n = cfg["gov_max_chars"]
    opens = cfg["structural_opens"]
    if n and opens and s[:1] in opens and len(s) > n:
        return "oversized_structural"

    # ④ complaint class — dangling @image + question/complaint ending.
    # Conservative: only LISTED; the human decides (downgrade or rewrite).
    # Idempotency: a row already governance-stamped (metadata
    # governance_downgraded, written by the apply path) is NOT re-listed —
    # a rerun reports 0 candidates (spec acceptance: 幂等重跑 0 候选).
    refs = IMAGE_REF_RE.findall(content)
    if not refs:
        return None
    if all((path_root / ref).exists() for ref in refs):
        return None  # images resolve -> ordinary row, not a dangling-ref candidate
    if (row.get("metadata") or {}).get("governance_downgraded"):
        return None  # already governed in a prior pass; stable re-run
    text_no_imgs = IMAGE_REF_RE.sub("", content).strip()
    if not text_no_imgs:
        return None
    low = text_no_imgs[-1]
    if low in ("?", "？"):
        return "complaint_dangling_ref"
    for marks in (cfg["complaint_markers"] or {}).values():
        for m in (marks or []):
            if m and m in text_no_imgs:
                return "complaint_dangling_ref"
    return None


def scan(store_dir: Path, cfg: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Scan memories/*.json; return {type: [rows]} grouped by class via the
    row's 'id' marker field _governance_class (set on each candidate)."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for t in MEMORY_TYPES:
        f = store_dir / "memories" / f"{t}.json"
        if not f.exists():
            continue
        with open(f, "r", encoding="utf-8") as fh:
            rows = json.load(fh)
        kept: List[Dict[str, Any]] = []
        for r in rows:
            cls = classify_row(r, cfg, store_dir)
            if cls:
                rr = dict(r)
                rr["_governance_class"] = cls
                kept.append(rr)
        if kept:
            out[t] = kept
    return out


def _flock(path: Path, fn):
    """Exclusive flock on <path>.lock (same protocol as plugin _shard_lock:
    sibling .lock file, LOCK_EX; Windows mirror; exotic host = loud no-op)."""
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
            f"govern_memories: WARNING: no cross-process lock primitive "
            f"(fcntl/msvcrt) for {lock_path.name} — running unlocked\n")
        return fn()


def govern(store_dir: Path, cfg: Dict[str, Any], *, dry_run: bool = True,
           downgrade: bool = True, remove_complaint: bool = False,
           salience: Optional[float] = None,
           yes: bool = False) -> Dict[str, Any]:
    """One governance pass over memories/*.json.

    dry_run (default): list candidates, write NOTHING.
    yes=True with dry_run=False: apply — classes tool_dump/echo/
    oversized_structural are archived (full original row + governance_reason
    + governed_ts into knowledge/archive/governance_<ts>.jsonl, append-only)
    and removed from the active file; class complaint is salience-DOWNGRADED
    in place to the decay key (or `salience`) unless remove_complaint, in
    which case it is archived+removed like the others.
    """
    if dry_run:
        yes = False
    if not yes:
        # a non-yes run is ALWAYS a dry-run (the --yes flag lifts it)
        dry_run = True

    candidates = scan(store_dir, cfg)
    n_cand = sum(len(v) for v in candidates.values())
    result: Dict[str, Any] = {
        "dry_run": dry_run,
        "candidates": {
            t: [
                {"id": r.get("id"), "class": r["_governance_class"],
                 "content_chars": _row_len(r)}
                for r in rows
            ]
            for t, rows in candidates.items()
        },
        "total_candidates": n_cand,
        "applied": 0,
        "archive_file": None,
    }

    if n_cand == 0:
        return result  # idempotent re-run: nothing to do, no audit file

    sal = cfg["gov_salience"]
    if salience is not None:
        sal = salience
    governed_ts = _utcnow_iso()
    archive_dir = store_dir / "knowledge" / "archive"

    def _apply() -> None:
        if dry_run:
            # 只列候选不动盘 (list candidates, touch nothing): no lock, no
            # rewrite, no archive file — classification was decided at scan
            # time above; this path only reports (zero disk side effects,
            # not even .lock siblings).
            return
        archive_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _ap = archive_dir / f"governance_{ts}.jsonl"
        for mem_type, rows in candidates.items():
            mem_file = store_dir / "memories" / f"{mem_type}.json"
            # class decided at SCAN time; the in-lock rewrite must not
            # re-decide (a concurrent writer between scan and RMW stays
            # out of this pass's view; the lock keeps the rewrite atomic)
            class_by_id = {r.get("id"): r["_governance_class"] for r in rows}

            def _rmw() -> None:
                with open(mem_file, "r", encoding="utf-8") as fh:
                    all_rows = json.load(fh)
                new_rows: List[Dict[str, Any]] = []
                for r in all_rows:
                    cls = class_by_id.get(r.get("id"))
                    if cls is None:
                        new_rows.append(r)
                        continue
                    if cls == "complaint_dangling_ref" and not remove_complaint:
                        # conservative default: salience DOWNGRADE in place
                        # (the script never rewrites conclusions — a manual
                        # rewrite is a human job outside this tool)
                        if downgrade:
                            r["salience"] = sal
                            r["updated_at"] = governed_ts
                        # idempotency stamp: a rerun's scan() skips rows that
                        # already carry this metadata flag (0 candidates on
                        # re-run, spec acceptance), without rewriting the
                        # row's content or judging it
                        r.setdefault("metadata", {})["governance_downgraded"] = True
                        new_rows.append(r)
                        audit = dict(r)
                        audit["governance_reason"] = (
                            "complaint class: dangling @image ref "
                            "(conservative; downgraded, not judged)")
                        audit["governance_class"] = cls
                        audit["governance_action"] = "downgrade_salience"
                        audit["governed_ts"] = governed_ts
                        audit["governance_origin"] = mem_type
                        with open(_ap, "a", encoding="utf-8") as af:
                            af.write(json.dumps(audit, ensure_ascii=False) + "\n")
                        continue
                    # classes 1-3 (and complaint w/ --remove-complaint):
                    # archive the FULL original row, then remove
                    reason = {
                        "tool_dump": "tool-dump fingerprint",
                        "echo": "tool-result echo fingerprint",
                        "oversized_structural": "oversized structural row",
                        "complaint_dangling_ref":
                            "complaint class: dangling @image ref "
                            "(conservative list; not auto-judged; "
                            "--remove-complaint applied)",
                    }[cls]
                    audit = dict(r)
                    audit["governance_reason"] = reason
                    audit["governance_class"] = cls
                    audit["governance_action"] = "archive_remove"
                    audit["governed_ts"] = governed_ts
                    audit["governance_origin"] = mem_type
                    with open(_ap, "a", encoding="utf-8") as af:
                        af.write(json.dumps(audit, ensure_ascii=False) + "\n")
                    # removed from the active file: do NOT append
                _tmp = mem_file.with_suffix(".tmp")
                with open(_tmp, "w", encoding="utf-8") as fh:
                    json.dump(new_rows, fh, ensure_ascii=False, indent=2)
                os.replace(_tmp, mem_file)

            _flock(mem_file, _rmw)
        result["applied"] = n_cand
        result["archive_file"] = str(_ap)

    _apply()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="One-off S2 entry-hygiene governance for memories/*.json "
                    "(dry-run by default; --yes applies)")
    ap.add_argument("--store-dir", required=True,
                    help="the filemem store root (contains memories/ + knowledge/)")
    ap.add_argument("--decay", default=str(PLUGIN_DIR_DEFAULT / "decay.json"),
                    help="decay config path (default: plugin decay.json)")
    ap.add_argument("--plugin-dir", default=str(PLUGIN_DIR_DEFAULT),
                    help="plugin root used to locate decay.json (default)")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="list candidates, write nothing (DEFAULT)")
    ap.add_argument("--yes", action="store_true",
                    help="apply: archive+remove noise rows, downgrade complaints")
    ap.add_argument("--downgrade", action="store_true", default=True,
                    help="downgrade complaint-class salience (DEFAULT for class 4)")
    ap.add_argument("--remove-complaint", action="store_true",
                    help="ALSO archive+remove complaint-class rows (off by default: "
                        "the human decides — downgrade is the conservative default)")
    ap.add_argument("--salience", type=float, default=None,
                    help="override the decay key egress_noise.governance.complaint_salience")
    args = ap.parse_args()

    store_dir = Path(args.store_dir)
    cfg = load_decay_cfg(Path(args.decay))
    res = govern(
        store_dir, cfg,
        dry_run=not args.yes,
        downgrade=args.downgrade,
        remove_complaint=args.remove_complaint,
        salience=args.salience,
        yes=args.yes,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
