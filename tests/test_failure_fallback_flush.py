"""Negative-example v1 fallback (堵悬空 marker 洞): session-end flush of
unconsumed failure markers into a durable L0 row so the lesson enters the
auto-inject pool (egress-kept via metadata.failure_type) instead of
evaporating with the provider instance.

Three legs:
  ① unconsumed marker → on_session_end synthesizes ONE L0 row carrying
     failure_type / failure_ctx / failure_tool + failure_fallback=True.
  ② consumed marker (a real memory write already stamped it) → no-op,
     no synthesized row.
  ③ re-flush with the same ctx collapses to the SAME row via the
     content-hash idempotent upsert (no stack of garbage rows).
"""
import sys, pathlib, tempfile, json
from collections import deque

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    import threading
    p._lock = threading.Lock()
    p.session_id = "sess_A"
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = None
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.trigger_router = None  # isolate the fallback path; no trigger routing
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = {"lambda_days": 0.05}
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._session_drafts = {}
    p._pending_memories = []
    p._last_recall = None
    # _save_memory internals the fallback path touches:
    p._turn_marker_index = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._episodic_written = set()
    return p


def _read_all(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


def _seed_marker(p, sid="sess_A", ftype="retry_loop", tool="terminal",
                 ctx="terminal: [Command timed out after 300s]"):
    p._pending_failure_meta[sid] = {
        "failure_type": ftype,
        "tool": tool,
        "ctx": ctx,
        "ts": "2026-11-13T09:59:25.845000",
    }


# ---- ① unconsumed marker → synthesized L0 row ----------------------------

def test_flush_synthesizes_l0_row_on_unconsumed_marker():
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        rows = _read_all(p)
        # Exactly one row, carrying the failure metadata + fallback stamp.
        assert len(rows) == 1, f"expected 1 synthesized row, got {len(rows)}"
        md = rows[0].get("metadata", {})
        assert md.get("failure_type") == "retry_loop"
        assert md.get("failure_tool") == "terminal"
        assert "300s" in (md.get("failure_ctx") or "")
        assert md.get("failure_fallback") is True
        assert md.get("failure_origin_sid") == "sess_A"
        # The body is a self-describing lesson (RRF recall keywords).
        assert "失败教训" in rows[0]["content"]
        assert "terminal" in rows[0]["content"]
        # Marker consumed: no re-fire next session.
        assert "sess_A" not in p._pending_failure_meta


# ---- ② consumed marker → no-op (no synthesized row) ----------------------

def test_flush_noop_when_marker_absent():
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        # No marker seeded — the normal case where a real memory write already
        # consumed it via _failure_meta_for.
        p._flush_unconsumed_failure_markers("sess_A")
        assert _read_all(p) == []


# ---- ③ re-flush same ctx → idempotent upsert (one row, no stack) ---------

def test_reflush_same_ctx_collapses_to_one_row():
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        first = _read_all(p)
        assert len(first) == 1
        first_hash = first[0].get("metadata", {}).get("content_hash")
        # Simulate a second session with the SAME wall (same ctx): the content
        # body is byte-identical → content-hash upsert keeps ONE row.
        _seed_marker(p)  # re-seed the same marker
        p._flush_unconsumed_failure_markers("sess_A")
        second = _read_all(p)
        assert len(second) == 1, "re-flush must not stack a second row"
        assert second[0].get("metadata", {}).get("content_hash") == first_hash


# ---- ④ distinct ctx → genuinely new row (no false dedup) ----------------

def test_reflush_distinct_ctx_is_new_row():
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p, ctx="terminal: HTTP 503 upstream")
        p._flush_unconsumed_failure_markers("sess_A")
        _seed_marker(p, ctx="terminal: Permission denied /root/x")
        p._flush_unconsumed_failure_markers("sess_A")
        rows = _read_all(p)
        assert len(rows) == 2, "distinct walls must be distinct rows"
