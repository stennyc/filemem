"""O1 1D (t_057eacb7): cross-profile DEFAULT ISOLATION + user-confirmation
escalation for ``filemem_supersede`` (supersedes 356 裁决 3 "drop the
cross-profile guard, exact old_id only"; 356's other 裁决 1/2/4/5/6 stand).

Locked semantics (owner 10-07 D-A, confirmed via clarify):
  * memory updates are profile (session) ISOLATED BY DEFAULT. A row stamped by
    ANOTHER profile's source_profile may not be superseded from this session
    without an EXPLICIT user confirmation.
  * the LLM face is the two OPTIONAL schema params added to filemem_supersede:
      - ``allow_cross_profile`` (bool, default false): opt in to a cross-profile
        override; ONLY meaningful when the target row is stamped by another
        profile.
      - ``user_confirmation`` (str, default null): REQUIRED alongside
        allow_cross_profile=true — the user's confirmation text / a faithful
        summary. A cross override with empty confirmation is mechanically
        blocked.
  * on a confirmed override the supersede still runs the EXISTING RMW (old row
    marked superseded, new stamped row appended) and APPENDS an audit row to
    the SAME append-only consolidation_log.jsonl channel (EP-1/EP-2) carrying
    cross_profile_overwrite=true + the user_confirmation ORIGINAL + session id.
  * OWN-profile and NEUTRAL (unstamped) rows keep the ORIGINAL RMW path with
    ZERO behavior change — the existing O1 test files are the regression gate.
  * UNRESOLVED session identity tightens to NEUTRAL-ROWS-ONLY and the override
    flag does NOT relax that (P1-4 convention).

Store hygiene: every provider is a P0-2-shaped partial instance on a tempfile
tree; ZERO writes to the live /root/.hermes/filemem store.

Case map (spec 测试 A-E):
  A. 他 profile 盖章行 + 无 flag        -> 拒绝 (信息含 profile 名 + clarify 指引)
  B. flag=true + user_confirmation 空    -> 拒绝 (机械阻塞)
  C. flag=true + user_confirmation 非空  -> supersede 成功 (旧行 superseded, 新行盖章,
                                              审计行 cross_profile_overwrite + 确认文 在盘)
  D. 本 profile / 中性行                 -> 无需 flag, 原 RMW 行为 (行为零变化)
  E. 拒绝错误消息稳定性                   -> 关键子串断言 (LLM 解析面)
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402
import pytest  # noqa: F401

_RECENT = "2026-10-01T00:00:00"  # recent L2 date -> no TTL expiry on any run date


def _make_provider(tmp: pathlib.Path, source_profile, session_id: str = "s-1d") -> FileMemProvider:
    """Same partial-provider shape as test_o1_supersede / test_o1_regression:
    enough fields for the REAL _tool_supersede (locate + RMW + audit append) to
    run without initialize(). source_profile is the SESSION identity (my_sp)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = None
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = source_profile
    p.session_id = session_id
    p._turn_active_sid = session_id
    p.trigger_router = fm.MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._session_drafts = {}
    p._pending_memories = []
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._last_recall = None
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {}
    p._pending_drafts_notified = {}
    p._main_runtime = None
    p._supersede_confirming = False
    p._fail_open_count = 0
    return p


def _row(rid: str, content: str, source_profile="default", mem_type: str = "fact") -> dict:
    """A minimal L2 pool row stamped with the shared P0-2 origin channel."""
    return {
        "id": rid, "type": mem_type, "content": content, "level": "L2",
        "metadata": {
            "origin": {"kind": "user", "trust": "high"},
            "source_profile": source_profile,
        },
        "created_at": _RECENT, "updated_at": _RECENT, "occurred_at": None,
        "source": "manual", "reference_count": 0, "salience": 0.5,
        "last_referenced": "", "ttl_days": None,
    }


def _neutral_row(rid: str, content: str) -> dict:
    """A NEUTRAL (unstamped) legacy row — no source_profile key anywhere."""
    r = _row(rid, content)
    del r["metadata"]["source_profile"]
    return r


