"""P1-2: filemem bring-back 跨 session 串项目根治（topic 维度 + 第 4 选项）.

PRD: /root/.hermes/workspace/references/prd-filemem-bringback-topic-guard-2026-10-14.md
（9876439 之后的新工作线；不碰 9876439 已落的 P1 relay-block 逻辑）。

三条修复（owner 裁决全锁定，PRD §4）：
  修复 1 footer 带 topic：_bringback_clarify_footer 取 prev_sid 后调
    _session_title_hint(prev_sid) 嵌进 clarify 文案（看清串了谁）。
  修复 2 第 4 选项 topic_mismatch（裁决 2 = A）：choice 枚举 3 值 -> 4 值；
    收行谓词 _cross_session_confirmed_fold —— 只折"带 session 戳的跨
    session confirmed 行"，保留无 session 戳全局骨架 + 当前 session 行。
    未选（默认 / 其他三值）时谓词恒 False（零回归）。
  修复 3 topic 重叠自动闸（BM25，裁决 1，默认关）：
    _TOPIC_OVERLAP_GATE_ENABLED=False（模块常量）；_topic_overlap_gate
    用 search_index.tokenize + BM25Index 单方向得分（cur topic 为语料、
    prev topic 当 query）；得分 >= threshold（decay.json
    topic_guard.threshold，默认 0.1，热改）同类放行、< 异类折出；
    只折"非当前 session 的 confirmed 行"；fail-soft（topic 空/None/
    非 str / BM25 异常 -> 放行不折，fail-open）。

测试面 T1-T9（PRD §3），Red->Green：现状（9876439）下
  T1-T5/T8/T9 失败（无新谓词/枚举值/footer 无 topic），
  T6/T7 通过（默认关 = 现状），精确锁缺口。

fixture: tests/fixtures/topic_guard_pairs.json（同/异类 topic 对 + 阈值）。
纯 stdlib + search_index（本仓模块），零 LLM，宿主零 diff，数据面零改动。
"""
import json
import pathlib
import sys
import threading
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from __init__ import (  # noqa: E402
    FileMemProvider,
    _auto_inject_eligible,
)
# The new P1-2 predicates may be absent in the pre-fix (RED) tree; import-guard
# so the module still COLLECTS. RED tests then fail at their behavior asserts,
# which is the proof the gap existed.
try:
    from __init__ import _topic_overlap_gate  # noqa: E402
    _HAS_TOPIC_GATE = True
except ImportError:
    _HAS_TOPIC_GATE = False
try:
    import __init__ as _fm_pkg
except ImportError:
    _fm_pkg = None

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
with open(FIXTURES / "topic_guard_pairs.json", encoding="utf-8") as _f:
    _PAIRS = json.load(_f)

CUR_SID = "s_cur"
OTHER_SID = "s_other"
PREV_SID = "s_prev"


# ── helpers (same provider shell as test_bringback_clarify / test_relay_block_purge)
def _make_provider(tmp: pathlib.Path, sid: str = CUR_SID,
                   profile: str = "coder") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = profile
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = None
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.auto_extract = False
    p._decay = {}
    p.min_search_score = 0.3
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p._pending_memories = []
    p._session_drafts = {}
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._main_runtime = None
    p._llm_ctx = None
    p._last_recall = None
    import contextlib
    p._shard_lock = lambda pth: contextlib.suppress()
    return p


def _row(content: str, *, sp=None, confirmed: bool = False,
         session_id: str = None, mem_type: str = "fact") -> dict:
    meta = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    row = {
        "id": f"{mem_type}_{content[:8]}_{session_id or 'na'}",
        "type": mem_type,
        "content": content,
        "level": "L0",
        "metadata": meta,
        "created_at": "2026-10-01T00:00:00",
        "updated_at": "2026-10-01T00:00:00",
        "salience": 0.9,
        "last_referenced": "2026-10-01T00:00:00",
    }
    if session_id is not None:
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    return row


