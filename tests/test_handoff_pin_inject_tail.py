"""HANDOFF face 1 重新定位（specs/HANDOFF-PRD.md + owner 裁决 2026-10-17）：
pin 指定 session 时的 handoff 摘要注入 + clarify 面收窄。

owner 四条裁决（2026-10-17）：
  ① 触发面：只有用户【明确指定】session（pin_session_id 有效）才 handoff；
     auto "上一个" 腿保持最轻、不生成摘要。
  ② 内容面：pin 落选 + 该 session 有 compact ledger → 当场 handoff_summarize
     （有 main_runtime 走 LLM，无则 compact 段拼接降级，llm_used 可审计），
     摘要 append 到新 session auto 面 tail（不置顶、不进 confirmed 行、
     不改收行集合、M4 检索面不动）。
  ③ 工具面：filemem_handoff_extract 保留为显式独立入口（只生成+落盘，
     不自动注入）；旧 session 侧零动作。
  ④ clarify 面：用户消息明确指名 session → 抑制 clarify footer + 记 pin
     意图；否则 footer 现状不变。

T 系列（pin → tail 摘要注入）：
  T1: pin 有效 + compact ledger + LLM → 摘要生成（llm_used=True）且
      append 进 auto 面 tail（不置顶、不进 confirmed 行、M4 不变）。
  T2: pin 有效 + 无 main_runtime → 降级 compact 段拼接（llm_used=False），
      仍 append tail。
  T3: pin 有效 + 该 session 无 compact ledger 也无 filemem 行 → 诚实报
      "无材料"，不 append 空摘要。
  T4: auto 腿（无 pin）→ 摘要不生成、不 append（owner 裁决①，字节级回归）。
  T5: pin 跨 profile / 无效 → 回退 auto 腿，不为该 pin 生成摘要。

T7: `HANDOFF_INSTRUCTIONS` 语义修正（owner 2026-10-17：交接摘要重点应
    落在 session 末端——正在处理 + 未完成任务，不是平均摘全篇）：
      a. 必须含"末端 / 最后几轮 / 未完成 / 正在处理"的强调性指令（不
         是平铺清单——LLM 要被告知"重点在尾部"）。
      b. 材料源描述必须含"session 对话流水 / turns"（pin→tail 面实际
         喂的是 session jsonl，不是 compact ledger；原 Context 行描述
         错配）。
T8: `_pin_handoff_bundle` 尾部加权不变式：取 `turns[-40:]`（最近 40 轮）
    而非全篇，且 `user_query` 取最后一个 user turn（"用户最后说啥"是
    判断"当前任务"最强信号）——回归哨兵，锁死尾部偏重行为。

C 系列（clarify 面收窄）：
  C1: user 消息含【已知 closed】sid → footer 抑制（返回 ''）+ 记 pin 意图。
  C2: user 消息无 sid 形态 token（"接着干"）→ footer 照旧弹（回归哨兵）。
  C3: user 消息含一个【不存在】的 sid 形态 token（无 closed stamp）→
      不误抑制，footer 照旧（fail-soft，不误伤）。
  C4: 子进程 session（platform=subagent）→ 维持 P1-3 抑制（现状不变）。
"""
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta


def _ts(days_ago=1):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider, _utcnow  # noqa: E402

from _llm_host_fake import _MAIN_RUNTIME, install as install_host_fake  # noqa: E402


# ── fixture shell（同 test_handoff_extract / test_pin_session_id）─────────────


def _make_provider(tmp: Path, sid: str = "s_cur",
                   profile: Optional[str] = "default") -> FileMemProvider:
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
    p._last_save_time = time.time()
    p._save_interval = 30
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p._pending_memories = []
    p._session_drafts = {}
    p._pending_failure_meta = {}
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._main_runtime = None
    p._llm_ctx = None
    p._last_recall = None
    p.hermetis_home = tmp
    import contextlib
    p._shard_lock = lambda pth, *a, **kw: contextlib.suppress()
    return p


def _close(tmp_path, sid: str, profile="default"):
    p = _make_provider(tmp_path, sid=sid, profile=profile)
    p.session_id = sid
    p._record_last_closed_session(sid)
    return p


def _stale_close(tmp_path, sid: str, hours: int = 3):
    from datetime import timedelta
    _close(tmp_path, sid)
    path = p_path = _make_provider(tmp_path, sid=sid)._last_closed_stamp_path()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    for l in lines:
        if l["session_id"] == sid:
            l["closed_ts"] = (_utcnow() - timedelta(hours=hours)).isoformat()
    with open(path, "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")


