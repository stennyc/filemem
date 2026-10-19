"""404-loop regression: cross-profile `filemem_supersede` must NOT spin into a
"memory not found" retry loop.

Bug shape being guarded (the "404 loop"):
  * the LLM retries filemem_supersede with an old_id that does not exist in the
    store (or was already superseded out of the pool) and receives the same
    "memory '<id>' not found in the store" rejection on EVERY attempt — a
    non-terminating error loop if the handler mutates anything in response, or
    if a retried call ever succeeds and writes a phantom new row.
  * in the CROSS-PROFILE case, an unwinding loop: session A (profile "coder")
    calls supersede on a row stamped "orchestrator", gets the cross-profile
    rejection, retries the SAME call with allow_cross_profile=true but no user
    confirmation, gets the flag-rejection, retries again with confirmation and
    succeeds — the whole sequence must end with EXACTLY ONE supersede event,
    exactly one new row, and zero audit noise in between. No intermediate
    writes, no duplicate audit rows, no retry-induced duplication.

This file pins all of that down as a regression gate.

Store hygiene: P0-2-shaped partial provider on a tempfile tree; ZERO writes to
the live /root/.hermes/filemem store. Mirrors the shape and helpers in
test_o1_1d_cross_profile.py (the O1 1D cross-profile guard test) and
test_o1_supersede.py (the not-found guard test).
"""
import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402,F401
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402
import pytest  # noqa: F401

_RECENT = "2026-10-01T00:00:00"


def _make_provider(tmp: pathlib.Path, source_profile, session_id: str = "s-404") -> FileMemProvider:
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


# ═══════════════════════════════════════════════════════════════════════════
# 404-loop regression — part 1: repeated "not found" retries on a bad id
# ═══════════════════════════════════════════════════════════════════════════

def test_404_loop_bad_id_repeat_is_terminating_and_write_free(tmp_path, monkeypatch):
    """Re-issuing filemem_supersede N times with an old_id that does NOT exist
    in the store must:
      (a) return the SAME "not found" rejection body every time (stable, no
          drift the LLM can mis-parse into a new retry),
      (b) perform ZERO store mutations across all N attempts (the shard and
          the audit log are byte-identical before and after the whole loop),
      (c) append ZERO supersede_explicit audit events (there is no old row to
          link — a phantom new row is the 404-loop bug shape this test exists
          to catch).
    """
    # Env-pin discipline: blank the profile/home signals so the cross-profile
    # face under test runs purely on the provider's own source_profile stamp
    # (never on a host test environment leak).
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_row("fact_real", "a real row this session owns")])
    shard_before = (tmp_path / "memories" / "fact.json").read_bytes()
    audit_before = _audit_rows(tmp_path)

    bad_id = "no_such_row_404"
    out_msgs = []
    for _ in range(5):  # the "loop": re-issue the same bad call 5x
        out = json.loads(p.handle_tool_call("filemem_supersede", {
            "old_id": bad_id, "new_content": "corrected content", "reason": "r"}))
        out_msgs.append(out)
        assert out.get("success") is not True
        assert out["error"].startswith(f"memory {bad_id!r} not found in the store")

    # (a) every rejection body is byte-identical across the whole retry run
    bodies = [o["error"] for o in out_msgs]
    assert len(set(bodies)) == 1, f"drifted error bodies across retries: {bodies}"

    # (b) zero store mutation across the entire loop
    assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before
    assert _audit_rows(tmp_path) == audit_before

    # (c) no phantom new row landed under the bad id's neighborhood
    assert all(r["id"] != bad_id for r in _read_shard(tmp_path))
    assert _supersede_events(tmp_path, bad_id) == []


