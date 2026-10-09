"""D-site: the [未决草稿] notice + [记忆报告] report show PENDING CONTENT,
not file names, counts, or memory ids (@user 2026-10-09: "不能显示文件名称，
这非常不专业，应该显示待确认的内容" + "不需要显示id, 必须走clarify, memory-context
改为和我的要求统一").

Design (plugin-only, host zero-change):
- Historical notice `[未决草稿]`: one `  - [LEVEL] content…` line per
  eligible row (P1-4 per-row owner filter unchanged; cross-profile rows
  never listed, A-site filename red line held: no drafts_*.json string
  ever surfaces). Footer instructs the clarify flow:
  确认保存/丢弃/取消 -> filemem_review_drafts(confirm|discard) without
  target (bulk sweep).
- Turn report `[记忆报告]`: same line shape, ids REMOVED (the user's
  decision goes through clarify as a bulk choice; per-item ids are no
  longer shown on this face — partial per-item disposition via memory_id
  still works mechanically, it just is no longer advertised).
- filemem_review_drafts(action=confirm|discard) WITHOUT target sweeps
  BOTH the current session's in-memory pool AND all other sessions'
  pending draft files on the shared storage root (each file: only its
  P1-4-ELIGIBLE rows are consumed; cross-profile rows stay in place),
  so the notice and the tool finally agree on what is pending.

Zero LLM; host source zero-change.
"""
import json, pathlib, re, sys, tempfile, threading

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
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = profile
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
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
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    return p


def _row(content: str, source_profile=None) -> dict:
    d = MemoryItem(id="x", type="fact", content=content).to_dict()
    if source_profile is None:
        d.pop("metadata", None)
    else:
        d.setdefault("metadata", {})["source_profile"] = source_profile
    return d


def _write(tmp: pathlib.Path, name: str, entries) -> None:
    with open(tmp / name, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)


# ── notice face ──────────────────────────────────────────────────────────

def test_1_notice_lists_eligible_content_not_filenames_or_counts():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [
        _row("本轮 hermes-ui 崩溃排查总结", source_profile="test"),
        _row("别人的草稿不要显示", source_profile="4d"),
    ])
    _write(tmp, "drafts_s_mid.json", [
        _row("修复完成 操作记录", source_profile="test"),
    ])
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    # content lines ARE listed (P1-4 eligible rows only)
    assert "本轮 hermes-ui 崩溃排查总结" in ctx, ctx
    assert "修复完成 操作记录" in ctx, ctx
    # cross-profile row is NOT listed (P1-4 owner filter held)
    assert "别人的草稿不要显示" not in ctx, ctx
    # no ids, no filenames, no aggregate counts (the old "N 条待处置" face
    # is gone from this block)
    assert not re.search(r"\(\s*id:", ctx), ctx
    assert "drafts_s_old.json" not in ctx, ctx
    assert "drafts_s_mid.json" not in ctx, ctx
    assert not re.search(r"drafts_\w+\.json", ctx), ctx
    assert "条待处置" not in ctx, ctx


def test_2_notice_footer_directs_clarify_flow():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [_row("content A", source_profile="test")])
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "clarify" in ctx, ctx
    assert "确认保存" in ctx and "丢弃" in ctx and "取消" in ctx, ctx
    assert "filemem_review_drafts" in ctx, ctx


def test_3_notice_still_one_shot_per_session_and_skips_own_session():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [_row("content A", source_profile="test")])
    p.search_memories = lambda *a, **k: []
    first = p.prefetch("a", session_id="s_cur")
    second = p.prefetch("b", session_id="s_cur")
    assert "[未决草稿]" in first
    assert "[未决草稿]" not in second, "one-shot gate must hold"
    # own-session draft file never appears in the notice
    p2 = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_cur.json", [_row("my own", source_profile="test")])
    p2.search_memories = lambda *a, **k: []
    assert "my own" not in p2.prefetch("c", session_id="s_cur")