def _write_pool_rows(tmp: pathlib.Path, rows):
    """Append rows into memories/<type>.json shards (the real pool read path)."""
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for t, items in by_type.items():
        f = tmp / "memories" / f"{t}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
        data.extend(items)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _seed_session_traces(tmp: pathlib.Path, traces):
    """Write sessions/<sid>.jsonl first-user-message traces for topic hints
    under tmp (NOT under sessions/<sid>/ — the trace is a flat jsonl at
    sessions/<sid>.jsonl, while _injected_snapshot_path uses the
    sessions/<sid>/last_injected.txt subdir)."""
    sdir = tmp / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    for sid, topic in traces.items():
        f = sdir / f"{sid}.jsonl"
        f.write_text(
            json.dumps({"role": "user", "content": topic},
                       ensure_ascii=False) + "\n",
            encoding="utf-8")


def _choice_set(p, sid, choice):
    p._set_bringback_choice(sid, choice)


def _fold_cross_session_confirmed(row, cur_sid):
    """The P1-2 fix-2 predicate (裁决 2=A). Import-guarded: RED tree -> stub
    that mirrors the CURRENT behavior (never folds), so T6/T7 pass RED and
    T5 fails RED."""
    if _fm_pkg is not None and hasattr(_fm_pkg, "_cross_session_confirmed_fold"):
        return _fm_pkg._cross_session_confirmed_fold(row, cur_sid)
    # pre-fix behavior: no such leg existed
    return False


# ── T1-T4: _topic_overlap_gate (BM25, 修复 3 谓词) ──────────────────────

def test_t1_bm25_same_class_topic_passes_gate():
    """T1: fixture same-class pairs (shared stem) -> gate True (放行不折)."""
    assert _HAS_TOPIC_GATE, "RED: _topic_overlap_gate not implemented yet"
    for pair in _PAIRS["same_class"]:
        assert _topic_overlap_gate(pair["prev_topic"], pair["cur_topic"]), (
            f"same-class pair {pair['id']!r} must pass the gate "
            f"(score >= threshold)")


def test_t2_bm25_cross_class_topic_folds():
    """T2: fixture cross-class pairs (no shared stem) -> gate False (折出)."""
    assert _HAS_TOPIC_GATE, "RED: _topic_overlap_gate not implemented yet"
    for pair in _PAIRS["cross_class"]:
        assert not _topic_overlap_gate(pair["prev_topic"], pair["cur_topic"]), (
            f"cross-class pair {pair['id']!r} must be folded "
            f"(score < threshold)")


def test_t3_fail_soft_empty_none_topic():
    """T3: empty / None / non-str topic -> gate True (fail-open, 不折不 raise)."""
    assert _HAS_TOPIC_GATE
    assert _topic_overlap_gate("", "x") is True
    assert _topic_overlap_gate(None, "x") is True
    assert _topic_overlap_gate("x", None) is True
    assert _topic_overlap_gate("filemem 串项目", "") is True
    assert _topic_overlap_gate(123, "x") is True


_TOPIC_GUARD_INJECT_EXC: Optional[type] = None  # set by T4


class _BrokenTokenize(Exception):
    """Exception type used by T4 to inject tokenize failures. The gate's
    fail-soft catches this exact class (plus its generic fallback) and
    passes — a custom exception type makes the injection unambiguous."""
    pass


def test_t4_fail_soft_bm25_exception_does_not_raise():
    """T4: BM25 construction / tokenize failure -> gate True (fail-open)."""
    global _TOPIC_GUARD_INJECT_EXC
    assert _HAS_TOPIC_GATE
    import search_index
    _TOPIC_GUARD_INJECT_EXC = _BrokenTokenize
    real_tokenize = search_index.tokenize
    try:
        def _boom(text):
            raise _BrokenTokenize("boom")
        search_index.tokenize = _boom
        assert _topic_overlap_gate("a b", "c d") is True, \
            "a tokenize failure must fail-open (no fold), never raise"
    finally:
        search_index.tokenize = real_tokenize
        _TOPIC_GUARD_INJECT_EXC = None
    # BM25Index constructor exception
    real_idx = search_index.BM25Index
    try:
        class _BoomIdx:
            def __init__(self, *a, **k):
                raise RuntimeError("boom")
        search_index.BM25Index = _BoomIdx
        assert _topic_overlap_gate("a b", "c d") is True, \
            "a BM25Index construction failure must fail-open"
    finally:
        search_index.BM25Index = real_idx


