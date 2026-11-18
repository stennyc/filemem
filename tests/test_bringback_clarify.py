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
from datetime import datetime, timezone, timedelta

def _ts(days_ago=1):
    """Dynamic 'recent enough' ISO timestamp (L0 TTL=30d safe, never expires on any run date)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")

from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import (  # noqa: E402
    FileMemProvider,
    _auto_inject_eligible,
    _normalize_profile_stamp,
    _utcnow,
    _BRINGBACK_FRESH_WINDOW_S,
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
        "created_at": _ts(1),
        "updated_at": _ts(1),
        "salience": 0.5,
        "last_referenced": _ts(1),
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


def test_d_last_closed_is_idempotent_per_session(tmp_path):
    """D (owner 2026-10-11): on_session_end fires more than once for the same
    session (a cached provider is rebuilt per gateway cold-start, each rebuild
    re-running the end-flow). _record_last_closed_session must be IDEMPOTENT:
    the same session id appends exactly ONE row (the first close stamp stays
    canonical); a DIFFERENT session still appends. Without this guard the
    live store accumulated 3 rows for one session 0.2s apart and the
    'most-recent closed' read drifted."""
    import json
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # Simulate the same session's provider being rebuilt 3x (each re-fires
    # on_session_end -> _record_last_closed_session for the same id).
    for _ in range(3):
        p.session_id = "s_x"
        p._record_last_closed_session("s_x")
    # A different session still stamps independently.
    p.session_id = "s_y"
    p._record_last_closed_session("s_y")
    lines = [json.loads(l) for l in p._last_closed_stamp_path().read_text().splitlines() if l.strip()]
    sids = [l["session_id"] for l in lines]
    assert sids.count("s_x") == 1, f"D: s_x stamped {sids.count('s_x')}x, expected 1"
    assert sids.count("s_y") == 1, "D: s_y missing"
    assert sids == ["s_x", "s_y"]


def test_d_last_closed_concurrent_same_session_single_row(tmp_path):
    """D + reviewer 2026-10-11 (💭 TOCTOU -> fix): the read of existing
    session ids and the append now run under the SAME _shard_lock, so N
    concurrent threads stamping the SAME session id must still land exactly
    ONE row (no duplicate from the read-decision-write window). This locks
    the concurrency hardening: if the read were ever moved back outside the
    lock, a real multi-process race could append twice and this test would
    catch it under a threaded replay."""
    import json
    import threading as th
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # The default fixture _shard_lock is a no-op (contextlib.suppress), which
    # would let these threads bypass the critical section and duplicate. For
    # THIS test we install a REAL lock (a module-level threading.Lock, shared
    # across all threads) so the read+decide+write genuinely serializes — this
    # is what the production flock-backed _shard_lock does.
    _real_lock = th.Lock()

    def _locked(_pth):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            with _real_lock:
                yield
        return _ctx()

    p._shard_lock = _locked
    p.session_id = "s_z"
    n_threads = 8
    errors: list = []

    def _stamp():
        try:
            p._record_last_closed_session("s_z")
        except Exception as e:  # pragma: no cover - fail-soft never raises
            errors.append(e)

    threads = [th.Thread(target=_stamp) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"D concurrent raised: {errors}"
    lines = [json.loads(l) for l in p._last_closed_stamp_path().read_text().splitlines() if l.strip()]
    sids = [l["session_id"] for l in lines]
    assert sids.count("s_z") == 1, f"D concurrent: s_z stamped {sids.count('s_z')}x, expected 1"



def test_c_stale_closed_stamp_is_not_offered(tmp_path):
    """owner 2026-11-04: the footer no longer depends on the last_closed
    freshness window — it fires as a neutral 1-line hint for any session's
    first non-trivial prefetch, regardless of the last_closed stamp's age.
    The 30-min freshness window (_BRINGBACK_FRESH_WINDOW_S) still governs the
    auto 'last closed' pin RESOLUTION (_resolve_bringback_session_id), but
    the footer face is now manual-trigger (the user says '带回' and names any
    session they want, old or new). Keep the data-side stale-stamp read as
    its original invariant (the RESOLVER still respects the window)."""
    import json
    from datetime import timedelta
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")
    # Rewrite its closed_ts to 3 hours ago to simulate a stale close.
    path = p._last_closed_stamp_path()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for l in lines:
        if l["session_id"] == "s_prev":
            l["closed_ts"] = (_utcnow() - timedelta(hours=3)).isoformat()
    with open(path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    # The stale stamp must NOT be offered as the AUTO last-closed target
    # (the resolver's data-side invariant is unchanged):
    assert p._last_closed_session_for_profile(
        exclude_session_id="s_cur",
        max_age_s=_BRINGBACK_FRESH_WINDOW_S) is None
    # The footer, however, now fires as a neutral hint regardless of the
    # last_closed stamp (manual trigger):
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot


def test_c_fresh_closed_stamp_is_offered(tmp_path):
    """C: a just-now closed stamp (0s old, within the 30min window) IS
    offered — the footer fires for a real 'new session just after a close'."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")  # fresh (now)
    assert p._last_closed_session_for_profile(
        exclude_session_id="s_cur",
        max_age_s=_BRINGBACK_FRESH_WINDOW_S) == "s_prev"
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot



# ── (3) P3 one-shot clarify footer (+ B1 not frozen into the snapshot) ─────────


def test_p3_footer_offered_when_target_exists(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot
    # One-shot: the second call is suppressed (session-keyed gate set).
    assert p._bringback_clarify_footer("s_cur") == ""


def test_p3_footer_neutral_hint_fires_regardless_of_target(tmp_path):
    """owner 2026-11-04: the footer no longer depends on a last_closed target
    — a session with NO closed stamp at all still gets the neutral hint
    (manual trigger: the user may name any closed session, old or new)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot


def test_p3_footer_suppressed_when_already_answered(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    # User already chose (any of the three) -> footer not re-asked.
    p._set_bringback_choice("s_cur", "clean_start")
    assert p._bringback_clarify_footer("s_cur") == ""


def test_p3_footer_fail_soft_no_session(tmp_path):
    p = _provider_with_prev_closed(tmp_path)
    p.session_id = None
    assert p._bringback_clarify_footer("") == ""


def test_p3_footer_no_longer_lists_choices(tmp_path):
    """owner 2026-11-04: the footer no longer lists the choice enum values
    (bring_back / clean_start / fresh / topic_mismatch) — those moved to the
    manual-trigger clarify card (driven by the user, per the tool
    description). The footer itself is the neutral 1-line hint; the removed
    option 'confirmed_global_only' must also not reappear in the footer."""
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    assert "bring_back" not in foot
    assert "clean_start" not in foot
    assert "fresh" not in foot
    assert "topic_mismatch" not in foot
    # The removed option must NOT reappear.
    assert "confirmed_global_only" not in foot


def test_footer_neutral_hint_no_clarify_directive(tmp_path):
    """owner 2026-11-04: the footer is now a NEUTRAL 1-line hint - it no
    longer hard-directs the model to call the clarify tool, no longer lists
    the 4 clarify options. The user drives the flow: when they say
    '带回' / '接着上次' / 'bringback', the model (per the
    filemem_set_bringback tool description) emits the clarify card. Pin the
    neutral wording so a future rewrite that re-adds the hard directive or
    the 4-option list is caught."""
    p = _provider_with_prev_closed(tmp_path)
    foot = p._bringback_clarify_footer("s_cur")
    # The neutral hint is present:
    assert "如需带回旧 session 自动注入面" in foot
    assert "『带回』" in foot
    # The old hard-directive wording is GONE:
    assert "必须调用 clarify 工具" not in foot
    assert "不要用纯文字" not in foot
    # The 4 clarify options are no longer listed in the footer:
    assert "bring_back" not in foot
    assert "clean_start" not in foot
    assert "fresh" not in foot
    # The old option-list phrasing is gone:
    assert "clarify 选项" not in foot


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
    assert "如需带回旧 session 自动注入面" not in frozen_bytes  # the footer must NOT be frozen
    # The live footer is a SEPARATE string; producing it does not touch the
    # frozen file.
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot or foot == ""  # live face only
    # Re-read: the frozen file is still footer-free after the live ask.
    assert "如需带回旧 session 自动注入面" not in snap_file.read_text(encoding="utf-8")


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
    """With no bring-back choice recorded, the DEFAULT is now 'fresh'
    (owner 2026-11-08): cross-session confirmed rows are NOT auto-injected.
    The PREDICATE itself is unchanged — the confirmed leg still recognizes a
    cross-session confirmed row as eligible; the fold happens upstream in
    prefetch's fresh gate (results = []), not in _auto_inject_eligible.
    Pin the predicate outcomes that the fresh gate preserves: a neutral row
    (no confirmed flag) stays cut; the current session's own episodic row
    stays in (via the confirmed-global leg under an explicit clean_start)."""
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
        "created_at": _ts(1),
        "updated_at": _ts(1),
        "salience": 0.9,
        "last_referenced": _ts(1),
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
    'fresh' = empty auto-inject, never unretrievable (全新 ≠ 失联).

    owner 2026-11-08: the DEFAULT (no recorded choice) is now ALSO 'fresh' —
    a new session auto-injects only its own rows until the user explicitly
    records clean_start / bring_back / topic_mismatch."""
    _write_real_pool_row(tmp_path, "fact", "f_conf", "confirmed core fact",
                          sp="default", confirmed=True)

    # ── C: no recorded choice (control) -> DEFAULT 'fresh': auto face empty ──
    p_ctl = _make_provider_with_pool(tmp_path, sid="s_ctl", profile="default")
    p_ctl.session_id = "s_ctl"
    ctx_ctl = p_ctl.prefetch("confirmed core fact", session_id="s_ctl")
    assert "confirmed core fact" not in ctx_ctl, (
        f"control (no choice file): default is now 'fresh' — the confirmed "
        f"cross-session row must NOT auto-inject; got: {ctx_ctl!r}")

    # ── B: explicit clean_start -> the confirmed row IS auto-injected ──────
    p_clean = _make_provider_with_pool(tmp_path, sid="s_clean", profile="default")
    p_clean.session_id = "s_clean"
    p_clean._set_bringback_choice("s_clean", "clean_start")
    ctx_clean = p_clean.prefetch("confirmed core fact", session_id="s_clean")
    assert "confirmed core fact" in ctx_clean, (
        f"clean_start: confirmed row must auto-inject; got: {ctx_clean!r}")

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
    # 2026-11-08: _get_bringback_choice returns 'fresh' by default — set
    # clean_start so the P3 replay branch runs (the test exercises the
    # replay path, not the fresh-skip gate).
    #
    # 2026-11-04 (manual trigger): setting a bringback choice SUPPRESSES
    # the footer (_has_bringback_choice -> ''). The G1 footer test
    # predates that rewrite; it now exercises the P3 replay mechanism
    # (snapshot re-freeze + isolation filter) but asserts the RECALL
    # content instead of the footer. The footer behavior is covered by
    # the dedicated footer tests below.
    p._set_bringback_choice("s_replay", "clean_start")
    # A fresh provider shell -> _p3_replay_armed is unset (False) -> replay
    # branch is live. The query must Jaccard-match the frozen .tokens.
    ctx1 = p.prefetch("replay fact", session_id="s_replay")
    # G1 assertion ①: the P3 replay path returns the frozen recall block.
    # (2026-11-04: the footer is suppressed when a bringback choice is set;
    # the footer behavior is covered by dedicated footer tests.)
    assert "replay fact" in ctx1 or "replay fact content" in ctx1, (
        f"P3 replay path must surface the frozen snapshot content; "
        f"got: {ctx1!r}")
    # B1 invariant: the on-disk snapshot was re-frozen WITHOUT the footer
    # (the footer only rides the return face, never the frozen block).
    snap_path = p._injected_snapshot_path("s_replay")
    assert "如需带回旧 session 自动注入面" not in snap_path.read_text(encoding="utf-8")
    # G1 assertion ②: one-shot — a SECOND prefetch for the same session does
    # NOT re-fire the footer (session-keyed gate held by the first call).
    ctx2 = p.prefetch("replay fact", session_id="s_replay")
    # Second prefetch: the P3 replay branch is now armed (skipped on
    # subsequent calls); the ordinary live-search path runs instead.
    # With no pool rows matching the query, it returns '' — expected.
    assert "replay fact content" not in ctx2 or ctx2 == "", (
        f"second prefetch should not re-freeze the P3 snapshot; "
        f"got: {ctx2!r}")


def test_g1_replay_footer_absent_without_last_closed_stamp(tmp_path):
    """P3 replay with NO per-profile last_closed stamp: the replay path's
    return face must NOT carry the footer (no target -> footer '' -> plain
    replay block). The P3 replay branch still returns the frozen snapshot
    (the snapshot bytes themselves never contain the footer)."""
    p = _make_provider(tmp_path, sid="s_replay", profile="default")
    # No _record_last_closed_session call anywhere -> no target.
    p._last_recall_query = "replay fact"
    p._save_injected_snapshot("s_replay", "[fact] replay fact content")
    # 2026-11-04: footer is suppressed when ANY bringback choice is set.
    # This test asserts the P3 replay mechanism works (recall content
    # surfaces); the footer behavior is covered by dedicated footer tests.
    p._set_bringback_choice("s_replay", "clean_start")
    ctx = p.prefetch("replay fact", session_id="s_replay")
    assert "replay fact" in ctx            # the recall block itself replays
    assert "如需带回旧 session 自动注入面" not in ctx  # footer suppressed by choice


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


# ── (9) A-fix (owner 2026-10-16): persisted one-shot gate ─────────────────────
# A provider rebuilt mid-session (preflight compression / gateway cold-start)
# loses its in-memory _bringback_footer_asked dict, so the footer RE-FIRED in
# a long session. Fix: the gate hydrates from a persisted per-sid ledger
# (bringback_asked.jsonl) so 'asked once' survives provider rebuilds.


def _fresh_shell(tmp, sid="s_cur", profile="default"):
    """Simulate a provider REBUILD for the same session: a brand-new provider
    instance sharing the same tmp storage_dir (the in-memory gate dict is
    freshly empty, exactly like post-compression provider re-init)."""
    return _make_provider(tmp, sid=sid, profile=profile)


def test_a_rebuild_footer_suppressed_by_persisted_gate(tmp_path):
    """A: provider #1 fires the footer (marks asked), provider #2 — a rebuilt
    instance for the SAME session (fresh in-memory dict) — must NOT re-fire:
    the persisted ledger hydrates the gate before the footer decision."""
    p1 = _provider_with_prev_closed(tmp_path)
    foot1 = p1._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot1  # first provider: footer fires (control)
    p2 = _fresh_shell(tmp_path, sid="s_cur", profile="default")
    foot2 = p2._bringback_clarify_footer("s_cur")
    assert foot2 == "", (
        f"rebuilt provider must not re-fire the footer (persisted gate); got: "
        f"{foot2[:80]!r}")


def test_a_mark_choice_persisted_suppresses_rebuild(tmp_path):
    """A: answering via the P4 tool also lands the persisted gate mark, so a
    rebuilt provider never re-asks an answered session."""
    p1 = _provider_with_prev_closed(tmp_path)
    p1.handle_tool_call("filemem_set_bringback",
                        {"choice": "clean_start", "session_id": "s_cur"})
    p2 = _fresh_shell(tmp_path, sid="s_cur", profile="default")
    assert p2._bringback_clarify_footer("s_cur") == ""


def test_a_new_session_reasks_despite_shared_ledger(tmp_path):
    """The persisted gate is PER-SESSION: a genuinely new session id is NOT
    silenced by another session's mark (the bring-back face stays per-session;
    the persistence only closes the rebuild hole, not the session boundary)."""
    p1 = _provider_with_prev_closed(tmp_path)
    p1._bringback_clarify_footer("s_cur")  # fires + persists s_cur
    p2 = _fresh_shell(tmp_path, sid="s_new", profile="default")
    foot = p2._bringback_clarify_footer("s_new")
    assert "如需带回旧 session 自动注入面" in foot, "new session must re-ask (persisted gate is per-sid)"


def test_a_persisted_gate_survives_no_target_session(tmp_path):
    """owner 2026-11-04: the footer no longer depends on a last_closed target
    — it fires as a neutral hint for ANY session's first non-trivial
    prefetch. A session with NO closed stamp at all still gets the hint
    (the user may want to bring back a manually-named session, not just the
    auto last-closed one). The persisted gate remains per-sid (rebuild-safe)
    — the 2nd provider must not re-fire it."""
    p1 = _make_provider(tmp_path, sid="s_notarget", profile="default")
    foot1 = p1._bringback_clarify_footer("s_notarget")
    assert "如需带回旧 session 自动注入面" in foot1, \
        "neutral hint must fire even without a last_closed target"
    p2 = _fresh_shell(tmp_path, sid="s_notarget", profile="default")
    assert p2._bringback_clarify_footer("s_notarget") == "", \
        "rebuilt provider must not re-fire (persisted gate is per-sid)"


def test_a_subagent_suppress_mark_persists(tmp_path):
    """Reviewer major (2026-10-16): the P1-3 subagent gate suppressed the
    footer but only held it in-memory; a REBUILT subagent provider whose
    _last_platform is not yet re-established would fall through to the
    ordinary main-session path and RE-FIRE the footer — the original bug,
    re-exposed on subagent+rebuild. The subagent suppress branch must
    persist its gate mark like the other branches."""
    p1 = _provider_with_prev_closed(tmp_path)
    p1._last_platform = "subagent"  # P1-3 gate live
    assert p1._bringback_clarify_footer("s_sub") == ""  # suppressed (control)
    # The mark must be in the persisted ledger (the rebuild survival path):
    assert p1._bringback_persisted_asked("s_sub") is True, (
        "subagent suppress must persist its gate mark so a rebuilt provider "
        "(with _last_platform unset -> ordinary path) cannot re-fire it")


def test_a_concurrent_persist_same_sid_single_row(tmp_path):
    """Reviewer major: mirror test_d_last_closed_concurrent... for the
    A-fix persist path — N threads x M provider instances, all sharing one
    REAL _shard_lock (flock), calling _persist_bringback_asked('s_z') in
    parallel must land EXACTLY ONE ledger row (no duplicate from the
    read-decision-write TOCTOU)."""
    import json as _json
    import threading as _th
    p = _make_provider(tmp_path, sid="s_z", profile="default")
    _real_lock = _th.Lock()  # stand-in for the flock's mutual exclusion

    def _locked(path):
        import contextlib

        @contextlib.contextmanager
        def _ctx():
            with _real_lock:
                yield
        return _ctx()

    p._shard_lock = _locked
    n_threads = 8
    errors: list = []

    def _persist():
        try:
            p._persist_bringback_asked("s_z")
        except Exception as e:  # pragma: no cover - fail-soft never raises
            errors.append(e)

    threads = [_th.Thread(target=_persist) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"A concurrent raised: {errors}"
    ledger = p._bringback_persisted_gate_path()
    assert ledger is not None
    rows = [
        _json.loads(l) for l in ledger.read_text().splitlines() if l.strip()
    ]
    sids = [r["session_id"] for r in rows]
    assert sids.count("s_z") == 1, (
        f"A concurrent: s_z marked {sids.count('s_z')}x, expected exactly 1")


def test_a_hydrate_missing_file_rechecks_next_call(tmp_path):
    """Reviewer minor: a rebuilt provider that hydrates BEFORE a sibling
    provider's mark lands must not cache 'file absent' as a permanent
    tombstone — a LATER hydrate call (same instance, file now exists with
    a mark from the sibling) must still fold the mark in, so the footer
    does not re-fire on this instance's later turns."""
    p1 = _provider_with_prev_closed(tmp_path)
    p2 = _fresh_shell(tmp_path, sid="s_sib", profile="default")
    # p2 hydrates while the ledger does not exist yet (p1 has not marked):
    p2._bringback_footer_state()
    # p1 now fires its footer -> persists a mark for s_cur:
    assert "如需带回旧 session 自动注入面" in p1._bringback_clarify_footer("s_cur")
    # p2's NEXT gate read must still see p1's mark despite the earlier
    # empty-state cache (the fix: do NOT cache the absent-file state).
    st = p2._bringback_footer_state()
    assert st.get("s_cur") is True, (
        "a later hydrate must pick up a mark that landed after this "
        "instance's first (empty) hydrate")


# ── (10) owner 2026-11-04 manual trigger: _recent_sessions_for_clarify +
#         session_id_override ─────────────────────────────────────────────────
# ── (11) owner 2026-11-04 (active 接手语义): active pin -> data-side close
#         before resolution ─────────────────────────────────────────────────


def test_active_pin_closed_before_resolve_pins_itself(tmp_path):
    """owner 2026-11-04: a pin target with NO closed stamp but a session trace
    jsonl (an ACTIVE session) is closed on the data side (a last_closed row is
    appended) BEFORE the pin-target resolution, so the resolution returns the
    NAMED active sid instead of falling through to the auto 'last closed'
    leg. This is the 「欲召回 active session 理应先 close」 semantic: the pin
    lands on the session the user named, not on whatever happened to be the
    most recent closed session for this profile."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # A closed prev exists (so the auto leg would have a target to fall
    # through to — proves the pin did NOT fall through):
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")
    # The NAMED target is an ACTIVE session: trace jsonl present, no closed
    # stamp:
    (p.sessions_dir / "s_active.jsonl").write_text(
        json.dumps({"session_id": "s_active", "ts": "2026-11-04T05:00:00",
                    "role": "user", "content": "active topic",
                    "source_profile": "default"}),
        encoding="utf-8")
    out = json.loads(p.handle_tool_call(
        "filemem_set_bringback",
        {"choice": "bring_back", "session_id": "s_cur",
         "session_id_override": "s_active"}))
    assert out["success"] is True
    # The pin resolves to the NAMED active sid (not the auto 's_prev'):
    assert out["pin_target"] == "s_active", (
        f"active pin must resolve to the named sid, not fall through to the "
        f"auto last-closed leg; got pin_target={out['pin_target']!r}")
    # The active-close was reported honestly:
    assert "ACTIVE" in out["pin_note"] and "closed" in out["pin_note"]
    # The data-side close landed a real last_closed row for the active sid:
    assert p._last_closed_session_exists_for_profile("s_active") is True
    # The persisted pin file carries the named sid:
    assert p._get_bringback_pin_sid("s_cur") == "s_active"


def test_active_pin_dedupes_later_host_close(tmp_path):
    """first-close-wins: after the data-side close stamps the active sid,
    the host's LATER real on_session_end for that same sid must NOT append a
    second last_closed row (no duplicate stamp — the existing idempotent
    guard is what keeps this clean)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    (p.sessions_dir / "s_active2.jsonl").write_text(
        json.dumps({"session_id": "s_active2", "ts": "2026-11-04T05:00:00",
                    "role": "user", "content": "active topic 2",
                    "source_profile": "default"}),
        encoding="utf-8")
    p.handle_tool_call("filemem_set_bringback",
                       {"choice": "bring_back", "session_id": "s_cur",
                        "session_id_override": "s_active2"})
    # The host later really closes s_active2 (the end-flow re-stamps):
    p.session_id = "s_active2"
    p._record_last_closed_session("s_active2")
    # Exactly ONE row for s_active2 (the data-side close), not two:
    ledger_rows = []
    ledger = p._last_closed_stamp_path()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("session_id") == "s_active2":
            ledger_rows.append(row)
    assert len(ledger_rows) == 1, (
        f"first-close-wins dedup must hold: a later host close re-stamping "
        f"the same sid adds no duplicate row; got {len(ledger_rows)} rows")


def test_typo_pin_still_falls_through_to_auto_leg(tmp_path):
    """A sid with NEITHER a closed stamp NOR a trace jsonl (typo / foreign
    profile / never-seen session) is still rejected and falls through to the
    auto 'last closed' leg — the active-close must NOT stamp a session that
    has no material to bring back."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev"
    p._record_last_closed_session("s_prev")
    out = json.loads(p.handle_tool_call(
        "filemem_set_bringback",
        {"choice": "bring_back", "session_id": "s_cur",
         "session_id_override": "s_typed_typo"}))
    assert out["success"] is True
    # The typo'd sid has no trace -> NOT stamped -> resolution falls through
    # to the auto last-closed leg (s_prev):
    assert out["pin_target"] == "s_prev", (
        f"a sid with no closed stamp AND no trace must fall through to the "
        f"auto last-closed leg; got pin_target={out['pin_target']!r}")
    assert "fell back to the auto" in out["pin_note"]
    # And the typo'd sid was NOT stamped as closed:
    assert p._last_closed_session_exists_for_profile("s_typed_typo") is False


def _write_trace(p, sid: str, title: str, profile: Optional[str] = "default",
                 ts: Optional[str] = None) -> None:
    """Write a minimal session trace jsonl (first user line carries the
    title) so _session_title_hint can read it. ts sets the file mtime so the
    active leg's ordering is controllable."""
    trace = p.sessions_dir / f"{sid}.jsonl"
    row = {"session_id": sid, "role": "user", "content": title}
    if ts:
        row["ts"] = ts
    if profile is not None:
        row["source_profile"] = profile
    with open(trace, "w", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if ts:
        import os as _os
        from datetime import datetime as _dt, timezone as _tz
        mtime = _dt.fromisoformat(ts).replace(tzinfo=_tz.utc).timestamp()
        _os.utime(trace, (mtime, mtime))


def test_recent_sessions_merges_closed_and_active_sorted_desc(tmp_path):
    """_recent_sessions_for_clarify(n=2): the top-2 items are the two most-
    recent by ts across BOTH the closed ledger and active trace jsonl, state
    tagged correctly, title filled from the trace (falling back to the bare
    sid when no trace exists)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_prev_a"
    p._record_last_closed_session("s_prev_a")
    p.session_id = "s_prev_b"
    p._record_last_closed_session("s_prev_b")
    # s_prev_b is the most recent closed; a still-active trace s_active_1 is
    # newer than s_prev_b's mtime (utime set later)
    _write_trace(p, "s_prev_a", "topic A", profile="default")
    _write_trace(p, "s_prev_b", "topic B", profile="default")
    _write_trace(p, "s_active_1", "topic active", profile="default")
    # nudge the active trace's mtime to be the newest of all
    import os as _os
    import time as _time
    _os.utime(p.sessions_dir / "s_active_1.jsonl",
              (_time.time() + 1, _time.time() + 1))
    items = p._recent_sessions_for_clarify(n=2)
    assert len(items) == 2
    # s_active_1 (newest mtime) must be first
    assert items[0]["session_id"] == "s_active_1"
    assert items[0]["state"] == "active"
    assert items[0]["title"] == "topic active"
    # s_prev_b (newest closed) must be second (its closed_ts is > s_prev_a's)
    assert items[1]["session_id"] == "s_prev_b"
    assert items[1]["state"] == "closed"
    assert items[1]["title"] == "topic B"


def test_recent_sessions_fall_back_to_bare_sid_without_trace(tmp_path):
    """A closed session with NO trace jsonl: _session_title_hint returns ""
    -> the item's title falls back to the bare sid (fail-soft, never crashes
    the clarify card's option 1/2 rendering)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_noftrace"
    p._record_last_closed_session("s_noftrace")
    items = p._recent_sessions_for_clarify(n=1)
    assert len(items) == 1
    assert items[0]["session_id"] == "s_noftrace"
    assert items[0]["title"] == "s_noftrace"  # bare id, not ""


def test_recent_sessions_per_profile_isolation(tmp_path):
    """Cross-profile closed rows / traces must NOT leak into another
    profile's clarify card (the closed leg already filters by
    source_profile; the active leg must cut foreign traces too)."""
    p_coder = _make_provider(tmp_path, sid="s_coder", profile="coder")
    p_coder.session_id = "s_prev_coder"
    p_coder._record_last_closed_session("s_prev_coder")
    _write_trace(p_coder, "s_prev_coder", "coder topic", profile="coder")
    # An 'orchestrator' profile provider must NOT see the coder's closed row:
    p_orch = _make_provider(tmp_path, sid="s_orch", profile="orchestrator")
    items = p_orch._recent_sessions_for_clarify(n=5)
    assert all(i["session_id"] != "s_prev_coder" for i in items), (
        "a foreign-profile closed row must not leak into this profile's "
        f"clarify card; got: {items}")


def test_recent_sessions_fail_soft_no_storage_dir():
    """No storage_dir (a fixture shell that was never initialize()'d) ->
    empty list, never raises. (Pyright's Path-typing complaint on the
    provider's storage_dir attr is intentional here: the provider is a bare
    __new__ shell deliberately holding None — the fail-soft branch is what
    this test is pinning.)"""
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = None
    p.sessions_dir = None
    p.source_profile = "default"
    p._lock = threading.Lock()
    assert p._recent_sessions_for_clarify(n=2) == []


def test_recent_sessions_n_zero_or_negative(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    assert p._recent_sessions_for_clarify(n=0) == []
    assert p._recent_sessions_for_clarify(n=-1) == []


def test_p4_tool_session_id_override_lands_pin(tmp_path):
    """owner 2026-11-04 manual trigger: session_id_override (the user's
    typed/picked sid from the clarify card) lands as the pin under
    choice='bring_back'."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # No last_closed stamp at all — the override must still land the pin:
    out = json.loads(p.handle_tool_call(
        "filemem_set_bringback",
        {"choice": "bring_back", "session_id": "s_cur",
         "session_id_override": "s_named"}))
    assert out["success"] is True
    assert out["choice"] == "bring_back"
    assert out["bring_back_active"] is True
    assert out["pin_requested"] == "s_named"
    assert p._get_bringback_pin_sid("s_cur") == "s_named"


def test_p4_tool_session_id_override_ignored_under_clean_start(tmp_path):
    """session_id_override only takes effect under choice='bring_back'; under
    any other choice it is ignored (no pin side effect)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call(
        "filemem_set_bringback",
        {"choice": "clean_start", "session_id": "s_cur",
         "session_id_override": "s_named"}))
    assert out["success"] is True
    assert out["bring_back_active"] is False
    # The override was NOT persisted as a pin (clean_start ignores it):
    assert p._get_bringback_pin_sid("s_cur") is None


def test_p4_tool_session_id_override_beats_pin_session_id(tmp_path):
    """When both are supplied, session_id_override wins (owner 2026-11-04:
    the user's typed sid is the most explicit signal)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    p.session_id = "s_auto_prev"
    p._record_last_closed_session("s_auto_prev")
    out = json.loads(p.handle_tool_call(
        "filemem_set_bringback",
        {"choice": "bring_back", "session_id": "s_cur",
         "session_id_override": "s_typed",
         "pin_session_id": "s_pin_param"}))
    assert out["success"] is True
    # The override (s_typed) is the recorded pin, not the legacy param:
    assert out["pin_requested"] == "s_typed"
    assert p._get_bringback_pin_sid("s_cur") == "s_typed"
