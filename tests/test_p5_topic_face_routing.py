"""P5: per-face topic routing for the two user-visible notice faces
(owner 2026-10-15, 裁决 A — "按面分流，不一刀切静默").

Owner symptom: cross-topic residue (other room / other topic: 依赖等待超时
监控, tasks 表空, gave_up blocked 根因) surfaced on this session's
[记忆报告] / [未决草稿] faces — "像这种不是当前 session 的应该静默 pass".

Ruling A (two separate cuts, NOT one global silence):
  Face 1  [记忆报告] (the L0 auto-save echo face, _turn_report_line):
          cross-topic rows are SILENTLY DROPPED from the notice line
          (the owner's 静默 pass). They stay in the pool with their TTL —
          the notice just stops announcing them.
  Face 2  [未决草稿] (the L1/L2 pending-draft history face,
          _pending_draft_notice): cross-topic historical draft files are
          KEPT but REORDERED to the bottom and each header carries a
          "（异类 topic）" tag — the owner still sees them (drafts are
          the "待你拍板" class, silently deleting them would violate the
          没有我点头不可以动手 red line) but they no longer ride the top
          of the notice. Same-topic files sort first.

Shared predicate: `_topic_overlap_gate` (P1-2 fix 3, the single-source
BM25 topic-overlap operator, default threshold 0.1) — the same family
as the topic_mismatch auto-inject fold; fail-open semantics (a missing
topic on either side -> treat as SAME topic -> keep current behavior,
never false-silence). The L0 face's topic is derived from the item's
own content (truncated, since L0 rows carry no session stamp); the
draft face's topic is the owning session's trace title
(_session_title_hint, fail-soft to bare id).

Config: the gate reads `topic_guard.threshold` from the provider's
merged decay block (the P1-2 knob, default 0.1) — 改表不改码; a missing
block degrades to the code default (fail-open, same posture as P1-2).

Acceptance (spec):
  - L0 report face: a pending L0 row whose topic overlaps the current
    session topic stays listed; a non-overlapping one is silently
    dropped from the line (0 rows -> empty string, silent); the
    fail-open path (current session has no topic hint) lists EVERYTHING
    (no false-silence).
  - draft notice face: eligible historical files are ordered by
    topic-overlap (same-topic first), each file header annotated with
    "（异类 topic）" when the overlap gate says cross-topic; an
    unreadable topic (fail-open) leaves the order and annotation
    byte-identical to the pre-P5 behavior (zero regression).
  - no new pool writes, no new store file, no touch to the search
    face (检索面不受影响).
"""
import json, sys, pathlib, tempfile, threading, traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider, _topic_overlap_gate,
)

CUR_TOPIC = "filemem P5 草稿面 静默分流 依赖等待超时监控"
L0_SAME = "依赖等待超时监控：依赖等待超过 60s 打 blocked，方案见 PRD"
L0_CROSS = "gave_up 打 blocked 的确切那几行 L2504，根因实锤"
DRAFT_SAME = "filemem P5 草稿面 静默分流 依赖等待超时监控 处置"
DRAFT_CROSS = "任务 9 的 kanban 收口：t_83e52a39 已转 done"
CUR_SID = "s_p5"


def _make_provider(tmp: pathlib.Path, *, with_cur_topic: bool = True,
                   session_id: str = CUR_SID) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p._session_drafts = {}
    p._pending_confirm = {}
    p._pending_drafts_notified = {}
    p._last_recall = None
    p._last_save_time = 0
    p._save_interval = 0
    p.trigger_router = None
    p.min_search_score = 0.0
    p._decay = {}
    p._turn_active_sid = session_id
    if with_cur_topic:
        # face-1/face-2 both read the CURRENT session topic from the trace
        # (first user message) — seed it so the gate has a corpus.
        _seed_trace(p, session_id, CUR_TOPIC)
    return p


def _seed_trace(p, owner_sid: str, first_user_msg: str) -> None:
    (p.sessions_dir / f"{owner_sid}.jsonl").write_text(
        json.dumps({"role": "user", "content": first_user_msg},
                   ensure_ascii=False) + "\n",
        encoding="utf-8")


# ── 1. module predicate: the shared gate (single source P1-2) ─────────────

def test_gate_reuses_p1_2_single_source():
    """P5 must NOT re-implement topic similarity — it reuses
    _topic_overlap_gate (the P1-2 BM25 operator, default threshold
    0.1, fail-open on missing topics). Same-topic pair -> overlap;
    unrelated pair -> no overlap; missing topic -> fail-open overlap."""
    assert _topic_overlap_gate(DRAFT_SAME, CUR_TOPIC) is True, \
        "same-family topics must pass the P1-2 gate"
    assert _topic_overlap_gate("完全无关的园艺话题 浇水 施肥", CUR_TOPIC) is False
    # fail-open: a missing current topic never false-silences
    assert _topic_overlap_gate(DRAFT_CROSS, "") is True
    assert _topic_overlap_gate(None, CUR_TOPIC) is True


# ── 2. L0 report face: cross-topic rows silently dropped ──────────────────

def test_l0_report_same_topic_stays_listed():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    p._pending_confirm["s_p5"] = [
        {"id": "l0_1", "level": "L0", "content": L0_SAME, "kind": "l0"},
    ]
    line = p._turn_report_line("s_p5")
    assert "[L0] 依赖等待超时监控" in line, \
        "a same-topic L0 row must stay on the [记忆报告] face"
    # the notice line numbers items sequentially (no per-row id, A-site
    # red line); the row's content is the identity check.