# ── T5-T6: topic_mismatch choice (修复 2, 裁决 2=A) ─────────────────────

def _t5_pool(tmp: pathlib.Path):
    """The exact T5 row set: cross-session stamped confirmed row (sid≠cur),
    global skeleton confirmed row (NO session_id), and the current session's
    own confirmed row (sid==cur)."""
    cross = _row("other project confirmed fact", sp="coder", confirmed=True,
                 session_id=OTHER_SID)
    skeleton = _row("global confirmed skeleton", sp="coder", confirmed=True)
    cur = _row("current session confirmed fact", sp="coder", confirmed=True,
               session_id=CUR_SID)
    _write_pool_rows(tmp, [cross, skeleton, cur])
    return cross, skeleton, cur


def test_t5_topic_mismatch_folds_cross_session_confirmed_only(tmp_path):
    """T5 (裁决 2=A): choice='topic_mismatch' -> the cross-session stamped
    confirmed row is folded OUT of the auto-inject face; the global
    skeleton (no session stamp) and the current session's own row stay."""
    cross, skeleton, cur = _t5_pool(tmp_path)
    p = _make_provider(tmp_path)
    p._session_title_hint = lambda sid, max_len=40: ""  # hint fail-soft
    _choice_set(p, CUR_SID, "topic_mismatch")
    ctx = p.prefetch("confirmed fact", session_id=CUR_SID)
    assert cross["content"] not in ctx, (
        "topic_mismatch must fold the cross-session stamped confirmed row")
    assert skeleton["content"] in ctx, (
        "the global confirmed skeleton (no session stamp) must stay")
    assert cur["content"] in ctx, (
        "the current session's own confirmed row must stay")


def test_t6_topic_mismatch_not_chosen_is_zero_regression(tmp_path):
    """T6: default / any of the other three choices -> the predicate is
    always False: every confirmed row (incl. cross-session stamped) stays.
    This test PASSES on the RED (pre-fix) tree: no 4th option existed, and
    the default behavior already keeps all rows."""
    cross, skeleton, cur = _t5_pool(tmp_path)
    p = _make_provider(tmp_path)
    _choice_set(p, CUR_SID, "clean_start")
    ctx = p.prefetch("confirmed fact", session_id=CUR_SID)
    for r in (cross, skeleton, cur):
        assert r["content"] in ctx, (
            f"clean_start must keep ALL confirmed rows: {r['content']!r}")
    # default (no choice file at all)
    p2 = _make_provider(tmp_path, sid="s_def")
    _write_pool_rows(tmp_path, [
        _row("other project confirmed fact", sp="coder", confirmed=True,
             session_id=OTHER_SID),
    ])
    ctx2 = p2.prefetch("confirmed fact", session_id="s_def")
    assert "other project confirmed fact" in ctx2, \
        "no recorded choice -> V2 default keeps cross-session confirmed"


# ── T7-T8: topic overlap auto-gate (修复 3, 默认关) ────────────────────

