"""Decided-ledger verdict OVERRIDE (owner ruling 2026-11-17, "加").

A topic re-adjudicated with a NEWER, opposite verdict must GOVERN: the old
contradictory row stays ON DISK (audit chain, zero raw deletion) but is
stamped ``superseded_by`` and the reader (_is_topic_decided) skips stamped
rows — so the user's LATEST adjudication is the effective one for BOTH the
panel re-ask gate and the discard-only write-guard.

Real incident that motivated this: the 12:22 adjudication round stamped
topic #1 (filemem/kbs auto-install) as CONFIRM and topic #3 (github token)
as DISCARD; the owner's later authoritative round flipped both (#1 discard,
#3 confirm/task_86). Under plain coexistence the reader's best-overlap
returned the STALE verdict, so #1's discard write-guard silently no-oped.

Semantics (append-only ledger, never delete):
  * _record_topic_decided(content, verdict): after the same-verdict fold,
    every ACTIVE row of a DIFFERENT verdict that overlaps this topic
    (effective floor) gets ``row["superseded_by"] = fp_of_new_adjudication``
    (+ ``superseded_ts``) — an audit pointer, the row stays on disk;
  * _is_topic_decided: rows carrying ``superseded_by`` are skipped
    (inactive; the later adjudication governs);
  * chains are transitive: confirm -> discard -> confirm leaves exactly ONE
    active row (the newest), the earlier two stay stamped in order.
"""
import json
import pathlib
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryTriggerRouter,
)

# topic + near-verbatim reword variant (same topic, re-ask shape) + a
# distinct topic (override must NEVER cross over).
TOPIC_A = "filemem 和 kbs 依赖安装能不能改为像刚才修 codemap 的自动安装方式"
TOPIC_A_V = "kbs 和 filemem 装依赖能不能也走 code_map 那种自动补装的路子"
TOPIC_B = "python 脚本 解析 pdf 表格 生成 excel"


