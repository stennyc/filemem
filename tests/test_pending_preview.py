"""F-site tool-gap fix (t_05679311): filemem_review_drafts gains a read-only
preview action that lists pending entries by content + id, so the model
decides confirm/discard on CONTENT without opening the internal
drafts_*.json blobs via read_file. The return body must NEVER contain an
internal draft filename.

Coverage: current scope, historical scope, limit, missing historical target
(fail-soft, no filename echo), and the tool schema (preview in the action
enum, no drafts_* example in the target parameter wording).
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


def test_1_preview_current_session_lists_content_id_no_filename():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._session_drafts["s_cur"] = [
        MemoryItem(id="c1", type="fact", content="current session fact")
    ]
    out = json.loads(p._tool_review_drafts({"action": "preview"}))
    assert out["success"] is True, out
    assert out["scope"] == "current"
    assert out["count"] == 1
    it = out["items"][0]
    assert it["content"] == "current session fact"
    assert it["id"] == "c1"
    assert "level" in it
    assert "drafts_" not in json.dumps(out), "internal filenames must never surface"


def test_2_preview_historical_scope_by_content():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id="o1", type="fact", content="old fact").to_dict()])
    out = json.loads(p._tool_review_drafts({"action": "preview", "target": "s_old"}))
    assert out["success"] is True, out
    assert out["scope"] == "historical"
    assert out["count"] == 1
    assert out["items"][0]["content"] == "old fact"
    assert out["items"][0]["id"] == "o1"
    assert "drafts_" not in json.dumps(out), "internal filenames must never surface"


def test_3_preview_respects_limit():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    _write_drafts(tmp, "drafts_s_old.json",
                  [MemoryItem(id=f"o{i}", type="fact", content=f"old fact {i}").to_dict()
                   for i in range(3)])
    out = json.loads(p._tool_review_drafts(
        {"action": "preview", "target": "s_old", "limit": 2}))
    assert out["count"] == 3
    assert out["shown"] == 2
    assert len(out["items"]) == 2


def test_4_preview_missing_historical_target_fails_soft_no_filename():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    out = json.loads(p._tool_review_drafts({"action": "preview", "target": "s_none"}))
    assert out["success"] is False
    assert "drafts_" not in json.dumps(out), "error must not echo a filename"


def test_5_preview_schema_enums_and_no_filename_wording():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()), "s_cur")
    schema = json.dumps(p.get_tool_schemas(), ensure_ascii=False)
    assert '"preview", "confirm", "discard"' in schema
    assert "limit" in schema
    assert "drafts_xxx.json" not in schema
    assert "drafts_abc123" not in schema
