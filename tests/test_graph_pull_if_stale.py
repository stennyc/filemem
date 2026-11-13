"""A1-1 read-side graph freshness pull: tests for
FileMemProvider._graph_pull_if_stale (the search hot-path pull that catches a
stale graph up on read, without waiting for the 30-min consolidation tick).

Acceptance coverage (the whole point of A1-1 — a reactive read of a stale
graph must catch up WITHOUT the tick-side cooldown / water-level gates):
1. stale store + no prior pull + not building -> spawns a background build
   (the _graph_building flag flips True on the way in; _graph_last_pull_ts
   is stamped).
2. a second pull immediately after the first (within the read-side
   throttle) -> no re-spawn (_graph_last_pull_ts gate holds).
3. fresh store -> pull is a no-op (no spawn, no flag set).
4. a pull while a build is already in flight (_graph_building True) -> no
   double-spawn (re-entrancy lock holds).
5. pull THOUGHTLY bypasses the tick-side 30-min cooldown: even when
   _graph_last_build_ts == 0 (cold start, which the tick cooldown would
   block forever) a stale pull STILL spawns. This is the regression guard
   against re-introducing the SA2-5 "defer to a later window" dead-end.

All stores are tmp synthetic; the module-level _graph_spawn_bg_thread is
monkeypatched to a recorder (no real ~39s build runs, no daemon thread
leaks). The provider is built via __new__ (no initialize()) and given just
the attrs the pull method reads.
"""
import json
import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


def _row(i):
    return {"id": f"fact_{i}", "content": f"row {i} pwa 9200 manifest login",
            "type": "fact", "created_at": "2026-11-01T00:00:00+00:00"}


def _make_store(tmp, rows, build_graph=True):
    root = tmp / "store"
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "fact.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if build_graph:
        import graph_autorebuild
        bg = graph_autorebuild._load_build_graph()
        bg.build_and_write(root)
    return root


def _make_pull_provider(root, *, building=False, last_pull_ts=0.0,
                        last_build_ts=0.0, spawned):
    """A FileMemProvider wired just enough to run _graph_pull_if_stale.
    The module-level spawner is already monkeypatched by the caller to
    record into `spawned`."""
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = root
    p.session_id = "pull-test"
    p._lock = threading.Lock()
    p._decay = {"search": dict(DECAY_DEFAULTS["search"])}
    p._graph_building = building
    p._graph_last_pull_ts = last_pull_ts
    p._graph_last_build_ts = last_build_ts
    p._graph_base_rows = 0
    p._graph_pending = False
    return p


def _force_stale(root):
    """Append a corpus row AND nudge fact.json's mtime strictly AFTER
    graph.json's (an explicit sleep defeats the same-second mtime window
    that would let _graph_freshness read 'fresh' on a fast machine)."""
    import os
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(i) for i in range(6)], ensure_ascii=False),
        encoding="utf-8")
    time.sleep(0.05)
    gmt = (root / "graph.json").stat().st_mtime
    cmt = (root / "memories" / "fact.json").stat().st_mtime
    if cmt <= gmt:
        os.utime(root / "memories" / "fact.json",
                 (gmt + 1, gmt + 1))


@pytest.fixture
def recorded_spawn(monkeypatch):
    """Patch fm._graph_spawn_bg_thread to a recorder (no real thread).
    Yields a dict {count, args}; the pull method's `spawn = ...()` then
    `spawn(...)` lands here."""
    spawned = {"count": 0, "args": []}

    def _record(target, args=(), name="", on_done=None):
        spawned["count"] += 1
        spawned["args"].append(args)
        return None

    monkeypatch.setattr(fm, "_graph_spawn_bg_thread", lambda: _record)
    return spawned


def test_stale_pull_spawns_background_build(tmp_path, recorded_spawn):
    """1. stale store + no prior pull + not building -> one spawn."""
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    _force_stale(root)
    p = _make_pull_provider(root, spawned=recorded_spawn)
    assert p._graph_freshness()["status"] == "stale"
    p._graph_pull_if_stale()
    assert recorded_spawn["count"] == 1, recorded_spawn
    assert p._graph_building is True
    assert p._graph_last_pull_ts > 0.0
    assert recorded_spawn["args"] == [(str(root),)]


def test_pull_throttled_within_window(tmp_path, recorded_spawn):
    """2. two pulls inside the read-side throttle -> exactly ONE spawn."""
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    _force_stale(root)
    p = _make_pull_provider(root, spawned=recorded_spawn)
    p._graph_pull_if_stale()
    assert recorded_spawn["count"] == 1
    p._graph_pull_if_stale()  # second, within 5-min throttle + in-flight
    assert recorded_spawn["count"] == 1, "second pull must not re-spawn"


def test_fresh_pull_is_noop(tmp_path, recorded_spawn):
    """3. fresh store -> pull is a no-op (no spawn, flag stays False)."""
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    p = _make_pull_provider(root, spawned=recorded_spawn)
    assert p._graph_freshness()["status"] == "fresh"
    p._graph_pull_if_stale()
    assert recorded_spawn["count"] == 0, recorded_spawn
    assert p._graph_building is False


def test_pull_during_inflight_build_is_noop(tmp_path, recorded_spawn):
    """4. a pull while a build is in flight -> no double-spawn."""
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    _force_stale(root)
    p = _make_pull_provider(root, building=True, spawned=recorded_spawn)
    p._graph_pull_if_stale()
    assert recorded_spawn["count"] == 0, "pull during in-flight build: no spawn"


def test_pull_bypasses_cold_start_cooldown(tmp_path, recorded_spawn):
    """5. COLD-START regression guard: _graph_last_build_ts == 0.0. The
    tick-side cooldown (`now - 0.0 < 30min` -> True) would block a stale
    rebuild FOREVER. The read-side pull must NOT apply that gate: a stale
    read still spawns even at cold start. This is the exact
    defer-reintroduction A1-1 must avoid."""
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    _force_stale(root)
    p = _make_pull_provider(root, last_build_ts=0.0,
                            last_pull_ts=time.time() - 3600,
                            spawned=recorded_spawn)
    p._graph_pull_if_stale()
    assert recorded_spawn["count"] == 1, recorded_spawn
    assert p._graph_building is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
