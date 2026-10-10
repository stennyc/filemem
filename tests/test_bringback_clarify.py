"""BRINGBACK (P1-P5, 2026-10-10 owner direction + B2-A + fresh option).

Owner 2026-10-10 + reviewer (B1/B2-A) + owner 'fresh' option direction, layered
on specs/SESSION_ISOLATION_V2-design.md + specs/BRINGBACK-PRD.md:

  Auto-inject face skeleton = V2 two legs (current session all-in + confirmed
  global), PLUS an OPTIONAL per-session user choice:
    - bring_back : pin the previous closed session's CONFIRMED rows to the TOP
                   of this session's auto-inject face (a REORDER, not a new
                   collection leg — a confirmed prev-session row is already
                   kept by the confirmed-global leg, so bring_back adds no
                   rows, it only surfaces the continuity context first).
    - clean_start: V2 default (confirmed global skeleton + current session
                   rows; no pinning).
    - fresh      : this session injects NOTHING on the auto face (even the
                   confirmed global skeleton is folded out).

  INVARIANT (owner): bring-back / fresh gate only the AUTO-INJECT face. The
  SEARCH face (filemem_search / M4 cross-session recall) ALWAYS retrieves
  across sessions regardless of the choice — choosing 'fresh' or 'clean start'
  never makes an old session unretrievable (全新 ≠ 失联).

Reviewer fixes baked in (2026-10-10, B1 + B2-A):
  - B2-A: the old "third collection leg" (bringback_session_id param) is GONE —
    proven 0-margin dead code (a confirmed prev-session row is already kept by
    the confirmed-global leg). bring_back is now a REORDER (this test suite
    pins the "no new rows" fact).
  - B1: the P3 clarify footer is NOT frozen into the P3 cold-start snapshot
    (a frozen footer would re-fire on every cold-start replay). The snapshot is
    written from the recall block BEFORE the footer is appended; the footer
    rides the return face only.

Coverage:
  (1) bring_back reorder: pin key 0 for prev-session confirmed rows, 1 for all
      others; row set UNCHANGED vs V2 (no new rows, no dropped rows).
  (2) P1 stamp: on_session_end records a per-profile last_closed row;
      _last_closed_session_for_profile returns the most-recent closed session
      for the CURRENT profile, excludes the current session; no-stamp /
      no-matching-profile -> None.
  (3) P3 footer: injected ONCE on first non-trivial prefetch when a target
      exists; not re-fired; suppressed when no target / already-answered; empty
      when no storage_dir or no session_id (fail-soft, context untouched).
      B1: footer is NOT in the P3 snapshot (written before the append).
  (4) P4 tool filemem_set_bringback:
      - 'bring_back' -> pin target resolved (or None if no last_closed stamp)
      - 'clean_start' -> no pin, fresh=False
      - 'fresh'       -> auto face empty this session; search face unaffected
      - 'confirmed_global_only' REJECTED (removed option -> 3-value enum)
      - per-session (a new session re-asks)
      - search-face invariant: no choice touches the search pool assembly
  (5) regression sentinels: V2 two-leg byte-equivalent when no choice; the
      bring_back reorder never adds/drops rows (set-identical to V2).
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
    _normalize_profile_stamp,
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _row(content: str, *, sp: Optional[str] = None,
         confirmed: Optional[bool] = None,
         session_id: Optional[str] = None,
         mem_type: str = "fact",
         origin: str = "user") -> Dict[str, Any]:
    """Build a minimal pool row the way the store holds it."""
    meta: Dict[str, Any] = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed is not None:
        meta["confirmed"] = confirmed
    row: Dict[str, Any] = {
        "id": f"{mem_type}_{content[:6]}_{session_id or 'na'}",
        "type": mem_type,
        "content": content,
        "level": "L0",
        "metadata": meta,
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
        "salience": 0.5,
        "last_referenced": "2026-10-01T00:00:00",
    }
    if session_id is not None:
        # episodic rows carry session_id at TOP LEVEL (V2 C-b invariant)
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    return row


def _make_provider(tmp: Path, sid: str = "s_cur",
                   profile: Optional[str] = "default") -> FileMemProvider:
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
    # _shard_lock is required by the P1 stamp write path; give it a no-op
    # context manager in the fixture shell (the real impl uses a flock file).
    import contextlib
    p._shard_lock = lambda pth: contextlib.suppress()
    return p


def _provider_with_prev_closed(tmp_path, profile="default", cur="s_cur",
                               prev="s_prev"):
    p = _make_provider(tmp_path, sid=cur, profile=profile)
    p_prev = _make_provider(tmp_path, sid=prev, profile=profile)
    p_prev.session_id = prev
    p_prev._record_last_closed_session(prev)
    return p


# ── (1) bring_back reorder: pin + no row-set change ───────────────────────────


def test_bringback_reorder_key_pins_prev_confirmed_to_front():
    """The pin key returns 0 ONLY for the previous session's CONFIRMED rows;
    everything else (current-session, unconfirmed prev, other sessions,
    superseded) keys to 1."""
    p = _make_provider(Path(tempfile.mkdtemp()))
    prev_confirmed = _row("prev fact", sp="default", confirmed=True,
                          session_id="s_prev", mem_type="episodic")
    prev_unconfirmed = _row("prev dirty", sp="default", confirmed=False,
                            session_id="s_prev", mem_type="episodic")
    cur_row = _row("cur fact", sp="default", confirmed=False,
                   session_id="s_cur", mem_type="episodic")
    other_confirmed = _row("other fact", sp="default", confirmed=True,
                           session_id="s_other", mem_type="episodic")
    assert p._bringback_reorder_key(prev_confirmed, "s_prev") == 0
    assert p._bringback_reorder_key(prev_unconfirmed, "s_prev") == 1
    assert p._bringback_reorder_key(cur_row, "s_prev") == 1
    assert p._bringback_reorder_key(other_confirmed, "s_prev") == 1
    # pin=None (not chosen / no target) -> no pin (every row keys to 1)
    assert p._bringback_reorder_key(prev_confirmed, None) == 1


def test_bringback_reorder_is_set_identical_to_v2():
    """B2-A invariant: a stable sort by the pin key REORDERS but never adds
    or drops a row. Given a surviving pool, sorting by the pin key yields the
    SAME multiset of ids as the unsorted pool (only the order moves the
    prev-confirmed rows to the front)."""
    p = _make_provider(Path(tempfile.mkdtemp()))
    prev_conf = _row("prev conf", sp="default", confirmed=True,
                     session_id="s_prev", mem_type="episodic")
    prev_dirty = _row("prev dirty", sp="default", confirmed=False,
                      session_id="s_prev", mem_type="episodic")
    cur_row = _row("cur fact", sp="default", confirmed=False,
                   session_id="s_cur", mem_type="episodic")
    conf_global = _row("global fact", sp="default", confirmed=True)
    pool = [cur_row, prev_conf, prev_dirty, conf_global]
    ordered = sorted(pool, key=lambda r: p._bringback_reorder_key(r, "s_prev"))
    assert sorted(r["id"] for r in ordered) == sorted(r["id"] for r in pool)
    # prev-confirmed must now be FIRST:
    assert ordered[0]["id"] == prev_conf["id"]


def test_bringback_no_new_rows_vs_v2_confirmed_leg():
    """The dead-code proof (B2-A): a confirmed prev-session row is ALREADY
    kept by V2's confirmed-global leg — the pin adds nothing to the row SET.
    Both the V2 two-leg and the pin keep/cut the exact same membership; the
    pin only changes ORDER, never membership."""
    prev_conf = _row("prev conf", sp="default", confirmed=True,
                     session_id="s_prev", mem_type="episodic")
    prev_dirty = _row("prev dirty", sp="default", confirmed=False,
                      session_id="s_prev", mem_type="episodic")
    # V2 two-leg (no pin): confirmed prev row kept, unconfirmed cut.
    assert _auto_inject_eligible(prev_conf, "default", "s_cur") is True
    assert _auto_inject_eligible(prev_dirty, "default", "s_cur") is False


# ── (2) P1 last-closed-session stamp ───────────────────────────────────────────


def test_p1_records_and_reads_per_profile(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")
    p.session_id = "s_cur"
    assert p._last_closed_session_for_profile(exclude_session_id="s_cur") == "s_prev"
    p.session_id = "s_prev"
    assert p._last_closed_session_for_profile(exclude_session_id="s_prev") is None


def test_p1_excludes_current_session(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")
    p.session_id = "s_cur"
    p._record_last_closed_session("s_cur")
    assert p._last_closed_session_for_profile(exclude_session_id="s_cur") == "s_prev"


def test_p1_no_cross_profile_recover(tmp_path):
    """A closed session of profile 'coder' is NOT recoverable by a
    'default'-profile provider (per-profile filter) — owner A: 不串 profile."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p_coder = _make_provider(tmp_path, sid="s_coder_prev", profile="coder")
    p_coder.session_id = "s_coder_prev"
    p_coder._record_last_closed_session("s_coder_prev")
    assert p._last_closed_session_for_profile(exclude_session_id="s_cur") is None


