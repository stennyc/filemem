"""Owner 2026-10-12 ruling (read/write consistency, landed together):
cross-session provenance on the auto-inject face.

The [记忆上下文] auto-inject surface had two related defects the owner
ruled on 2026-10-12 (both fixed in ONE commit — the read side is useless
without the write side, and vice versa):

WRITE SIDE:
- memories-pool rows had NO origin-session field (only the episodic pool
  carried top-level session_id from the B4 write). `_save_memory` now
  stamps `metadata.origin_session` at the single write choke point when a
  real session is active (a standalone/"default" save leaves it unset —
  fail-soft: absence never blocks the save).

READ SIDE:
- the auto-inject face (`prefetch` formatting) annotates CROSS-session
  rows with "（来自 session <sid>）" so a stale other-session "待办"
  statement re-read as current truth is visible at a glance. The source
  is read from metadata.origin_session (memories pool) or the top-level
  session_id (episodic pool). Same-session rows are NOT annotated; rows
  with no provenance field render UNCHANGED (fail-soft: no stamp = nothing
  to show, never a guess).
- lifecycle hint: a row carrying an EXISTING `metadata.stale` /
  `metadata.settled` key gets ONE trailing "可能已过时" hint line. The
  row is KEPT (the explicit search face is untouched, no new fold-out);
  the hint is what lets the model/user weigh it as "可能过时" rather than
  current state. No new state, no new verdict, zero LLM — only the two
  already-present lifecycle keys trigger it.

Red line held: plugin source + tests only, host zero-change.
"""
import json, pathlib, sys, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmpdir: pathlib.Path, session_id: str = "s_cur",
                   profile: str = "test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.sessions_dir = tmpdir / "sessions"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = profile
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    p._decay = {}
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._failure_recent = {}
    p._pending_failure_meta = {}
    p._last_save_time = 0
    p._save_interval = 30
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 30
    p._turn_marker_index = {}
    p._session_save_count = 0
    p._last_recall = None
    return p


# ── write side: _save_memory stamps metadata.origin_session ─────────────
def test_save_memory_stamps_origin_session_prefers_turn_active_sid():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_stale")
    p._failure_recent = {}
    # a reused-instance scenario: _turn_active_sid is the caller's real
    # current session (fresh), self.session_id is STALE (prior session).
    p._turn_active_sid = "s_fresh"
    item_id = p._save_memory("一条带来源戳的新记忆", level="L0", source="auto")
    assert item_id, "save should return an id"
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    row = [r for r in items if r["id"] == item_id][0]
    # _turn_active_sid wins over the stale self.session_id
    assert row["metadata"].get("origin_session") == "s_fresh"


def test_save_memory_falls_back_to_session_id_when_no_turn_active_sid():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._failure_recent = {}
    p._turn_active_sid = None  # standalone / no active turn
    item_id = p._save_memory("无活跃 turn 的保存", level="L0", source="auto")
    assert item_id
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    row = [r for r in items if r["id"] == item_id][0]
    assert row["metadata"].get("origin_session") == "s_cur"


def test_save_memory_default_session_stamps_no_origin_session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="default")
    p._failure_recent = {}
    p._turn_active_sid = "default"
    item_id = p._save_memory("无活跃 session 的独立保存", level="L0", source="auto")
    assert item_id, "save should return an id"
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    row = [r for r in items if r["id"] == item_id][0]
    assert "origin_session" not in row.get("metadata", {}), \
        "default session must NOT stamp origin_session (fail-soft: unset, not a guess)"


def test_save_memory_none_session_stamps_no_origin_session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p.session_id = None  # a __new__ shell that never initialized
    p._failure_recent = {}
    p._turn_active_sid = None
    item_id = p._save_memory("无 session_id 属性值的保存", level="L0", source="auto")
    assert item_id
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    row = [r for r in items if r["id"] == item_id][0]
    assert "origin_session" not in row.get("metadata", {})


