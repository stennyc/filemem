"""SESSION_ISOLATION_V2: the confirmed gate on the auto-inject face.

Owner 2026-10-10 design (specs/SESSION_ISOLATION_V2-design.md):

  auto-inject face skeleton =
    current profile's confirmed=True global rows
    + current session's rows (episodic pool, session_id match)
  exclude = other sessions' unconfirmed rows / dirty thought-streams / tracebacks

The predicate under test is `_auto_inject_eligible(row, session_sp, cur_sid)`,
shared by THREE faces: prefetch (live auto-inject), P3 snapshot replay
(`_filter_replayed_snapshot`), and the egress drop path.

C-b choice (owner decision): memories-pool rows carry NO session_id, so the
"current session all-in" leg only applies to episodic rows; memories rows
enter the auto face ONLY when metadata.confirmed is True.

Coverage (spec §测试):
  (a) _auto_inject_eligible three faces:
       - old row (no session_id, no confirmed)          -> cut
       - episodic current-session row                   -> kept
       - confirmed global row (cross session)            -> kept
       - session_sp=None fail-open
      plus a PREFETCH-face test proving the episodic all-in leg end-to-end
      and that unconfirmed old rows do NOT leak into the auto face.
  (b) _filter_replayed_snapshot: unconfirmed old rows fold out, confirmed rows kept
  (c) _tool_search default: no global cascade on 0-hits
  (d) D-site write-side clamp: unconfirmed L1/L2 -> L0
"""
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import (  # noqa: E402
    FileMemProvider,
    _auto_inject_eligible,
    _row_source_profile,
)


def _row(content: str, *, sp: Optional[str] = None,
        confirmed: Optional[bool] = None,
        session_id: Optional[str] = None,
        mem_type: str = "fact",
        superseded: bool = False,
        settled: bool = False,
        origin: str = "user") -> Dict[str, Any]:
    """Build a minimal memory row the way the pool stores it."""
    meta: Dict[str, Any] = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed is not None:
        meta["confirmed"] = confirmed
    if superseded:
        meta["superseded"] = True
    if settled:
        meta["settled"] = True
    row = {
        "id": "test-row",
        "type": mem_type,
        "content": content,
        "level": "L0",
        "source": origin,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "metadata": meta,
    }
    if session_id is not None:
        row["session_id"] = session_id
    return row


# ── 1. _auto_inject_eligible unit level ─────────────────────────────────────


def test_unconfirmed_old_memories_row_cut():
    """A memories-pool row with no session_id and no confirmed flag is folded
    out of the auto face — the core V2 defect class (dirty thought-streams,
    tracebacks, old cross-session task noise)."""
    row = _row("old dirty thought stream", sp="default")  # no confirmed
    assert not _auto_inject_eligible(row, "default", "session-abc")


def test_episodic_current_session_row_kept():
    """An episodic row whose session_id matches the current session is kept
    even without confirmed (the 'current session all-in' leg)."""
    row = _row("episodic current session", sp="default",
               session_id="session-abc", mem_type="episodic")
    assert _auto_inject_eligible(row, "default", "session-abc")


def test_episodic_other_session_unconfirmed_row_cut():
    """An episodic row from a DIFFERENT session that is not confirmed is cut."""
    row = _row("other session episodic", sp="default",
               session_id="session-OLD", mem_type="episodic")
    assert not _auto_inject_eligible(row, "default", "session-abc")


def test_confirmed_global_row_cross_session_kept():
    """A confirmed row (the per-profile global skeleton) is kept regardless of
    session_id — it is the durable layer that crosses sessions."""
    row = _row("confirmed architecture decision", sp="default",
               confirmed=True)
    assert _auto_inject_eligible(row, "default", "session-abc")


def test_session_sp_none_fail_open_profile_leg():
    """session_sp=None -> the P0-2 profile leg fails open (every row passes
    the profile test), but the confirmed leg STILL applies: an unconfirmed
    row with no session_id is cut, a confirmed row is kept."""
    unconfirmed = _row("unconfirmed", sp="default")
    confirmed = _row("confirmed", sp="default", confirmed=True)
    assert not _auto_inject_eligible(unconfirmed, None, "s-1")
    assert _auto_inject_eligible(confirmed, None, "s-1")


