"""D-site 2026-11-08: readable drafts (owner feedback: truncated raw-
fragment drafts gave the user no basis to confirm/discard).

Three fixes, three tests:
1. notice/preview truncation widened 80 -> 240 chars
2. source-type tag `[来源: …]` on every notice line (legacy rows
   heuristic-stamped at display time; new rows carry the stamp from
   generation)
3. preview (current + historical) returns FULL content + source_tag +
   trigger (never truncated) so the confirm/discard judgment has a basis.
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
    p._pending_drafts_notified = {}
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p.sessions_dir = None
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._no_popup_gate = lambda *a, **k: False
    p._load_session_drafts = lambda: None
    p._save_session_drafts = lambda: None
    p._push_pending_confirm = lambda *a, **k: None
    p._session_title_hint = lambda *a, **k: ""
    return p


def test_1_notice_line_widened_to_240():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    # a clean 300-char row: prefix-free, so the 240-cap applies at exactly 240
    long_content = "x" * 300
    with open(tmp / "drafts_s_old.json", "w", encoding="utf-8") as f:
        json.dump([MemoryItem(id="d1", type="fact", content=long_content).to_dict()],
                  f, ensure_ascii=False)
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    # truncated at 240 (upper bound, not target): exactly 240 x's survive
    assert "x" * 240 in ctx, ctx
    assert "x" * 241 not in ctx, ctx
    # ③ source tag present (legacy unstamped row -> heuristic stamp)
    assert "来源" in ctx, ctx


def test_1b_notice_line_no_truncation_when_short():
    """Short rows render verbatim (the 240 cap only truncates long rows)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    with open(tmp / "drafts_s_old.json", "w", encoding="utf-8") as f:
        json.dump([MemoryItem(id="d1", type="fact", content="short fact").to_dict()],
                  f, ensure_ascii=False)
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "short fact" in ctx, ctx


def test_2_preview_returns_full_content_and_tags():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    # current-session draft, full content survives in preview (no truncation)
    p._session_drafts["s_cur"] = [
        MemoryItem(id="c1", type="fact", content="F" * 500)
    ]
    out = json.loads(p._tool_review_drafts({"action": "preview"}))
    assert out["success"] is True
    items = out["items"]
    assert len(items[0]["content"]) == 500, "preview must not truncate"
    assert "source_tag" in items[0], items[0]
    # historical preview (target face)
    with open(tmp / "drafts_s_old.json", "w", encoding="utf-8") as f:
        json.dump([MemoryItem(id="o1", type="fact", content="H" * 400).to_dict()],
                  f, ensure_ascii=False)
    out2 = json.loads(p._tool_review_drafts(
        {"action": "preview", "target": "s_old"}))
    assert out2["success"] is True
    assert len(out2["items"][0]["content"]) == 400, out2["items"][0]
    assert "source_tag" in out2["items"][0], out2["items"][0]


def test_3_source_tag_heuristic_on_legacy_rows():
    """Legacy draft rows (no source_tag in metadata) get a display-time
    heuristic stamp so the user sees 用户原话 vs 未提炼对话片段 vs 提炼后
    WITHOUT a data migration."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    rows = [
        # explicit user-statement wording -> stated
        MemoryItem(id="a", type="fact",
                   content="用户明确要求每次都 commit 后再推远程").to_dict(),
        # raw fragment (numbered complaints, no refinement) -> raw
        MemoryItem(id="b", type="fact",
                   content="1. 不要弹提示 2. 加复制按钮 3. 关闭后卡住").to_dict(),
    ]
    with open(tmp / "drafts_s_old.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    out = json.loads(p._tool_review_drafts({"action": "preview",
                                            "target": "s_old"}))
    tags = {i["id"]: i["source_tag"] for i in out["items"]}
    assert "stated" in tags["a"], tags
    assert "raw" in tags["b"], tags
    # I-3 (reviewer P2): raw-fragment FEATURES beat stated WORDING — a numbered
    # list that merely MENTIONS "用户要求" is still an unrefined fragment.
    p._session_drafts["s_cur"] = [
        MemoryItem(id="c", type="fact",
                   content="1. 用户要求先出 patch\n2. 小步推进\n3. 验收全绿")
    ]
    out4 = json.loads(p._tool_review_drafts({"action": "preview"}))
    assert out4["items"][0]["source_tag"] == "raw", out4["items"]
    # new rows carry an explicit stamp from generation (no heuristic needed)
    p._session_drafts["s_cur"] = [
        MemoryItem(id="n1", type="fact", content="提炼后的一句话陈述")
    ]
    p._session_drafts["s_cur"][0].metadata["source_tag"] = "inferred"
    out3 = json.loads(p._tool_review_drafts({"action": "preview"}))
    assert out3["items"][0]["source_tag"] == "inferred", out3["items"]


def test_6_preview_trigger_reads_metadata_draft_trigger():
    """I-1 (reviewer P1): the preview 'trigger' key must read
    metadata.draft_trigger — MemoryItem has no top-level trigger attribute,
    so the old getattr(d, 'trigger', None) always returned None."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    item = MemoryItem(id="c1", type="fact", content="提炼后的陈述")
    item.metadata["draft_trigger"] = "当讨论 9200 右键菜单时想起这条"
    item.metadata["source_tag"] = "inferred"
    p._session_drafts["s_cur"] = [item]
    out = json.loads(p._tool_review_drafts({"action": "preview"}))
    assert out["items"][0]["trigger"] == "当讨论 9200 右键菜单时想起这条", out["items"]
    # historical face (dict rows) reads the same key
    with open(tmp / "drafts_s_old.json", "w", encoding="utf-8") as f:
        json.dump([item.to_dict()], f, ensure_ascii=False)
    out2 = json.loads(p._tool_review_drafts({"action": "preview",
                                             "target": "s_old"}))
    assert out2["items"][0]["trigger"] == "当讨论 9200 右键菜单时想起这条", out2["items"]


