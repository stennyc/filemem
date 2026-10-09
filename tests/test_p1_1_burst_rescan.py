"""P1-1 burst-reduction & re-scan test suite (task t_9823942a).

Replicates the log.2 L36036-36214 incident: a burst of ~50 distinct L0
auto-save bodies fired within ~1.6s because each ``sync_turn`` re-scanned the
full history, so every re-scan re-routed the same bodies into the rate-limited
queue (overflowing ``cap=30`` and degrading to ``raw_pending``).

The P1-1 per-session high-water mark (HWM) makes re-scans of an UNCHANGED
window produce zero new distinct queued bodies; only genuinely new
(incremental) messages are routed. This suite drives the LIVE implementation
offline (no gateway, no LLM, no host) and asserts the five invariants the card
calls out:

  #2  new distinct queued bodies == 0 when the message window is unchanged
  #3  only bodies derived from incremental parts are produced when new
      messages are added
  #4  the measured re-scan burst count <= 2
  #5  bg-review sees only the incremental tail, not the full 324-item history

Run (matches the card's recipe):
    cd /root/.hermes/plugins/filemem
    PYTHONPATH=/usr/local/lib/hermes-agent \
      /usr/bin/python3 -m pytest tests/test_p1_1_burst_rescan.py -q -p no:cacheprovider

NOTE (scope): new file under tests/ only — zero bytes changed in __init__.py.
Measurement is on the rate-limited queue's DISTINCT content bodies (the HWM
directly cuts the number of trigger-loop routes, which is what the secondary
content-hash / same-marker dedup layers do NOT cover on the re-scan path).
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider, MemoryTriggerRouter  # noqa: E402


# ── burst geometry (faithful to the log.2 incident) ──────────────────────────
BURST = 50            # 50 distinct message bodies in the window
RESCAN_TIMES = 50     # 50 auto-save re-scans within the ~1.6s window
INTERVAL = 30.0       # large save interval -> every trigger save queues
HIST_SIZE = 324       # the "full 324-item history" the bg-review re-scan sees


def _make_provider(tmp: Path, sid: str = "s_burst") -> FileMemProvider:
    """Minimal offline provider wired to a tmp storage dir.

    Enough fields for the REAL trigger-router loop + rate-limited queue + HWM
    to run without initialize(); interval large so every L0 save queues rather
    than writing to disk (keeps the test fast + side-effect-free).
    """
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = "test_burst"
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = None
    p.sessions_dir = None                       # skip the session-trace append
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False
    p._decay = {}
    p._last_save_time = time.time()
    p._save_interval = INTERVAL
    p._session_save_count = 0
    p._session_save_count_max = 1000
    p._turn_marker_index = {}
    p._session_hwm = {}                          # P1-1 HWM (the object under test)
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 10 ** 6          # high cap: no overflow, isolate the queue
    p._pending_memories = []
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._main_runtime = None                       # reclassify LLM face -> skip
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._llm_consolidating = False
    p._last_consolidation_ts = 0.0
    return p


def _window(n: int) -> list:
    """n distinct L0 trigger bodies (each carries an L0 environment keyword).

    Keyword set {ip, port, config, endpoint} routes to L0 auto_save. Distinct
    per index so same-marker / content-hash dedup do NOT fold them — the
    reduction the HWM provides is then measurable.
    """
    return [
        {
            "role": "user",
            "content": (
                f"Server ip 10.0.0.{i} listening on port {8000 + i} at "
                f"config endpoint /api/v{i} route table entry {i}."
            ),
        }
        for i in range(n)
    ]


def _distinct_queued(p) -> set:
    """Distinct content bodies currently held in the rate-limited queue."""
    return {it["content"] for it in p._rate_limited_queue}


class _CountingRouter:
    """Wraps the real router and records every routed content.

    Lets assertion #5 prove the trigger loop routed ONLY the incremental tail
    and none of the full-history items (the HWM gate's job).
    """

    def __init__(self, base):
        self.base = base
        self.calls = []

    def detect_and_route(self, content):
        self.calls.append(content)
        return self.base.detect_and_route(content)


class TestP11BurstReduction:
    """Burst re-scan reduction: 50 distinct bodies -> <= 2 with the HWM."""

    @pytest.fixture(autouse=True)
    def _no_flush(self, tmp_path, monkeypatch):
        # Drain nothing to disk during the measured phase: the HWM cuts the
        # queue-append events themselves; we measure the queue, not the pool.
        monkeypatch.setattr(
            FileMemProvider, "_flush_rate_limited_queue", lambda self: None
        )
        self._tmp = tmp_path

    def test_unchanged_window_zero_new_bodies(self, tmp_path):
        """#2: re-scanning an unchanged window adds 0 new distinct queue bodies."""
        p = _make_provider(tmp_path)
        window = _window(BURST)

        # Setup pass establishes the HWM (legitimately new content -> queues).
        p.sync_turn("", "", messages=window, session_id="s_burst")
        assert _distinct_queued(p) == set(m["content"] for m in window)
        hwm = p._session_hwm["s_burst"]
        assert hwm == BURST - 1

        # Drain, then fire the burst re-scan of the SAME unchanged window.
        p._rate_limited_queue.clear()
        for _ in range(RESCAN_TIMES):
            p.sync_turn("", "", messages=window, session_id="s_burst")
        new_bodies = _distinct_queued(p)

        assert new_bodies == set(), (
            f"unchanged-window re-scan should queue 0 new bodies, "
            f"got {len(new_bodies)}: {list(new_bodies)[:3]!r}"
        )
        # HWM must not have regressed by the re-scan.
        assert p._session_hwm["s_burst"] == BURST - 1

    def test_burst_count_ceiling(self, tmp_path):
        """#4: the measured re-scan burst count (new distinct bodies) is <= 2."""
        p = _make_provider(tmp_path)
        window = _window(BURST)
        p.sync_turn("", "", messages=window, session_id="s_burst")
        p._rate_limited_queue.clear()

        burst_count = 0
        for _ in range(RESCAN_TIMES):
            before = _distinct_queued(p)
            p.sync_turn("", "", messages=window, session_id="s_burst")
            burst_count = max(burst_count, len(_distinct_queued(p) - before))

        # The "50 -> <= 2" reduction: without the HWM each re-scan would re-queue
        # all 50 distinct bodies; with it the re-scan burst is bounded.
        assert burst_count <= 2, f"re-scan burst {burst_count} bodies exceeds the 2 ceiling"

    def test_burst_reduction_before_after(self, tmp_path):
        """Concrete 50 -> <= 2 A/B: the HWM is what produces the reduction."""
        window = _window(BURST)

        # AFTER (HWM live): re-scan burst.
        p_hwm = _make_provider(tmp_path / "after")
        p_hwm.sync_turn("", "", messages=window, session_id="s_burst")
        p_hwm._rate_limited_queue.clear()
        for _ in range(RESCAN_TIMES):
            p_hwm.sync_turn("", "", messages=window, session_id="s_burst")
        hwm_distinct = len(_distinct_queued(p_hwm))

        # BEFORE (HWM cleared each re-scan): the legacy full-history re-route.
        p_legacy = _make_provider(tmp_path / "before")
        p_legacy.sync_turn("", "", messages=window, session_id="s_burst")
        p_legacy._rate_limited_queue.clear()
        for _ in range(3):  # 3 re-scans is enough to expose the legacy behavior
            p_legacy._session_hwm = {}          # simulate "no high-water mark"
            p_legacy.sync_turn("", "", messages=window, session_id="s_burst")
        legacy_distinct = len(_distinct_queued(p_legacy))

        # The legacy path re-queues the whole 50-body window; the HWM path does not.
        assert legacy_distinct >= BURST, (
            f"legacy (no-HWM) burst should re-queue all {BURST} distinct bodies, "
            f"got {legacy_distinct}"
        )
        assert hwm_distinct <= 2 and hwm_distinct < legacy_distinct, (
            f"HWM burst {hwm_distinct} not reduced below legacy {legacy_distinct}"
        )

    def test_incremental_only_when_new_messages_added(self, tmp_path):
        """#3: adding new messages produces only the incremental bodies, not a re-route."""
        p = _make_provider(tmp_path)
        base = _window(BURST)
        p.sync_turn("", "", messages=base, session_id="s_burst")
        hwm_after_base = p._session_hwm["s_burst"]
        assert hwm_after_base == BURST - 1

        incremental = _window(BURST + 5)[BURST:]   # the 5 genuinely-new bodies
        full = base + incremental                   # 55-item window

        p._rate_limited_queue.clear()
        p.sync_turn("", "", messages=full, session_id="s_burst")
        new = _distinct_queued(p)

        inc_set = {m["content"] for m in incremental}
        base_set = {m["content"] for m in base}

        # Only the 5 incremental bodies entered the queue...
        assert new == inc_set, (
            f"expected only the {len(inc_set)} incremental bodies, "
            f"got {len(new)} distinct"
        )
        # ...and NONE of the 50 already-consumed bodies were re-routed.
        assert not (new & base_set), "re-scanned history bodies leaked into the queue"
        # HWM advanced exactly to the new tail.
        assert p._session_hwm["s_burst"] == BURST + 5 - 1

    def test_bg_review_sees_incremental_not_full_history(self, tmp_path):
        """#5: bg-review over the full 324-item history routes only the incremental tail.

        HWM is seeded to 323 (as a prior turn that consumed the 324-item history
        would have advanced it). The bg-review turn re-hands the full history + 5
        new messages; the HWM gate must route exactly the 5 new bodies and none of
        the 324 old ones. The "review the conversation" prompt text must NOT be
        auto-saved as an L0 fact (the L3335 second-path guard).
        """
        p = _make_provider(tmp_path)
        p.trigger_router = _CountingRouter(p.trigger_router)
        full = _window(HIST_SIZE + 5)
        old_set = {m["content"] for m in full[:HIST_SIZE]}
        new_set = {m["content"] for m in full[HIST_SIZE:]}

        # Seed the HWM as if the full 324-item history was already consumed.
        p._session_hwm["s_burst"] = HIST_SIZE - 1

        p.sync_turn(
            "Review the conversation above and flag anything worth remembering.",
            "",
            messages=full,
            session_id="s_burst",
        )

        routed = set(p.trigger_router.calls)
        routed_msgs = {c for c in routed if c in old_set | new_set}

        # Only the 5 incremental bodies were routed through the trigger loop.
        assert routed_msgs == new_set, (
            f"bg-review routed {len(routed_msgs)} history bodies, expected the "
            f"{len(new_set)} incremental tail"
        )
        # No body from the full 324-item history was re-routed.
        assert not (routed_msgs & old_set), "full-history re-route leaked into bg-review"
        # The bg-review prompt text stayed out of the L0 pool (guard L3335).
        pool_texts = {it["content"] for it in p._rate_limited_queue}
        assert not any(t.lower().startswith("review the conversation") for t in pool_texts), (
            "bg-review prompt text was auto-saved as an L0 fact (guard L3335 missing)"
        )
        # HWM advanced to the new tail index.
        assert p._session_hwm["s_burst"] == HIST_SIZE + 5 - 1


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as _d:
        sys.exit(
            pytest.main([__file__, "-q", "-p", "no:cacheprovider"])
            or 0
        )
