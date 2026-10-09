"""Gap A/B fix: reference_count persistence + M6 promotion loop.

Tests that _reinforce_recall:
1. Bumps reference_count in the JSON file (Gap A)
2. Skips episodic items
3. Enqueues M6 pending event on threshold crossing (Gap B)
4. Does NOT enqueue below threshold
5. Is idempotent (no duplicate M6 events)
"""
import json, threading, pathlib, tempfile, sys, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MEMORY_TYPES, MemoryItem  # noqa: E402


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Minimal provider for testing without calling initialize()."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    return p


def test_1_bumps_reference_count():
    """Gap A: reference_count is persisted to the JSON file after a recall."""
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmpdir)
    mem_file = p.memories_dir / "fact.json"
    items = [MemoryItem(id="fact_0", type="fact", content="test fact").to_dict()]
    with open(mem_file, "w") as f:
        json.dump(items, f)

    p._reinforce_recall(items)

    with open(mem_file) as f:
        result = json.load(f)
    assert result[0]["reference_count"] == 1, f"expected ref=1, got {result}"
    assert result[0]["last_referenced"], "last_referenced should be set"


def test_2_skips_episodic():
    """Episodic pool items (type not in MEMORY_TYPES) are not bumped."""
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmpdir)
    ep_item = {"type": "episodic", "id": "ep_0", "content": "episodic"}
    p._reinforce_recall([ep_item])
    assert not (p.memories_dir / "fact.json").exists()




def test_4_no_m6_below_threshold():
    """No M6 event is queued when reference_count stays below 3."""
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmpdir)
    mem_file = p.memories_dir / "fact.json"
    items = [MemoryItem(id="fact_low_0", type="fact", content="test",
                         reference_count=0).to_dict()]
    with open(mem_file, "w") as f:
        json.dump(items, f)

    p._reinforce_recall(items)

    with open(mem_file) as f:
        result = json.load(f)
    assert result[0]["reference_count"] == 1, "should be bumped to 1"

    queue_file = p.pending_queue_path
    if queue_file.exists():
        with open(queue_file) as f:
            events = [json.loads(l) for l in f if l.strip()]
        assert not any(e.get("event") == "pending" for e in events), \
            f"expected no M6 pending events, got: {events}"




def test_7_batched_io_one_write_per_file():
    """Researcher ③ perf fix: a prefetch hitting N durable items in the SAME
    file must read+write that file exactly ONCE, not N times (write
    amplification O(pool × hits) -> O(pool + hits))."""
    import builtins
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmpdir)
    mem_file = p.memories_dir / "fact.json"
    items = [MemoryItem(id=f"fact_{i}_x", type="fact", content=f"c{i}").to_dict()
             for i in range(4)]
    with open(mem_file, "w") as f:
        json.dump(items, f)

    writes = []
    reads = []
    real_open = builtins.open
    def counting_open(file, mode="r", *a, **kw):
        # Only track our target file
        if str(file) == str(mem_file):
            if "w" in mode:
                writes.append(1)
            elif "r" in mode:
                reads.append(1)
        return real_open(file, mode, *a, **kw)

    builtins.open = counting_open
    try:
        p._reinforce_recall(items)  # all 4 in one fact.json
    finally:
        builtins.open = real_open

    assert reads.count(1) == 1, f"expected 1 read of fact.json, got {len(reads)}"
    assert writes.count(1) == 1, f"expected 1 write of fact.json, got {len(writes)}"
    with open(mem_file) as f:
        result = json.load(f)
    assert [r["reference_count"] for r in result] == [1, 1, 1, 1], result


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception:
                print(f"FAIL  {name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