# ── read side: prefetch annotates cross-session rows ─────────────────────
def _seed_pool(p: FileMemProvider, rows: list) -> None:
    """Write a fact.json with the given rows so prefetch can recall them."""
    (p.memories_dir / "fact.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def test_prefetch_annotations_cross_session_memories_row():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._session_drafts = {}
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    p._decay = {"search": {"auto_recall_max": 8}}
    _seed_pool(p, [{
        "id": "fact_1_1790000000", "type": "fact",
        "content": "filemem 跨 session 待办话术",
        "level": "L1",
        "metadata": {"confirmed": True, "origin_session": "s_other_77"},
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
    }])
    ctx = p.prefetch("filemem 待办", session_id="s_cur")
    assert "（来自 session s_other_77）" in ctx, ctx


def test_prefetch_same_session_row_not_annotated():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "fact_1_1790000000", "type": "fact",
        "content": "本 session 内的新事实",
        "level": "L0",
        "metadata": {"confirmed": True, "origin_session": "s_cur"},
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
    }])
    ctx = p.prefetch("本 session 事实", session_id="s_cur")
    assert "（来自 session" not in ctx, ctx


def test_prefetch_episodic_row_uses_top_level_session_id():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    p._decay = {"search": {"auto_recall_max": 8}}
    _seed_pool(p, [{
        "id": "episodic_1_1790000000", "type": "episodic",
        "content": "旧 session 的 episodic 快照",
        "level": "L2",
        "session_id": "s_hist_11",
        "metadata": {"confirmed": True},
        "created_at": "2026-09-15T00:00:00",
        "updated_at": "2026-09-15T00:00:00",
    }])
    ctx = p.prefetch("episodic 快照", session_id="s_cur")
    assert "（来自 session s_hist_11）" in ctx, ctx


def test_prefetch_legacy_row_no_provenance_unannotated():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "fact_0_1790000000", "type": "fact",
        "content": "无来源戳的老行",
        "level": "L0",
        "metadata": {"confirmed": True},  # no origin_session, no session_id
        "created_at": "2026-08-01T00:00:00",
        "updated_at": "2026-08-01T00:00:00",
    }])
    ctx = p.prefetch("老行事实", session_id="s_cur")
    assert "（来自 session" not in ctx, ctx


def test_prefetch_default_session_id_not_annotated():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="default")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "fact_9_1790000000", "type": "fact",
        "content": "default session 行",
        "level": "L0",
        "metadata": {"confirmed": True, "origin_session": "default"},
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
    }])
    ctx = p.prefetch("default 行", session_id="default")
    assert "（来自 session" not in ctx, ctx


# ── read side: lifecycle hint on existing stale/settled keys ─────────────
def test_prefetch_stale_row_gets_lifecycle_hint():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "task_3_1790000000", "type": "task",
        "content": "一个已确认但可能过时的 task",
        "level": "L1",
        "metadata": {"confirmed": True, "stale": True,
                     "stale_reason": "该 task 已完成，被更新行替代"},
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
    }])
    ctx = p.prefetch("过时 task", session_id="s_cur")
    assert "生命周期提示" in ctx, ctx
    assert "stale" in ctx


def test_prefetch_settled_row_folded_out_not_hinted():
    """A settled row is folded out of the auto face by O1 BEFORE the
    formatting loop — it never reaches the hint section, so no hint is
    expected for it (hinting it would be dead code)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "decision_2_1790000000", "type": "decision",
        "content": "一个已定案的 decision",
        "level": "L2",
        "metadata": {"confirmed": True, "settled": True},
        "created_at": "2026-09-01T00:00:00",
        "updated_at": "2026-09-01T00:00:00",
    }])
    ctx = p.prefetch("定案 decision", session_id="s_cur")
    # O1 fold cuts it -> the content itself should NOT appear in the auto face
    assert "定案" not in ctx, ctx


def test_prefetch_normal_row_no_lifecycle_hint():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._injected_snapshot_path = lambda sid, *a, **k: None
    p._save_injected_snapshot = lambda *a, **k: None
    p._bringback_clarify_footer = lambda *a, **k: ""
    p.min_search_score = 0.0
    _seed_pool(p, [{
        "id": "fact_5_1790000000", "type": "fact",
        "content": "一条正常的 fact，无生命周期标记",
        "level": "L0",
        "metadata": {"confirmed": True},
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
    }])
    ctx = p.prefetch("正常 fact", session_id="s_cur")
    assert "生命周期提示" not in ctx, ctx
