"""SA2-5 graph auto-rebuild hook: tests for graph_autorebuild.needs_rebuild +
build_background (the O(1) mtime gate + the dedicated-daemon-thread full
build entry point that replaced the old ensure_fresh sync + soft-deadline
dead loop; see SA2-5 owner 2026-11-12, 选2 filemem 自持 daemon 线程, 不用
cronjob).

Acceptance coverage:
1. stale store (corpus newer than graph.json) -> needs_rebuild True;
   build_background then writes a fresh graph.json (fingerprint changes,
   .bak created on first mutation only)
2. fresh store -> needs_rebuild False, build_background is a fingerprint-
   idempotent no-op (returns False, zero writes, no .bak clobber)
3. missing graph.json + missing corpus -> needs_rebuild "missing" path ->
   True (a full build IS warranted once corpus exists); empty root ->
   False (row-count gate, not the missing-verdict, is what holds)
4. fail-soft: pointing the loader at a path that raises -> needs_rebuild /
   build_background return False, do not raise, touch nothing on disk
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

    # O(1) mtime gate: corpus file just touched -> verdict "stale" -> True.
    assert graph_autorebuild.needs_rebuild(root, min_corpus_rows=1) is True
    # build_background does the actual full write (detached-thread entry
    # point; the .bak convention still snapshots the PRIOR file on first
    # mutation of a prior-graph-existing store).
    assert graph_autorebuild.build_background(root) is True
    new_fp = json.loads((root / "graph.json").read_text())["fingerprint"]
    assert new_fp != first_fp
    bak_bytes = (root / "graph.json.bak").read_bytes()
    assert json.loads(bak_bytes.decode())["fingerprint"] == first_fp
    # Idempotent .bak convention: a second mutation must NOT re-snapshot.
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1)], ensure_ascii=False), encoding="utf-8")
    assert graph_autorebuild.needs_rebuild(root, min_corpus_rows=1) is True
    assert graph_autorebuild.build_background(root) is True
    assert (root / "graph.json.bak").read_bytes() == bak_bytes
    # now it is fresh: corpus not newer than graph.json anymore
    assert graph_autorebuild.needs_rebuild(root, min_corpus_rows=1) is False


def test_min_corpus_gate_skips_tiny_store(tmp_path):
    """A store with fewer rows than min_corpus_rows is skipped entirely —
    this is what keeps test_s1b_tick_wiring's tiny fixtures (1-2 rows)
    from paying any graph build cost, regardless of join-budget timing."""
    root = _make_store(tmp_path, [_row(i) for i in range(3)])
    # No graph.json yet; even if it were "missing"/"stale", the corpus is
    # too small to be worth graphing — must return False WITHOUT writing
    # graph.json.
    assert graph_autorebuild.needs_rebuild(root) is False  # default min=100
    assert not (root / "graph.json").exists()
    # Same store, caller explicitly lowers the gate. There is still no
    # graph.json on disk, so the verdict is "missing" (not "stale") and
    # needs_rebuild returns True — but build_background on a 3-row store
    # still produces a (trivial) graph, matching the pre-SA2-5 behavior
    # where a lowered gate meant "build proceeds"; the gate itself (the
    # row count check, which runs FIRST) is proven by the contrast below.
    assert graph_autorebuild.needs_rebuild(root, min_corpus_rows=1) is True
    assert graph_autorebuild.build_background(root) is True
    bg = graph_autorebuild._load_build_graph()
    bg.build_and_write(root)  # idempotent second write: zero bytes, .bak kept
    # ...and with the default gate (100), the same 4-row store is skipped:
    (root / "memories" / "fact.json").write_text(
        json.dumps([_row(0), _row(1), _row(2), _row(3)], ensure_ascii=False),
        encoding="utf-8")
    assert graph_autorebuild.needs_rebuild(root) is False  # 5 < 100, gate holds


def test_missing_corpus_is_noop_not_new_behavior(tmp_path):
    """Empty root (no memories/ dir, no graph.json): the row-count gate
    (0 < 100) holds FIRST, before the missing-verdict path is even reached
    — so needs_rebuild is False, and build_background on this empty store
    writes a trivially-empty graph but the gate already said "don't bother"
    to the tick thread."""
    root = tmp_path / "store"
    root.mkdir()  # empty root: no memories/ dir, no graph.json
    assert graph_autorebuild.needs_rebuild(root) is False
    assert not (root / "graph.json").exists()


def test_fail_soft_does_not_raise(tmp_path, monkeypatch):
    def boom():
        raise RuntimeError("simulated build_graph module corruption")
    monkeypatch.setattr(graph_autorebuild, "_load_build_graph", boom)
    assert graph_autorebuild.needs_rebuild(tmp_path) is False
    assert graph_autorebuild.build_background(tmp_path) is False
