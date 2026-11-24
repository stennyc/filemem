"""handoff stub + auto-detect compact-ledger candidates (owner 2026-11-24).

Covers:
  1. _handoff_compact_ledger_candidates — auto-detect candidate roots
     (override env / hermetis_home primary / per-profile / neutral-root floor),
     ordered, de-duped, NONE hardcoded literal.
  2. _write_handoff_stub — deterministic grab of EVERY user msg + last assistant
     (全量 user per owner ruling), zero LLM, lands <storage>/handoff_stubs/<sid>.
  3. on_session_end stub hook — a real session-end lands the stub.
  4. _gather_handoff_materials priority: compact.json (candidate hit) > stub >
     rows floor; trace_transcript only when compact resolved; focus raw-line
     falls back to stub user intents when no raw trace at any candidate.

On-disk truth (not mock): tmp storage roots, real file writes/reads.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Optional

import pytest

# --- plugin import posture (mirrors test_bringback_clarify.py) -------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402


def _make_provider(tmp: Path, sid: str = "s_cur",
                   profile: Optional[str] = "default",
                   hermes_home: Optional[Path] = None) -> FileMemProvider:
    """Fixture provider shell (mirrors the bringback test's _make_provider,
    plus an explicit hermes_home so the candidate auto-detect probe has a
    primary root to look under)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = profile
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = None
    p.auto_extract = False
    p._decay = {}
    p.min_search_score = 0.3
    # hermetis_home: the host primary root the candidate probe looks under
    p.hermetis_home = hermes_home
    return p


def _mk_home(tmp: Path) -> Path:
    home = tmp / "hermes_home"
    home.mkdir(parents=True, exist_ok=True)
    return home


# ---- 1. candidate auto-detect ------------------------------------------------

def test_candidates_ordered_and_deduped(tmp_path):
    p = _make_provider(tmp_path, profile="coder", hermes_home=_mk_home(tmp_path))
    cands = p._handoff_compact_ledger_candidates("sid_X")
    assert cands, "candidate list must be non-empty"
    # per-profile candidate MUST be present (coder sp)
    assert any("profiles" in str(c) and "coder" in str(c) for c in cands), cands
    # neutral-root floor (~/.hermes/supercompress_store) present
    assert any(str(c).endswith("sid_X.compact.json") for c in cands)
    # de-duped: no two candidates resolve to the same path
    resolved = [str(c.resolve()) for c in cands if c.exists()]
    assert len(resolved) == len(set(resolved))


def test_candidate_hit_picks_existing_root(tmp_path):
    """Auto-detect resolves a compact ledger living under the per-profile root,
    NOT the host primary — proves the probe is real (first-hit-wins), not a
    hardcoded single path."""
    home = _mk_home(tmp_path)
    p = _make_provider(tmp_path, profile="coder", hermes_home=home)
    prof_root = home / "profiles" / "coder" / "supercompress_store"
    prof_root.mkdir(parents=True, exist_ok=True)
    (prof_root / "sid_X.compact.json").write_text(
        json.dumps({"entries": [{"text": "P0 seg", "seq": 1}]}), encoding="utf-8")
    # host-primary root is empty -> candidate probe must fall through to per-profile
    assert not (home / "supercompress_store" / "sid_X.compact.json").exists()
    lp = p._handoff_compact_ledger_path("sid_X")
    assert lp is not None
    assert "profiles" in str(lp) and "coder" in str(lp)


