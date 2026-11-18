"""HANDOFF face 2 + face 3 (specs/HANDOFF-PRD.md, 2026-10-17 owner direction).

Face 2: ``filemem_set_bringback`` gains an optional ``pin_session_id`` — the
user names a SPECIFIC closed session to pin (its confirmed rows go to the TOP
of this session's auto-inject face, a pure reorder, B2-A invariant unchanged).
Owner-locked semantics:
  - an explicit pin BYPASSES the 30-min freshness window (the window stays on
    the AUTO last-closed leg only — it guards against auto-reviving stale
    closes; a deliberate "bring back the session I named" is full authority);
  - a pin whose closed-stamp row doesn't exist (typo / never closed /
    foreign profile) is NOT an error — it falls through to the auto leg
    (honest pin_target report), preserving some continuity instead of zeroing.

Face 3 (light, owner 裁决 A): when the last-closed session carries a handoff
artifact (handoffs/<sid>.json, written by face 1), the bring-back footer gains
one line announcing it — the note rides the existing clarify card, no new
option, no acknowledgement lifecycle (that's face B, a later round).

P-series + F-series coverage:
  P1: valid pin (last_closed row present, this profile) -> resolver returns
      the PINNED sid even when its closed_ts is STALE (>30min window — the
      explicit-authority bypass).
  P2: end-to-end prefetch: the pinned session's confirmed rows sit at the TOP
      of the auto face (reorder sentinel); the row SET is unchanged (B2-A:
      no new rows, no dropped rows); the search face M4 is untouched.
  P3: invalid pin (no last_closed row for that sid) -> fall through to the
      auto last-closed target; the tool reports the fallback honestly.
      Invalid pin on a no-stamp provider -> pin_target None (no crash).
  P4: choice != bring_back + pin param -> pin ignored (resolver None, no
      pinning side effects).
  P5: no pin param -> byte-identical to pre-pin behavior (the existing
      test_bringback_clarify P4 suite stays green — regression sentinel).
  P6: a pin from a FOREIGN source_profile is not honored (profile isolation,
      same gate as the last_closed read) -> fall through to auto.
  F1: last-closed session HAS a handoff artifact -> the footer mentions it
      (the "留有交接摘要" line); choosing bring_back pins it (the note's
      source sid becomes the pin target).
  F2: no handoff artifact -> footer text is byte-identical to today's
      (regression sentinel).
"""
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta


def _ts(days_ago=1):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import (  # noqa: E402
    FileMemProvider,
    _utcnow,
)


# ── helpers (same fixture shell as test_bringback_clarify) ───────────────────


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
    p.hermetis_home = tmp
    import contextlib
    p._shard_lock = lambda pth, *a, **kw: contextlib.suppress()
    return p


def _close(tmp_path, sid: str, profile="default"):
    p = _make_provider(tmp_path, sid=sid, profile=profile)
    p.session_id = sid
    p._record_last_closed_session(sid)
    return p


def _stale_close(tmp_path, sid: str, hours: int = 3):
    """A last_closed row whose closed_ts is pushed into the past (outside
    the 30-min freshness window) — the 'deadlocked session from days ago'
    case that the explicit pin must reach."""
    from datetime import timedelta
    p = _close(tmp_path, sid)
    path = p._last_closed_stamp_path()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for l in lines:
        if l["session_id"] == sid:
            l["closed_ts"] = (_utcnow() - timedelta(hours=hours)).isoformat()
    with open(path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    return p


def _write_pool_row(tmp: Path, mem_type: str, row_id: str, content: str,
                    sp: Optional[str] = None, confirmed: bool = True,
                    session_id: Optional[str] = None):
    meta: Dict[str, Any] = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    row: Dict[str, Any] = {
        "id": row_id, "type": mem_type, "content": content,
        "level": "L0", "source": "manual",
        "created_at": _ts(1),
        "updated_at": _ts(1),
        "salience": 0.9, "last_referenced": _ts(1),
        "metadata": meta,
    }
    if session_id is not None:
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    f = tmp / "memories" / f"{mem_type}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


def _set_pin(tmp_path, sid: str, pin: Optional[str],
             profile="default") -> FileMemProvider:
    """Record the bring-back choice + optional explicit pin for a session."""
    p = _make_provider(tmp_path, sid=sid, profile=profile)
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": sid,
        **({"pin_session_id": pin} if pin else {})}))
    assert out["success"] is True, out
    return p


