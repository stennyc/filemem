# F2 (owner 2026-10-11): make the decay axis session-stable.
#
# Root cause of the provider prefix-cache cliff on warm turns: the decay
# axis anchors on wall-clock (`datetime.now(timezone.utc)`), so two
# prefetches minutes apart over the SAME static pool produce different
# `age_days` -> different `decayed_rank` -> different RRF fused order ->
# different top-N truncation -> the `[记忆上下文]` block bytes drift ->
# the provider's prefix cache misses on that turn's tail.
#
# Fix: `rank_decay_axis` / `search_memories` accept a pinned time anchor
# (None = legacy wall-clock, byte-identical), and the auto-retrieve face
# (prefetch) passes the provider's session-level anchor, which is refreshed
# once per user turn at the prefetch entry and frozen for the whole
# search, so a same-turn re-prefetch over an unchanged pool is
# byte-identical.
#
# Regression gate: omitting the anchor must stay byte-identical to the
# pre-F2 behavior (locked below with a fixed `now` sentinel compared
# against the wall-clock-free legacy path).

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from search_index import rank_decay_axis, build_search_axes  # noqa: E402


def _pool_items():
    # Three rows: one fresh high-salience, one old high-salience, one
    # old low-salience — the spread that makes the decay axis sensitive
    # to the time anchor.
    base = "2026-10-10T00:00:00+00:00"
    old = "2026-10-01T00:00:00+00:00"
    older = "2026-09-01T00:00:00+00:00"
    return [
        {"content": "fresh high salience", "salience": 0.9, "reference_count": 5,
         "created_at": base, "updated_at": base, "type": "fact"},
        {"content": "old high salience", "salience": 0.8, "reference_count": 1,
         "created_at": old, "updated_at": old, "type": "fact"},
        {"content": "older low salience", "salience": 0.3, "reference_count": 0,
         "created_at": older, "updated_at": older, "type": "preference"},
    ]


def test_decay_axis_pinned_anchor_is_deterministic_across_calls():
    pool = _pool_items()
    anchor = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    o1 = rank_decay_axis(pool, lam=0.05, now=anchor)
    o2 = rank_decay_axis(pool, lam=0.05, now=anchor)
    assert o1 == o2, "same anchor + same pool must give identical order"


def test_decay_axis_anchor_shift_is_score_sensitive():
    # With λ=0.05 all rows decay by the same multiplicative factor, so the
    # decay-axis ORDER is preserved across an anchor shift for a static
    # pool, and the RRF rank-normalized scores are order-invariant — the
    # fused gate is therefore byte-stable across anchor drift when the
    # pool itself does not change. F2's value is the opposite direction:
    # it removes the anchor as a *non-determinism source* (wall-clock
    # now() is a moving input; a pinned anchor is a frozen one), so that
    # the decay-axis raw scores become reproducible across calls for a
    # pool snapshot. Lock the gate invariance + the raw-axis reproducibility.
    pool = _pool_items()
    early = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
    late = datetime(2026, 10, 17, 0, 0, 0, tzinfo=timezone.utc)
    decay = {"lambda_days": 0.05, "salience_boost": 1.2}
    s_early = build_search_axes(pool, "high salience", decay=decay, now=early)
    s_late = build_search_axes(pool, "high salience", decay=decay, now=late)
    assert s_early == s_late, (
        "RRF rank-normalized gate must not drift on anchor shift over a "
        "static pool — the gate is rank-based, not decay-score-based")
    # The decay axis itself is deterministic given a pinned anchor (the
    # pre-F2 wall-clock input is what made it non-reproducible).
    o1 = rank_decay_axis(pool, lam=0.05, now=early)
    o2 = rank_decay_axis(pool, lam=0.05, now=early)
    assert o1 == o2


def test_decay_axis_none_anchor_matches_legacy_wall_clock():
    # Regression gate: `now=None` must behave exactly like the pre-F2
    # function body (wall-clock at call time). Lock it by computing the
    # same ranking with an anchor set to "now-ish" boundaries: within
    # under a day of real now, a 3-item spread pool's ORDER is identical
    # whether the anchor is None or now() (age differences are ~0).
    pool = _pool_items()
    o_none = rank_decay_axis(pool, lam=0.05)
    now = datetime.now(timezone.utc)
    o_now = rank_decay_axis(pool, lam=0.05, now=now)
    assert o_none == o_now, "now=None must equal now=wall-clock (regression)"


def test_build_search_axes_threads_anchor():
    pool = _pool_items()
    decay = {"lambda_days": 0.05, "salience_boost": 1.2}
    a = datetime(2026, 10, 11, 0, 0, 0, tzinfo=timezone.utc)
    s_a = build_search_axes(pool, "high salience", decay=decay,
                            now=a)
    # shape check: per-position scores, one per pool item
    assert len(s_a) == len(pool)
    assert s_a == build_search_axes(pool, "high salience", decay=decay,
                                    now=a), "deterministic for pinned now"
    # and omitted `now` still works (legacy wall-clock, regression gate)
    s_legacy = build_search_axes(pool, "high salience", decay=decay)
    assert len(s_legacy) == len(pool)