def test_cur_sid_none_only_confirmed_leg_survives():
    """cur_session_id=None -> the session_id leg never matches; only the
    confirmed leg can pass."""
    row_no_conf = _row("unconfirmed no sid", sp="default")
    row_conf = _row("confirmed", sp="default", confirmed=True)
    assert not _auto_inject_eligible(row_no_conf, "default", None)
    assert _auto_inject_eligible(row_conf, "default", None)


def test_superseded_row_cut_even_if_confirmed():
    """O1 auto-face fold: a superseded row is cut even when confirmed=True
    (the fold-out check runs before the confirmed gate)."""
    row = _row("superseded but confirmed", sp="default",
               confirmed=True, superseded=True)
    assert not _auto_inject_eligible(row, "default", "s-1")


def test_non_dict_row_fail_soft():
    """The predicate is fail-soft: a non-dict row returns False, never raises."""
    assert not _auto_inject_eligible("garbage", "default", "s-1")
    assert not _auto_inject_eligible(None, "default", "s-1")
    assert not _auto_inject_eligible(["not", "a", "dict"], "default", "s-1")


def test_malformed_metadata_fail_soft():
    """A row whose metadata is not a dict degrades to the confirmed leg not
    matching (fail-soft), never raising."""
    row = _row("bad metadata", sp="default")
    row["metadata"] = "not-a-dict"
    assert not _auto_inject_eligible(row, "default", "s-1")


# ── 1b. prefetch face: episodic current-session leg at the injection level ──


def _make_provider(tmp: Path, sid: str = "s_v2", profile: Optional[str] = None) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = profile
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = None
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.auto_extract = False
    p._decay = {}
    p.min_search_score = 0.3
    p._last_save_time = time.time()
    p._save_interval = 30
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p._pending_memories = []
    p._session_drafts = {}
    p._pending_failure_meta = {}
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._main_runtime = None
    p._llm_ctx = None
    p._last_recall = None
    return p


def _make_episodic_provider(tmp: Path, sid: str, profile: Optional[str]) -> FileMemProvider:
    """A provider whose episodic_dir is REAL (not None) so search_memories
    loads the episodic pool — the shape needed to test the current-session
    all-in leg at the PREFETCH face (not just unit-level)."""
    p = _make_provider(tmp, sid=sid, profile=profile)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    return p


