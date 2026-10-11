"""Task-anchor-on-pre-compress (O1 line, source-only).

Problem (owner 2026-10-11, "漂移" root-cause ②): after a context
supercompress, the waking model sees the compacted summary + filemem
auto-recall, but NOT the in-flight worktree/task state (which subagent
dispatches were open, which git commits the session had landed, which
test files were touched). The pre-compress boundary is the LAST moment
that state is fully knowable (the messages list still holds the whole
history), so we mechanically extract "task anchors" from the messages
and pin them as a salience-1.0 episodic row right before the
compression happens. Post-compact auto-recall then carries them back
in.

Zero-LLM, stdlib-only extraction: regex scan over tool-call text for
four anchor classes:
  A1  subagent/delegation ids  (deleg_xxxxx, t_[0-9a-f]{8} kanban)
  A2  git short hashes          ([0-9a-f]{7,40}, word-bounded)
  A3  touched test-file paths   (tests/test_*.py)
  A4  worktree dirty markers    ("git status" / "dirty" / "worktree"
      state lines are NOT extracted — no false-claim; instead we
      anchor the LANDING STATE: which commits are HEAD-adjacent)

Invariants:
  #1  _extract_task_anchors on an empty / messages-None input -> []
  #2  _extract_task_anchors on a messages list with NO tool calls -> []
      (user/assistant prose alone never yields anchors — no
      hallucinated state)
  #3  A delegation id in tool-call content surfaces exactly once
      (dedup), in first-seen order
  #4  A git short hash (7+ hex, word-bounded) surfaces; a plain word
      like "abcdef" (no boundary context of a path/commit message) is
      NOT treated as a hash when it appears as a bare python identifier
      -- we require the token to be adjacent to a git-context marker
      (commit/HEAD/revert/cherry-pick) OR appear inside a diff/status
      dump to avoid matching random hex-looking identifiers.
  #5  A tests/test_foo.py path surfaces exactly once
  #6  write_task_anchor writes a row with salience=1.0,
      metadata.kind=task_anchor, status=active, source_profile stamp,
      occurred_at = now (the compress moment, not the event time)
  #7  write_task_anchor is a NO-OP (no row) when anchors list is empty
      (nothing to pin — don't write noise)
  #8  on_pre_compress, when it has anchors available, calls
      write_task_anchor ONCE per process-lifetime per session
      (idempotent via _task_anchor_written set)
  #9  on_pre_compress with zero anchors -> write_task_anchor NOT
      called, no row written, no exception (fail-soft)
  #10 write_task_anchor's disk failure (episodic_dir missing /
      unwritable) raises NOTHING into on_pre_compress (fail-soft,
      logged warning)
  #11 the row is discoverable by _load_episodic_pool (the read face
      that auto-recall uses)

Run:
    cd /root/.hermes/plugins/filemem
    PYTHONPATH=/usr/local/lib/hermes-agent \
      /usr/bin/python3 -m pytest tests/test_task_anchor_on_compress.py -q -p no:cacheprovider
"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmp: Path, sid: str = "s_anchor") -> FileMemProvider:
    """Minimal offline provider (mirrors test_p3_1_turn_close_b4)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = "test_anchor"
    p.storage_dir = tmp
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p._decay = {"salience_threshold": 0.5}
    # on_pre_compress first calls _flush_pending() — needs this attr.
    p._pending_memories = []
    # D4(a) path: on_pre_compress also calls _spawn_llm_consolidation();
    # stub it out so the offline test never touches a real LLM ctx.
    p._spawn_llm_consolidation = lambda: None
    # _shard_lock: the real one yields the lock; tests just need the
    # with-block to be enterable.
    p._shard_lock = _shard_lock_cm()
    # task-anchor dedup set
    if not hasattr(p, "_task_anchor_written"):
        p._task_anchor_written = set()
    return p


