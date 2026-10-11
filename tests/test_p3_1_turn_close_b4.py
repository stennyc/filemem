"""P3-1: turn-level B4 fallback for _write_episodic_entry.

Context: the host's on_session_end hook can be skipped (30s hook-timeout +
60s suppression window in hermes_cli/plugins_dispatch.py). When it is
skipped, FileMemProvider.on_session_end never runs, so _write_episodic_entry
never fires and the episodic pool receives no new rows — starving the
score_salience LLM face's input side.

Fix: run _on_turn_close_check at the end of every sync_turn. If the current
session has drafts in _session_drafts and the provider has not yet written
an episodic entry for this session this process lifetime, write one now.

Invariants:
  #1  _on_turn_close_check fires B4 write when drafts exist and not yet written
  #2  _on_turn_close_check is a no-op when _session_drafts[sid] is empty
  #3  _on_turn_close_check is a no-op when _episodic_written already contains sid
  #4  on_session_end, after B4 write, stamps _episodic_written so the
      turn-level fallback does NOT re-write
  #5  The B4 episodic file lands on disk with the correct content shape
  #6  _write_episodic_entry raising does NOT stamp _episodic_written
      (failure = retry next turn), and the exception propagates out of
      _on_turn_close_check to be caught by sync_turn's fail-soft wrapper
  #7  on_session_end stamp makes the fallback a no-op (standalone pin)
  #8  failed write leaves sid unstamped (short retry-probe form)

Run:
    cd /root/.hermes/plugins/filemem
    PYTHONPATH=/usr/local/lib/hermes-agent \
      /usr/bin/python3 -m pytest tests/test_p3_1_turn_close_b4.py -q -p no:cacheprovider
"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmp: Path, sid: str = "s_p3_1") -> FileMemProvider:
    """Minimal offline provider with P3-1 fields wired.

    Mirrors test_p1_1_burst_rescan._make_provider: __new__-construction so
    initialize() never runs (no storage-dir probing, no disk I/O), then
    set the attributes _on_turn_close_check and _write_episodic_entry need.
    """
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = "test_p3_1"
    p.storage_dir = tmp
    # B4 episodic write path
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    # P3-1 in-memory dedup set
    p._episodic_written = set()
    # session drafts (the input to B4)
    p._session_drafts = {}
    # decay config: salience_threshold gate inside _write_episodic_entry
    p._decay = {"salience_threshold": 0.5, "consolidation": {}}
    # _shard_lock: used by _write_episodic_entry for cross-process consistency
    import contextlib
    p._shard_lock = contextlib.contextmanager(lambda path: (lambda: _nullctx())[0]()
    ) if False else _shard_lock_stub
    # _load_rescore_events: needed by _unscored_episodic_candidates (not called here,
    # but _write_episodic_entry does not call it — keep a stub just in case)
    p._load_rescore_events = lambda: {}
    return p


def _shard_lock_stub(path):
    """In-test no-op context manager (cross-process locking is not under test)."""
    import contextlib
    return contextlib.nullcontext()


def _mk_draft(content: str = "test fact", salience: float = 0.8) -> MemoryItem:
    return MemoryItem(
        id="test_draft_0", type="fact", content=content,
        level="L0", salience=salience,
    )


def test_p31_01_fallback_fires_b4_write_when_drafts_present(tmp_path):
    """#1: _on_turn_close_check calls _write_episodic_entry when drafts exist."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1"
    p._session_drafts[sid] = [_mk_draft("first turn fact")]

    with patch.object(p, "_write_episodic_entry") as mock_write:
        mock_write.return_value = None
        p._on_turn_close_check(sid)
        mock_write.assert_called_once_with(sid, p._session_drafts[sid])

    # sid should now be in _episodic_written
    assert sid in p._episodic_written


def test_p31_02_fallback_noop_when_no_drafts(tmp_path):
    """#2: no drafts → _write_episodic_entry not called, sid not marked."""
    p = _make_provider(tmp_path)
    sid = "s_empty"
    p._session_drafts[sid] = []

    with patch.object(p, "_write_episodic_entry") as mock_write:
        p._on_turn_close_check(sid)
        mock_write.assert_not_called()

    assert sid not in p._episodic_written


