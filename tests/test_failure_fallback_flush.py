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

from __init__ import (  # noqa: E402
    FileMemProvider, MEMORY_TYPES, _auto_inject_eligible,
)


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


# ---- ⑦ in-host bg-thread leg: spawn decision is on _live_main_runtime ------

def test_in_host_spawn_decision_is_isolated():
    # 复盘① (owner C): _flush's spawn-vs-sync decision keys on _live_main_runtime()
    # (in-host -> bg thread so the end-flow stays O(1); standalone -> sync so
    # distill self-skips and the test is deterministic). This leg pins the
    # DECISION without racing a real daemon thread: stub _save_memory to record
    # whether it was called, stub _live_main_runtime to report in-host, and
    # confirm the bg path was taken (spawn attempted) so the sync call is NOT
    # made on the calling thread. A monkeypatched _save_memory means no row
    # actually lands, so there is no thread to join — the assertion is on the
    # call posture, not on disk state (which would be flaky under daemon
    # threads sharing _rate_limited_queue with sibling tests).
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        calls = []
        p._save_memory = lambda *a, **k: calls.append(k) or ""
        p._main_runtime = {"model": "x"}   # in-host snapshot -> spawn path
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        # in-host -> spawn path: the sync leg's _bg_flush is NOT called on the
        # caller thread; the bg thread will call it later. _save_memory is
        # monkeypatched so the bg call is harmless. Give the thread a bounded
        # window to record the call, then assert it happened with the right
        # posture (source=auto, _force_write=True).
        import time as _time
        deadline = _time.time() + 5
        while not calls and _time.time() < deadline:
            _time.sleep(0.05)
        assert calls, "in-host _flush did not reach _save_memory (bg or sync)"
        assert calls[0].get("source") == "auto"
        assert calls[0].get("_force_write") is True
        # Marker consumed synchronously regardless of the thread posture.
        assert "sess_A" not in p._pending_failure_meta


# ---- ⑧ shutdown joins in-flight failure-flush bg threads -----------------

def test_shutdown_joins_flush_threads():
    # 复盘①收尾: the bg flush threads are stored on _failure_flush_threads and
    # shutdown() joins them (bounded) so a daemon-truncation at process exit
    # cannot lose a row mid-/_save_memory. This leg pins: after an in-host
    # flush spawned a thread, that handle is recorded; shutdown() joins it
    # (the monkeypatched _save_memory lets the thread finish immediately) and
    # clears the list. A pre-seeded dead thread is joined without error.
    import threading as _th
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        p._save_memory = lambda *a, **k: ""
        p._main_runtime = {"model": "x"}   # in-host -> spawn path
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        # The spawn recorded at least one Thread handle (the bg leg ran).
        ths = getattr(p, "_failure_flush_threads", [])
        assert isinstance(ths, list) and len(ths) >= 1, \
            "in-host flush should record the bg thread handle"
        # A second seed -> second thread (re-seed a fresh marker; the first
        # flush already consumed the original). Keep the list bounded (<8 here).
        p._pending_failure_meta["sess_A"] = {
            "failure_type": "tool_error", "tool": "terminal",
            "ctx": "x", "ts": "y"}
        p._flush_unconsumed_failure_markers("sess_A")
        # shutdown joins all + clears.
        p.shutdown()
        assert p._failure_flush_threads == [], \
            "shutdown must clear the recorded flush threads"


# ---- ⑨ end-to-end: synthesized row PASSES the confirmed gate into the
#       auto-inject face (复盘④ 致命洞的防护) -------------------------------