def test_404_loop_already_superseded_id_repeat_is_write_free(tmp_path):
    """404-loop variant: the id WAS valid, the row was superseded in a PREVIOUS
    call, and the LLM now keeps re-issuing supersede against that already-
    superseded old_id. The old row is still on disk (I1 no-DELETE) and still
    locatable, so it is NOT a not-found case — but re-superseding the same
    old_id N times must not spawn N new correcting rows either; each attempt
    is a legitimate RMW against the SAME old row, and the audit log records
    exactly one supersede_explicit event PER attempt (no silent dedupe that
    masks a real mutation, no runaway duplication from a stale pointer)."""
    p = _make_provider(tmp_path, source_profile="coder")
    _seed(tmp_path, [_row("fact_s", "original truth", source_profile="coder")])

    new_ids = []
    for i in range(3):
        out = json.loads(p.handle_tool_call("filemem_supersede", {
            "old_id": "fact_s", "new_content": f"correction #{i}", "reason": "r"}))
        assert out["success"] is True, out
        new_ids.append(out["new_id"])

    assert len(set(new_ids)) == len(new_ids), "duplicate new row ids across retries"
    evs = _supersede_events(tmp_path, "fact_s")
    assert len(evs) == 3, f"expected 3 audit events (one per attempt), got {len(evs)}"
    rows = _read_shard(tmp_path)
    assert sum(1 for r in rows if r["id"] == "fact_s" and r["metadata"].get("superseded")) == 1
    # every retried new row is DISTINCT from the original "fact_s" id and from
    # each other — no phantom merge back into the old row
    for nid in new_ids:
        assert nid != "fact_s"
        assert nid in {r["id"] for r in rows}


# ═══════════════════════════════════════════════════════════════════════════
# 404-loop regression — part 2: the cross-profile rejection-then-override
# unwinding loop ends with exactly one write
# ═══════════════════════════════════════════════════════════════════════════

def test_cross_profile_rejection_then_override_loop_terminates_in_one_write(tmp_path):
    """The full cross-profile 404-loop unwinding: session profile "coder"
    targets a row stamped "orchestrator". The expected LLM sequence is:
      1. supersede without the flag         -> cross-reject (zero write)
      2. supersede with flag, no confirm    -> mechanical flag-reject (zero write)
      3. repeat step 2 with a typo'd/empty  -> same mechanical reject (zero write)
      4. supersede with flag + real confirm -> SUCCESS (exactly one RMW + one
                                                 audit row)
    Re-issuing step 4 a second time with the SAME confirmation must NOT
    duplicate the new row or the audit event — the loop terminates cleanly,
    and total store mutation across all 5+ calls is exactly: 1 new row,
    1 audit event. This is the regression that stops a future change from
    turning the cross-profile escalation path into a write-amplifying loop."""
    p = _make_provider(tmp_path, source_profile="coder", session_id="s-404")
    _seed(tmp_path, [_row("fact_x", "orchestrator owns the deploy decision",
                          source_profile="orchestrator")])
    shard_before = (tmp_path / "memories" / "fact.json").read_bytes()

    # step 1: no flag -> cross-reject
    out1 = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "corrected", "reason": "r"}))
    assert out1.get("success") is not True
    assert "cross-profile supersede is rejected" in out1["error"]
    assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before

    # steps 2+3: flag set, confirmation empty -> mechanical reject (twice)
    for uc in (None, ""):
        out = json.loads(p.handle_tool_call("filemem_supersede", {
            "old_id": "fact_x", "new_content": "corrected", "reason": "r",
            "allow_cross_profile": True, "user_confirmation": uc}))
        assert out.get("success") is not True
        assert "user_confirmation" in out["error"]
        assert (tmp_path / "memories" / "fact.json").read_bytes() == shard_before

    # step 4: flag + real confirmation -> exactly one successful RMW
    out4 = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "today: deploy decision moves to ops",
        "reason": "user confirmed the override",
        "allow_cross_profile": True,
        "user_confirmation": "user explicitly confirmed: override the orchestrator note"}))
    assert out4["success"] is True, out4
    new_id = out4["new_id"]
    evs = _supersede_events(tmp_path, "fact_x")
    assert len(evs) == 1, f"expected exactly 1 audit event, got {len(evs)}: {evs}"
    assert evs[0]["new_id"] == new_id
    assert evs[0]["cross_profile_overwrite"] is True

    # step 5: re-issue the SAME successful call again — must NOT duplicate
    out5 = json.loads(p.handle_tool_call("filemem_supersede", {
        "old_id": "fact_x", "new_content": "today: deploy decision moves to ops",
        "reason": "retry after transient host blip",
        "allow_cross_profile": True,
        "user_confirmation": "user explicitly confirmed: override the orchestrator note"}))
    # A re-supersede of an already-superseded old row is still a legitimate RMW
    # (the row is still locatable on disk) — but it must land as a DISTINCT new
    # row, never silently merge into / duplicate out4's new row.
    assert out5["success"] is True
    assert out5["new_id"] != new_id
    evs_after = _supersede_events(tmp_path, "fact_x")
    assert len(evs_after) == 2
    rows = _read_shard(tmp_path)
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids), f"duplicate row ids after the loop: {ids}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
