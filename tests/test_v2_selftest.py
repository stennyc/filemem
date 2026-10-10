"""v2 self-test gate (P2 G1/G2/G3 code side): TDD for the surgical cut.

Covers:
1. module predicates: is_room_meta_turn / is_room_marker_item
2. ingress stamping: room meta turn -> every born item carries self_test
3. option-1 boundary: NON-room user_content stays untagged (no over-cut)
4. egress: prefetch drops self_test items, keeps ordinary ones
5. canary: run_canary_turn double-stamp (self_test + canary) positive leg
6. negative leg: count_untagged_room_markers window semantics
"""
import json, sys, pathlib, tempfile, threading, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, MemoryTriggerRouter, MEMORY_TYPES, MemoryItem,
    is_room_meta_turn, is_room_marker_item,
)

ROOM_BLOCK = (
    '[Group chat: "T"] You are @coder, one participant in a group chat. '
    "DevOps host config endpoint port 8080 verified reachable."
)
PLAIN_FACT = "the dev host config endpoint runs on port 8080"


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = None
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.episodic_dir = None
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False          # isolate the per-message trigger path under test
    p.min_search_score = 0.0
    p._decay = {"lambda_days": 0.05}
    p._last_save_time = 0
    p._save_interval = 0           # no rate-limit between test saves
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
    return p


def _read_all(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


# ---- 1. module predicates ------------------------------------------------

def test_is_room_meta_turn_positive():
    assert is_room_meta_turn(ROOM_BLOCK) is True
    assert is_room_meta_turn("  [Group chat: \"T\"] x") is True   # leading ws
    assert is_room_meta_turn(None) is False
    assert is_room_meta_turn("") is False


def test_is_room_meta_turn_negative():
    # A real user message that QUOTES the marker mid-text is NOT a room turn.
    assert is_room_meta_turn("I noted [Group chat blocks] are meta; config port 8080.") is False
    assert is_room_meta_turn("plain fact about endpoint port 8080") is False


def test_is_room_marker_item_selectors():
    # structural opener
    assert is_room_marker_item("[Group chat: \"X\"] ...") is True
    assert is_room_marker_item("{\"json\": 1}") is True
    # META_MARKERS substring (M1/M3 shared table)
    assert is_room_marker_item("text quoting Rules for this room discussion") is True
    # warn marker
    assert is_room_marker_item("some note \u26a0\ufe0f content") is True
    # failure_type metadata selector
    assert is_room_marker_item("plain prod text", {"failure_type": "tool_error"}) is True
    # plain content, no metadata -> not a marker
    assert is_room_marker_item("the config endpoint is port 8080") is False


# ---- 2. ingress stamping --------------------------------------------------

def test_room_turn_stamps_item():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.sync_turn(ROOM_BLOCK, "", messages=[{"role": "user", "content": ROOM_BLOCK}])
    items = [i for i in _read_all(p) if i["metadata"].get("self_test")]
    assert items, "room meta turn must stamp every born item with self_test"
    for i in items:
        assert i["metadata"].get("canary") is None, "non-canary turn must not carry canary"


def test_non_room_turn_untagged():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p.sync_turn(PLAIN_FACT, "", messages=[{"role": "user", "content": PLAIN_FACT}])
    items = _read_all(p)
    assert items, "ordinary user fact must still auto-save (option-1 boundary)"
    assert not any(i["metadata"].get("self_test") for i in items), \
        "non-room turns must never stamp self_test"


def test_stale_lease_guard_resets_flags():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    p._turn_self_test = True
    p._turn_canary = True
    p.sync_turn(PLAIN_FACT, "")
    items = _read_all(p)
    assert not any(i["metadata"].get("self_test") for i in items), \
        "flags must reset at sync_turn entry (stale-lease guard)"


# ---- 3. egress ------------------------------------------------------------

def test_prefetch_drops_selftest_keeps_ordinary():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    # Seed two items directly (bypass trigger): one tagged, one not.
    items = [
        MemoryItem(id="fact_0", type="fact",
                   content="port 8080 endpoint reachable", level="L0",
                   metadata={"source_profile": "test", "self_test": True},
                   created_at="2026-09-25T00:00:00").to_dict(),
        MemoryItem(id="fact_1", type="fact",
                   content="port 8080 endpoint healthy", level="L0",
                   metadata={"source_profile": "test", "confirmed": True},
                   created_at="2026-09-25T00:00:00").to_dict(),
    ]
    with open(tmp / "memories" / "fact.json", "w") as f:
        json.dump(items, f)
    ctx = p.prefetch("port endpoint", session_id="s1", confidence=0.5)
    assert "port 8080 endpoint healthy" in ctx, "ordinary item must still inject"
    assert "port 8080 endpoint reachable" not in ctx, "self_test item must NOT inject"


# ---- 4. canary + negative leg --------------------------------------------

def test_canary_double_stamp_and_negative_leg():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    res = p.run_canary_turn(tag="TDD-CANARY")
    assert res["stamped"] is True, f"canary positive leg failed: {res}"
    assert res["items"], "canary item must be double-stamped on disk"
    # Seed ONE untagged room-marker item (residue shape, created 1999 < canary ts).
    f = tmp / "memories" / "fact.json"
    with open(f) as fh:
        rows = json.load(fh)
    rows.append(MemoryItem(id="fact_99", type="fact",
                           content='[Group chat: "RESIDUE"] untagged room text port 8080',
                           level="L0", metadata={"source_profile": "test"},
                           created_at="1999-01-01T00:00:00").to_dict())
    with open(f, "w") as fh:
        json.dump(rows, fh)
    # Negative leg: seeded untagged marker counts for an old window...
    assert p.count_untagged_room_markers("1970-01-01T00:00:00") == 1
    # ...but NOT for the post-effective window (seeded 1999 <= canary ts).
    assert p.count_untagged_room_markers(res["ts"]) == 0


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