def _shard_lock_cm():
    """A no-op context-manager that satisfies `with self._shard_lock(p):`."""
    from contextlib import contextmanager

    @contextmanager
    def _cm(path):
        yield

    return _cm


def _tool_msgs(*contents):
    """Build a messages list whose tool-call content carries the given
    texts (as tool-role messages — the anchor scan only reads
    tool-role / assistant-tool-invocation content, never raw user
    prose)."""
    msgs = []
    for c in contents:
        msgs.append({"role": "tool", "content": c})
    return msgs


# ── _extract_task_anchors invariants ───────────────────────────────────────

def test_anchor_empty_input(tmp_path):
    p = _make_provider(tmp_path)
    assert p._extract_task_anchors([]) == []
    assert p._extract_task_anchors(None) == []


def test_anchor_no_tool_calls_yields_nothing(tmp_path):
    p = _make_provider(tmp_path)
    msgs = [
        {"role": "user", "content": "please commit d87aa17 now"},
        {"role": "assistant", "content": "sure, deleg_ab12 will handle it"},
    ]
    # user/assistant prose is NOT scanned (no hallucinated anchors)
    assert p._extract_task_anchors(msgs) == []


def test_anchor_delegation_id_dedup_order(tmp_path):
    p = _make_provider(tmp_path)
    msgs = _tool_msgs(
        'dispatched deleg_a1b2c3d4 to coder',
        'dispatched deleg_a1b2c3d4 again',  # duplicate
        'and deleg_e5f6a7b8',
    )
    anchors = p._extract_task_anchors(msgs)
    text = json.dumps(anchors)
    assert "deleg_a1b2c3d4" in text
    assert "deleg_e5f6a7b8" in text
    # dedup: a1b2c3d4 appears exactly once
    assert text.count("deleg_a1b2c3d4") == 1
    # order preserved: a1b2 before e5f6
    assert text.index("deleg_a1b2c3d4") < text.index("deleg_e5f6a7b8")


def test_anchor_git_hash_with_git_context(tmp_path):
    p = _make_provider(tmp_path)
    msgs = _tool_msgs(
        "git log: d87aa17 filemem P3-1 turn-level B4 fallback",
        "HEAD is at 2fcf241 review round close",
    )
    anchors = p._extract_task_anchors(msgs)
    text = json.dumps(anchors)
    assert "d87aa17" in text
    assert "2fcf241" in text


def test_anchor_bare_hex_identifier_not_a_hash(tmp_path):
    p = _make_provider(tmp_path)
    # A bare 7-hex token NOT adjacent to a git-context marker must NOT
    # be captured (avoids matching random python identifiers like
    # "0xabc1234" or a color code).
    msgs = _tool_msgs("const color = abc1234; that's a css value")
    anchors = p._extract_task_anchors(msgs)
    assert "abc1234" not in json.dumps(anchors)


def test_anchor_test_file_path_dedup(tmp_path):
    p = _make_provider(tmp_path)
    msgs = _tool_msgs(
        "modified tests/test_p3_1_turn_close_b4.py",
        "and again tests/test_p3_1_turn_close_b4.py plus tests/test_sa2_4_graph.py",
    )
    anchors = p._extract_task_anchors(msgs)
    text = json.dumps(anchors)
    assert text.count("tests/test_p3_1_turn_close_b4.py") == 1
    assert "tests/test_sa2_4_graph.py" in text


# ── write_task_anchor invariants ──────────────────────────────────────────

def test_write_anchor_empty_is_noop(tmp_path):
    p = _make_provider(tmp_path)
    p.write_task_anchor("s_anchor", [])
    # no episodic shard files at all
    shards = list(p.episodic_dir.rglob("*.jsonl"))
    assert shards == []