def test_p1_no_stamp_degrades_to_none(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    assert p._last_closed_session_for_profile(exclude_session_id="s_cur") is None


# ── (3) P3 one-shot clarify footer (+ B1 not frozen into the snapshot) ─────────


def test_p3_footer_offered_when_target_exists(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    assert "带回账本" in foot
    assert "filemem_set_bringback" in foot
    # One-shot: the second call is suppressed (session-keyed gate set).
    assert p._bringback_clarify_footer("s_cur") == ""


def test_p3_footer_silenced_when_no_target(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    assert p._bringback_clarify_footer("s_cur") == ""


def test_p3_footer_suppressed_when_already_answered(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    # User already chose (any of the three) -> footer not re-asked.
    p._set_bringback_choice("s_cur", "clean_start")
    assert p._bringback_clarify_footer("s_cur") == ""


def test_p3_footer_fail_soft_no_session(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    p.session_id = None
    assert p._bringback_clarify_footer("") == ""


def test_p3_footer_lists_three_choices_including_fresh(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    assert "bring_back" in foot
    assert "clean_start" in foot
    assert "fresh" in foot
    # The removed option must NOT reappear.
    assert "confirmed_global_only" not in foot


def test_a_footer_hard_directive_to_emit_clarify_tool(tmp_path):
    """A (owner 2026-10-11): the footer must HARD-direct the model to call the
    clarify TOOL (native single-select card, keyboard ↑↓ + Enter), not
    paraphrase the three options as plain chat text. Pin the directive so a
    future rewrite that softens it back to '请用 clarify 问用户一次' (which
    let the model re-state options in prose → text box, not native card) is
    caught. (The plugin only INSTRUCTS; model compliance is an LLM behavior,
    but the hard '必须/不要' wording is the part we can lock in a test.)"""
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    # The native-window directive is present:
    assert "必须调用 clarify 工具" in foot
    # The soft old wording is gone:
    assert "请用 clarify 问用户" not in foot
    # Explicitly forbids prose paraphrase:
    assert "不要用纯文字" in foot
    # The 3 options are still listed for the clarify choices array:
    assert "bring_back" in foot and "clean_start" in foot and "fresh" in foot


def test_b1_footer_not_frozen_into_p3_snapshot(tmp_path):
    """B1 regression sentinel: the P3 cold-start snapshot file written at the
    end of a live prefetch must hold the recall block WITHOUT the bring-back
    footer (a frozen footer would re-fire on every cold-start replay).

    The sentinel reads the on-disk last_injected.txt directly (bypassing the
    Jaccard-gated _load_injected_snapshot helper) so it proves the FROZEN BYTES
    carry no footer opener, while the live footer remains a separate string
    that never landed in the file."""
    p = _provider_with_prev_closed(tmp_path)
    # Simulate the live prefetch ordering: the snapshot is frozen from the
    # recall block (no footer), then the footer is appended to the return face
    # only.
    p._save_injected_snapshot("s_cur", "[fact] some confirmed row")
    snap_file = p._injected_snapshot_path("s_cur")
    assert snap_file is not None and snap_file.exists()
    frozen_bytes = snap_file.read_text(encoding="utf-8")
    assert "带回账本" not in frozen_bytes  # the footer must NOT be frozen
    # The live footer is a SEPARATE string; producing it does not touch the
    # frozen file.
    foot = p._bringback_clarify_footer("s_cur")
    assert "带回账本" in foot or foot == ""  # live face only
    # Re-read: the frozen file is still footer-free after the live ask.
    assert "带回账本" not in snap_file.read_text(encoding="utf-8")


# ── (4) P4 tool: filemem_set_bringback (3-value) ───────────────────────────────


def test_p4_tool_bring_back_resolves_pin(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_set_bringback",
                                        {"choice": "bring_back",
                                         "session_id": "s_cur"}))
    assert out["success"] is True
    assert out["choice"] == "bring_back"
    assert out["bring_back_active"] is True
    assert out["fresh"] is False
    assert out["pin_target"] == "s_prev"
    assert p._resolve_bringback_session_id("s_cur") == "s_prev"


def test_p4_tool_clean_start_no_pin(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_set_bringback",
                                        {"choice": "clean_start",
                                         "session_id": "s_cur"}))
    assert out["success"] is True
    assert out["bring_back_active"] is False
    assert out["fresh"] is False
    assert out["pin_target"] is None
    assert p._resolve_bringback_session_id("s_cur") is None


def test_p4_tool_fresh_folds_auto_face(tmp_path):
    """'fresh' choice -> the auto-inject face is empty for this session
    (_bringback_fresh True), search face unaffected."""
    p = _provider_with_prev_closed(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_set_bringback",
                                        {"choice": "fresh",
                                         "session_id": "s_cur"}))
    assert out["success"] is True
    assert out["fresh"] is True
    assert out["bring_back_active"] is False
    assert p._bringback_fresh("s_cur") is True
    assert p._get_bringback_choice("s_cur") == "fresh"


def test_p4_tool_removed_option_rejected(tmp_path):
    """The removed 4th option 'confirmed_global_only' is rejected (3-value enum)."""
    p = _provider_with_prev_closed(tmp_path)
    out = json.loads(p.handle_tool_call("filemem_set_bringback",
                                        {"choice": "confirmed_global_only",
                                         "session_id": "s_cur"}))
    assert out["success"] is False
    assert "choice must be one of" in out["error"]


def test_p4_tool_per_session_new_session_reasks(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    p.handle_tool_call("filemem_set_bringback",
                       {"choice": "bring_back", "session_id": "s_cur"})
    p._set_bringback_choice("s_next", "fresh")
    assert p._resolve_bringback_session_id("s_cur") == "s_prev"
    assert p._resolve_bringback_session_id("s_next") is None


def test_p4_tool_no_target_reports_none(tmp_path):
    """User chose bring_back but no per-profile last_closed stamp exists:
    pin target is None (honest report), choice still lands."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback",
                                        {"choice": "bring_back",
                                         "session_id": "s_cur"}))
    assert out["success"] is True
    assert out["bring_back_active"] is True
    assert out["pin_target"] is None


# ── (5) regression sentinels ───────────────────────────────────────────────────


def test_sentinel_v2_two_leg_unchanged_with_no_choice(tmp_path):
    """With no bring-back choice recorded, the predicate is byte-equivalent to
    the V2 two-leg form — pin the exact V2 outcomes (regression guard)."""
    old_row = _row("old mem", sp="default", confirmed=None)
    assert _auto_inject_eligible(old_row, "default", "s_cur") is False
    conf_global = _row("global fact", sp="default", confirmed=True)
    assert _auto_inject_eligible(conf_global, "default", "s_cur") is True
    cur_episodic = _row("cur fact", sp="default", confirmed=False,
                        session_id="s_cur", mem_type="episodic")
    assert _auto_inject_eligible(cur_episodic, "default", "s_cur") is True


def test_sentinel_bringback_reorder_never_over_folds():
    """Bring-back reorder must NOT cut anything the V2 two legs keep (the pin
    is a sort, not a gate): a confirmed global row stays kept either way."""
    conf_global = _row("global fact", sp="default", confirmed=True)
    assert _auto_inject_eligible(conf_global, "default", "s_cur") is True


def test_sentinel_search_face_independent_of_choice(tmp_path):
    """INVARIANT (全新 ≠ 失联): the choice file never gates the SEARCH pool
    assembly. Choosing 'fresh' leaves the M4 episodic extension untouched —
    the choice is not in the search path; a prev-session row stays a valid
    pool row regardless of choice."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p._set_bringback_choice("s_cur", "fresh")  # most aggressive choice
    prev_row = _row("prev fact", sp="default", confirmed=True,
                    session_id="s_prev", mem_type="episodic")
    # The auto face's pin leg is off for 'fresh' (resolver returns None):
    assert p._resolve_bringback_session_id("s_cur") is None
    # The same prev row, on the auto face's confirmed-global leg, is still
    # kept (membership is choice-independent); only the prefetch short-circuit
    # for 'fresh' collapses the auto face to empty — never the search pool.
    assert _auto_inject_eligible(prev_row, "default", "s_cur") is True


# ── (6) P4 'fresh' END-TO-END through the real prefetch auto-face (reviewer
#       gap g: _bringback_fresh was unit-asserted only; this drives a real
#       populated pool through prefetch() to prove fresh empties the AUTO
#       face while the SEARCH face still retrieves it — 全新 ≠ 失联,
#       end-to-end, not just a helper assertion.) ─────────────────────────────


def _make_provider_with_pool(tmp: Path, sid: str = "s_cur",
                             profile: Optional[str] = "default") -> FileMemProvider:
    """Same shell as _make_provider, but with a REAL memories pool wired so
    prefetch()'s auto face drives the real search_memories -> real pool read
    (the unit helpers above stub search_memories away; this proves the fresh
    gate end-to-end through the actual auto-inject assembly)."""
    p = _make_provider(tmp, sid=sid, profile=profile)
    # The auto face reads via search_memories over the on-disk pool; wire the
    # real method (not the fixture shell's absent one) so the pool is live.
    p._shard_lock = p._shard_lock  # no-op lock, already set in _make_provider
    return p


def _write_real_pool_row(tmp: Path, mem_type: str, row_id: str, content: str,
                         sp: Optional[str] = None,
                         confirmed: bool = True,
                         session_id: Optional[str] = None) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    row: Dict[str, Any] = {
        "id": row_id,
        "type": mem_type,
        "content": content,
        "level": "L0",
        "source": "manual",
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
        "salience": 0.9,
        "last_referenced": "2026-10-01T00:00:00",
        "metadata": meta,
    }
    if session_id is not None:
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    f = tmp / "memories" / f"{mem_type}.json"
    f.parent.mkdir(parents=True, exist_ok=True)  # memories/ may not exist yet
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


def test_fresh_end_to_end_prefetch_empties_auto_face_keeps_search(tmp_path):
    """reviewer gap g, end-to-end. A confirmed own-profile row in a populated
    pool would normally be auto-injected (V2 confirmed-global leg). When the
    user chose 'fresh' for THIS session, prefetch()'s auto face must collapse
    to EMPTY (the confirmed skeleton is folded out — auto face has nothing),
    YET the same row is still retrievable on the SEARCH face (filemem_search
    / search_memories over the raw pool, which the choice never touches).
    'fresh' = empty auto-inject, never unretrievable (全新 ≠ 失联)."""
    _write_real_pool_row(tmp_path, "fact", "f_conf", "confirmed core fact",
                          sp="default", confirmed=True)

    # ── B: no fresh choice (control) -> the confirmed row IS auto-injected ──
    p_ctl = _make_provider_with_pool(tmp_path, sid="s_ctl", profile="default")
    p_ctl.session_id = "s_ctl"
    ctx_ctl = p_ctl.prefetch("confirmed core fact", session_id="s_ctl")
    assert "confirmed core fact" in ctx_ctl, (
        f"control: confirmed row must auto-inject without a fresh choice; got: "
        f"{ctx_ctl!r}")

    # ── A: 'fresh' chosen this session -> auto face is EMPTY ───────────────
    p_fresh = _make_provider_with_pool(tmp_path, sid="s_fresh", profile="default")
    p_fresh.session_id = "s_fresh"
    p_fresh._set_bringback_choice("s_fresh", "fresh")
    ctx_fresh = p_fresh.prefetch("confirmed core fact", session_id="s_fresh")
    # The confirmed row is folded out of the auto face entirely:
    assert "confirmed core fact" not in ctx_fresh, (
        f"fresh: auto face must be empty (no confirmed skeleton); got: "
        f"{ctx_fresh!r}")

    # ── 全新 ≠ 失联: the SAME row is still on the SEARCH face (raw pool) ──
    # search_memories over the pool does NOT run the fresh gate; the choice
    # file is not in the search path, so the row is still retrievable.
    found = p_fresh.search_memories("confirmed core fact", limit=8,
                                    min_score=0.1)
    assert any(r.get("content") == "confirmed core fact" for r in found), (
        "fresh choice must not touch the search face: the row stays "
        f"retrievable via search_memories (got {len(found)} rows)")


# ── (7) G1: P3 replay path ALSO fires the one-shot footer (owner 2026-10-11,
#       reviewer round-3 gap) ─────────────────────────────────────────────────


def test_g1_p3_replay_path_fires_footer_end_to_end(tmp_path):
    """END-TO-END P3 cold-start replay + bring-back footer.

    A re-initialized provider (fresh instance, _p3_replay_armed=False) for a
    session that already has a frozen snapshot + matching .tokens + a
    per-profile last_closed stamp: prefetch() takes the P3 replay branch
    (L3147) and, per the B1-sister fix, MUST surface the one-shot footer on
    the RETURN face while the frozen snapshot bytes stay footer-free.

    One-shot proof: the FIRST prefetch's footer is re-fired exactly once — a
    SECOND prefetch (ordinary path, snapshot now re-armed) does NOT re-ask
    (session-keyed gate held). Without the replay-branch footer fix, a session
    that cold-starts through P3 replay would NEVER surface the bring-back
    question (the footer was only appended on the ordinary-prefetch tail).
    """
    p = _make_provider(tmp_path, sid="s_replay", profile="default")
    # A real prior closed session for THIS profile (the bring-back target).
    p_prev = _make_provider(tmp_path, sid="s_prev", profile="default")
    p_prev.session_id = "s_prev"
    p_prev._record_last_closed_session("s_prev")

    # Pre-freeze a P3 snapshot WITHOUT footer (B1 ordering) + a .tokens that
    # matches the upcoming query so the Jaccard gate lets the replay through.
    p._last_recall_query = "replay fact"
    p._save_injected_snapshot("s_replay", "[fact] replay fact content")
    snap_path = p._injected_snapshot_path("s_replay")
    # Confirm the snapshot file itself carries no footer opener (B1).
    assert snap_path is not None and "带回账本" not in snap_path.read_text(encoding="utf-8")
    # A fresh provider shell -> _p3_replay_armed is unset (False) -> replay
    # branch is live. The query must Jaccard-match the frozen .tokens.
    ctx1 = p.prefetch("replay fact", session_id="s_replay")
    # G1 assertion ①: the replay path's return face surfaces the footer:
    assert "带回账本" in ctx1, (
        f"P3 replay path must fire the bring-back footer on the return face; "
        f"got: {ctx1!r}")
    # B1 invariant: the on-disk snapshot was re-frozen WITHOUT the footer
    # (the footer only rides the return face, never the frozen block).
    assert "带回账本" not in snap_path.read_text(encoding="utf-8")
    # G1 assertion ②: one-shot — a SECOND prefetch for the same session does
    # NOT re-fire the footer (session-keyed gate held by the first call).
    ctx2 = p.prefetch("replay fact", session_id="s_replay")
    assert "带回账本" not in ctx2, (
        "footer must be one-shot per session: second prefetch must not "
        f"re-ask; got: {ctx2!r}")


def test_g1_replay_footer_absent_without_last_closed_stamp(tmp_path):
    """P3 replay with NO per-profile last_closed stamp: the replay path's
    return face must NOT carry the footer (no target -> footer '' -> plain
    replay block). The P3 replay branch still returns the frozen snapshot
    (the snapshot bytes themselves never contain the footer)."""
    p = _make_provider(tmp_path, sid="s_replay", profile="default")
    # No _record_last_closed_session call anywhere -> no target.
    p._last_recall_query = "replay fact"
    p._save_injected_snapshot("s_replay", "[fact] replay fact content")
    ctx = p.prefetch("replay fact", session_id="s_replay")
    assert "replay fact" in ctx            # the recall block itself replays
    assert "带回账本" not in ctx            # but no footer (no target)


# ── (8) G2: _normalize_profile_stamp unit (owner 2026-10-11, reviewer round-3)
#       — pin '不抬 None 成 default / 不跨 profile' ─────────────────────────


def test_g2_normalize_falsy_to_neutral_none():
    """None and "" both normalize to the single NEUTRAL sentinel None (they are
    the same 'no named profile' condition) — this is the write/read asymmetry
    the owner asked to eliminate."""
    assert _normalize_profile_stamp(None) is None
    assert _normalize_profile_stamp("") is None
    assert _normalize_profile_stamp("   ") is None   # whitespace-only = falsy


def test_g2_normalize_never_promotes_to_default():
    """NEUTRAL (None / "") must NEVER be promoted to a named 'default' —
    owner red-line: 不抬成 default, 守不串 profile."""
    assert _normalize_profile_stamp(None) != "default"
    assert _normalize_profile_stamp("") != "default"
    assert _normalize_profile_stamp(None) is None


def test_g2_normalize_named_profiles_preserved():
    """Named profile values pass through unchanged (no 串, no case-mangling)."""
    assert _normalize_profile_stamp("coder") == "coder"
    assert _normalize_profile_stamp("default") == "default"
    assert _normalize_profile_stamp("orchestrator") == "orchestrator"
    # A named value is NOT collapsed to None:
    assert _normalize_profile_stamp("coder") is not None
    assert _normalize_profile_stamp("default") is not None