def test_7_confirm_promotion_carries_refine_verdict_into_pool_row():
    """I-7 (owner 落池保留拍板): the draft-stage source_tag + draft_trigger
    must ride into the pool row's metadata at confirm-promotion, not be
    dropped. A legacy unrefined draft (no tag/trigger) promotes unchanged."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._session_drafts["s_cur"] = []
    # a refined draft (LLM face succeeded)
    refined = MemoryItem(id="r1", type="fact", content="提炼后陈述")
    refined.metadata["source_tag"] = "inferred"
    refined.metadata["draft_trigger"] = "讨论 9200 右键时想起"
    p._session_drafts["s_cur"].append(refined)
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"] is True and out["count"] == 1, out
    pool_rows = json.loads(
        (p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    rows = [r for r in pool_rows if r.get("content") == "提炼后陈述"]
    assert rows, pool_rows
    assert rows[0]["metadata"].get("source_tag") == "inferred", rows[0]
    assert rows[0]["metadata"].get("draft_trigger") == "讨论 9200 右键时想起", rows[0]
    # legacy unrefined draft: no tag/trigger keys on the pool row
    legacy = MemoryItem(id="l1", type="fact", content="普通原始片段")
    p._session_drafts["s_cur"] = [legacy]
    out2 = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out2["count"] == 1, out2
    pool_rows2 = json.loads(
        (p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    lrows = [r for r in pool_rows2 if r.get("content") == "普通原始片段"]
    assert lrows, pool_rows2
    assert "source_tag" not in lrows[0]["metadata"], lrows[0]
    assert "draft_trigger" not in lrows[0]["metadata"], lrows[0]


def test_4_turn_report_line_widened_to_240():
    """[记忆报告] L0 auto-save lines: same 240 judgment-basis rule."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._pending_confirm = {
        "s_cur": [
            {"id": "l1", "level": "L0", "kind": "l0",
             "content": "y" * 300, "ts": "t"},
            {"id": "l2", "level": "L1", "kind": "draft",
             "content": "z" * 120, "ts": "t"},
        ]
    }
    line = p._turn_report_line("s_cur")
    assert line, line
    # both rows survive at 240 / verbatim (120 < 240)
    assert "y" * 240 in line, line
    assert "y" * 241 not in line, line
    assert "z" * 120 in line, line


def test_5_draft_generator_tags_new_rows_via_refine_gate():
    """①+③ generation-side: _add_draft stamps source_tag / draft_trigger on
    success, and lands the RAW content unrefined when the LLM face is off
    (feature gate / no main_runtime) — fail-soft legacy path."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur")
    p._session_drafts = {}
    # feature off (limit 0): raw content lands, no tag, no trigger
    p._draft_refine_limit = 0
    p._live_main_runtime = lambda: None
    did = p._add_draft("原始片段内容", level="L1")
    assert did
    item = p._session_drafts["s_cur"][-1]
    assert item.content == "原始片段内容"
    assert item.metadata.get("source_tag") is None
    assert item.metadata.get("draft_trigger") is None
    # budget off (no limit attr default 3) but no main_runtime -> same raw path
    p2 = _make_provider(tmp / "b", "s_cur")
    p2._session_drafts = {}
    p2._live_main_runtime = lambda: None
    did2 = p2._add_draft("另一段原始片段", level="L1")
    assert p2._session_drafts["s_cur"][-1].content == "另一段原始片段"