def test_write_anchor_row_shape(tmp_path):
    p = _make_provider(tmp_path)
    p.write_task_anchor("s_anchor", [
        {"kind": "delegation", "value": "deleg_a1b2c3d4"},
        {"kind": "git_hash", "value": "d87aa17"},
    ])
    shards = list(p.episodic_dir.rglob("*.jsonl"))
    assert len(shards) == 1
    rows = [json.loads(l) for l in shards[0].read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s_anchor"
    assert row["salience"] == 1.0, "task anchor must be salience 1.0"
    assert row["status"] == "active", "anchor must never decay-gate to 'decayed'"
    assert row["metadata"].get("kind") == "task_anchor"
    assert row.get("source_profile") == "test_anchor"
    assert row.get("occurred_at"), "anchor must carry an occurred_at stamp"
    content = row["content"]
    assert "deleg_a1b2c3d4" in content
    assert "d87aa17" in content


def test_write_anchor_discovers_in_pool(tmp_path):
    p = _make_provider(tmp_path)
    p.write_task_anchor("s_anchor", [{"kind": "git_hash", "value": "d87aa17"}])
    pool = p._load_episodic_pool()
    assert len(pool) == 1
    # pool rows normalize metadata to {"origin": ...}; the DISK row is the
    # source of truth for the task_anchor kind stamp.
    shards = list(p.episodic_dir.rglob("*.jsonl"))
    row = json.loads(shards[0].read_text().splitlines()[0])
    assert row["metadata"].get("kind") == "task_anchor"
    assert row["salience"] == 1.0
    # pool layer keeps the origin stamp + active status (not decayed-cut)
    assert pool[0]["metadata"].get("origin")
    assert pool[0].get("status") in (None, "active")


def test_write_anchor_disk_failure_is_failsafe(tmp_path):
    p = _make_provider(tmp_path)
    # Force the shard write to raise (simulating a vanished episodic dir)
    real_rglob = Path.rglob
    with patch.object(Path, "rglob", side_effect=OSError("disk gone")):
        # must NOT propagate
        p.write_task_anchor("s_anchor", [{"kind": "git_hash", "value": "d87aa17"}])
    # nothing raised; verify via direct call that the happy path is intact
    # (re-import the real rglob by calling on a fresh provider)
    p2 = _make_provider(tmp_path / "ok")
    p2.write_task_anchor("s2", [{"kind": "git_hash", "value": "d87aa17"}])
    assert p2._load_episodic_pool()


# ── on_pre_compress invariants ────────────────────────────────────────────

def _msgs_with_anchor():
    return _tool_msgs("dispatched deleg_ab12cd34 to coder; landed d87aa17")


def test_on_pre_compress_writes_anchor_once(tmp_path):
    p = _make_provider(tmp_path, sid="s_once")
    p.session_id = "s_once"
    out = p.on_pre_compress(_msgs_with_anchor(), session_id="s_once")
    # idempotent set stamped
    assert "s_once" in p._task_anchor_written
    # exactly one row on disk
    assert len(p._load_episodic_pool()) == 1
    # returns the messages untouched (on_pre_compress contract: pass-through)
    assert out == _msgs_with_anchor()


def test_on_pre_compress_second_call_no_duplicate(tmp_path):
    p = _make_provider(tmp_path, sid="s_once")
    p.session_id = "s_once"
    p.on_pre_compress(_msgs_with_anchor(), session_id="s_once")
    p.on_pre_compress(_msgs_with_anchor(), session_id="s_once")
    assert len(p._load_episodic_pool()) == 1, "second call must be a no-op"


def test_on_pre_compress_no_anchor_no_write(tmp_path):
    p = _make_provider(tmp_path, sid="s_no")
    p.session_id = "s_no"
    p.on_pre_compress([{"role": "user", "content": "hello"}], session_id="s_no")
    # no anchor row
    assert p._load_episodic_pool() == []
    # but the set is stamped only if a write happened; no write -> sid absent
    assert "s_no" not in p._task_anchor_written


if __name__ == "__main__":
    import tempfile
    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for fn in fns:
        with tempfile.TemporaryDirectory() as t:
            fn(Path(t))
        print("ok", fn.__name__)
