"""
Two owner-mandated fixes for the "kanban L0 drafts re-asked 10+ times" loop:

P6-A (drain dedup neutral-fallback):
  The host write path (turn-end auto-save to native MEMORY.md) can land an
  entry WITHOUT a source_profile stamp (neutral row, sp=None) while the
  drain consumer runs under a resolved profile (sp='coder').  The D4 dedup
  key is (row_stamp == my_stamp, content-fp) — a neutral row NEVER matches
  'coder', so every drain re-writes a brand-new pool row, and each new row
  is pushed into _pending_confirm -> the [记忆报告] re-asks the same content
  forever.  Fix: a NEUTRAL pool row (stamp None/'') is treated as matching
  ANY consumer stamp for dedup purposes (symmetric with the read face's
  "neutral = shared" semantics).  A cross-profile stamped row (row_sp set
  and != my_sp) still does NOT dedup — profile isolation is preserved.

P6-B (P5 face-1 fail-closed topic gate):
  _turn_report_line's P5 face-1 currently fails OPEN: if the current
  session topic is unresolvable ("") EVERY pending l0 row is listed, even
  rows whose content is clearly from another topic (kanban residue, room
  leftovers).  Fix: add a config knob topic_guard.fail_open (default true,
  preserving today's behavior) + topic_guard.block (list of content
  substrings, default = kanban/room-residue markers).  When fail_open is
  false AND the row content hits a block marker, the row is DROPPED from
  the notice even when the current topic is unresolvable.  L1/L2
  (kind != 'l0') rows and plan_probe entries are never silenced (the 待拍板
  class + probe entries keep their own faces).  Default config keeps
  fail_open=true so this change is a no-op until the owner flips the knob
  in decay.json.
"""
import copy
import json
import os
import pathlib
import threading
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")

from __init__ import FileMemProvider, MEMORY_TYPES, NATIVE_MD_ENTRY_SEP
from consolidation import DECAY_DEFAULTS


# ── shared fixtures (mirror test_native_drain.py's synthetic provider) ─────

def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "filemem"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_home(tmp: pathlib.Path) -> pathlib.Path:
    home = tmp / "home"
    (home / "memories").mkdir(parents=True, exist_ok=True)
    return home


def _noop_router():
    class _R:
        def detect_and_route(self, content):
            return {"tier": None, "level": None, "content": content}
    return _R()