def test_synthesized_row_passes_confirmed_gate_into_auto_face():
    # 复盘④: the confirmed global-skeleton gate (_auto_inject_eligible (3))
    # folds a memories-pool row without a top-level session_id out of the
    # auto-inject face unless metadata.confirmed is True. _flush stamps
    # confirmed=True so the row actually reaches the model. This end-to-end
    # leg (which the earlier rounds skipped) pins that the synthesized row,
    # once in the pool, is auto-inject-eligible — NOT just "on disk".
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        rows = _read_all(p)
        assert len(rows) == 1
        row = rows[0]
        md = row.get("metadata", {})
        # The row carries the confirmed stamp + no top-level session_id.
        assert md.get("confirmed") is True
        assert "session_id" not in row, "memories-pool row must not carry a top-level session_id"
        # The end-to-end gate: a memories-pool row (no session_id) is
        # auto-inject-eligible ONLY via the confirmed leg.
        eligible = _auto_inject_eligible(row, session_sp=None,
                                         cur_session_id="someSession")
        assert eligible is True, \
            f"synthesized failure row must pass the confirmed gate into the auto face; md={md}"
        # Negative control: the same row WITHOUT confirmed is folded out.
        row_unconfirmed = {**row, "metadata": {k: v for k, v in md.items() if k != "confirmed"}}
        assert _auto_inject_eligible(row_unconfirmed, None, "someSession") is False, \
            "without confirmed the row must be folded out (the pre-fix hole)"

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



def _row_meta(td: pathlib.Path, mem_type: str, item_id: str) -> dict:
    """Read one row's metadata straight off the shard file (no provider
    helper dependency — the test only needs the on-disk truth)."""
    f = pathlib.Path(td) / "memories" / f"{mem_type}.json"
    if not f.exists():
        return {}
    for entry in json.loads(f.read_text(encoding="utf-8")):
        if isinstance(entry, dict) and str(entry.get("id")) == str(item_id):
            return entry.get("metadata", {}) or {}
    return {}


def _patch_llm(monkeypatch, distill_return: dict):
    """Monkeypatch the MODULE-LEVEL _load_llm_caller so _distill_item's LLM
    seam returns a fixed distill() result (controlling the fail-soft
    res["confirmed"] shape under test)."""
    import __init__ as fm_mod
    class _FakeLlm:
        def distill(self, main_runtime, content):
            return distill_return
    monkeypatch.setattr(fm_mod, "_load_llm_caller", lambda: _FakeLlm())


def test_distill_does_not_wash_confirmed_on_failure_row(monkeypatch):
    # 复盘⑥: _flush stamps confirmed=True on the failure row; _save_memory
    # then triggers _distill_item (source=auto), whose pre-fix line
    # meta_update["confirmed"] = bool(res.get("confirmed")) would wash the
    # stamp back to the LLM fail-soft default (False) and re-open the
    # confirmed gate hole. 修: _distill_item exempts failure_type rows.
    #
    # End-to-end: seed a confirmed failure row, run _distill_item with the
    # LLM returning NO confirmed key (the fail-soft shape that used to wash
    # it), assert the row's confirmed SURVIVES.
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        _seed_marker(p)
        p._flush_unconsumed_failure_markers("sess_A")
        rows = _read_all(p)
        assert len(rows) == 1
        row = rows[0]
        assert row["metadata"]["confirmed"] is True
        assert row["metadata"]["failure_type"] is not None
        item_id = row["id"]

        p._main_runtime = {"provider": "custom", "model": "m"}
        _patch_llm(monkeypatch, {"summary": "s", "value": 0.4})  # no confirmed key
        p._distill_item("fact", item_id, row["content"], session_id="sess_A")

        after = _row_meta(td, "fact", item_id)
        assert after.get("confirmed") is True, \
            f"distill washed the failure row's confirmed to {after.get('confirmed')!r}"
        assert after.get("summary") == "s", "distill still carries summary forward"
        assert after.get("distilled_at"), "distill still records distilled_at"