def _write_episodic(tmp: Path, content: str, *, sid: str, sp: Optional[str] = None):
    """Append one row to the current month's episodic shard."""
    dt = datetime.now(timezone.utc)
    shard_dir = tmp / "episodic" / f"{dt.year:04d}" / f"{dt.month:02d}"
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard = shard_dir / f"{sid}.jsonl"
    row = {
        "id": f"ep_{abs(hash(content)) & 0xFFFFFF:06x}",
        "content": content,
        "session_id": sid,
        "ts": dt.isoformat(),
        "salience": 0.5,
        "metadata": {},
    }
    if sp is not None:
        row["source_profile"] = sp
    with open(str(shard), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _write_v2_row(tmp: Path, content: str, *, sp=None, confirmed=False,
                  superseded=False):
    meta = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    if superseded:
        meta["superseded"] = True
    row = {
        "id": f"v2_{abs(hash(content)) & 0xFFFFFF:06x}",
        "type": "fact",
        "content": content,
        "level": "L0",
        "source": "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "metadata": meta,
    }
    f = tmp / "memories" / "fact.json"
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


def test_prefetch_episodic_current_session_row_kept(tmp_path):
    """At the PREFETCH face (not just unit-level _auto_inject_eligible): an
    episodic row whose session_id matches provider.session_id is KEPT by the
    gate even though it is not confirmed — the 'current session all-in' leg.
    A confirmed memories-pool row is ALSO kept. An unconfirmed memories row
    is CUT. This is the spec-required proof that a legitimate current-session
    row does not leak out while a dirty old row does."""
    sid = "s_ep"
    p = _make_episodic_provider(tmp_path, sid=sid, profile="default")
    # A confirmed memories row (the global skeleton) — survives.
    _write_v2_row(tmp_path, "confirmed deploy rule", sp="default", confirmed=True)
    # An unconfirmed old memories row (the dirty thought-stream class) — CUT.
    _write_v2_row(tmp_path, "old unconfirmed debug stream", sp="default")
    # A current-session episodic row (all-in leg) — survives, not confirmed.
    _write_episodic(tmp_path, "current session episodic note", sid=sid, sp="default")
    # A DIFFERENT session's episodic row (not confirmed) — CUT.
    _write_episodic(tmp_path, "other session episodic note", sid="OTHER", sp="default")

    p._set_bringback_choice(sid, "clean_start")
    ctx = p.prefetch("deploy rule note", session_id=sid, confidence=0.5)
    assert "confirmed deploy rule" in ctx, \
        "confirmed memories row must survive the auto face"
    assert "current session episodic note" in ctx, \
        "the current session's episodic row (all-in leg) must survive"
    assert "old unconfirmed debug stream" not in ctx, \
        "an unconfirmed old memories row must NOT leak into the auto face"
    assert "other session episodic note" not in ctx, \
        "another session's unconfirmed episodic row must NOT leak in"


def test_prefetch_unconfirmed_memories_row_does_not_leak(tmp_path):
    """Spec-required proof (b): a LEGITIMATE but unconfirmed old memories row
    does NOT leak into the auto face via the live prefetch path. A pool with
    ONLY unconfirmed rows yields an empty/no-content injection for that row."""
    sid = "s_leak"
    p = _make_episodic_provider(tmp_path, sid=sid, profile="default")
    _write_v2_row(tmp_path, "unconfirmed old task noise", sp="default")
    _write_v2_row(tmp_path, "another unconfirmed row", sp="default")
    ctx = p.prefetch("old task noise", session_id=sid, confidence=0.5)
    assert "unconfirmed old task noise" not in ctx, \
        "unconfirmed old rows must not reach the auto-inject face"
    assert "another unconfirmed row" not in ctx


# ── 2. _filter_replayed_snapshot (P3 replay) ───────────────────────────────


def test_p3_replay_unconfirmed_row_cut_confirmed_kept(tmp_path):
    """V2: a frozen P3 block re-gated through _auto_inject_eligible cuts
    unconfirmed old rows but keeps confirmed global rows."""
    p = _make_provider(tmp_path, sid="s_v2", profile="coder")
    _write_v2_row(tmp_path, "confirmed coder rule", sp="coder", confirmed=True)
    _write_v2_row(tmp_path, "unconfirmed coder noise", sp="coder")
    block = "\n".join([
        "[fact] confirmed coder rule",
        "[fact] unconfirmed coder noise",
    ])
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_v2", block)
    replayed = p._filter_replayed_snapshot(p._load_injected_snapshot("s_v2", "facts"))
    assert "confirmed coder rule" in replayed
    assert "unconfirmed coder noise" not in replayed


def test_p3_replay_unresolvable_profile_confirmed_gate_still_applies(tmp_path):
    """V2 C-b invariant: profile=None fails open at the profile leg, but the
    confirmed leg still cuts unconfirmed rows."""
    p = _make_provider(tmp_path, sid="s_v2b", profile=None)
    _write_v2_row(tmp_path, "unconfirmed neutral row")  # no sp, no confirmed
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_v2b", "[fact] unconfirmed neutral row")
    replayed = p._filter_replayed_snapshot(p._load_injected_snapshot("s_v2b", "facts"))
    assert replayed == ""


def test_p3_replay_superseded_row_cut_even_if_confirmed(tmp_path):
    """O1 fold: a superseded row is cut even when confirmed=True."""
    p = _make_provider(tmp_path, sid="s_v2c", profile="coder")
    _write_v2_row(tmp_path, "superseded confirmed row", sp="coder",
                  confirmed=True, superseded=True)
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_v2c", "[fact] superseded confirmed row")
    replayed = p._filter_replayed_snapshot(p._load_injected_snapshot("s_v2c", "facts"))
    assert replayed == ""


# ── 3. _tool_search default: no global cascade ──────────────────────────────


def test_tool_search_default_no_global_cascade(tmp_path, monkeypatch):
    """V2 design point ③: the tool face default (profile=None) pins the
    scope to the session's own profile pool and disables the global cascade —
    a 0-hit scoped result is returned empty (recall_scope='session', hit_pool
    'self'), NOT cross-profile. Only the explicit 'ALL' value reaches the
    global pool."""
    p = _make_provider(tmp_path, sid="s_tool", profile="default")
    # Seed a row ONLY in a different profile so a global pool would find it.
    _write_v2_row(tmp_path, "cross-profile row", sp="other-profile", confirmed=True)
    # No rows in the 'default' profile pool for this query.
    results = p._tool_search({"query": "cross-profile row", "limit": 5})
    parsed = json.dumps(json.loads(results))
    parsed_dict = json.loads(results)
    # recall_scope='session' means the hit came from the self (scoped) pool,
    # not a global fallback — exactly the V2 B-path guarantee.
    assert parsed_dict.get("recall_scope") == "session", \
        (f"default scope must pin to the self pool (no global cascade); "
         f"got {parsed_dict.get('recall_scope')}")
    # The cross-profile row must NOT leak into the default-scope result.
    assert "cross-profile row" not in parsed, \
        "cross-profile row must not appear in the default-scoped result"


def test_tool_search_all_reaches_global_pool(tmp_path, monkeypatch):
    """Explicit 'ALL' is the only path that reaches the global pool; it is
    marked reference-only (recall_scope='direct_all')."""
    p = _make_provider(tmp_path, sid="s_tool2", profile="default")
    _write_v2_row(tmp_path, "cross-profile row", sp="other-profile", confirmed=True)
    results = p._tool_search({"query": "cross-profile row", "limit": 5,
                              "profile": "ALL"})
    parsed = json.loads(results)
    assert parsed.get("recall_scope") == "direct_all", \
        f"ALL must report direct_all scope; got {parsed.get('recall_scope')}"
    # The cross-profile row IS now visible (global pool).
    assert "cross-profile row" in json.dumps(parsed), \
        "ALL scope must reach the global pool"


# ── 4. D site: write-side promotion clamp ───────────────────────────────────


def test_save_memory_unconfirmed_L1_clamped_to_L0(tmp_path):
    """V2 D (write-side gate): an unconfirmed auto-catch row requesting L1 is
    clamped to L0 — the durable layer is reserved for confirmed rows. The row
    lands in the L0 pool, NOT the L1 pool."""
    p = _make_provider(tmp_path, sid="s_d", profile="default")
    p._save_memory(
        "unconfirmed auto insight", level="L1", source="auto", _force_write=True)
    shard = (tmp_path / "memories" / "fact.json").read_text() if \
        (tmp_path / "memories" / "fact.json").exists() else "[]"
    items = json.loads(shard)
    matching = [it for it in items if it.get("content") == "unconfirmed auto insight"]
    assert matching, "the auto-catch row must be written to the pool"
    assert matching[0].get("level") == "L0", \
        f"unconfirmed L1 must be clamped to L0, got {matching[0].get('level')}"


def test_save_memory_manual_L1_not_clamped(tmp_path):
    """A manual (user-confirmed) L1 row is NOT clamped — the durable layer is
    reachable for confirmed origin rows (the D-site whitelist)."""
    p = _make_provider(tmp_path, sid="s_d2", profile="default")
    p._save_memory("manual confirmed insight", level="L1", source="manual",
                   _force_write=True)
    shard = (tmp_path / "memories" / "fact.json").read_text() if \
        (tmp_path / "memories" / "fact.json").exists() else "[]"
    items = json.loads(shard)
    matching = [it for it in items if it.get("content") == "manual confirmed insight"]
    assert matching, "the manual row must be written to the pool"
    assert matching[0].get("level") == "L1", \
        f"a manual L1 row must NOT be clamped, got {matching[0].get('level')}"
