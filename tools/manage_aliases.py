#!/usr/bin/env python
"""EP-3 human alias-table face: deterministic alias management. ZERO LLM.

Companion to the LLM face (consolidation/llm_caller.suggest_aliases, host
main-model path): both land in the SAME {storage}/aliases.json table. This
face is the deterministic human verdict (人拍板) — no model call anywhere,
no third-party SDK import, no hardcoded model names or paths.

Usage:
  python3 tools/manage_aliases.py --list
      read-only: list every topic in aliases.json as
      `[topic] -> [a1, a2, a3, ...]`. Zero disk writes.
  python3 tools/manage_aliases.py --add <topic> <alias1> [alias2 ...]
      human-supplement a topic's aliases (deterministic union — old
      aliases are PRESERVED, never deleted; a fresh topic is created with
      source="human"). Audited:
      {"action":"alias_add_human","topic":...,"added":[...]}
  python3 tools/manage_aliases.py --remove <topic>
      delete one topic's aliases (the topic's entry is dropped from
      aliases.json; other topics untouched). Audited:
      {"action":"alias_remove_human","topic":...}
  python3 tools/manage_aliases.py --prune
      lazy cleanup: drop alias entries that have been recorded as 0-hit in
      the most recent `PRUNE_STREAK` consecutive `alias_search_hit` audit
      rows (the search face may append one row per query+topic hit-count;
      streak of 0 -> prune). Purely deterministic, zero LLM. Audited:
      {"action":"alias_prune","pruned":{...}}
  python3 tools/manage_aliases.py --add <topic> <a1> ... --store DIR
      point at an alternate live store (default: $HERMES_HOME/filemem,
      HERMES_HOME defaulting to ~/.hermes — no hardcoded paths).

Every write RMWs aliases.json under the cross-process _shard_lock (ONE
flock per call path — the same lock file must never be flock'd twice in
a single process, or the second LOCK_EX self-blocks) and appends one
audit line to <store>/consolidation_log.jsonl — the same append-only log
the consolidation LLM face writes (never rewrite history).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
from dep_bootstrap import _resolve_hermes_home  # noqa: E402

import __init__ as fm  # noqa: E402  (plugin module; import-time is I/O-free)

# Dynamic path resolution (mirrors confirm_supersede.py / build_graph.py /
# backfill_origin.py): HERMES_HOME env > ~/.hermes. No hardcoded paths.
HERMES_HOME = Path(_resolve_hermes_home())
LIVE_STORE = HERMES_HOME / "filemem"

# Lazy-prune streak: an alias recorded as 0-hit in this many CONSECUTIVE
# most-recent alias_search_hit rows is pruned (spec: 连续 3 次搜索命中 0 条).
PRUNE_STREAK = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _build_provider(store: Path) -> "fm.FileMemProvider":
    """Provider shell with ONLY path attrs assigned (like
    confirm_supersede._build_provider): __init__ does zero I/O, so no
    initialize() — no mkdir, no drafts, no decay write. _shard_lock needs
    nothing from self (fcntl/msvcrt + the lock path), so RMWs work on
    this shell."""
    p = fm.FileMemProvider()
    p.storage_dir = store
    p.memories_dir = store / "memories"
    return p


def _alias_file(p: "fm.FileMemProvider") -> Path:
    return p.storage_dir / "aliases.json"


def _audit(p: "fm.FileMemProvider", event: dict) -> None:
    """Append one audit row to the shared consolidation log (fail-soft: a
    log hiccup never blocks the human verdict — _shard_lock RMW inside)."""
    p._append_consolidation_event(event)


def _commit_data(p: "fm.FileMemProvider", data: dict) -> None:
    """Write the whole alias table. Caller MUST already hold
    p._shard_lock(_alias_file(p)) — the same lock file must never be
    flock'd twice in one process (the second LOCK_EX self-blocks)."""
    with open(_alias_file(p), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_data(p: "fm.FileMemProvider") -> dict:
    data = p._load_aliases()
    if not isinstance(data, dict):
        data = {}
    return data


def _topics_of(data: dict) -> dict:
    topics = data.get("topics")
    return topics if isinstance(topics, dict) else {}


def _cmd_list(p: "fm.FileMemProvider") -> int:
    """Read-only: list every topic. Zero disk writes (gate: the store file
    hashes must be byte-identical before/after — locked by the test)."""
    topics = _topics_of(_load_data(p))
    if not topics:
        print("(no alias topics yet)")
        return 0
    for topic, entry in sorted(topics.items()):
        aliases = (entry or {}).get("aliases") or [] if isinstance(entry, dict) else []
        print(f"[{topic}] -> [{', '.join(str(a) for a in aliases)}]")
    return 0


def _cmd_add(p: "fm.FileMemProvider", topic: str, aliases: list) -> int:
    """Human-supplement: deterministic union (never deletes old aliases).
    A fresh topic is stamped source="human"; an existing LLM topic keeps
    its human-added entries and is unioned (the LLM face unions too, so a
    human write is durable on both faces)."""
    topic = topic.strip()
    if not topic:
        print("error: empty topic", file=sys.stderr)
        return 2
    added = sorted({a.strip().lower() for a in aliases if a.strip()})
    if not added:
        print("error: no valid aliases supplied", file=sys.stderr)
        return 2
    with p._shard_lock(_alias_file(p)):
        data = _load_data(p)
        topics = _topics_of(data)
        entry = topics.get(topic) if isinstance(topics.get(topic), dict) else None
        if entry is None:
            entry = {"aliases": [], "source": "human", "ts": _now_iso()}
        prev = set(entry.get("aliases") or [])
        entry["aliases"] = sorted(prev | set(added))
        src = entry.get("source")
        entry["source"] = src + "+human" if src and src != "human" else "human"
        entry["ts"] = _now_iso()
        topics[topic] = entry
        data["topics"] = topics
        data["ts"] = _now_iso()
        _commit_data(p, data)
    _audit(p, {"ts": _now_iso(), "action": "alias_add_human",
               "topic": topic, "added": added})
    print(f"[{topic}] +{len(added)} alias(es): {', '.join(added)}")
    return 0


def _cmd_remove(p: "fm.FileMemProvider", topic: str) -> int:
    """Deterministic delete of one topic's alias entry. Other topics
    round-trip verbatim. Removing an unknown topic is a no-op."""
    existed = False
    with p._shard_lock(_alias_file(p)):
        data = _load_data(p)
        topics = _topics_of(data)
        existed = topic in topics
        topics.pop(topic, None)
        data["topics"] = topics
        data["ts"] = _now_iso()
        _commit_data(p, data)
    _audit(p, {"ts": _now_iso(), "action": "alias_remove_human",
               "topic": topic, "existed": existed})
    print(f"[{topic}] removed" if existed else f"[{topic}] not present (no-op)")
    return 0


def _recent_hit_rows(p: "fm.FileMemProvider", topic: str, alias: str) -> list:
    """Most-recent `alias_search_hit` audit rows for one (topic, alias),
    in newest-last order (the log is append-only; we scan it in order)."""
    log_path = p.storage_dir / "consolidation_log.jsonl"
    if not log_path.exists():
        return []
    rows = []
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("action") == "alias_search_hit" \
                    and ev.get("topic") == topic and ev.get("alias") == alias:
                rows.append(ev)
    return rows  # append order = oldest first, newest last


def _cmd_prune(p: "fm.FileMemProvider") -> int:
    """Lazy cleanup (纯确定性, zero LLM): for each topic+alias in
    aliases.json, look at the most recent PRUNE_STREAK `alias_search_hit`
    audit rows; if they exist AND every one records hits==0, the alias is
    dropped (an alias that demonstrably never helped recall widens
    nothing — dead weight). No recent hit rows / any nonzero hit -> the
    alias is kept (fail-soft: absence of evidence never deletes).
    Audited: {"action":"alias_prune","pruned":{...}}"""
    pruned = {}
    with p._shard_lock(_alias_file(p)):
        data = _load_data(p)
        topics = _topics_of(data)
        for topic, entry in list(topics.items()):
            aliases = entry.get("aliases") if isinstance(entry, dict) else []
            aliases = [a for a in (aliases or []) if str(a).strip()]
            kept, dropped = [], []
            for alias in aliases:
                last = _recent_hit_rows(p, topic, alias)[-PRUNE_STREAK:]
                if len(last) >= PRUNE_STREAK and all(
                        int(r.get("hits") or 0) == 0 for r in last):
                    dropped.append(alias)
                else:
                    kept.append(alias)
            if dropped:
                entry["aliases"] = kept
                entry["pruned"] = dropped
                pruned[topic] = dropped
        if pruned:
            data["ts"] = _now_iso()
        data["topics"] = topics
        _commit_data(p, data)
    if pruned:
        _audit(p, {"ts": _now_iso(), "action": "alias_prune", "pruned": pruned})
        for topic, dropped in pruned.items():
            print(f"[{topic}] pruned: {', '.join(dropped)}")
    else:
        print("no aliases pruned (nothing with a 0-hit streak of %d)" % PRUNE_STREAK)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EP-3 human alias-table face (zero LLM)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="read-only: list every topic's aliases")
    ap.add_argument("--add", nargs="+", metavar=("TOPIC", "ALIAS..."),
                    help="human-supplement <topic> with <alias> [alias...] "
                         "(union, never deletes old aliases)")
    ap.add_argument("--remove", metavar="TOPIC",
                    help="delete one topic's alias entry")
    ap.add_argument("--prune", action="store_true",
                    help="lazy-prune 0-hit aliases (streak of %d)" % PRUNE_STREAK)
    ap.add_argument("--store", metavar="DIR",
                    help="alternate live store (default: $HERMES_HOME/filemem)")
    args = ap.parse_args()

    store = Path(args.store).expanduser() if args.store else LIVE_STORE
    p = _build_provider(store)

    ops = [bool(args.list), args.add is not None, args.remove is not None,
           bool(args.prune)]
    if sum(ops) == 0:
        ap.print_help()
        return 1
    try:
        if args.list:
            return _cmd_list(p)
        if args.add is not None:
            topic, aliases = args.add[0], args.add[1:]
            return _cmd_add(p, topic, aliases)
        if args.remove is not None:
            return _cmd_remove(p, args.remove)
        if args.prune:
            return _cmd_prune(p)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