# ── P1: explicit pin bypasses the freshness window ──────────────────────────


def test_p1_explicit_pin_bypasses_stale_window(tmp_path):
    _stale_close(tmp_path, "s_stale")          # closed 3h ago (outside window)
    _close(tmp_path, "s_fresh")                # closed now (inside window)
    p = _set_pin(tmp_path, "s_cur", "s_stale")
    # The AUTO leg would pick s_fresh (the only in-window close); the
    # explicit pin overrides it AND the stale window:
    assert p._resolve_bringback_session_id("s_cur") == "s_stale"


def test_p1b_pin_stale_target_reports_pin_not_auto(tmp_path):
    _stale_close(tmp_path, "s_stale")
    p = _set_pin(tmp_path, "s_cur", "s_stale")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur",
        "pin_session_id": "s_stale"}))
    assert out["pin_target"] == "s_stale", out


# ── P2: end-to-end reorder through the real prefetch auto face ──────────────


def test_p2_end_to_end_pin_reorder_top_and_set_unchanged(tmp_path):
    _stale_close(tmp_path, "s_old")
    _close(tmp_path, "s_fresh")
    _write_pool_row(tmp_path, "fact", "f_pinned", "pinned session fact",
                    sp="default", confirmed=True, session_id="s_old")
    _write_pool_row(tmp_path, "fact", "f_other", "other confirmed fact",
                    sp="default", confirmed=True, session_id="s_fresh")
    # owner 2026-11-08: a named pin (session_id_override) under bring_back
    # carries FULL authority — it must bypass the no-choice 'fresh' default
    # (which otherwise folds the whole auto face) and re-order the pinned
    # session's confirmed rows to the TOP of the auto face, NOT collect new
    # rows (the set is unchanged from the confirmed-global leg).
    p = _set_pin(tmp_path, "s_cur", "s_old")
    p.session_id = "s_cur"
    ctx = p.prefetch("pinned session fact", session_id="s_cur")
    assert "pinned session fact" in ctx, (
        f"the pinned session's confirmed row must auto-inject under a named "
        f"bring_back pin (pin authority bypasses the fresh default); got: "
        f"{ctx!r}")
    # B2-A sentinel: reorder, not collect — the row SET is unchanged:
    assert "other confirmed fact" in ctx, (
        "pinning must NOT drop the other confirmed global rows")
    # The pin is a TOP-of-face reorder:
    assert ctx.index("pinned session fact") < ctx.index("other confirmed fact"), \
        "the pinned session's confirmed rows must sort ahead of the rest"
    # Search face M4 is untouched by the pin:
    found = p.search_memories("other confirmed fact", limit=8, min_score=0.1)
    assert any(r.get("content") == "other confirmed fact" for r in found)


# ── P3: invalid pin -> honest fall-through to the auto leg ──────────────────


def test_p3_invalid_pin_falls_through_to_auto(tmp_path):
    _close(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur",
        "pin_session_id": "s_typo"}))
    assert out["success"] is True, out
    # Honest report: the requested pin didn't land, the auto target did:
    assert out["pin_target"] == "s_fresh", out
    assert out["pin_requested"] == "s_typo", out
    assert out.get("pin_note"), out
    assert "s_typo" in out["pin_note"], (
        "the tool must report which pin was requested + why it fell "
        "through (audit-visible)")
    assert p._resolve_bringback_session_id("s_cur") == "s_fresh"


def test_p3b_invalid_pin_no_stamps_reports_none(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur",
        "pin_session_id": "s_ghost"}))
    assert out["success"] is True, out
    assert out["pin_target"] is None, out
    assert p._resolve_bringback_session_id("s_cur") is None


