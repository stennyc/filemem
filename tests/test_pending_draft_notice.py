"""Pending-draft confirmation loop (closes the README over-claim).

The on_session_end prompt is dropped by the engine (agent/memory_manager.py
wraps on_session_end and discards the return value; base contract is
on_session_end -> None). So the "session-end manual confirm" surface never
reached the user. The closed loop lives in pre_llm_call instead:

1. prefetch injects a one-line [未决草稿] notice when OTHER sessions leave
   pending draft files on disk (once per session, not repeated every turn)
2. the same notice fires when recall returned nothing but pending drafts exist
3. _pending_drafts_notified resets per session (new session_id -> can notify again)

Targeted draft processing in filemem_review_drafts:
4. target=<drafts_*.json name> confirms entries from a HISTORICAL file
5. target + memory_id acts on only that entry
6. target action=discard clears the historical file
7. no target -> current-session behavior unchanged (backward compat)
"""
import json, sys, pathlib, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmpdir: pathlib.Path, session_id: str = "s_cur") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = False
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    return p


def _write_drafts(tmpdir: pathlib.Path, name: str, entries) -> None:
    with open(tmpdir / name, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


def test_1_notice_injected_when_other_session_drafts_exist():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id="d1", type="fact", content="old fact").to_dict()])
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    # t_05679311 (A-site): internal blob filenames must NOT surface in the
    # user-visible notice; the aggregate item count is what the model sees.
    assert "drafts_s_old.json" not in ctx, ctx
    import re as _re
    assert not _re.search(r"drafts_\w+\.json", ctx), ctx
    assert "1" in ctx, ctx
    assert p._pending_drafts_notified.get("s_cur") is True


def test_2_no_pending_drafts_no_notice():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert ctx == "", repr(ctx)


def test_3_notice_fires_exactly_once_per_session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id="d1", type="fact", content="old fact").to_dict()])
    p.search_memories = lambda *a, **k: []
    assert "[未决草稿]" in p.prefetch("a", session_id="s_cur")
    assert "[未决草稿]" not in p.prefetch("b", session_id="s_cur")
    # NEW session: the flag resets, notice may fire again
    p.session_id = "s_new"
    p._pending_drafts_notified = False
    p.min_search_score = 0.3
    assert "[未决草稿]" in p.prefetch("c", session_id="s_new")


def test_4_current_session_own_drafts_do_not_notify():
    """Drafts written this session are handled at session end, not re-announced."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._session_drafts["s_cur"] = [MemoryItem(id="x", type="fact", content="cur")]
    _write_drafts(tmp, "drafts_s_cur.json",
                  [MemoryItem(id="x", type="fact", content="cur").to_dict()])
    p.search_memories = lambda *a, **k: []
    assert p.prefetch("q", session_id="s_cur") == ""


def test_5_targeted_confirm_saves_from_historical_file():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id="o1", type="fact", content="old fact").to_dict()])
    out = json.loads(p._tool_review_drafts(
        {"action": "confirm", "target": "drafts_s_old.json"}))
    assert out["success"] is True
    assert out["target"] == "drafts_s_old.json"
    assert out["count"] == 1
    saved = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert any(i.get("content") == "old fact" for i in saved)
    assert not (tmp / "drafts_s_old.json").exists()


def test_6_targeted_confirm_with_memory_id_filter():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json", [
        MemoryItem(id="o1", type="fact", content="keep me").to_dict(),
        MemoryItem(id="o2", type="task", content="drop me").to_dict(),
    ])
    out = json.loads(p._tool_review_drafts(
        {"action": "confirm", "target": "drafts_s_old.json", "memory_id": "o1"}))
    assert out["count"] == 1
    saved = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert any(i.get("content") == "keep me" for i in saved)
    assert not (p.memories_dir / "task.json").exists()
    # partial confirm: un-confirmed entry o2 REMAINS in the historical file
    remaining = json.loads((tmp / "drafts_s_old.json").read_text(encoding="utf-8"))
    assert [d["id"] for d in remaining] == ["o2"], remaining


def test_7_targeted_discard_clears_historical_file():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id="o1", type="fact", content="old").to_dict()])
    out = json.loads(p._tool_review_drafts(
        {"action": "discard", "target": "drafts_s_old.json"}))
    assert out["success"] is True
    assert out["discarded_count"] == 1
    assert not (p.memories_dir / "fact.json").exists()


def test_8_no_target_keeps_current_session_behavior():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._session_drafts["s_cur"] = [MemoryItem(id="c1", type="fact", content="cur fact")]
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"] is True
    assert out["count"] == 1
    assert p._session_drafts["s_cur"] == []