def test_p31_03_fallback_noop_when_already_written(tmp_path):
    """#3: _episodic_written already contains sid → no re-write."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1"
    p._session_drafts[sid] = [_mk_draft("second call")]
    p._episodic_written.add(sid)  # pre-marked (as on_session_end would)

    with patch.object(p, "_write_episodic_entry") as mock_write:
        p._on_turn_close_check(sid)
        mock_write.assert_not_called()


def test_p31_04_on_session_end_stamps_episodic_written(tmp_path):
    """#4: on_session_end's B4 write marks _episodic_written so the
    turn-level fallback does not re-write."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1"
    p._session_drafts[sid] = [_mk_draft("via session-end")]

    with patch.object(p, "_spawn_llm_consolidation"), \
         patch.object(p, "_record_last_closed_session"), \
         patch.object(p, "_write_episodic_entry") as mock_write:
        mock_write.return_value = None
        p.on_session_end([])
        # _write_episodic_entry should have been called by on_session_end
        mock_write.assert_called_once_with(sid, p._session_drafts[sid])
        # and _episodic_written should now contain sid
        assert sid in p._episodic_written

    # Now the turn-level fallback should no-op (already written)
    with patch.object(p, "_write_episodic_entry") as mock_write2:
        p._on_turn_close_check(sid)
        mock_write2.assert_not_called()


def test_p31_05_episodic_file_written_to_disk(tmp_path):
    """#5: real _write_episodic_entry (no mock) writes a JSONL row on disk."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1_disk"
    drafts = [_mk_draft("disk-write fact", salience=0.8)]
    p._session_drafts[sid] = drafts

    p._on_turn_close_check(sid)  # no mock — real B4 write

    # Find the episodic shard file
    now_utc = datetime.now(timezone.utc)
    year = now_utc.strftime("%Y")
    month = now_utc.strftime("%m")
    shard = p.episodic_dir / year / month / f"{sid}.jsonl"
    assert shard.exists(), f"expected {shard} to exist"
    rows = [json.loads(l) for l in shard.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["session_id"] == sid
    assert rows[0]["drafts"] == 1
    assert rows[0]["status"] == "active"  # salience 0.8 > threshold 0.5


def test_p31_06_fallback_retries_when_write_raises(tmp_path):
    """S1: _on_turn_close_check propagates OSError; the sync_turn-side
    try/except swallows it without stamping _episodic_written, so a
    subsequent turn retry fires the B4 write again."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1"
    p._session_drafts[sid] = [_mk_draft("x")]

    # Phase 1: _write_episodic_entry raises → exception propagates out of
    # _on_turn_close_check; sid must NOT be stamped.
    with patch.object(p, "_write_episodic_entry",
                      side_effect=OSError("disk full")):
        try:
            p._on_turn_close_check(sid)
            raise AssertionError("expected OSError to propagate")
        except OSError:
            pass

    assert sid not in p._episodic_written

    # Phase 2: simulate the sync_turn-side try/except wrapping the retry.
    # Now _write_episodic_entry succeeds → sid stamped, no exception.
    with patch.object(p, "_write_episodic_entry") as mock_ok:
        mock_ok.return_value = None
        try:
            p._on_turn_close_check(sid)
        except Exception as _e:
            pytest.fail(f"sync_turn-side try/except should have swallowed: {_e}")
        mock_ok.assert_called_once()
        assert sid in p._episodic_written


def test_p31_07_on_session_end_prevents_fallback_rewrite(tmp_path):
    """Optional: after on_session_end stamps _episodic_written, a later
    _on_turn_close_check is a no-op (pins the invariant in a standalone
    case, reusing test_p31_04's logic)."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1"
    p._session_drafts[sid] = [_mk_draft("via session-end")]

    with patch.object(p, "_spawn_llm_consolidation"), \
         patch.object(p, "_record_last_closed_session"), \
         patch.object(p, "_write_episodic_entry") as mock_write:
        mock_write.return_value = None
        p.on_session_end([])
        mock_write.assert_called_once_with(sid, p._session_drafts[sid])
        assert sid in p._episodic_written

    # Turn-level fallback must not re-write
    with patch.object(p, "_write_episodic_entry") as mock_write2:
        p._on_turn_close_check(sid)
        mock_write2.assert_not_called()


def test_p31_08_write_failure_not_marked(tmp_path):
    """#6: _write_episodic_entry raising OSError -> sid NOT stamped
    (retry next turn), exception propagates (caught by sync_turn wrapper)."""
    p = _make_provider(tmp_path)
    sid = "s_p3_1_fail"
    p._session_drafts[sid] = [_mk_draft("will fail")]

    def _raise(*args, **kwargs):
        raise OSError("disk write failed")

    with patch.object(p, "_write_episodic_entry", side_effect=_raise):
        with pytest.raises(OSError, match="disk write failed"):
            p._on_turn_close_check(sid)

    # (b) sid must NOT be in _episodic_written after a failed write
    assert sid not in p._episodic_written, (
        "failed B4 write must not stamp _episodic_written; "
        "next turn should retry"
    )