# ── P4: pin is only honored under bring_back ─────────────────────────────────


def test_p4_pin_ignored_outside_bring_back(tmp_path):
    _close(tmp_path, "s_fresh")
    for choice in ("clean_start", "fresh", "topic_mismatch"):
        p = _make_provider(tmp_path, sid=f"s_{choice}", profile="default")
        out = json.loads(p.handle_tool_call("filemem_set_bringback", {
            "choice": choice, "session_id": f"s_{choice}",
            "pin_session_id": "s_fresh"}))
        assert out["success"] is True, out
        assert out["pin_target"] is None, (
            f"{choice} must ignore the pin (pin only pins under bring_back)")
        assert p._resolve_bringback_session_id(f"s_{choice}") is None


# ── P5: no pin param -> byte-identical to today's behavior ──────────────────


def test_p5_no_pin_param_is_today_behavior(tmp_path):
    """Regression sentinel: omitting pin_session_id must resolve exactly as
    the pre-pin code did (auto last-closed, in-window only)."""
    _stale_close(tmp_path, "s_stale")   # would be chosen pre-window era, but
    _close(tmp_path, "s_fresh")         # today's auto leg is window-gated
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur"}))
    assert out["success"] is True, out
    assert out["pin_target"] == "s_fresh", out


# ── P6: foreign-profile pin is not honored (不串 profile) ────────────────────


def test_p6_foreign_profile_pin_falls_through(tmp_path):
    _close(tmp_path, "s_coder_session", profile="coder")
    _close(tmp_path, "s_fresh", profile="default")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur",
        "pin_session_id": "s_coder_session"}))
    # The pin row is stamped source_profile=coder; a default-profile provider
    # must not pin it -> fall through to the auto leg (s_fresh):
    assert out["pin_target"] == "s_fresh", out


# ── F1: the footer announces a last-closed handoff artifact ────────────────


def _write_handoff_artifact(tmp_path, sid: str, profile="default"):
    hd = tmp_path / "handoffs"
    hd.mkdir(parents=True, exist_ok=True)
    (hd / f"{sid}.json").write_text(json.dumps({
        "source_session_id": sid, "generated_by_session": "s_gen",
        "generated_ts": "2026-10-17T00:00:00",
        "materials": ["compact.json"],
        "summary": "HANDOFF: 任务 X 进行中；下一步 = 验证 Y",
        "llm_used": True}, ensure_ascii=False), encoding="utf-8")


def test_f1_footer_no_longer_announces_last_closed_handoff(tmp_path):
    """owner 2026-11-04 (manual trigger): the footer no longer announces the
    last-closed handoff artifact — that interpolation was part of the old
    hard-directive clarify card, which moved to the user-driven manual
    trigger. The footer is now the byte-stable neutral 1-line hint. The
    handoff artifact STILL rides the pin on the auto-inject face tail when
    the user picks bring_back (that consumer is unchanged)."""
    _stale_close(tmp_path, "s_stale")
    _close(tmp_path, "s_fresh")
    _write_handoff_artifact(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    foot = p._bringback_clarify_footer("s_cur")
    assert "如需带回旧 session 自动注入面" in foot, foot
    # The old handoff artifact announcement is GONE from the footer:
    assert "交接" not in foot, (
        "footer no longer announces the last-closed handoff artifact "
        "(moved to the auto-inject face tail on the pin, not the footer); "
        f"got: {foot!r}")
    # Choosing bring_back STILL pins the last-closed target (which carries
    # the handoff), so the summary rides the pin:
    p2 = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p2.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur"}))
    assert out["pin_target"] == "s_fresh", out


def test_f2_no_handoff_footer_byte_unchanged(tmp_path):
    """Regression sentinel: without a handoff artifact, the footer text is
    exactly today's (no 交接 line)."""
    _close(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    foot = p._bringback_clarify_footer("s_cur")
    assert "带回" in foot
    assert "交接" not in foot, (
        f"without a handoff artifact the footer must be today's text; got: "
        f"{foot!r}")
