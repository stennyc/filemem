"""HANDOFF face 1 (specs/HANDOFF-PRD.md, 2026-10-17 owner direction):
the life-line for deadlocked super-long sessions (the 474k "Compression
refused" class, session 20261015_142319_4bb2e8 as the live example).

Owner intent (2026-10-17): when a super-long session CANNOT compact and CANNOT
continue, name that session -> extract a handoff summary from its compact
ledger + filemem rows (NOT its 6.9MB raw transcript by default — the 474k
lesson: never re-generate from full raw text) -> the summary + one decision row
(stamped with the GENERATOR session) flows into the next session's auto face.
All plugin-side: zero host edits (the 474k guard itself is host pure-code and
stays off-limits unless the owner signs off a host edit separately).

H-series coverage (face 1 only; face 2 pin + face 3 footer live in
test_pin_session_id.py):
  H1: compact ledger + filemem rows -> handoffs/{sid}.json written, materials
      listed, llm_used=True (a scripted host-LLM face returns a fixed summary).
  H2: compact.json missing -> filemem-rows-only materials, no crash, honest
      degradation.
  H3: LLM unavailable (no main_runtime) -> llm_used=False, summary =
      compact-segment concatenation (audit-visible degradation, data kept).
  H4: focus="" -> raw transcript NEVER in materials (sentinel: the 474k
      lesson); focus set -> raw lines enter the material bundle.
  H5: exactly ONE decision row per (generator, source) extraction; a re-
      extraction REPLACES the row (one handoff per session pair, not a log).
      The row's session_id stamp = the GENERATOR session (the handoff act is
      THIS session's decision; the source sid rides in the content header).
  H6: no materials at all (no compact ledger, no filemem rows) -> honest
      {"success": False, "reason": "no materials"}, nothing written.
  H7: profile isolation — a source session whose filemem rows carry a
      FOREIGN profile stamp is NOT collected into this profile's handoff
      (不串 profile; the compact ledger is sourceless and still counts as
      material).

Reviewer notes baked in:
  - the handoff row lands as an UNCONFIRMED decision row (metadata.confirmed
    False): it goes through the normal confirm/draft chain before the
    confirmed-global leg auto-injects it next session — an unconfirmed
    handoff summary never reaches the auto face on its own (不串脏).
  - handoffs/{sid}.json is a per-SOURCE artifact: re-extracting the same
    source session overwrites (replace, not append — one live handoff per
    source, same posture as the bringback choice file).
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider  # noqa: E402

from _llm_host_fake import _MAIN_RUNTIME, install as install_host_fake  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_provider(tmp: Path, sid: str = "s_cur",
                   profile: Optional[str] = "default") -> FileMemProvider:
    """Same fixture shell as test_bringback_clarify._make_provider, plus a
    hermetis_home pointing at tmp (so the handoff face can locate the
    source session's supercompress_store ledger deterministically)."""
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
    p.hermetis_home = tmp  # handoff face: supercompress_store lives <home>/supercompress_store
    import contextlib
    p._shard_lock = lambda pth: contextlib.suppress()
    return p


def _write_compact_ledger(tmp: Path, sid: str, entries: Optional[list] = None,
                          profile: str = "coder") -> Path:
    """Seed a source session's supercompress compact ledger (the real disk
    layout: <home>/supercompress_store/<sid>.compact.json)."""
    d = tmp / "supercompress_store"
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / f"{sid}.compact.json"
    ledger.write_text(json.dumps(
        {"version": 1, "seq": len(entries or []), "entries": entries or [
            {"seq": 1, "text": "#1 P0 prov=user seq=0 [L3]\n任务状态：supercompress 90% 超压面已实施，699 tests green"},
            {"seq": 2, "text": "#2 P0 prov=model seq=1 [L3]\n下一步：等 owner 点头 commit/push；红线：本地先行"},
        ]}, ensure_ascii=False), encoding="utf-8")
    return ledger


def _write_source_rows(tmp: Path, sid: str, rows: list) -> Path:
    """Seed a source session's filemem rows (the real layout:
    <store>/sessions/<sid>.jsonl)."""
    d = tmp / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return f


def _row(content: str, *, sp: Optional[str] = None,
         confirmed: Optional[bool] = None,
         session_id: Optional[str] = None,
         mem_type: str = "fact") -> Dict[str, Any]:
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
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    return row


def _call(p: FileMemProvider, session_id: str, src: str,
          focus: str = "", monkeypatch=None,
          replies: Any = None) -> Dict[str, Any]:
    """Invoke the handoff tool face the way the host would; optionally route
    the host-LLM face through a scripted fake."""
    if monkeypatch is not None:
        fake, _state = install_host_fake(monkeypatch, replies=replies or {})
        p._main_runtime = _MAIN_RUNTIME
    out = p.handle_tool_call("filemem_handoff_extract", {
        "session_id": src, "focus": focus})
    return json.loads(out)


# ── H1: the happy path ────────────────────────────────────────────────────────


def test_h1_extract_writes_handoff_artifact_llm_used(tmp_path, monkeypatch):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    _write_source_rows(tmp_path, "s_src", [
        _row("90% 超压面 owner-locked", sp="default", confirmed=True,
             session_id="s_src")])
    canned = {"summary": "HANDOFF: supercompress 90% 面已完成；下一步 = owner 点头后 commit",
              "confirmed": True, "value": "high"}
    out = _call(p, "s_cur", "s_src", monkeypatch=monkeypatch,
                replies={"*": canned})
    assert out["success"] is True, out
    art = tmp_path / "handoffs" / "s_src.json"
    assert art.exists(), "the per-source handoff artifact must be written"
    data = json.loads(art.read_text())
    assert data["source_session_id"] == "s_src"
    assert data["generated_by_session"] == "s_cur"
    assert data["llm_used"] is True
    assert data["summary"] == canned["summary"]
    assert "compact.json" in data["materials"]
    assert any("filemem" in m for m in data["materials"])


# ── H2: compact ledger missing -> filemem-only, honest degradation ──────────


def test_h2_no_compact_ledger_degrades_to_rows_only(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_source_rows(tmp_path, "s_src", [
        _row("只有 filemem 行素材", sp="default", confirmed=True,
             session_id="s_src")])
    out = _call(p, "s_cur", "s_src")  # no LLM fake: main_runtime stays None
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert "compact.json" not in data["materials"]
    assert any("filemem" in m for m in data["materials"])
    assert data["llm_used"] is False


# ── H3: LLM unavailable -> deterministic fallback, audit-visible ────────────


def test_h3_llm_unavailable_falls_back_to_segment_concat(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src", entries=[
        {"seq": 1, "text": "#1 P0 prov=user seq=0 [L3]\n状态行一"},
        {"seq": 2, "text": "#2 P1 prov=model seq=1 [L3]\n状态行二"},
    ])
    out = _call(p, "s_cur", "s_src")  # _main_runtime None -> no LLM
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert data["llm_used"] is False
    assert "状态行一" in data["summary"] and "状态行二" in data["summary"], (
        "the fallback summary must be the compact segments concatenated "
        "(audit-visible degradation, 不吞脏)")


# ── H4: raw transcript ONLY on explicit focus (474k lesson) ──────────────────


def test_h4_raw_transcript_never_in_default_materials(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    d = tmp_path / "supercompress_store"
    (d / "s_src.jsonl").write_text(
        json.dumps({"role": "user", "content": "RAW-SECRET-6.9MB"}) + "\n"
        + json.dumps({"role": "assistant", "content": "RAW-SECRET-reply"}) + "\n",
        encoding="utf-8")
    out = _call(p, "s_cur", "s_src")  # focus="" -> raw stays OUT
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert not any("raw" in m for m in data["materials"]), (
        "the raw transcript must NEVER enter the default material bundle "
        "(474k lesson: no full-raw regeneration)")
    assert "RAW-SECRET-6.9MB" not in data["summary"]


def test_h4b_focus_pulls_raw_lines_into_bundle(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    d = tmp_path / "supercompress_store"
    (d / "s_src.jsonl").write_text(
        json.dumps({"role": "user", "content": "重建 dist 并打 APK"}) + "\n"
        + json.dumps({"role": "user", "content": "无关的闲聊"}) + "\n",
        encoding="utf-8")
    out = _call(p, "s_cur", "s_src", focus="dist")
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert any("raw" in m for m in data["materials"]), (
        "focus='dist' must pull the matching raw lines into the bundle")
    assert "重建 dist" in data["summary"]
    assert "无关的闲聊" not in data["summary"]


# ── H5: one decision row per (generator, source); replace, not append ───────


def test_h5_one_decision_row_replaced_on_reextract(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    out1 = _call(p, "s_cur", "s_src")
    assert out1["success"] is True, out1
    out2 = _call(p, "s_cur", "s_src")  # second extraction, same pair
    assert out2["success"] is True, out2
    shard = tmp_path / "memories" / "decision.json"
    rows = json.loads(shard.read_text()) if shard.exists() else []
    handoff_rows = [r for r in rows if "s_src" in r.get("content", "")]
    assert len(handoff_rows) == 1, (
        "re-extraction must REPLACE the one decision row, not append a log "
        f"(got {len(handoff_rows)} rows)")
    row = handoff_rows[0]
    assert row.get("session_id") == "s_cur", (
        "the row's session_id stamp = the GENERATOR session (the handoff act "
        "is THIS session's decision); the source sid rides in the content")
    assert "s_src" in row["content"]
    assert (row.get("metadata") or {}).get("confirmed") in (None, False), (
        "the handoff row lands UNCONFIRMED — the confirm chain gates the "
        "auto face (不串脏)")


# ── H6: no materials at all -> honest failure, nothing written ──────────────


def test_h6_no_materials_honest_failure(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = _call(p, "s_cur", "s_ghost")  # no ledger, no rows
    assert out["success"] is False, out
    assert "no materials" in out.get("reason", ""), out
    assert not (tmp_path / "handoffs" / "s_ghost.json").exists()
    assert not (tmp_path / "memories" / "decision.json").exists()


# ── H7: profile isolation on source filemem rows ─────────────────────────────


def test_h7_foreign_profile_source_rows_not_collected(tmp_path):
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    _write_source_rows(tmp_path, "s_src", [
        _row("foreign row", sp="coder", confirmed=True, session_id="s_src"),
        _row("own row", sp="default", confirmed=True, session_id="s_src"),
    ])
    out = _call(p, "s_cur", "s_src")
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert "own row" in data["summary"] or "own row" in json.dumps(
        data.get("filemem_rows", data)), "own-profile row must be collected"
    assert "foreign row" not in data["summary"] and \
        "foreign row" not in json.dumps(data.get("filemem_rows", [])), (
        "a foreign-profile source row must NOT enter this profile's "
        "handoff (不串 profile)")