def _make_provider(tmpdir: pathlib.Path, min_shared: int = 9) -> FileMemProvider:
    """Partial-provider fixture (same shape as the decided-ledger suites)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.ledgers_dir = None
    p.sessions_dir = None
    p.episodic_dir = None
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    (tmpdir / "consolidated").mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p.trigger_router = MemoryTriggerRouter()
    p.auto_extract = False
    p._decay = {"lambda_days": 0.05,
                "consolidation": {"decided_ledger": {"enable": True,
                                                     "min_shared": min_shared}}}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._pending_drafts_notified = {}
    return p


def _read_rows(p):
    lp = p._decided_ledger_path()
    assert lp is not None and lp.exists(), "ledger file must exist after records"
    return json.loads(lp.read_text(encoding="utf-8"))


def _active(rows):
    return [r for r in rows if not r.get("superseded_by")]


# ── override stamping (write side) ─────────────────────────────────────────


def test_re_adjudication_supersedes_opposing_row(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "confirm")
    assert len(_read_rows(p)) == 1
    p._record_topic_decided(TOPIC_A_V, "discard")   # NEWER opposite verdict
    rows = _read_rows(p)
    assert len(rows) == 2, "opposite verdict appends a row, never deletes"
    act = _active(rows)
    assert len(act) == 1, "exactly ONE active row per topic after override"
    assert act[0]["verdict"] == "discard"
    stale = [r for r in rows if r["verdict"] == "confirm"][0]
    assert stale.get("superseded_by"), "stale row must carry a superseded_by pointer"
    assert stale.get("superseded_ts"), "stale row must carry the override ts"


def test_override_pointer_refers_to_the_new_adjudication_fp(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "confirm")
    p._record_topic_decided(TOPIC_A_V, "discard")
    rows = _read_rows(p)
    stale = [r for r in rows if r["verdict"] == "confirm"][0]
    new = [r for r in rows if r["verdict"] == "discard"][0]
    assert stale["superseded_by"] == new["fp"], \
        "the pointer must reference the NEW adjudication row's fp"
    assert not new.get("superseded_by")


# ── reader only sees active rows ───────────────────────────────────────────


def test_reader_returns_latest_verdict_not_stale_one(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "confirm")
    p._record_topic_decided(TOPIC_A_V, "discard")
    # RED on pre-override code: best-overlap returned the STALE confirm.
    assert p._is_topic_decided(TOPIC_A_V) == "discard"
    # the stale confirm row is invisible to the confirm-filtered query:
    assert p._is_topic_decided(TOPIC_A_V, verdicts=("confirm",)) is None
    # ...and the write-guard (discard-only) now fires for the topic:
    assert p._is_topic_decided(TOPIC_A, verdicts=("discard",)) == "discard"


def test_double_flip_returns_to_original_verdict(tmp_path):
    """confirm -> discard -> confirm: the topic ends on the NEWEST verdict.

    The 3rd record (confirm, same text as the 1st) FOLDS into the 1st row
    and reactivates it (clears its stale stamp); the 2nd (discard) row gets
    superseded instead. Net: 2 rows on disk (audit), exactly ONE active row,
    and the reader answers the newest verdict.
    """
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "confirm")
    p._record_topic_decided(TOPIC_A_V, "discard")
    p._record_topic_decided(TOPIC_A, "confirm")    # flip back
    rows = _read_rows(p)
    assert len(rows) == 2, "audit rows stay on disk (zero raw deletion)"
    act = _active(rows)
    assert len(act) == 1 and act[0]["verdict"] == "confirm", \
        f"one ACTIVE row, newest verdict; got {[(r['verdict'], r.get('superseded_by')) for r in rows]}"
    discard_row = [r for r in rows if r["verdict"] == "discard"][0]
    assert discard_row.get("superseded_by") == act[0]["fp"], \
        "the overridden discard row must point at the reactivated row's fp"
    assert p._is_topic_decided(TOPIC_A) == "confirm"
    assert p._is_topic_decided(TOPIC_A, verdicts=("discard",)) is None, \
        "the superseded discard verdict must no longer answer the write-guard"


# ── invariants the override must NOT break ─────────────────────────────────


def test_same_verdict_still_folds_single_row(tmp_path):
    """Regression guard: same-verdict upsert behavior is untouched."""
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "discard")
    p._record_topic_decided(TOPIC_A_V, "discard")
    rows = _read_rows(p)
    assert len(rows) == 1, "same-verdict reword must still FOLD (one row)"
    assert not any(r.get("superseded_by") for r in rows), \
        "a same-verdict fold must never stamp superseded_by"


def test_distinct_topics_never_superseded(tmp_path):
    """Override only crosses over the SAME topic (effective-floor overlap) —
    a different topic's rows must never be stamped by topic-A's verdicts."""
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_B, "confirm")
    p._record_topic_decided(TOPIC_A, "discard")
    rows = _read_rows(p)
    b_rows = [r for r in rows if r["verdict"] == "confirm"]
    assert b_rows and not b_rows[0].get("superseded_by"), \
        "a distinct topic's row must not be overridden by another topic"
    # and it still answers its own queries:
    assert p._is_topic_decided(TOPIC_B, verdicts=("confirm",)) == "confirm"


def test_corrupt_superseded_by_stamp_fails_open(tmp_path):
    """A malformed stamp (e.g. not a dict row / missing verdict) must not
    crash the reader and must not hide an ACTIVE row: only a well-formed
    superseded_by pointer inactivates a row."""
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_A, "discard")
    rows = _read_rows(p)
    lp = p._decided_ledger_path()
    rows[0]["superseded_by"] = {"weird": "shape"}   # malformed but present
    lp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    # the row is stamped -> inactive even if the pointer is malformed
    # (a stamp present is a stamp; fail-soft = treat as overridden, the
    # LATER adjudication whose fp it points at is authoritative):
    assert p._is_topic_decided(TOPIC_A, verdicts=("discard",)) is None
    assert p._is_topic_decided(TOPIC_A) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