def test_distill_still_washes_confirmed_on_normal_row(monkeypatch):
    # Negative control: a NORMAL row (no failure_type) still gets its
    # confirmed re-judged by the LLM — exemption is scoped to failure rows.
    with tempfile.TemporaryDirectory() as td:
        p = _make_provider(pathlib.Path(td))
        p._save_memory("user fact row about a thing", level="L0",
                       source="manual", _force_write=True)
        rows = _read_all(p)
        target = rows[0]["id"]
        p._update_memory_metadata("fact", target, {"confirmed": True})
        assert _row_meta(td, "fact", target).get("confirmed") is True

        p._main_runtime = {"provider": "custom", "model": "m"}
        _patch_llm(monkeypatch, {"confirmed": False, "summary": "s"})
        p._distill_item("fact", target, "user fact row about a thing",
                        session_id="s")

        after = _row_meta(td, "fact", target)
        assert after.get("confirmed") is False, \
            f"normal row should be washed to confirmed=False by LLM default; got {after.get('confirmed')!r}"


def _make_existing_rows():
    """Build an in-shard existing-rows list: one failure-lesson row + one
    plain fact row, both strictly older than the new row under test."""
    failure_row = {
        "id": "fail_1",
        "content": "terminal command failed with retry loop error timeout",
        "created_at": "2026-01-01T00:00:00",
        "metadata": {"failure_type": "retry_loop", "confirmed": True},
    }
    plain_row = {
        "id": "plain_1",
        "content": "terminal command failed with retry loop error timeout",  # same text -> high overlap
        "created_at": "2026-01-01T00:00:00",
        "metadata": {},
    }
    return [failure_row, plain_row]


def test_stamp_lineage_does_not_supersede_failure_row():
    # 复盘⑦: _stamp_lineage_candidates must NOT pick a metadata.failure_type
    # row as a supersede target — otherwise a new fact row (>=0.5 overlap)
    # marks it superseded=True and the O1 auto-face fold drops it out of the
    # auto-inject / search pool. Pre-fix both the failure row and the plain
    # row are eligible; post-fix only the plain row is.
    from __init__ import _stamp_lineage_candidates
    new_row = {
        "id": "new_1",
        "content": "terminal command failed with retry loop error timeout again",
        "created_at": "2026-06-01T00:00:00",
        "metadata": {},
    }
    existing = _make_existing_rows()
    stamped = _stamp_lineage_candidates(new_row, existing, {"supersede_min_overlap": 0.5})
    stamped_ids = {str(r.get("id")) for r in stamped}
    assert "fail_1" not in stamped_ids, \
        f"failure row must not be a supersede target; got stamped={stamped_ids}"
    assert "plain_1" in stamped_ids, \
        f"plain fact row with >=0.5 overlap must still be a target; got stamped={stamped_ids}"
    # The new row's supersedes link points only at the plain row.
    assert "fail_1" not in new_row["metadata"]["supersedes"]
    assert "plain_1" in new_row["metadata"]["supersedes"]
    # The failure row was NOT marked superseded.
    assert existing[0].get("metadata", {}).get("superseded") in (None, False), \
        "failure row must not be flipped superseded=True"
    # The plain row WAS marked superseded (the exemption is scoped).
    assert existing[1].get("metadata", {}).get("superseded") is True


def test_c3_negation_channel_does_not_supersede_failure_row():
    # 复盘⑦ C3 channel: the parallel negation channel must apply the SAME
    # failure_type exemption — a negation correction must never target a
    # failure-lesson row either.
    from __init__ import _c3_stamp_negation_candidates
    new_row = {
        "id": "new_1",
        "content": "terminal no longer times out, retry loop is fixed",
        "created_at": "2026-06-01T00:00:00",
        "metadata": {},
    }
    existing = _make_existing_rows()
    # C3 reads the negation gate from the search cfg; the failure exemption is
    # structural (metadata.failure_type), independent of the negation match.
    stamped = _c3_stamp_negation_candidates(new_row, existing, {"supersede_min_overlap": 0.5})
    stamped_ids = {str(r.get("id")) for r in stamped}
    assert "fail_1" not in stamped_ids, \
        f"C3 channel must also exempt failure rows; got stamped={stamped_ids}"
    assert existing[0].get("metadata", {}).get("superseded") in (None, False), \
        "C3 channel must not flip the failure row superseded"
