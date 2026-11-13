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
    # B4 trace-write attributes (the fixture shell mirrors the full
    # FileMemProvider.__init__ init list so direct sync_turn calls in
    # H9 tests don't AttributeError on a missing per-turn flag)
    p._turn_failure = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_author = None
    p._turn_foreign = False
    p._turn_active_sid = ""
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


# ── H8: trace transcript settles the compact gap (owner 2026-11-13) ──────────

def _write_trace(tmp: Path, sid: str, user_rows: list, model_rows: list):
    """Seed the REAL-TIME trace pair the supercompress engine keeps beside
    the compact ledger: <home>/supercompress_store/<sid>.jsonl (user source,
    one seq per user message) + <sid>.model.jsonl (model source, one seq per
    model turn — the two seq spaces are INDEPENDENT counters, so a given seq
    value can exist in BOTH files at different ts)."""
    d = tmp / "supercompress_store"
    d.mkdir(parents=True, exist_ok=True)
    def _dump(fn, rows):
        (d / fn).write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in rows) + "\n", encoding="utf-8")
    _dump(f"{sid}.jsonl", user_rows)
    _dump(f"{sid}.model.jsonl", model_rows)


def _trace_row(seq, ts, prov, text, tier="P0"):
    return {"seq": seq, "ts": ts, "provenance": prov, "tier": tier,
            "original_text": text}


def test_h8_trace_transcript_covers_compact_gap(tmp_path):
    """compact.json's watermark = the max GLOBAL page seq parsed by the same
    regex the compact_ledger engine uses (NOT the top-level seq field, which
    is a generation counter); the trace transcript is the user+model pair
    merged by ts, filtered to seq strictly ABOVE that watermark — exactly
    the settling-gap the compact snapshot can't cover (the '接手不完整' root
    cause, owner 2026-11-13)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # compact ledger: 57 page segments, global seqs 0..41 (top-level seq=1 on
    # purpose — the generation counter, NOT the watermark)
    _write_compact_ledger(tmp_path, "s_src", entries=[{
        "seq": 1,
        "text": "\n---\n".join(
            f"#1 P0 prov=user seq={i} [L3]\nline {i}" for i in range(42))}])
    # trace pair: user source seq 0/1/42 (42 is the settling gap); model
    # source seq 40/41/43 (40/41 already covered by the compact watermark,
    # 43 is the settling gap)
    _write_trace(tmp_path, "s_src",
                 user_rows=[_trace_row(0, 100.0, "user", "u-0"),
                             _trace_row(1, 110.0, "user", "u-1"),
                             _trace_row(42, 140.0, "user", "u-42")],
                 model_rows=[_trace_row(40, 120.0, "model", "m-40"),
                             _trace_row(41, 130.0, "model", "m-41"),
                             _trace_row(43, 150.0, "model", "m-43")])

    wm = p._handoff_compact_watermark("s_src")
    assert wm == 41, (
        "the watermark must be the page-level max GLOBAL seq (41), parsed by "
        "the _PAGE_SEQ_RE-style regex — NOT the top-level seq field (1, the "
        f"generation counter): got {wm}")

    segs = p._handoff_trace_transcript("s_src", wm)
    bodies = [s.split("\n", 1)[1] for s in segs]
    assert bodies == ["u-42", "m-43"], (
        f"only the settling-gap rows (seq strictly > watermark 41) may enter "
        f"the trace transcript, ts-ordered: {bodies}")

    # the watermark itself (seq 41) is EXACTLY covered by compact -> excluded
    segs_boundary = p._handoff_trace_transcript("s_src", 40)
    assert [s.split("\n", 1)[1] for s in segs_boundary] == \
        ["m-41", "u-42", "m-43"], "seq 41 (== watermark 40+1) must be kept"


def test_h8b_no_trace_files_degrades_to_empty(tmp_path):
    """A dead session may have lost its raw trace pair: the trace leg must
    degrade to an empty list, never a crash (the compact + rows legs are the
    floor, same fail-soft posture as the pre-H8 design)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src")
    assert p._handoff_trace_transcript("s_src", 0) == []
    out = _call(p, "s_cur", "s_src")  # no LLM fake, no trace files
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    assert data.get("trace_transcript") == []
    assert not any("trace" in m for m in data["materials"])


def test_h8c_trace_transcript_feeds_fallback_summary(tmp_path):
    """The deterministic fallback (no host LLM) must concatenate the settling-
    gap segments AFTER the compact segments, BEFORE the filemem rows — the
    same leg order the LLM bundle face uses (compact -> trace -> rows)."""
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    _write_compact_ledger(tmp_path, "s_src", entries=[{
        "seq": 1, "text": "#1 P0 prov=user seq=0 [L3]\ncompact 基线段"}])
    _write_trace(tmp_path, "s_src",
                 user_rows=[], model_rows=[
                     _trace_row(5, 200.0, "model", "gap-段-model")],
                 )
    # compact watermark from the single-segment ledger = 0 -> the seq-5 model
    # row is above it and must enter the settling-gap leg
    out = _call(p, "s_cur", "s_src")  # main_runtime None -> fallback face
    assert out["success"] is True, out
    data = json.loads((tmp_path / "handoffs" / "s_src.json").read_text())
    summary = data["summary"]
    assert "compact 基线段" in summary and "gap-段-model" in summary
    assert any("trace" in m for m in data["materials"]), (
        f"the settling-gap leg must be declared in materials: {data['materials']}")


