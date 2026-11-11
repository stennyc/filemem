"""SA2-4 graph auto-rebuild hook: tests for graph_autorebuild.ensure_fresh.

Acceptance coverage:
1. stale store -> ensure_fresh writes a fresh graph.json (fingerprint matches,
   returns True, .bak created on first mutation only)
2. fresh store -> ensure_fresh is a no-op (returns False, zero writes, no .bak)
3. missing graph.json + missing corpus -> status "missing" -> ensure_fresh is a
   no-op (returns False, no graph.json created — matches today's two-axis
   degraded behavior, does NOT introduce a new empty-graph behavior)
4. fail-soft: pointing the loader at a path that raises (e.g. corrupted
   build_graph module surface) -> ensure_fresh returns False, does not raise,
   touches nothing on disk
"""
import json
import pathlib
import shutil
import sys
import tempfile

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import graph_autorebuild  # noqa: E402


def _make_store(tmp, rows):
    """Build a minimal fake filemem store layout: <tmp>/memories/fact.json +
    <tmp>/decay.json (optional). Returns the root Path."""
    root = tmp / "store"
    (root / "memories").mkdir(parents=True)
    (root / "memories" / "fact.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return root


def _row(i):
    return {"id": f"fact_{i}", "content": f"row {i} about pwa 9200 manifest login",
            "type": "fact", "created_at": "2026-11-01T00:00:00+00:00"}


def test_stale_store_gets_rebuilt(tmp_path):
    # 5-row store is under the default min_corpus_rows=100 gate; the test
    # explicitly opts back in with a tiny threshold (the gate itself is
    # covered separately in test_min_corpus_gate_skips_tiny_store).
    root = _make_store(tmp_path, [_row(i) for i in range(5)])
    # Build once on a store that has no graph.json yet: no .bak possible
    # (.bak convention snapshots the PRIOR file, which doesn't exist here).
    bg = graph_autorebuild._load_build_graph()
    bg.build_and_write(root)
    first_fp = json.loads((root / "graph.json").read_text())["fingerprint"]
    assert not (root / "graph.json.bak").exists()  # no prior file, no snapshot

    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1), _row(2), _row(3), _row(4), _row(5)],
                   ensure_ascii=False), encoding="utf-8")

    assert bg.graph_status(root)["status"] == "stale"
    # Now a prior graph.json exists: the mutation snapshots it as .bak.
    assert graph_autorebuild.ensure_fresh(root, min_corpus_rows=1) is True
    new_fp = json.loads((root / "graph.json").read_text())["fingerprint"]
    assert new_fp != first_fp
    bak_bytes = (root / "graph.json.bak").read_bytes()
    assert json.loads(bak_bytes.decode())["fingerprint"] == first_fp
    # Idempotent .bak convention: a second mutation must NOT re-snapshot.
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1)], ensure_ascii=False), encoding="utf-8")
    assert graph_autorebuild.ensure_fresh(root, min_corpus_rows=1) is True
    assert (root / "graph.json.bak").read_bytes() == bak_bytes
    # now it is fresh
    assert bg.graph_status(root)["status"] == "fresh"


def test_min_corpus_gate_skips_tiny_store(tmp_path):
    """A store with fewer rows than min_corpus_rows is skipped entirely —
    this is what keeps test_s1b_tick_wiring's tiny fixtures (1-2 rows)
    from paying any graph build cost, regardless of join-budget timing."""
    root = _make_store(tmp_path, [_row(i) for i in range(3)])
    # No graph.json yet; even if it were "missing"/"stale", the corpus is
    # too small to be worth graphing — must return False WITHOUT writing
    # graph.json.
    assert graph_autorebuild.ensure_fresh(root) is False  # default min=100
    assert not (root / "graph.json").exists()
    # Same store, caller explicitly lowers the gate. There is still no
    # graph.json on disk, so graph_status verdicts "missing" (not "stale")
    # and the build is a no-op by design — but the gate itself (the row
    # count check, which runs FIRST, before graph_status) is proven by the
    # contrast below: forcing the gate through with min_corpus_rows=1
    # changes NOTHING yet (still missing, not stale), until we materialize
    # a graph.json and then drift the corpus to make it genuinely stale.
    bg = graph_autorebuild._load_build_graph()
    bg.build_and_write(root)  # now a real graph.json exists
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1), _row(2), _row(3)], ensure_ascii=False),
        encoding="utf-8")  # 4 rows: still under the DEFAULT gate (100) but
                          # the caller explicitly lowered it to 1 -> build proceeds
    assert graph_autorebuild.ensure_fresh(root, min_corpus_rows=1) is True
    # ...and with the default gate (100), the same 4-row store is skipped:
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1), _row(2), _row(3), _row(4)],
                   ensure_ascii=False), encoding="utf-8")
    assert graph_autorebuild.ensure_fresh(root) is False  # 5 < 100, gate holds


def test_soft_deadline_defers_second_pass(tmp_path, monkeypatch):
    """If graph_status alone exceeds max_wall_time_s, build_and_write must
    NOT run for this tick (deferred to the next tick), and ensure_fresh
    returns False."""
    root = _make_store(tmp_path, [_row(i) for i in range(100)])
    bg = graph_autorebuild._load_build_graph()
    bg.build_and_write(root)  # graph.json now exists
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(i) for i in range(101)], ensure_ascii=False),
        encoding="utf-8")  # now stale again

    # NOTE: _load_build_graph() re-executes the module fresh on every call
    # (no sys.modules caching), so patching one loaded instance's attribute
    # does NOT reach the instance ensure_fresh loads internally. The
    # reliable seam is to patch _load_build_graph itself to return a module
    # whose graph_status is slowed down.
    import types
    real_bg = graph_autorebuild._load_build_graph()
    slow_mod = types.SimpleNamespace(
        graph_status=lambda sr: _slow(real_bg.graph_status, sr),
        build_and_write=real_bg.build_and_write,
        default_storage_root=real_bg.default_storage_root,
    )
    def _slow(fn, sr):
        import time
        time.sleep(0.2)
        return fn(sr)

    monkeypatch.setattr(graph_autorebuild, "_load_build_graph", lambda: slow_mod)
    # max_wall_time_s below the simulated sleep -> deadline hit, second
    # pass (build_and_write) must be skipped.
    assert graph_autorebuild.ensure_fresh(root, min_corpus_rows=1,
                                          max_wall_time_s=0.1) is False
    # A subsequent call with a generous budget does complete the write.
    assert graph_autorebuild.ensure_fresh(root, min_corpus_rows=1,
                                          max_wall_time_s=30.0) is True


def test_fresh_store_is_noop(tmp_path):
    root = _make_store(tmp_path, [_row(i) for i in range(100)])
    bg = graph_autorebuild._load_build_graph()
    bg.build_and_write(root)
    written_before = (root / "graph.json").stat().st_mtime_ns
    assert graph_autorebuild.ensure_fresh(root) is False
    assert (root / "graph.json").stat().st_mtime_ns == written_before


def test_missing_corpus_and_graph_is_noop_not_new_behavior(tmp_path):
    root = tmp_path / "store"
    root.mkdir()  # empty root: no memories/ dir, no graph.json
    assert graph_autorebuild.ensure_fresh(root) is False
    assert not (root / "graph.json").exists()


def test_fail_soft_does_not_raise(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("simulated build_graph module corruption")
    monkeypatch.setattr(graph_autorebuild, "_load_build_graph", boom)
    assert graph_autorebuild.ensure_fresh(tmp_path) is False