def test_env_override_wins(tmp_path, monkeypatch):
    home = _mk_home(tmp_path)
    p = _make_provider(tmp_path, profile="", hermes_home=home)
    ov = tmp_path / "sc_override"
    ov.mkdir(exist_ok=True)
    (ov / "sid_X.compact.json").write_text(
        json.dumps({"entries": [{"text": "override seg"}]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_SUPERCOMPRESS_ROOT", str(ov))
    cands = p._handoff_compact_ledger_candidates("sid_X")
    # override candidate is FIRST (highest priority)
    assert cands[0] == ov / "sid_X.compact.json"
    assert p._handoff_compact_ledger_path("sid_X") == ov / "sid_X.compact.json"


# ---- 2. stub write (deterministic, 全量 user + last assistant) ---------------

def test_stub_write_grabs_all_user_and_last_assistant(tmp_path):
    p = _make_provider(tmp_path, profile="coder", hermes_home=_mk_home(tmp_path))
    msgs = [
        {"role": "user", "content": "first ask: do X"},
        {"role": "assistant", "content": "interim note (should NOT be last)"},
        {"role": "tool", "content": "tool noise (skipped)"},
        {"role": "user", "content": "second ask: do Y"},
        {"role": "assistant", "content": "final progress: done"},
    ]
    out = p._write_handoff_stub("sid_S", msgs)
    assert out is not None and out.exists()
    stub = json.loads(out.read_text(encoding="utf-8"))
    # 全量 user (owner ruling) — both user msgs, tool skipped
    assert stub["user_msgs"] == ["first ask: do X", "second ask: do Y"]
    # last assistant = current progress
    assert stub["last_assistant"] == "final progress: done"
    assert stub["session_id"] == "sid_S"
    assert stub["source_profile"] == "coder"
    # lands under storage/handoff_stubs, NOT under any supercompress_store
    assert "handoff_stubs" in str(out)
    assert "supercompress_store" not in str(out)


def test_stub_write_openai_block_list_content(tmp_path):
    p = _make_provider(tmp_path, hermes_home=_mk_home(tmp_path))
    msgs = [{"role": "user", "content": [{"type": "text", "text": "block A"}]}]
    out = p._write_handoff_stub("sid_B", msgs)
    stub = json.loads(out.read_text(encoding="utf-8"))
    assert stub["user_msgs"] == ["block A"]


def test_stub_write_nothing_useful_returns_none(tmp_path):
    p = _make_provider(tmp_path, hermes_home=_mk_home(tmp_path))
    assert p._write_handoff_stub("sid_N", [{"role": "tool", "content": "noise"}]) is None
    assert p._write_handoff_stub("sid_N", []) is None


# ---- 3. on_session_end hook lands the stub ----------------------------------

def test_on_session_end_lands_handoff_stub(tmp_path):
    p = _make_provider(tmp_path, profile="coder", hermes_home=_mk_home(tmp_path))
    p.session_id = "sid_H"
    p._session_drafts = {}   # fixture shell needs the draft-dict attr the body checks
    # the on_session_end face calls _spawn_llm_consolidation + _record_last_closed
    # session (both fail-soft in the fixture shell) — the stub write is the new
    # side-effect under test. Patch the two sibling hooks to no-ops so the
    # assertion isolates the stub leg (disk truth, not the spawn).
    p._spawn_llm_consolidation = lambda *a, **k: None
    p._record_last_closed_session = lambda *a, **k: None
    p.on_session_end([
        {"role": "user", "content": "the task"},
        {"role": "assistant", "content": "where I stopped"},
    ])
    sp = p._handoff_stub_path("sid_H")
    assert sp is not None and sp.exists()
    stub = json.loads(sp.read_text(encoding="utf-8"))
    assert stub["user_msgs"] == ["the task"]
    assert stub["last_assistant"] == "where I stopped"


# ---- 4. gather priority: compact > stub > rows --------------------------------

def test_gather_compact_beats_stub_when_compact_present(tmp_path):
    """When a compact.json is auto-detected, the stub leg must NOT fire."""
    home = _mk_home(tmp_path)
    p = _make_provider(tmp_path, profile="", hermes_home=home)
    sc = home / "supercompress_store"
    sc.mkdir(parents=True, exist_ok=True)
    (sc / "sid_P.compact.json").write_text(
        json.dumps({"entries": [{"text": "P0 compact seg"}]}), encoding="utf-8")
    p._write_handoff_stub("sid_P", [
        {"role": "user", "content": "stub intent"},
        {"role": "assistant", "content": "stub progress"},
    ])
    m = p._gather_handoff_materials("sid_P", "")
    assert m["stub_used"] is False
    assert any("P0 compact seg" in s for s in m["compact_segments"])
    assert not any("stub intent" in s for s in m["compact_segments"])


def test_gather_stub_fires_when_no_compact(tmp_path):
    """No compact.json at any candidate root -> the stub is the floor."""
    p = _make_provider(tmp_path, profile="", hermes_home=_mk_home(tmp_path))
    p._write_handoff_stub("sid_Q", [
        {"role": "user", "content": "intent A"},
        {"role": "user", "content": "intent B"},
        {"role": "assistant", "content": "progress Q"},
    ])
    m = p._gather_handoff_materials("sid_Q", "")
    assert m["stub_used"] is True
    assert any("intent A" in s and "intent B" in s for s in m["compact_segments"])
    assert any("progress Q" in s for s in m["compact_segments"])
    assert any("intent A" in t for t in m["trace_transcript"])


def test_gather_rows_floor_when_no_compact_and_no_stub(tmp_path):
    """Dead session: no compact at any candidate, no stub -> rows-only floor."""
    p = _make_provider(tmp_path, profile="", hermes_home=_mk_home(tmp_path))
    sf = p.sessions_dir / "sid_R.jsonl"
    sf.write_text(
        json.dumps({"content": "row fact", "source_profile": "", "type": "fact"}) + "\n",
        encoding="utf-8",
    )
    m = p._gather_handoff_materials("sid_R", "")
    assert m["stub_used"] is False
    assert m["compact_segments"] == []
    assert len(m["filemem_rows"]) == 1
    assert m["filemem_rows"][0]["content"] == "row fact"


def test_gather_focus_falls_back_to_stub_intents(tmp_path):
    """focus set + no raw trace at any candidate -> grep the stub's user msgs."""
    p = _make_provider(tmp_path, profile="", hermes_home=_mk_home(tmp_path))
    p._write_handoff_stub("sid_F", [
        {"role": "user", "content": "deploy the service"},
        {"role": "user", "content": "unrelated line"},
        {"role": "assistant", "content": "done"},
    ])
    m = p._gather_handoff_materials("sid_F", "deploy")
    assert any("deploy the service" in l for l in m["raw_lines"])
    assert not any("unrelated" in l for l in m["raw_lines"])