# ── H9: B4 trace dup-suppression (owner 2026-11-13, 300s window) ─────────────
#
# 宿主侧 _sync_external_memory_for_turn 有两条独立调用路径（chat-completions
# runtime 的 agent/turn_finalizer.py:773 + codex runtime 的
# agent/codex_runtime.py:714），同一轮 turn 可能各触发一次 provider.sync_turn，
# 宿主侧无幂等门控 → 同一 (content, assistant) 会被 B4 append 两次、ts 错开
# 2~3s。幂等只能落在 provider 侧：append 前读该 trace 文件最后一行，若
# content+assistant 字节级相等且 ts 差 < 300s → 跳过本次 append。


def _seed_trace_line(p, sid, ts_iso, content, assistant="a-reply"):
    """Pre-seed exactly one B4-format line in the session's trace file (the
    state sync_turn would have written on the FIRST of two identical
    host-side triggers; the test then re-invokes sync_turn to prove the
    second append is suppressed, not the first)."""
    line = json.dumps({
        "session_id": sid, "ts": ts_iso, "role": "user",
        "content": content, "assistant": assistant,
        "source_profile": p.source_profile,
    }, ensure_ascii=False) + "\n"
    f = p.sessions_dir / f"{sid}.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line)


# 300s-window dedupe tests: monkeypatch the module-level _utcnow (via
# sys.modules['__init__'] — the real module object, regardless of what
# name the test file imported it under) so the ts gap is deterministic,
# independent of live wall-clock time.
import sys as _sys


def test_h9_b4_trace_dup_suppressed_within_300s(tmp_path, monkeypatch):
    """Identical (content, assistant) with a 2-second ts gap (the host-side
    dual-hook real-world form, owner 2026-11-13: 宿主侧两条独立调用路径
    无幂等门控，同一轮 turn 各触发一次 sync_turn) must NOT produce a second
    B4 trace line — the provider-side 300s idempotent gate skips it.

    monkeypatch _utcnow to a fixed "T1+2s" so the ts gap is deterministic
    (2s, well inside 300s) regardless of live wall-clock time."""
    p = _make_provider(tmp_path, sid="s_dup", profile="default")
    p.trigger_router = None
    from datetime import datetime, timezone
    t1 = datetime(2026, 11, 13, 6, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    t2 = t1.replace(second=2)
    _seed_trace_line(p, "s_dup", t1.isoformat(), "同一轮的用户消息")
    # fix _utcnow to "T1+2s" so the second append's ts is deterministically 2s
    # after the seed line's ts — inside the 300s window, must be suppressed
    _real_mod = _sys.modules.get("__init__") or _sys.modules.get("filemem")
    monkeypatch.setattr(_real_mod, "_utcnow", lambda: t2)
    p.sync_turn("同一轮的用户消息", "a-reply", session_id="s_dup")
    lines = [l for l in (p.sessions_dir / "s_dup.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, (
        "the second identical B4 trace append (ts gap < 300s) must be "
        f"suppressed by the idempotent gate; got {len(lines)} lines: {lines}")


def test_h9b_b4_trace_legal_repeat_beyond_300s_still_appended(tmp_path, monkeypatch):
    """A genuinely later, user-initiated re-send of the identical message
    (ts gap > 300s — a NEW logical turn, not a host-side double-hook) must
    still append a second B4 trace line: the 300s window is a 宁漏勿错
    boundary, NOT a global dedupe (a same-content re-send across days is
    new evidence, not a duplicate)."""
    p = _make_provider(tmp_path, sid="s_dup", profile="default")
    p.trigger_router = None
    from datetime import datetime, timezone
    t1 = datetime(2026, 11, 13, 6, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    t2 = t1.replace(minute=8)  # 480s later (> 300s window)
    _seed_trace_line(p, "s_dup", t1.isoformat(), "同一条消息再发一次")
    _real_mod = _sys.modules.get("__init__") or _sys.modules.get("filemem")
    monkeypatch.setattr(_real_mod, "_utcnow", lambda: t2)
    p.sync_turn("同一条消息再发一次", "a-reply", session_id="s_dup")
    lines = [l for l in (p.sessions_dir / "s_dup.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, (
        "a same-content re-send beyond the 300s window is a new logical "
        f"turn, not a duplicate; both B4 lines must be kept: {lines}")


def test_h9c_b4_trace_different_content_always_appended(tmp_path, monkeypatch):
    """The dedupe gate is content-keyed, NOT a blanket 'one line per
    session' lock: a genuinely different turn (different user_content,
    regardless of ts gap) must always append, even seconds after the last
    line — otherwise normal multi-turn sessions would start losing
    legitimate distinct turns to an over-eager dedupe."""
    p = _make_provider(tmp_path, sid="s_dup", profile="default")
    p.trigger_router = None
    from datetime import datetime, timezone
    t1 = datetime(2026, 11, 13, 6, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    t2 = t1.replace(second=1)
    _seed_trace_line(p, "s_dup", t1.isoformat(), "第一条消息")
    _real_mod = _sys.modules.get("__init__") or _sys.modules.get("filemem")
    monkeypatch.setattr(_real_mod, "_utcnow", lambda: t2)
    p.sync_turn("第二条完全不同的消息", "另一个回复", session_id="s_dup")
    lines = [l for l in (p.sessions_dir / "s_dup.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, (
        "a genuinely different user_content must NEVER be deduped, "
        f"regardless of ts proximity: {lines}")
