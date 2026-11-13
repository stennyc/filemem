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

def test_reflush_distinct_tool_is_new_row():
    # Body no longer carries ctx (复盘② fix), so dedup granularity is
    # tool+type, not ctx. Same tool+type with different ctx COLLAPSES to one
    # row; a different tool is a genuinely different wall = a new row.
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p, tool="terminal", ftype="retry_loop",
                     ctx="terminal: HTTP 503 upstream")
        p._flush_unconsumed_failure_markers("sess_A")
        # same tool+type, different ctx -> same fixed body -> collapse
        _seed_marker(p, tool="terminal", ftype="retry_loop",
                     ctx="terminal: Permission denied /root/x")
        p._flush_unconsumed_failure_markers("sess_A")
        assert len(_read_all(p)) == 1, "same tool+type collapses to one row"
        # different tool -> different body -> new row
        _seed_marker(p, tool="browser_vault", ftype="tool_error",
                     ctx="browser_vault: origin_mismatch")
        p._flush_unconsumed_failure_markers("sess_A")
        assert len(_read_all(p)) == 2, "different tool is a new row"


def test_body_immune_to_ctx_strong_signal_words():
    # 复盘② core guard: a ctx containing task/decision strong-signal words
    # (必须 / 拍板 / 待办 …) must NOT refile the synthesized row into the
    # task/decision shard (which would trip the plan-probe). The body is a
    # fixed skeleton, so _detect_type always returns 'fact' regardless of ctx.
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p, tool="terminal", ftype="retry_loop",
                     ctx="terminal: error: 必须用 background=true；拍板用 docker")
        p._flush_unconsumed_failure_markers("sess_A")
        rows = _read_all(p)
        assert len(rows) == 1
        # The row must land in the fact shard, NOT task/decision.
        assert rows[0]["type"] == "fact", \
            f"ctx strong-signal leaked into type: {rows[0]['type']}"
        # And the fixed body never contains the ctx's strong-signal words.
        assert "必须" not in rows[0]["content"]
        # The full ctx is preserved in metadata for the recall ⚠️ line.
        assert "必须用 background=true" in rows[0]["metadata"]["failure_ctx"]


# ---- ⑤ recall-side boost: _failure_reorder_key (pure) --------------------

def test_failure_reorder_key_puts_failure_rows_first():
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        fail_row = {"id": "f1", "type": "fact", "content": "wall",
                    "metadata": {"failure_type": "retry_loop"}}
        ord_row = {"id": "o1", "type": "fact", "content": "ordinary",
                   "metadata": {}}
        stale_row = {"id": "s1", "type": "fact", "content": "no meta"}
        # key: failure -> 0, everything else -> 1
        assert p._failure_reorder_key(fail_row) == 0
        assert p._failure_reorder_key(ord_row) == 1
        assert p._failure_reorder_key(stale_row) == 1
        # stable sort: failure row floats to the top, others keep relative
        # order (o1 before s1, both key 1).
        ordered = sorted([ord_row, stale_row, fail_row],
                         key=p._failure_reorder_key)
        assert [r["id"] for r in ordered] == ["f1", "o1", "s1"]
        # Byte-stability: a set with NO failure row keys all-1 -> the stable
        # sort preserves the exact input order (== pre-boost, no cache drift).
        no_fail = [ord_row, stale_row]
        assert [r["id"] for r in sorted(no_fail, key=p._failure_reorder_key)] \
            == ["o1", "s1"]


# ---- ⑥ boost precedes P0-2 cap: a low-rank failure row survives top-N ------

def test_boost_precedes_cap_so_low_rank_failure_row_survives():
    """AT-RISK① fix: the boost runs BEFORE the P0-2 cap, so a failure row that
    ranks 4th (below a cap=3 trim) is floated into the top-3 by the stable
    sort and therefore SURVIVES the cap — it is injected, not silently
    dropped. Pre-fix (boost after cap) the cap trimmed it first and the boost
    had nothing to reorder, so the 'wall you hit' row never reached the model."""
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        # A surviving pool where the failure row is LAST (rank 4 of 4, below
        # a cap=3 trim that keeps the first 3).
        fail_row = {"id": "f1", "type": "fact", "content": "wall",
                    "metadata": {"failure_type": "retry_loop"}}
        pool = [
            {"id": "o1", "type": "fact", "content": "a", "metadata": {}},
            {"id": "o2", "type": "fact", "content": "b", "metadata": {}},
            {"id": "o3", "type": "fact", "content": "c", "metadata": {}},
            fail_row,
        ]
        cap = 3
        # Reproduce the production order: boost (stable sort by key) THEN cap.
        boosted = sorted(pool, key=p._failure_reorder_key)
        surviving = boosted[:cap] if cap > 0 else boosted
        # The failure row (key 0) floats to the FRONT, so it is in the top-3.
        assert surviving[0]["id"] == "f1", \
            f"failure row must survive cap=3 (boost precedes cap), got {surviving}"
        # And the ordinary rows keep their relative order behind it.
        assert [r["id"] for r in surviving] == ["f1", "o1", "o2"]
        # Contrast: WITHOUT the boost (cap alone, pre-fix order) the failure
        # row at rank 4 would be trimmed away.
        no_boost_surviving = pool[:cap]
        assert "f1" not in [r["id"] for r in no_boost_surviving], \
            "pre-fix: cap alone would drop the rank-4 failure row"