def test_4_corrupt_file_stays_fail_soft():
    """A corrupt draft file must not block the notice (fail-soft held),
    and it still lists a fallback line instead of inventing content."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    with open(tmp / "drafts_s_bad.json", "w", encoding="utf-8") as f:
        f.write("{corrupt json")
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "不可读" in ctx, ctx  # honest fallback, not fabricated content


# ── turn report face (memory-context) ────────────────────────────────────

def test_5_turn_report_drops_ids_keeps_clarify_tail():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    p._session_drafts["s_cur"] = [
        MemoryItem(id="c1", type="fact", content="database connection string is at env DATABASE_URL")
    ]
    p._pending_confirm = {"s_cur": [{"id": "c1", "level": "L0",
                                     "content": "database connection string is at env DATABASE_URL"}]}
    p.search_memories = lambda *a, **k: []
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[记忆报告]" in ctx, ctx
    assert "database connection string is at env DATABASE_URL" in ctx, ctx
    # ids are no longer shown on this face (@user: 不需要显示id)
    assert not re.search(r"\(\s*id:", ctx), ctx
    # clarify-driven tail is unified
    assert "clarify" in ctx, ctx
    assert "确认保存" in ctx and "丢弃" in ctx and "取消" in ctx, ctx


# ── tool face: no-target bulk sweep ─────────────────────────────────────

def test_6_confirm_without_target_sweeps_current_and_historical():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    # current in-memory pool
    p._session_drafts["s_cur"] = [MemoryItem(id="c1", type="fact", content="current fact")]
    # historical files: 2 eligible + 1 cross-profile row that must survive
    _write(tmp, "drafts_s_old.json", [
        _row("hist fact one", source_profile="test"),
        _row("cross profile row", source_profile="4d"),
    ])
    _write(tmp, "drafts_s_mid.json", [
        _row("hist fact two", source_profile="test"),
    ])
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"] is True, out
    assert out["count"] == 3, out  # 1 current + 2 historical eligible
    # historical eligible rows gone, cross-profile row KEPT in place
    mid = json.loads((tmp / "drafts_s_mid.json").read_text(encoding="utf-8")) \
        if (tmp / "drafts_s_mid.json").exists() else []
    old = json.loads((tmp / "drafts_s_old.json").read_text(encoding="utf-8")) \
        if (tmp / "drafts_s_old.json").exists() else []
    surviving = [r.get("content") for r in old + mid]
    assert "cross profile row" in surviving, surviving
    assert "hist fact one" not in surviving, surviving
    assert "hist fact two" not in surviving, surviving
    # confirmed rows landed in the memories pool
    saved = []
    for mf in (tmp / "memories").glob("*.json"):
        saved += json.loads(mf.read_text(encoding="utf-8"))
    contents = [r.get("content") for r in saved]
    assert "current fact" in contents, contents
    assert "hist fact one" in contents, contents


def test_7_discard_without_target_sweeps_and_keeps_cross_profile():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    p._session_drafts["s_cur"] = [MemoryItem(id="c1", type="fact", content="current fact")]
    _write(tmp, "drafts_s_old.json", [
        _row("hist fact", source_profile="test"),
        _row("cross profile row", source_profile="4d"),
    ])
    out = json.loads(p._tool_review_drafts({"action": "discard"}))
    assert out["success"] is True, out
    old = json.loads((tmp / "drafts_s_old.json").read_text(encoding="utf-8")) \
        if (tmp / "drafts_s_old.json").exists() else []
    surviving = [r.get("content") for r in old]
    assert surviving == ["cross profile row"], surviving
    assert p._session_drafts.get("s_cur") in ([], None), "current pool cleared"


def test_8_no_target_nothing_pending_is_clean_noop():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    out = json.loads(p._tool_review_drafts({"action": "confirm"}))
    assert out["success"] is True, out
    assert out["count"] == 0, out
    out2 = json.loads(p._tool_review_drafts({"action": "discard"}))
    assert out2["success"] is True, out2


def test_9_memory_id_filter_still_works_on_the_sweep():
    """memory_id filters the sweep to a single row; other rows stay pending
    (per-item disposition remains mechanically possible without the id
    being ADVERTISED on the notice face)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [
        {**MemoryItem(id="o1", type="fact", content="keep me").to_dict(),
         "metadata": {"source_profile": "test"}},
        {**MemoryItem(id="o2", type="fact", content="drop me").to_dict(),
         "metadata": {"source_profile": "test"}},
    ])
    out = json.loads(p._tool_review_drafts({"action": "discard", "memory_id": "o2"}))
    assert out["success"] is True, out
    old = json.loads((tmp / "drafts_s_old.json").read_text(encoding="utf-8")) \
        if (tmp / "drafts_s_old.json").exists() else []
    assert [r.get("content") for r in old] == ["keep me"], old


# ── reviewer 10-09 P1/P2 regression guards ─────────────────────────────

def test_10_sweep_survives_malformed_rows_no_crash():
    """P1: malformed rows (42 / None / shape-less dict) in a historical
    file must not crash the whole tool call (no JSON return). They are
    kept VERBATIM in place; well-formed rows around them still sweep."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_bad.json", [
        42, None, {"metadata": "not-a-dict"},
        _row("good row", source_profile="test"),
    ])
    out = json.loads(p._tool_review_drafts({"action": "discard"}))
    assert out["success"] is True, out
    # file still exists: malformed rows kept verbatim, good row consumed
    raw = json.loads((tmp / "drafts_s_bad.json").read_text(encoding="utf-8"))
    assert 42 in raw and None in raw and {"metadata": "not-a-dict"} in raw, raw
    assert "good row" not in [r.get("content") for r in raw if isinstance(r, dict)], raw


def test_11_partial_disposal_consumes_only_touched_pending_confirm():
    """P2: a memory_id-scoped confirm/discard consumes ONLY that item's
    pending-confirm queue entry; un-touched items re-report next turn."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    p._session_drafts["s_cur"] = [
        MemoryItem(id="c1", type="fact", content="fact one"),
        MemoryItem(id="c2", type="fact", content="fact two"),
    ]
    p._pending_confirm = {"s_cur": [
        {"id": "c1", "level": "L0", "content": "fact one"},
        {"id": "c2", "level": "L0", "content": "fact two"},
    ]}
    out = json.loads(p._tool_review_drafts(
        {"action": "confirm", "memory_id": "c1"}))
    assert out["success"] is True, out
    assert out["count"] == 1, out
    # c1 consumed; c2 still pending -> re-reports
    pc = p._pending_confirm.get("s_cur", [])
    assert [e["id"] for e in pc] == ["c2"], pc