def _seed(tmp: pathlib.Path, items, mem_type: str = "fact") -> None:
    (tmp / "memories" / f"{mem_type}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_shard(tmp: pathlib.Path, mem_type: str = "fact") -> list:
    with open(tmp / "memories" / f"{mem_type}.json", encoding="utf-8") as fh:
        return json.load(fh)


def _audit_rows(tmp: pathlib.Path) -> list:
    log = tmp / "consolidation_log.jsonl"
    if not log.exists():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _supersede_events(tmp: pathlib.Path, old_id: str) -> list:
    return [a for a in _audit_rows(tmp)
            if a.get("action") == "supersede_explicit" and a.get("old_id") == old_id]


# ══ Case A — 他 profile 盖章行 + 无 flag -> 拒绝 (actionable, clarify 指引) ══

def test_a_cross_profile_row_without_flag_rejected(tmp_path):
    """A row stamped by ANOTHER profile, no flag passed -> rejected BEFORE any
    write. The message is LLM-actionable: it names the row's owning profile,
    the content, and points the LLM to clarify-then-retry with
    allow_cross_profile=true + user_confirmation. Zero shard/audit mutation."""
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_row("fact_x", "orchestrator owns the deploy decision",
                          source_profile="orchestrator")])
    shard_before = (tmp_path / "memories" / "fact.json").read_bytes()

    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "corrected", "reason": "r"}))
    assert out.get("success") is not True
    err = out["error"]
    # profile name + content summary present
    assert "'orchestrator'" in err, err
    assert "orchestrator owns the deploy decision" in err, err
    # the clarify-then-retry guidance key substrings
    assert "cross-profile supersede is rejected" in err
    assert "clarify" in err
    assert "allow_cross_profile" in err and "user_confirmation" in err
    # zero writes: shard byte-identical, no superseded marker, no audit event
    assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before
    assert "superseded" not in _read_shard(tmp_path)[0]["metadata"]
    assert _supersede_events(tmp_path, "fact_x") == []


# ══ Case B — flag=true + user_confirmation 空 -> 机械阻塞 ═══════════════════

def test_b_flag_without_confirmation_rejected(tmp_path):
    """allow_cross_profile=true but user_confirmation empty/null -> mechanically
    REJECTED: a cross-profile override with no user confirmation is refused,
    never bypassed. Zero writes."""
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_row("fact_x", "orchestrator owns the deploy decision",
                          source_profile="orchestrator")])
    shard_before = (tmp_path / "memories" / "fact.json").read_bytes()

    for uc in (None, "", "   "):
        out = json.loads(p.handle_tool_call("filemem_supersede", {
            "old_id": "fact_x", "new_content": "corrected", "reason": "r",
            "allow_cross_profile": True, "user_confirmation": uc}))
        assert out.get("success") is not True
        assert "user_confirmation" in out["error"], out["error"]
        assert "non-empty" in out["error"], out["error"]
    # still zero writes across all three empty-confirmations
    assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before
    assert _supersede_events(tmp_path, "fact_x") == []


# ══ Case C — flag=true + user_confirmation 非空 -> supersede 成功 + 审计行 ════

def test_c_confirmed_cross_profile_override_succeeds(tmp_path):
    """allow_cross_profile=true + a non-empty user_confirmation -> the RMW runs:
    the old row is marked superseded + back-linked, the new stamped row is
    written (carrying THIS session's profile stamp), and an audit row is
    APPENDED to the SAME consolidation_log channel carrying
    cross_profile_overwrite=true + the confirmation ORIGINAL + session id."""
    p = _make_provider(tmp_path, source_profile="coder", session_id="s-1d")
    _seed(tmp_path, [_row("fact_x", "orchestrator owns the deploy decision",
                          source_profile="orchestrator")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "today we do not use docker",
        "reason": "user asked to override the cross-profile note",
        "allow_cross_profile": True,
        "user_confirmation": "user confirmed: override the orchestrator note"}))
    assert out["success"] is True, out
    new_id = out["new_id"]
    assert out["supersede_state"] == "confirmed"

    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    # old row: superseded + back-linked, KEPT ON DISK (I1 无 DELETE)
    assert rows["fact_x"]["metadata"]["superseded"] is True
    assert rows["fact_x"]["metadata"]["superseded_by"] == new_id
    # new row: explicit lineage link + THIS session's stamp (coder)
    nm = rows[new_id]["metadata"]
    assert nm["supersedes"] == ["fact_x"]
    assert nm["source_profile"] == "coder"  # stamped by the calling session

    # audit row on the SAME append-only channel: the override marker + the
    # confirmation ORIGINAL + the session id.
    ev = _supersede_events(tmp_path, "fact_x")
    assert len(ev) == 1, ev
    a = ev[0]
    assert a["cross_profile_overwrite"] is True
    assert a["user_confirmation"] == "user confirmed: override the orchestrator note"
    assert a["session_id"] == "s-1d"
    assert a["old_id"] == "fact_x" and a["new_id"] == new_id