def _make_provider(tmp: pathlib.Path, store: pathlib.Path, home: pathlib.Path,
                   source_profile: str, session_id: str = "s-test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = source_profile
    p.hermetis_home = home
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = store / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._main_runtime = None
    p.trigger_router = _noop_router()
    p._session_drafts = {}
    p._last_recall = None
    p._turn_marker_index = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_active_sid = session_id
    p._drain_seen = {}
    p._drain_memories_dir_fn = lambda: home / "memories"
    p._drain_host_store_fn = lambda: None  # D7 bare-write (zero live-store coupling)
    return p


def _seed_md(home: pathlib.Path, target: str, entries) -> None:
    fname = "USER.md" if target == "user" else "MEMORY.md"
    path = home / "memories" / fname
    path.write_text(NATIVE_MD_ENTRY_SEP.join(entries), encoding="utf-8")


def _md_entries(home: pathlib.Path, target: str):
    fname = "USER.md" if target == "user" else "MEMORY.md"
    path = home / "memories" / fname
    if not path.exists():
        return []
    return [e for e in (x.strip() for x in
                        path.read_text(encoding="utf-8-sig").split(NATIVE_MD_ENTRY_SEP)) if e]


def _pool_rows(store: pathlib.Path, t: str):
    p = store / "memories" / f"{t}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _pool_rows_all(store: pathlib.Path):
    out = []
    for t in MEMORY_TYPES:
        out.extend(_pool_rows(store, t))
    return out


def _seed_pool_row(store: pathlib.Path, t: str, row: dict) -> None:
    p = store / "memories" / f"{t}.json"
    rows = _pool_rows(store, t)
    rows.append(row)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
#  P6-A  drain dedup: neutral pool row must dedup under a stamped consumer
# ═══════════════════════════════════════════════════════════════════════

def test_drain_neutral_row_dedups_under_stamped_profile(tmp_path):
    """P6-A: a NEUTRAL pool row (source_profile missing/None) with the same
    content-fp as an incoming drain entry is a DUP for ANY consumer profile
    (the shared pool's neutral rows are the shared-skeleton class — the same
    semantics the read face's _auto_recall_eligible gives neutral rows:
    'neutral = shared, not clobberable by a sibling').  Without the fix the
    row is re-written as a new row on every drain -> the re-ask loop."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    _seed_md(home, "memory", ["shared kanban card t_1234abcd blocked"])
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    # A neutral pool row (legacy / host-written without a profile stamp) with
    # the IDENTICAL content already lives in fact.json.
    _seed_pool_row(store, "fact", {
        "id": "fact_1", "type": "fact",
        "content": "shared kanban card t_1234abcd blocked",
        "level": "L0", "source": "auto",
        "metadata": {},                      # no source_profile = neutral
    })
    res = prov.drain_native_md()
    rows = _pool_rows(store, "fact")
    kanban_rows = [r for r in rows if "t_1234abcd" in (r.get("content") or "")]
    # The neutral row is the dup: NO new row is written, the md IS cleared
    # (dedup = safe-to-delete per the one-directional invariant).
    assert len(kanban_rows) == 1, f"expected the single pre-seeded neutral row, got {kanban_rows}"
    assert kanban_rows[0]["id"] == "fact_1"
    assert res["moved"] == 1                # the entry was consumed (dedup counts as moved)
    assert _md_entries(home, "memory") == []


def test_drain_cross_profile_stamped_row_does_not_dedup(tmp_path):
    """P6-A negative control: a pool row stamped with ANOTHER profile
    (orchestrator) must NOT dedup against a coder drain write — the shared
    pool must not let profile A clobber profile B (D4 isolation preserved)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    _seed_md(home, "memory", ["orchestrator-only note"])
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    _seed_pool_row(store, "fact", {
        "id": "fact_2", "type": "fact",
        "content": "orchestrator-only note",
        "level": "L0", "source": "auto",
        "metadata": {"source_profile": "orchestrator"},
    })
    prov.drain_native_md()
    rows = _pool_rows(store, "fact")
    note_rows = [r for r in rows if (r.get("content") or "") == "orchestrator-only note"]
    # Both the orchestrator row AND a new coder row exist — no cross-profile clobber.
    assert len(note_rows) == 2
    assert {r.get("id") for r in note_rows} == {"fact_2"} | {r.get("id") for r in note_rows if r.get("id") != "fact_2"}
    assert any((r.get("metadata") or {}).get("source_profile") == "coder" for r in note_rows)


def test_drain_same_profile_stamped_row_dedups(tmp_path):
    """P6-A unchanged-path control: a pool row stamped with the SAME profile
    as the consumer still dedups exactly as before (the original D4 rule —
    the fix only WIDENS the match to neutral rows, never narrows)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    _seed_md(home, "memory", ["coder fact about 9200"])
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    _seed_pool_row(store, "fact", {
        "id": "fact_3", "type": "fact",
        "content": "coder fact about 9200",
        "level": "L0", "source": "auto",
        "metadata": {"source_profile": "coder"},
    })
    res = prov.drain_native_md()
    rows = _pool_rows(store, "fact")
    assert len(rows) == 1 and rows[0]["id"] == "fact_3"
    assert res["moved"] == 1
    assert _md_entries(home, "memory") == []


def test_drain_neutral_consumer_matches_stamped_row(tmp_path):
    """P6-A symmetric direction: a consumer whose source_profile is UNRESOLVED
    (None — single-profile store / identity not wired) must dedup against a
    stamped pool row with the same content (the unresolved consumer falls
    back to content-fp-only matching, symmetric with neutral=shared)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    _seed_md(home, "memory", ["host entry without profile"])
    prov = _make_provider(tmp_path, store, home, source_profile=None)
    _seed_pool_row(store, "fact", {
        "id": "fact_4", "type": "fact",
        "content": "host entry without profile",
        "level": "L0", "source": "auto",
        "metadata": {"source_profile": "coder"},
    })
    res = prov.drain_native_md()
    rows = _pool_rows(store, "fact")
    assert len(rows) == 1 and rows[0]["id"] == "fact_4"
    assert res["moved"] == 1
    assert _md_entries(home, "memory") == []


# ═══════════════════════════════════════════════════════════════════════
#  P6-B  P5 face-1 fail-closed: kanban-residue rows dropped when the current
#        topic is unresolvable (fail_open=false + block-marker hit)
# ═══════════════════════════════════════════════════════════════════════

def _push_l0(prov, content, kind="l0"):
    sid = prov.session_id
    prov._pending_confirm.setdefault(sid, []).append({
        "id": f"l0_{len(prov._pending_confirm[sid])}",
        "level": "L0", "content": content, "kind": kind, "ts": "2026-10-20T00:00:00Z",
    })


def _turn_report(prov):
    return prov._turn_report_line(prov.session_id)


def test_p5_block_drops_kanban_row_when_topic_unresolvable(tmp_path):
    """P6-B: topic_guard.fail_open=false + the row content hits a default
    block marker (kanban card fragment) + the current session topic is
    unresolvable ("" — TUI short-prompt session) -> the row is DROPPED from
    the [记忆报告] notice even without a topic to compare against."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    # No session trace file -> _session_title_hint returns "" (unresolvable).
    _p5 = {
        "enable": True,
        "fail_open": False,
        "block": ["Kanban t_", "kanban-block-relay", "卡掉进了 blocked", "看板"],
    }
    prov._decay["topic_guard"] = _p5
    _push_l0(prov, "Kanban t_1234abcd done — push evidence chain")
    _push_l0(prov, "user asked about 9200 max-depth fix")
    report = _turn_report(prov)
    assert "Kanban t_1234abcd" not in report
    assert "9200 max-depth" in report
    # The kanban row is still in the pending queue (silence the NOTICE, not
    # the queue — a later discard-all can still consume it).
    q = prov._pending_confirm[prov.session_id]
    assert any("Kanban t_1234abcd" in e["content"] for e in q)


def test_p5_default_fail_open_keeps_everything(tmp_path):
    """P6-B default (fail_open=true, no block markers set) = today's behavior:
    an unresolvable topic lists EVERY pending l0 row (zero false-silence,
    the pre-P6 regression baseline must hold under default config)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    prov._decay["topic_guard"] = {"enable": True, "fail_open": True}
    _push_l0(prov, "Kanban t_1234abcd done — push evidence chain")
    report = _turn_report(prov)
    assert "Kanban t_1234abcd" in report


def test_p5_block_does_not_silence_l1_l2_or_probe(tmp_path):
    """P6-B scope: the block filter applies to kind=='l0' ONLY.  L1/L2
    待拍板 rows and plan_probe entries are never silenced by the topic
    face (face-2 handles the 待拍板 class; the probe has its own entry)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    prov._decay["topic_guard"] = {
        "enable": True, "fail_open": False,
        "block": ["Kanban t_", "看板"],
    }
    prov._pending_confirm[prov.session_id] = [
        {"id": "l1", "level": "L1", "content": "Kanban t_99999999 lesson",
         "kind": "l1", "ts": "2026-10-20T00:00:00Z"},
        {"id": "probe1", "level": "L0", "content": "[plan-probe] fix 9200",
         "kind": "plan_probe", "ts": "2026-10-20T00:00:00Z"},
        {"id": "l0a", "level": "L0", "content": "Kanban t_11111111 residue",
         "kind": "l0", "ts": "2026-10-20T00:00:00Z"},
    ]
    report = _turn_report(prov)
    assert "Kanban t_99999999" in report      # L1 待拍板 never silenced
    assert "plan-probe" in report             # probe entry never silenced
    assert "Kanban t_11111111" not in report  # l0 + block-hit + fail-closed


def test_p5_block_no_hit_topic_resolvable_keeps_row(tmp_path):
    """P6-B: fail-closed only applies when the row content hits a block
    marker.  A non-kanban l0 row with an unresolvable topic is still listed
    (the topic BM25 gate is skipped, not faked — the row simply passes)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    prov._decay["topic_guard"] = {"enable": True, "fail_open": False,
                                  "block": ["Kanban t_"]}
    _push_l0(prov, "9200 dashboard sidebar drawer bug verified fixed")
    report = _turn_report(prov)
    assert "sidebar drawer" in report


def test_p5_disabled_gate_lists_all(tmp_path):
    """P6-B rollback: topic_guard.enable=false -> the whole face-1 gate is
    off and every pending row is listed (exact pre-P5 behavior, zero
    false-silence, the documented 1A 保守 rollback posture)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    prov._decay["topic_guard"] = {"enable": False, "fail_open": False,
                                  "block": ["Kanban t_"]}
    _push_l0(prov, "Kanban t_22222222 residue")
    report = _turn_report(prov)
    assert "Kanban t_22222222" in report


def test_p5_fail_open_false_empty_block_keeps_all(tmp_path):
    """P6-B: fail_open=false with an EMPTY block list = only the unresolvable
    topic's old fail-open is removed; without block markers every l0 row is
    still kept (no silent drop of legitimate rows — the block list is the
    ONLY new silence vector)."""
    store = _make_store(tmp_path)
    home = _make_home(tmp_path)
    prov = _make_provider(tmp_path, store, home, source_profile="coder")
    prov._decay["topic_guard"] = {"enable": True, "fail_open": False, "block": []}
    _push_l0(prov, "normal l0 fact about 9200")
    report = _turn_report(prov)
    assert "normal l0 fact" in report
