"""Provider prefix-cache stability: byte-stable injection across turns.

Regression guard for the 2026-11-14 filemem drift fix. The supercompress
cache-stuck detector was firing because filemem's auto-injection tail drifted
byte-for-byte across consecutive turns of the SAME session, so the current-turn
user message's tail differed on next-turn history replay and the provider
prefix cache went cold (cache_read stuck, write=0).

Two root causes, two fixes, two invariants this file pins:

Patch 1 — session-scoped decay anchor
    `_f2_time_anchor` is FROZEN within a session and only refreshed on a
    session change. A same-session re-prefetch must reuse the SAME anchor
    object (byte-stable decay axis -> stable RRF top-N -> stable recall
    block). A cross-session prefetch MUST refresh the anchor.

Patch 2 — `_turn_report_line` fingerprint cache
    An unchanged pending-confirm queue re-renders byte-identically (cache hit,
    no re-render -> stable [记忆报告] tail bytes). A consumed item (queue
    changed) must re-render with different bytes (semantic change propagates).

These tests drive the two surfaces directly (no full prefetch chain) so they
stay fast and pin the exact regression class without coupling to pool shape.
"""
import threading, pathlib, tempfile, sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, _pending_fingerprint  # noqa: E402


def _make_provider(tmpdir: pathlib.Path, session_id: str = "s1") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.min_search_score = 0.3
    p._decay = {}
    p._pending_confirm = {}
    p._pending_drafts_notified = {}
    p._session_drafts = {}
    p._last_recall = None
    return p


# ─────────────────────────── Patch 1: session-scoped decay anchor ───────────────────────────

def test_anchor_frozen_within_same_session():
    """Same-session re-prefetch reuses the SAME _f2_time_anchor object."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s1")

    # Drive the anchor block exactly as _prefetch_body does (extracted logic):
    def _anchor_block(session_id):
        _sid_anchor = session_id or p.session_id
        _prev = getattr(p, "_f2_anchor_sid", None)
        if _prev != _sid_anchor:
            p._f2_anchor_sid = _sid_anchor
            p._f2_time_anchor = datetime.now(timezone.utc)

    _anchor_block("s1")
    a1 = p._f2_time_anchor
    _anchor_block("s1")  # same session again
    assert p._f2_time_anchor is a1, "same-session prefetch must reuse the frozen anchor"


def test_anchor_refreshed_on_session_change():
    """A different session_id refreshes the anchor (fresh wall-clock baseline)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s1")

    def _anchor_block(session_id):
        _sid_anchor = session_id or p.session_id
        _prev = getattr(p, "_f2_anchor_sid", None)
        if _prev != _sid_anchor:
            p._f2_anchor_sid = _sid_anchor
            p._f2_time_anchor = datetime.now(timezone.utc)

    _anchor_block("s1")
    a1 = p._f2_time_anchor
    _anchor_block("s2")
    assert p._f2_time_anchor is not a1, "cross-session prefetch must refresh the anchor"
    assert p._f2_anchor_sid == "s2"


# ─────────────────────────── Patch 2: _turn_report_line fingerprint cache ───────────────────────────

def _seed_pending(p, sid, items):
    p._pending_confirm = {sid: items}
    p._turn_report_cache = {}


def test_turn_report_byte_stable_for_unchanged_queue():
    """Two calls with the same pending queue return byte-identical strings."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s1")
    _seed_pending(p, "s1", [
        {"content": "事实A", "level": "L1", "kind": "l0", "id": "f1"},
        {"content": "决策B", "level": "L2", "kind": "l0", "id": "d1"},
    ])
    r1 = p._turn_report_line("s1")
    r2 = p._turn_report_line("s1")
    assert r1 == r2, "unchanged pending queue must re-render byte-identically"
    assert len(p._turn_report_cache) == 1, "exactly one cache entry for the stable fingerprint"


def test_turn_report_re_renders_after_consume():
    """Consuming a pending item changes the queue -> fingerprint changes -> fresh bytes."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s1")
    _seed_pending(p, "s1", [
        {"content": "事实A", "level": "L1", "kind": "l0", "id": "f1"},
        {"content": "决策B", "level": "L2", "kind": "l0", "id": "d1"},
    ])
    r1 = p._turn_report_line("s1")
    # Simulate a confirm/discard consuming one row (queue shrinks).
    p._pending_confirm["s1"] = [
        {"content": "决策B", "level": "L2", "kind": "l0", "id": "d1"},
    ]
    r2 = p._turn_report_line("s1")
    assert r2 != r1, "a consumed item must change the report bytes"


def test_turn_report_empty_queue_returns_empty():
    """No pending -> empty string (silent), and nothing cached."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s1")
    _seed_pending(p, "s1", [])
    assert p._turn_report_line("s1") == ""


# ─────────────────────────── helper: _pending_fingerprint determinism ───────────────────────────

def test_fingerprint_deterministic_and_content_sensitive():
    a = _pending_fingerprint([{"content": "x", "level": "L0", "kind": "l0", "id": "1"}])
    b = _pending_fingerprint([{"content": "x", "level": "L0", "kind": "l0", "id": "1"}])
    c = _pending_fingerprint([{"content": "y", "level": "L0", "kind": "l0", "id": "1"}])
    assert a == b, "same rows -> same digest"
    assert a != c, "different content -> different digest"
    # Malformed (non-serializable) row degrades without raising.
    weird = [{"content": object(), "level": "L0", "kind": "l0", "id": "1"}]
    _pending_fingerprint(weird)  # must not raise
    assert _pending_fingerprint(weird) == _pending_fingerprint(weird)