def _gate_t8_setup(tmp: pathlib.Path):
    """异类 cross-session confirmed row + 同类 cross-session confirmed row,
    session traces seeded so _session_title_hint returns the fixture topics.

    cross_row seeds the TRULY cross-class topic 'supercompress 上下文压缩
    水位复盘' (cross_class[1].cur_topic, no shared tokens with the current
    filemem topic -> BM25 score 0) so the gate folds it. same_row seeds a
    same-class topic ('filemem bring-back 串项目', shares filemem tokens
    with the current topic -> score 0.396) so the gate keeps it.
    (An earlier draft seeded cross_row with cross_class[*].prev_topic, which
    is 'filemem bring-back' on every fixture row and shares tokens with the
    current filemem topic -> wrongly judged same-class -> the row survived
    the gate; that was the T8 data bug.)"""
    prev_cross_topic = _PAIRS["cross_class"][1]   # 'supercompress ... 水位复盘' = cur_topic
    prev_same_topic = _PAIRS["same_class"][0]     # 'filemem bring-back 串项目' = prev_topic
    cross_row = _row("supercompress 水位复盘 confirmed fact", sp="coder",
                     confirmed=True, session_id=OTHER_SID)
    same_row = _row("filemem 跨 session confirmed 骨架 fact", sp="coder",
                    confirmed=True, session_id="s_same")
    _write_pool_rows(tmp, [cross_row, same_row])
    traces = {
        CUR_SID: prev_same_topic["cur_topic"],
        OTHER_SID: prev_cross_topic["cur_topic"],   # genuinely no shared tokens
    }
    _seed_session_traces(tmp, traces)
    return cross_row, same_row


def test_t7_topic_gate_default_off_changes_nothing(tmp_path):
    """T7: _TOPIC_OVERLAP_GATE_ENABLED=False (shipped default) -> NO cross-
    session confirmed row is folded by the gate (behavior == 现状). This
    test PASSES on the RED tree: the gate constant does not exist yet, so
    nothing is gated."""
    cross_row, same_row = _gate_t8_setup(tmp_path)
    p = _make_provider(tmp_path)
    ctx = p.prefetch("filemem confirmed 骨架 fact", session_id=CUR_SID)
    assert cross_row["content"] in ctx, \
        "gate off: cross-class row must stay (现状不变)"
    assert same_row["content"] in ctx
    # explicit RED/GREEN switch: the constant must exist and be False now.
    assert hasattr(_fm_pkg, "_TOPIC_OVERLAP_GATE_ENABLED"), \
        "RED: _TOPIC_OVERLAP_GATE_ENABLED constant not defined yet"
    assert _fm_pkg._TOPIC_OVERLAP_GATE_ENABLED is False


def test_t8_topic_gate_on_filters_cross_class_row(tmp_path, monkeypatch):
    """T8: gate ON (monkeypatched) -> the cross-class session's confirmed row
    is folded out of the LIVE auto face, the same-class one stays (BM25
    score filter). The P3 replay round-trip keeps the POST-gate snapshot
    (a cold-start replay cannot resurrect the folded row — proven by the
    frozen-snapshot bytes carrying no cross-class line)."""
    cross_row, same_row = _gate_t8_setup(tmp_path)
    monkeypatch.setattr(_fm_pkg, "_TOPIC_OVERLAP_GATE_ENABLED", True)
    p = _make_provider(tmp_path)
    ctx = p.prefetch("filemem confirmed 骨架 fact", session_id=CUR_SID)
    # LIVE auto face: cross-class row folded by the BM25 gate, same-class kept.
    assert cross_row["content"] not in ctx, (
        f"gate ON + cross-class topic -> confirmed row folded out (live face); "
        f"got: {ctx!r}")
    assert same_row["content"] in ctx, (
        "gate ON + same-class topic -> confirmed row kept (live face)")
    # P3 snapshot round-trip: the frozen block carries the POST-gate bytes —
    # a later cold-start replay (which re-gates through
    # _filter_replayed_snapshot) cannot resurrect the folded cross-class row.
    snap = p._injected_snapshot_path(CUR_SID)
    assert snap is not None and snap.exists()
    snap_text = snap.read_text(encoding="utf-8")
    assert cross_row["content"] not in snap_text, (
        "the frozen P3 snapshot must carry the POST-gate block (no "
        f"cross-class line); got: {snap_text!r}")
    assert same_row["content"] in snap_text
    # 全新 ≠ 失联: both rows remain on the explicit search face.
    hits = p.search_memories("confirmed", limit=8, min_score=0.0)
    assert any(r.get("content") == cross_row["content"] for r in hits)