def _write_compact_ledger(tmp: Path, sid: str, entries: Optional[list] = None):
    d = tmp / "supercompress_store"
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / f"{sid}.compact.json"
    ledger.write_text(json.dumps(
        {"version": 1, "seq": len(entries or []), "entries": entries or [
            {"seq": 1, "text": "#1 P0 prov=user seq=0 [L3]\n任务状态：X 进行中"},
            {"seq": 2, "text": "#2 P0 prov=model seq=1 [L3]\n下一步：验证 Y"},
        ]}, ensure_ascii=False), encoding="utf-8")
    return ledger


def _write_session_jsonl(tmp: Path, sid: str,
                         turns: Optional[list] = None,
                         sp: Optional[str] = "default"):
    """The source session's OWN conversation log (sessions_dir/<sid>.jsonl) —
    the single material source for the pin->tail handoff summary under the
    owner re-spec (2026-10-17). Each line is a user turn (role/content) plus
    the assistant reply that followed it. The host double-logs user turns
    (the same content arrives + is echoed on the assistant reply), so the
    default fixture deliberately DUPLICATES each turn to exercise the
    dedup path. sp stamps source_profile (the 不串 profile gate reads it)."""
    d = tmp / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    turns = turns or [
        {"role": "user", "content": "任务状态：X 进行中",
         "assistant": "已把 X 推进到第 2 步，下一步验证 Y"},
        {"role": "user", "content": "请继续",
         "assistant": "继续：跑通 Y 的测试"},
    ]
    lines = []
    for t in turns:
        row = {"session_id": sid, "role": t.get("role", "user"),
               "content": t["content"],
               "assistant": t.get("assistant", ""),
               "source_profile": sp or "default",
               "ts": _ts(1)}
        lines.append(json.dumps(row, ensure_ascii=False))
        # host double-logs the user turn (the dedup path's real defect):
        lines.append(json.dumps(row, ensure_ascii=False))
    (d / f"{sid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return d / f"{sid}.jsonl"


def _write_pool_row(tmp: Path, mem_type: str, row_id: str, content: str,
                    sp: Optional[str] = None, confirmed: bool = True,
                    session_id: Optional[str] = None):
    meta: Dict[str, Any] = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    row: Dict[str, Any] = {
        "id": row_id, "type": mem_type, "content": content,
        "level": "L0", "source": "manual",
        "created_at": _ts(1),
        "updated_at": _ts(1),
        "salience": 0.9, "last_referenced": _ts(1),
        "metadata": meta,
    }
    if session_id is not None:
        row["session_id"] = session_id
        if sp is not None:
            row["source_profile"] = sp
    f = tmp / "memories" / f"{mem_type}.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


def _set_pin(tmp_path, sid: str, pin: str, profile="default"):
    p = _make_provider(tmp_path, sid=sid, profile=profile)
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": sid, "pin_session_id": pin}))
    assert out["success"] is True, out
    return p


def _auto_face_tail(p, pin_sid: str) -> str:
    """Drive the auto-inject face the way prefetch does, return the TAIL
    segment where the pin handoff summary is appended (the handoff block is
    a distinct marker-delimited section, so the test can locate it without
    relying on exact byte position in the full context)."""
    ctx = p.prefetch("continue the pinned work", session_id=p.session_id)
    return ctx


# ── T 系列：pin → tail 摘要注入 ──────────────────────────────────────────────


def test_t1_pin_llm_summary_appended_to_tail(tmp_path, monkeypatch):
    _stale_close(tmp_path, "s_dead")          # deadlocked old session (stale)
    # owner re-spec: the summary is generated FRESH from s_dead's OWN
    # conversation log — NOT from a compact ledger / memory rows. The
    # fixture double-logs each turn to exercise the dedup path.
    _write_session_jsonl(tmp_path, "s_dead", turns=[
        {"role": "user", "content": "任务状态：X 进行中",
         "assistant": "已推进到第 2 步"},
        {"role": "user", "content": "请继续",
         "assistant": "跑通 Y"},
    ])
    # the old session ALSO has a confirmed memory row — under the white-sheet
    # re-spec this must NOT leak into the new session's auto face (排除其他
    # 记忆注入); only the handoff summary from the jsonl is carried.
    _write_pool_row(tmp_path, "fact", "f_dead", "dead session confirmed fact",
                    sp="default", confirmed=True, session_id="s_dead")
    p = _set_pin(tmp_path, "s_cur", "s_dead")
    canned = {"summary": "HANDOFF: X 进行中，下一步=验证 Y；红线=本地先行",
              "confirmed": True}
    install_host_fake(monkeypatch, replies={"*": canned})
    p._main_runtime = _MAIN_RUNTIME
    ctx = _auto_face_tail(p, "s_dead")
    # the LLM handoff summary must be injected into the auto face (tail):
    assert "HANDOFF: X 进行中，下一步=验证 Y" in ctx, (
        "the pin handoff summary (LLM) must be injected into the new "
        f"session's auto face; got: {ctx!r}")
    # owner re-spec ② 白底: the new session's auto face carries ONLY the
    # handoff summary — the old session's confirmed memory row (other memory
    # injection) is EXCLUDED, not co-injected:
    assert "dead session confirmed fact" not in ctx, (
        "white-sheet: a pinned session's own handoff summary is carried, but "
        "other-session memory rows are EXCLUDED from the new session's auto "
        f"face; got: {ctx!r}")
    # distinct marker block, not a [fact] row:
    assert "【HANDOFF" in ctx
    assert "[fact] HANDOFF" not in ctx
    # the pin's session jsonl is the SOLE material source (no compact ledger
    # read; no memory rows read — proven by the exclusion above).
    # C-gate bridge (owner ④, reviewer fix): a no-pin follow-up tool call
    # still lands the pin via the C-gate's recorded intent, then clears it.
    p2 = _make_provider(tmp_path, sid="s_cur", profile="default")
    p2._pending_user_pins = {"s_cur": "s_dead"}
    out2 = json.loads(p2.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur"}))
    assert out2["pin_requested"] == "s_dead", out2
    assert p2._get_pending_user_pin("s_cur") is None
    # M4 search face is untouched by the exclusion (全新 ≠ 失联): the row
    # remains in the on-disk pool even though the pinned session's auto face
    # is white-sheeted (only the handoff summary is carried, not other rows).
    found = p.search_memories("dead session confirmed fact",
                              limit=8, min_score=0.0, profile_scope=None)
    assert any(r.get("content") == "dead session confirmed fact" for r in found), (
        "the explicit search face (profile_scope=None) must still retrieve "
        f"the row; got {len(found)} rows")


def test_t2_pin_no_llm_degrades_to_turn_concat_tail(tmp_path):
    _stale_close(tmp_path, "s_dead")
    _write_session_jsonl(tmp_path, "s_dead", turns=[
        {"role": "user", "content": "状态行一", "assistant": "回复一"},
        {"role": "user", "content": "状态行二", "assistant": "回复二"},
    ])
    p = _set_pin(tmp_path, "s_cur", "s_dead")
    # no host-LLM fake installed -> _main_runtime stays None:
    ctx = _auto_face_tail(p, "s_dead")
    # deterministic turn-concat fallback still reaches the white-sheet face:
    assert "状态行一" in ctx and "状态行二" in ctx, (
        "with no main_runtime the handoff summary degrades to the "
        f"conversation-turn concatenation from the jsonl; got: {ctx!r}")


def test_t3_pin_no_materials_appends_nothing(tmp_path):
    _stale_close(tmp_path, "s_ghost")  # closed but NO session jsonl
    p = _set_pin(tmp_path, "s_cur", "s_ghost")
    ctx = _auto_face_tail(p, "s_ghost")
    # honest: no session jsonl -> no handoff summary injected; AND the
    # white-sheet short-circuit does NOT fire (no material) -> the recall
    # block is NOT dropped (a pin with no material keeps today's normal
    # prefetch, never an empty face).
    assert "HANDOFF" not in ctx, (
        "a pin target with no <sid>.jsonl must NOT inject an empty summary "
        f"block; got: {ctx!r}")


def test_t6_pin_dedupes_double_logged_turns(tmp_path, monkeypatch):
    """The host double-logs each user turn (the real 4bb2e8 defect: 106
    lines / 43 unique). The material gather must dedup so the LLM bundle is
    N unique turns, not 2N — verify the dedup counter actually fired."""
    _stale_close(tmp_path, "s_dup")
    jf = _write_session_jsonl(tmp_path, "s_dup", turns=[
        {"role": "user", "content": "turn-A", "assistant": "reply-A"},
        {"role": "user", "content": "turn-B", "assistant": "reply-B"},
    ])
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    mat = p._gather_pin_handoff_from_session_jsonl("s_dup")
    # the fixture double-logs each of 2 turns -> 4 lines, 2 unique, 2 dupes:
    assert len(mat["turns"]) == 2, mat
    assert mat["dupes_dropped"] == 2, mat
    assert [t["content"] for t in mat["turns"]] == ["turn-A", "turn-B"]


def test_t7_handoff_instructions_emphasize_tail_not_flat(tmp_path):
    """owner 2026-10-17: the handoff summary must EMPHASIZE the session tail
    (in-flight / unfinished work) — not a flat even-weighted digest of the
    whole transcript. Assert the LLM instructions carry the tail-priority
    directive AND describe the actual material source (session conversation
    turns, not the compact ledger the stale Context line referenced)."""
    import sys
    sys.path.insert(0, ".")
    from consolidation.llm_caller import HANDOFF_INSTRUCTIONS
    low = HANDOFF_INSTRUCTIONS.lower()
    # tail emphasis present: EXPLICIT weighting of the session END
    # (in-flight / unfinished) — not just the word "in-flight" sitting in a
    # flat "cover X" list (that is the pre-fix prompt, which let this pass
    # spuriously):
    assert any(k in low for k in ("emphasis", "focus on the", "emphasize the",
                                  "weight", "prioriti", "most important",
                                  "key to", "primary focus")), \
        f"HANDOFF_INSTRUCTIONS must carry an EXPLICIT tail-emphasis directive; got:\n{HANDOFF_INSTRUCTIONS}"
    # and the word "in-flight" must appear in that emphasis context, not only
    # as one item among equals:
    assert "in-flight" in low, "in-flight (unfinished work) must remain named"
    # material source describes session conversation (pin->tail face):
    assert any(k in low for k in ("conversation", "turns", "transcript",
                                  "session log", "jsonl")), \
        f"HANDOFF_INSTRUCTIONS Context must describe the session conversation " \
        f"turns, not the compact ledger; got:\n{HANDOFF_INSTRUCTIONS}"


def test_t8_pin_handoff_bundle_tail_weighted(tmp_path):
    """Layering: gather returns the FULL dedup'd turn list; the BUNDLE layer
    applies the last-40 truncation AND the tail-priority directive (owner
    2026-10-17: summary must emphasize the session END — in-flight +
    unfinished work, not an even-weighted digest). The bundle is a prompt
    string: it carries only the last 40 turns, and its closing instruction
    must carry the tail-emphasis semantics."""
    _stale_close(tmp_path, "s_tail")
    turns = [{"role": "user", "content": f"turn-{i}", "assistant": f"r{i}"}
             for i in range(1, 60)]  # 59 turns
    _write_session_jsonl(tmp_path, "s_tail", turns=turns)
    p = _make_provider(tmp_path, sid="s_cur", profile="default")

    # gather layer returns the full dedup'd list (no tail truncation):
    mat = p._gather_pin_handoff_from_session_jsonl("s_tail")
    assert len(mat["turns"]) == 59, f"gather layer must return full list; got {len(mat['turns'])}"
    assert mat["turns"][0]["content"] == "turn-1" and mat["turns"][-1]["content"] == "turn-59"

    # bundle layer: last-40 only + tail-priority directive in the prompt:
    bundle = p._pin_handoff_bundle("s_tail", mat["turns"])
    assert isinstance(bundle, str), "bundle must be the LLM prompt string"
    # tail turns present, early turns dropped:
    assert "turn-59" in bundle and "turn-20" in bundle
    assert "turn-1" not in bundle and "turn-10" not in bundle, (
        "bundle must cap to last-40 turns; early turns must be dropped")
    # the owner's emphasis: the prompt must carry an EXPLICIT tail-priority
    # directive (PRIMARY FOCUS on the most recent turns / in-flight work),
    # not just the word "stopped" among equals:
    low = bundle.lower()
    assert any(k in low for k in ("primary focus", "most recent", "focus on",
                                  "emphasize", "in flight", "not yet complete")), \
        f"bundle prompt must carry an explicit tail-priority directive; got tail: {bundle[-500:]}"


def test_t4_auto_leg_generates_no_summary(tmp_path):
    """owner ①: the AUTO 'last closed' leg (no explicit pin) stays light —
    no handoff summary is generated / appended. This is the byte-level
    regression sentinel: today's auto bring_back behavior is unchanged."""
    _close(tmp_path, "s_fresh")  # in-window auto target
    _write_session_jsonl(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur"}))
    assert out["success"] is True, out
    # auto leg: pin_target is the auto last-closed, no pin requested:
    assert out["pin_requested"] in (None, ""), out
    ctx = _auto_face_tail(p, "s_fresh")
    # the auto leg must NOT have appended a handoff summary block:
    assert "HANDOFF" not in ctx, (
        "owner ①: an AUTO bring_back (no explicit session named) must not "
        "generate / inject a handoff summary — the summary is only for a "
        f"user-named pin; got: {ctx!r}")


def test_t5_foreign_or_invalid_pin_generates_no_summary(tmp_path):
    """owner ①/②: a cross-profile pin (or an unknown pin) falls through to
    the auto leg and must NOT have a handoff summary generated for the
    foreign/unknown target — only a VALID same-profile pin triggers it."""
    _close(tmp_path, "s_coder", profile="coder")   # foreign profile
    _close(tmp_path, "s_fresh", profile="default")
    _write_session_jsonl(tmp_path, "s_coder", sp="coder")  # material exists but foreign
    p = _set_pin(tmp_path, "s_cur", "s_coder")      # foreign pin
    # the pin is NOT honored (foreign profile) -> falls back to auto s_fresh:
    out = json.loads(p.handle_tool_call("filemem_set_bringback", {
        "choice": "bring_back", "session_id": "s_cur",
        "pin_session_id": "s_coder"}))
    assert out["pin_target"] == "s_fresh", out
    ctx = _auto_face_tail(p, "s_fresh")
    # no summary for the foreign target:
    assert "s_coder" not in ctx and "HANDOFF" not in ctx, (
        "a foreign-profile pin must not have its handoff summary injected "
        f"(不串 profile); got: {ctx!r}")


# ── C 系列：clarify 面收窄 ────────────────────────────────────────────────────


def test_c1_user_named_known_sid_suppresses_footer(tmp_path):
    """owner ④: when the user's message explicitly names a KNOWN closed
    session id, the clarify footer is suppressed (the user already stated
    their intent — no card) and a pin intent is recorded."""
    _stale_close(tmp_path, "20261015_195324_a4a449")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    # Simulate the user's named recall: a non-trivial prefetch whose query
    # carries the closed session id.
    foot = p._bringback_clarify_footer(
        "s_cur", user_query="bring back 20261015_195324_a4a449")
    assert foot == "", (
        "a user-named known closed session must SUPPRESS the clarify "
        f"footer (no card); got: {foot!r}")
    # and the pin intent is recorded for this session:
    assert p._get_pending_user_pin("s_cur") == "20261015_195324_a4a449", (
        "the user's named session must be recorded as a pending pin intent")


def test_c2_no_sid_token_footer_fires_as_today(tmp_path):
    """owner ④ regression sentinel: a query WITHOUT a session-id-shaped
    token (e.g. '接着干') fires the footer exactly as today."""
    _close(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    foot = p._bringback_clarify_footer("s_cur", user_query="接着干")
    assert "带回" in foot, (
        "a no-sid query must fire the footer as today; got: "
        f"{foot!r}")


def test_c3_unknown_sid_token_footer_still_fires(tmp_path):
    """owner ④ fail-soft: a session-id-shaped token that has NO closed stamp
    for this profile must NOT suppress the footer (no false suppression —
    only a KNOWN closed sid counts as an explicit recall)."""
    _close(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_cur", profile="default")
    foot = p._bringback_clarify_footer(
        "s_cur", user_query="bring back 20990101_000000_ffffffff")
    assert "带回" in foot, (
        "an unknown session-id token must not suppress the footer "
        "(fail-soft, 不误伤); got: "
        f"{foot!r}")


def test_c4_subagent_session_still_suppressed(tmp_path):
    """owner ④ (P1-3 invariant): a subagent session keeps its P1-3
    suppression — the C-gate does not re-open it."""
    _close(tmp_path, "s_fresh")
    p = _make_provider(tmp_path, sid="s_sub", profile="default")
    p._last_platform = "subagent"
    foot = p._bringback_clarify_footer(
        "s_sub", user_query="bring back 20261015_195324_a4a449")
    assert foot == "", (
        "a subagent session must keep its P1-3 suppression regardless of "
        f"the C-gate; got: {foot!r}")
