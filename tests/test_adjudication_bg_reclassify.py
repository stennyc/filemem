"""②+②a (owner 2026-11-17): 裁决 confirm 路径的 LLM reclassify 搬出主进程热路径.

GOAL: pure zero-blocking of the main process (the model's tool loop runs
``filemem_review_drafts`` confirm/discard). The adjudication path's ONLY slow
operation is the budget-gated ``_reclassify_type_llm`` host-LLM call (fired
when a confirmed draft's content regex-classifies to ``fact`` = a strong-
signal miss). distill / plan-probe are ``source=="auto"``-gated and do NOT
fire on ``confirmed_draft``.

DESIGN (option ② + ②a, the owner's pick — minimal change, keep the tool
return shape + the settled-verdict-lands invariant + the in-memory
``_pending_confirm`` panel + the host ``main_runtime`` all intact):

  * A ``confirmed_draft`` save that regex-classifies to ``fact`` lands the
    row SYNCHRONOUSLY in the PURE-regex shard (``fact.json``) — O(1), zero
    LLM on the main thread (the row is durably in the pool before the tool
    returns, so the settled verdict is confirmed).
  * The budget-gated LLM reclassify is DEFERRED to a background daemon
    thread (``spawn_context_thread`` in-host, plain daemon fallback
    standalone). On an LLM upgrade it ADVISORY-STAMPS the just-landed row's
    ``metadata.llm_suggested_type`` (single-shard RMW — no cross-shard move,
    no deadlock surface). A miss / over-cap / no-runtime / spawn hiccup all
    leave the row on the regex verdict (the upgrade is non-load-bearing
    audit-trail, never blocks, never loses the row).
  * Non-``confirmed_draft`` sources (auto / manual / drain / rate-limited)
    KEEP the pre-existing SYNCHRONOUS reclassify (unchanged behavior) — the
    offload is scoped to the adjudication path the owner asked about.

Case map:
  1. confirm fact-miss lands in the REGEX shard (fact.json) synchronously,
     a tracked bg thread is spawned, and after joining it the row carries
     the advisory ``llm_suggested_type`` stamp.
  2. LLM verdict is NOT an upgrade (says ``fact``) -> no advisory stamp.
  3. no host main_runtime -> the bg reclassify self-skips (zero LLM call,
     zero stamp, row keeps the regex verdict).
  4. regression: a NON-confirmed_draft source (manual) fact-miss still runs
     the SYNCHRONOUS reclassify and lands in the LLM shard (no bg thread).
  5. the bg reclassify respects the per-session budget (over-cap -> zero
     LLM call, no stamp).

Store hygiene: tempfile-tree partial provider, ZERO writes to the live
store. Drives the REAL _save_memory + _reclassify_type_llm + the new
advisory-stamp face through the shared host-LLM fake
(_llm_host_fake.install).
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
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

_RECENT = "2026-10-01T00:00:00"

# A neutral declarative fact: no en/zh strong-signal row for
# preference/decision/task -> _detect_type must land on the `fact` default
# (the "strong-signal miss" the reclassify accuracy layer exists for).
FACT_MISS_CONTENT = "数据库连接字符串是 postgres://localhost:5432/app"


def _make_provider(tmp: pathlib.Path, source_profile: str = "default") -> FileMemProvider:
    """Same partial-provider shape as test_c3_negation_channel (enough fields
    for the REAL _save_memory / _reclassify_type_llm path, without
    initialize())."""
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
    p.session_id = "s-adjudicate"
    p._turn_active_sid = "s-adjudicate"
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
    p._main_runtime = _MAIN_RUNTIME
    p._supersede_confirming = False
    p._fail_open_count = 0
    # ②+②a budget (mirrors the provider's real default: 5/session).
    p._type_classify_limit = 5
    p._type_classify_budget = {}
    return p


def _read_shard(p: FileMemProvider, mem_type: str):
    f = p.memories_dir / f"{mem_type}.json"
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def _row_in(p: FileMemProvider, rid: str):
    for mem_type in ("fact", "preference", "decision", "task"):
        for row in _read_shard(p, mem_type):
            if str(row.get("id")) == str(rid):
                return mem_type, row
    return None, None


# ── precondition guard: the test content must be a regex `fact`-miss ─────
def test_precondition_fact_miss_content(tmp_path):
    p = _make_provider(tmp_path)
    assert p._detect_type(FACT_MISS_CONTENT) == "fact", (
        "the fixture content must regex-classify to `fact` (a strong-signal "
        "miss) for the reclassify accuracy layer to be the only LLM face")


# ── 1. confirm fact-miss -> sync regex-shard landing + bg advisory stamp ──
def test_confirm_fact_miss_sync_landing_plus_bg_advisory_stamp(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    fake, state = install(monkeypatch, replies={
        "*": {"type": "preference", "model_used": True}})
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0", source="confirmed_draft")
    assert rid, "the confirmed draft must land (sync, zero-blocking)"
    # THE zero-blocking assertion: the row landed in the PURE-REGEX shard
    # (fact.json), NOT the LLM-upgraded shard (preference.json). The main
    # thread did not wait on the LLM.
    shard, row = _row_in(p, rid)
    assert shard == "fact", f"confirmed_draft fact-miss must land in the " \
        f"regex shard (fact), not {shard}"
    assert row["type"] == "fact"
    # A tracked bg thread was spawned (the reclassify moved off the main path).
    threads = getattr(p, "_reclassify_threads", None) or []
    assert len(threads) == 1, "the bg reclassify thread must be tracked"
    # Join it; the LLM (fake -> preference) advisory-stamps the row.
    threads[-1].join(timeout=5)
    assert state["calls"] >= 1, "the bg reclassify must issue the LLM call"
    _, row_after = _row_in(p, rid)
    assert row_after["metadata"].get("llm_suggested_type") == "preference", (
        "on an LLM upgrade the bg thread advisory-stamps "
        "metadata.llm_suggested_type (single-shard, no move)")
    # The row physically STAYS in fact.json (②a: advisory stamp, no move).
    assert shard == "fact" and row_after["type"] == "fact"


# ── 2. LLM verdict is not an upgrade (says fact) -> no advisory stamp ─────
def test_confirm_fact_miss_llm_says_fact_no_stamp(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    install(monkeypatch, replies={"*": {"type": "fact", "model_used": True}})
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0", source="confirmed_draft")
    threads = getattr(p, "_reclassify_threads", None) or []
    assert len(threads) == 1
    threads[-1].join(timeout=5)
    _, row = _row_in(p, rid)
    assert row["metadata"].get("llm_suggested_type") is None, (
        "an LLM `fact` verdict agrees with the regex layer -> no override, "
        "no advisory stamp")


# ── 3. no host main_runtime -> bg reclassify self-skips (zero LLM, no stamp)
def test_confirm_fact_miss_no_runtime_no_stamp(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    p._main_runtime = None  # standalone / no host ctx
    fake, state = install(monkeypatch, replies={"*": {"type": "preference",
                                                       "model_used": True}})
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0", source="confirmed_draft")
    threads = getattr(p, "_reclassify_threads", None) or []
    # The row still lands (sync regex shard); the bg thread may or may not
    # have been spawned (a no-runtime spawn is a legal no-op) — either way the
    # LLM is never called and nothing is stamped.
    assert _row_in(p, rid)[0] == "fact"
    if threads:
        threads[-1].join(timeout=5)
    assert state["calls"] == 0, "no main_runtime -> the reclassify self-skips"
    _, row = _row_in(p, rid)
    assert row["metadata"].get("llm_suggested_type") is None


# ── 4. regression: NON-confirmed_draft keeps the SYNCHRONOUS reclassify ────
def test_manual_fact_miss_keeps_sync_reclassify(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    fake, state = install(monkeypatch, replies={"*": {"type": "preference",
                                                       "model_used": True}})
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0", source="manual")
    # Unchanged legacy behavior: the sync reclassify upgrades fact->preference
    # and the row lands in the LLM shard (preference.json), no bg thread.
    assert state["calls"] >= 1, "manual fact-miss must run the sync reclassify"
    assert _row_in(p, rid)[0] == "preference", (
        "non-confirmed_draft keeps the sync reclassify -> lands in the LLM "
        "shard (preference), not the regex shard")
    assert len(getattr(p, "_reclassify_threads", None) or []) == 0, (
        "no bg thread for the non-confirmed_draft source")


# ── 5. the bg reclassify respects the per-session budget ───────────────────
def test_confirm_fact_miss_over_budget_no_llm_call(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    fake, state = install(monkeypatch, replies={"*": {"type": "preference",
                                                       "model_used": True}})
    # Pre-exhaust the session budget (cap 5) so the bg reclassify is over-cap.
    p._type_classify_budget = {p.session_id: 5}
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0", source="confirmed_draft")
    assert _row_in(p, rid)[0] == "fact", "over-cap row still lands in regex shard"
    threads = getattr(p, "_reclassify_threads", None) or []
    if threads:
        threads[-1].join(timeout=5)
    assert state["calls"] == 0, "over-cap -> no LLM call (budget gate holds)"
    _, row = _row_in(p, rid)
    assert row["metadata"].get("llm_suggested_type") is None