# ── T9: footer carries the previous session's topic (修复 1) ───────────

def test_t9_footer_no_longer_carries_prev_session_topic(tmp_path):
    """owner 2026-11-04 (manual trigger): the footer no longer interpolates
    the previous session's topic hint or lists the 4 clarify options — both
    moved to the user-driven clarify card (option 1/2 titles come from
    _recent_sessions_for_clarify at the moment the user says '带回'). The
    footer itself is the byte-stable neutral 1-line hint; pin that the old
    topic/handoff interpolation is GONE."""
    p = _make_provider(tmp_path)
    p_prev = _make_provider(tmp_path, sid=PREV_SID)
    p_prev._record_last_closed_session(PREV_SID)
    prev_topic = "filemem 跨 session 串项目"
    _seed_session_traces(tmp_path, {PREV_SID: prev_topic})
    ctx = p._bringback_clarify_footer(CUR_SID)
    assert ctx, "footer must fire (fresh prev-session stamp exists)"
    # The neutral hint is present:
    assert "如需带回旧 session 自动注入面" in ctx
    # The old topic interpolation is GONE:
    assert prev_topic not in ctx, (
        "footer no longer carries the prev session's topic hint "
        f"(moved to the manual-trigger clarify card); got: {ctx!r}")
    # The 4th-option wording is GONE from the footer:
    assert "topic_mismatch" not in ctx, \
        "footer no longer lists the clarify options (owner 2026-11-04)"


# ── T10-T11: P1-3 子进程闸（platform=subagent → 完全不 bring-back + 不 clarify footer）─

def test_t10_subagent_session_suppresses_footer(tmp_path):
    """T10: platform='subagent'（delegate_task child session）->
    _bringback_clarify_footer 返回空串（不弹 3 选、不 clarify、不置顶），
    且 one-shot 标记已置位（同 session 不再重弹）。主 session（platform
    != 'subagent'）footer 照弹（T9 已覆盖）。"""
    p = _make_provider(tmp_path)
    p_prev = _make_provider(tmp_path, sid=PREV_SID)
    p_prev._record_last_closed_session(PREV_SID)
    _seed_session_traces(tmp_path, {PREV_SID: "filemem 跨 session 串项目"})
    # 模拟宿主 pre_llm_call 传入 platform='subagent'（filemem_auto_retrieve 的缓存路径）
    p._last_platform = "subagent"
    ctx = p._bringback_clarify_footer(CUR_SID)
    assert ctx == "", (
        f"subagent session must NOT emit bring-back footer; got: {ctx!r}")
    # one-shot held：再调一次仍空（不会重弹）
    assert p._bringback_clarify_footer(CUR_SID) == ""
    # _is_subagent_session 谓词本身
    assert p._is_subagent_session() is True


def test_t11_non_subagent_session_keeps_footer(tmp_path):
    """T11: 主 session（platform != 'subagent'，含 None 缺省 / 其他平台）
    -> _bringback_clarify_footer 照弹（现状不变，零回归）；reorder 腿
    在子进程上被短路、主 session 上正常。"""
    p = _make_provider(tmp_path)
    p_prev = _make_provider(tmp_path, sid=PREV_SID)
    p_prev._record_last_closed_session(PREV_SID)
    _seed_session_traces(tmp_path, {PREV_SID: "filemem 跨 session 串项目"})
    # 缺省（宿主没传 platform）-> 不判子进程
    assert p._is_subagent_session() is False
    # 显式非 subagent 平台（如 cli / tui）-> 不判子进程
    p._last_platform = "cli"
    assert p._is_subagent_session() is False
    # 主 session footer 照弹
    ctx = p._bringback_clarify_footer(CUR_SID)
    assert ctx, "non-subagent session must still emit bring-back footer (现状不变)"