# ══ Case D — 本 profile / 中性行 -> 无需 flag, 原 RMW 行为 (行为零变化) ══════

def test_d_own_profile_row_no_flag_needed(tmp_path):
    """An OWN-profile stamped row takes the ORIGINAL RMW path with NO flag:
    the override flag is a no-op here, and the audit row keeps the NORMAL
    10-key shape (no cross_profile_overwrite / user_confirmation / session id
    fields) — behavior zero-change vs the pre-1D guard."""
    p = _make_provider(tmp_path, source_profile="coder", session_id="s-1d")
    _seed(tmp_path, [_row("fact_m", "my own note", source_profile="coder")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_m", "new_content": "corrected", "reason": "r"}))
    assert out["success"] is True, out
    new_id = out["new_id"]
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows["fact_m"]["metadata"]["superseded"] is True
    assert rows[new_id]["metadata"]["source_profile"] == "coder"
    # audit row is the NORMAL shape: the cross-override keys are ABSENT
    ev = _supersede_events(tmp_path, "fact_m")
    assert len(ev) == 1, ev
    a = ev[0]
    assert "cross_profile_overwrite" not in a
    assert "user_confirmation" not in a
    assert "session_id" not in a
    # the pre-existing guard's pinned trailing phrase still holds (tail kept)
    assert out.get("success") is True


def test_d_neutral_row_no_flag_needed(tmp_path):
    """A NEUTRAL (unstamped) row is claimable by ANY profile with NO flag:
    the original RMW path, the new row stamped by the calling session, and the
    audit row keeps its normal shape (behavior zero-change)."""
    p = _make_provider(tmp_path, source_profile="coder", session_id="s-1d")
    _seed(tmp_path, [_neutral_row("fact_n", "neutral legacy note")])
    out = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_n", "new_content": "corrected", "reason": "r"}))
    assert out["success"] is True, out
    new_id = out["new_id"]
    rows = {r["id"]: r for r in _read_shard(tmp_path)}
    assert rows["fact_n"]["metadata"]["superseded"] is True
    assert rows[new_id]["metadata"]["source_profile"] == "coder"
    ev = _supersede_events(tmp_path, "fact_n")
    assert len(ev) == 1
    assert "cross_profile_overwrite" not in ev[0]
    assert "session_id" not in ev[0]


# ══ Case E — 拒绝错误消息稳定性 (关键子串, LLM 解析面) ═════════════════════

def test_e_rejection_message_substring_stability(tmp_path):
    """The two rejection bodies are STABLE for the LLM to parse: the
    cross-profile no-flag reject pins its key substrings AND still ends with
    "(unstamped) row" (the P0-2 guard tripwire in
    test_o1_regression_docker_guard case B stays green); the flag-without-
    confirmation reject pins its own key substrings."""
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_row("fact_x", "orchestrator owns the deploy decision",
                          source_profile="orchestrator")])

    cross_err = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "x", "reason": "r"}))["error"]
    # stable key substrings the LLM relies on to recover:
    for sub in ("'orchestrator'", "cross-profile supersede is rejected",
                "clarify", "allow_cross_profile", "user_confirmation",
                "(unstamped) row"):
        assert sub in cross_err, f"cross reject drifted: {cross_err!r}"
    # and the pinned trailing phrase (the P0-2 guard tripwire):
    assert cross_err.endswith("(unstamped) row"), cross_err

    flag_err = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "x", "reason": "r",
        "allow_cross_profile": True}))["error"]
    for sub in ("user_confirmation", "non-empty"):
        assert sub in flag_err, f"flag reject drifted: {flag_err!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