def test_l0_report_cross_topic_silently_dropped():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    p._pending_confirm["s_p5"] = [
        {"id": "l0_1", "level": "L0", "content": L0_SAME, "kind": "l0"},
        {"id": "l0_2", "level": "L0", "content": L0_CROSS, "kind": "l0"},
    ]
    line = p._turn_report_line("s_p5")
    assert "依赖等待超时监控" in line, "the same-topic row must survive"
    assert "l0_2" not in line, \
        "a cross-topic L0 row is silently dropped from the notice"
    assert "gave_up" not in line


def test_l0_report_all_cross_topic_yields_silence():
    """the owner's exact symptom: a report face that would otherwise
    announce 4 unrelated rows now says NOTHING (the pending items
    remain in the queue — only the NOTICE line is empty, so the next
    turn re-reports them; silent != consumed)."""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    p._pending_confirm["s_p5"] = [
        {"id": "l0_1", "level": "L0", "content": L0_CROSS, "kind": "l0"},
    ]
    line = p._turn_report_line("s_p5")
    assert line == "", "all-cross-topic report face must be silent"
    # the queue is NOT consumed (静默 ≠ 丢弃): the item is still pending
    assert p._pending_confirm["s_p5"], "dropped-from-notice rows stay queued"


def test_l0_report_fail_open_when_no_cur_topic():
    """a current session with NO resolvable topic hint: the gate is
    fail-open -> EVERYTHING is listed (the pre-P5 behavior, zero
    false-silence)."""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()), with_cur_topic=False)
    p._pending_confirm["s_p5"] = [
        {"id": "l0_1", "level": "L0", "content": L0_CROSS, "kind": "l0"},
    ]
    line = p._turn_report_line("s_p5")
    assert "gave_up" in line, "fail-open: no cur topic -> nothing is dropped"


# ── 3. draft notice face: reorder + tag, never silence ────────────────────

def _hist_draft_notice(p, owner_sid: str) -> str:
    _seed_trace(p, owner_sid, "见草稿")
    return p._pending_draft_notice("", session_id="s_p5")


def test_draft_notice_cross_topic_moved_last_and_tagged():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    # two historical draft files: one same-family (its trace topic overlaps
    # the current session topic), one unrelated
    f_same = tmp / "drafts_20261010_same.json"
    f_cross = tmp / "drafts_20261010_cross.json"
    f_same.write_text(json.dumps([
        {"id": "d1", "type": "fact", "content": DRAFT_SAME,
         "level": "L1", "metadata": {"source_profile": "test"}}]),
        encoding="utf-8")
    f_cross.write_text(json.dumps([
        {"id": "d2", "type": "fact", "content": DRAFT_CROSS,
         "level": "L2", "metadata": {"source_profile": "test"}}]),
        encoding="utf-8")
    _seed_trace(p, "20261010_same", "filemem P5 草稿面 静默分流")
    _seed_trace(p, "20261010_cross", "kanban 收口 任务 9")
    notice = p._pending_draft_notice("", session_id="s_p5")
    assert "[未决草稿]" in notice, "historical drafts are NEVER silenced"
    # both files stay listed — identified by their CONTENT (draft notice
    # lines carry no per-row id, A-site red line)
    assert DRAFT_SAME in notice and DRAFT_CROSS in notice, \
        "both files stay listed"
    # same-topic file FIRST, cross-topic file LAST (reorder)
    assert notice.index("20261010_same") < notice.index("20261010_cross"), \
        "the same-topic draft file must sort ahead of the cross-topic one"
    # the cross-topic header carries the 异类 tag, the same-topic one does not
    same_hdr = notice[notice.index("[session 20261010_same"):
                     notice.index("[session 20261010_same") + 120]
    cross_hdr = notice[notice.index("[session 20261010_cross"):
                      notice.index("[session 20261010_cross") + 120]
    assert "（异类 topic）" not in same_hdr
    assert "（异类 topic）" in cross_hdr


def test_draft_notice_fail_open_byte_identical():
    """an unreadable/absent trace (no topic hint on either side) must
    leave the ordering and annotation EXACTLY as pre-P5: files in
    sorted(glob) order, no 异类 tag anywhere — the zero-regression
    gate (the P1-2 fail-open posture applied to the notice face)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, with_cur_topic=False)
    f1 = tmp / "drafts_20261010_a.json"
    f2 = tmp / "drafts_20261010_b.json"
    f1.write_text(json.dumps([
        {"id": "d1", "type": "fact", "content": DRAFT_SAME,
         "level": "L1", "metadata": {"source_profile": "test"}}]),
        encoding="utf-8")
    f2.write_text(json.dumps([
        {"id": "d2", "type": "fact", "content": DRAFT_CROSS,
         "level": "L2", "metadata": {"source_profile": "test"}}]),
        encoding="utf-8")
    # NOTE: no trace files seeded for either owner -> _session_title_hint
    # returns "" for both -> the gate is fail-open for both.
    notice = p._pending_draft_notice("", session_id="s_p5")
    assert notice.index("20261010_a") < notice.index("20261010_b"), \
        "fail-open: sorted-glob order is unchanged"
    assert "（异类 topic）" not in notice, \
        "fail-open: no 异类 tag may appear without a resolvable topic"


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
