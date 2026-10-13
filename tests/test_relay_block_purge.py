"""P1 relay-block purge: write ingress (d) leg + read-side auto-face fold.

Root cause (owner 2026-10-13, PRD relay-block-purge §2/§3):
a host delegate_task batch-completion relay (``[ASYNC DELEGATION BATCH
COMPLETE — deleg_…]`` with an embedded old-task spec + a ``Full live
transcript (…)"`` tail + transcript path) was mis-captured by the extractor
as a durable preference/decision/task row. Because it carries
``metadata.confirmed`` (the global-skeleton leg) + a neutral
``source_profile`` (P0-2 fail-open), it re-injected the OLD task's full text
into every session — including delegate sub-processes — and after compact
the shrunken live context amplified its weight, making the model "jump back
to an old task".

Fix legs under test (single-source detector ``_is_relay_block``):
  T1  write ingress (d): a full relay block -> ``host_relay_block``
      (RED: before the A leg it landed in the pool as ``None``).
  T2  zero-regression: clean prose lines -> every leg passes (None).
  T3  false-positive lock: a line that mentions ``deleg_xxxx`` but LACKS the
      'Full live transcript' tail -> NOT rejected (宁漏勿错, miss is fine).
  T4  read fold, two faces: a confirmed+failure poison row is folded OUT of
      the auto-inject face (prefetch) AND out of the P3 replay face.
  T5  isolation invariant: a folded relay row stays visible on the EXPLICIT
      search face (only the auto face is cut).
  T6  replay lock: a frozen P3 snapshot that was written BEFORE the A leg
      lands (a poison block on disk) is re-gated and the relay line is cut.

RED-first: T1/T4/T6 fail against pre-A-leg code (relay block passes ingress
and auto-face), proving the gap, then Green after the (d) leg + the fold.

Run:
  /usr/bin/python3 -m pytest tests/test_relay_block_purge.py -q -p no:cacheprovider
"""
import json, sys, pathlib, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import (  # noqa: E402
    FileMemProvider,
    MEMORY_TYPES,
    ingress_reject_class,
)
# The P1 detector/fold predicate may be absent in the pre-fix (RED) tree;
# import-guard so the module still COLLECTS — the RED tests then fail at their
# behavior asserts (ingress returns None / relay line still injected), which is
# the proof that the gap existed. After the A/B legs land, the imports succeed.
try:
    from __init__ import _is_relay_block, _relay_pollution_fold  # noqa: E402
    _HAS_P1 = True
except ImportError:
    _is_relay_block = lambda *a, **k: False  # type: ignore
    _relay_pollution_fold = lambda *a, **k: False  # type: ignore
    _HAS_P1 = False


# ── fixtures ────────────────────────────────────────────────────────────────
# A verbatim capture of a host delegate_task batch-completion relay block
# (the poison-row shape): head marker + embedded old-task spec + tail marker
# + transcript path. Kept compact (the detector is marker-based, not
# length-based), so the test does not embed a 3KB blob.
RELAY_BLOCK = (
    "[ASYNC DELEGATION BATCH COMPLETE — deleg_4f93e18d]\n"
    "A background fan-out unit you dispatched earlier — 2 subagent(s) — "
    "has finished.\n"
    "--- ✓ TASK 1/2: 修改 /root/.hermes/plugins/filemem 的 P3-1 改动 (status=completed) ---\n"
    '{"verdict":"ok","tests_after":"791 passed, 2 failed"}\n'
    "Full live transcript (complete tool/assistant trace): "
    "/root/.hermes/cache/delegation/live/deleg_4f93e18d/task-0.log\n"
)

# T3: a legitimate experience line that merely MENTIONS a delegation batch id
# but has NONE of the relay-block's head/tail structure -> must NOT be cut.
CLEAN_LINE_WITH_DELEG_ID = (
    "the owner batches remote git pushes across the O1/C3 lines rather than "
    "per-card; last batch was deleg_4f93e18d (two filemem P3-1 fixes)."
)
# T2: plain durable facts.
CLEAN_FACTS = [
    "the dev host config endpoint runs on port 8080",
    "user prefers TDD: red-green-refactor, tests before implementation",
    "the deployment uses the tools/py314 stable floor, not the installs venv",
]
# An isolated batch id alone (no head, no tail) -> also not a relay block.
BARE_DELEG_ID = "deleg_abc12345 was dispatched"


def _make_provider(tmpdir: pathlib.Path, sid: str = "s_relay",
                   profile: str = "coder") -> FileMemProvider:
    """Partial provider wired for the prefetch / replay / search faces."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.source_profile = profile
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = None
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
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
    return p


def _read_all(p) -> list:
    out = []
    for t in MEMORY_TYPES:
        f = p.memories_dir / f"{t}.json"
        if f.exists():
            out += json.load(open(f))
    return out


def _write_memory_row(p, content: str, *, sp=None, confirmed=False,
                      failure_type=None):
    """Seed one durable row in a memories shard (fact), the poison-row shape."""
    meta = {}
    if sp is not None:
        meta["source_profile"] = sp
    if confirmed:
        meta["confirmed"] = True
    if failure_type is not None:
        meta["failure_type"] = failure_type
    row = {
        "id": f"relay_{abs(hash(content)) & 0xFFFFFF:06x}",
        "type": "fact",
        "content": content,
        "level": "L0",
        "source": "auto",
        "metadata": meta,
    }
    f = p.memories_dir / "fact.json"
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


# ── T1: write ingress (d) leg ──────────────────────────────────────────────

def test_relay_block_is_rejected_at_ingress(tmp_path):
    """RED pre-A-leg: a host relay block passed ingress (None) and landed in
    the pool. After the (d) leg it must be classified host_relay_block and
    therefore NOT written."""
    assert ingress_reject_class(RELAY_BLOCK) == "host_relay_block"
    p = _make_provider(tmp_path)
    res = p._ingress_rejection(RELAY_BLOCK, origin="ingress_reject")
    assert res == "host_relay_block", \
        "the relay block must be rejected at the write ingress"
    assert _read_all(p) == [], "a rejected relay block must land 0 rows"


def test_relay_block_detector_unit():
    assert _is_relay_block(RELAY_BLOCK) is True
    # T3 shapes: no head+tail structure -> not a relay block.
    assert _is_relay_block(CLEAN_LINE_WITH_DELEG_ID) is False
    assert _is_relay_block(BARE_DELEG_ID) is False
    assert _is_relay_block(None) is False
    assert _is_relay_block("") is False


# ── T2: zero-regression on clean lines ─────────────────────────────────────

def test_clean_lines_pass_all_ingress_legs():
    for line in CLEAN_FACTS + [CLEAN_LINE_WITH_DELEG_ID, BARE_DELEG_ID]:
        assert ingress_reject_class(line) is None, \
            f"clean line must pass every ingress leg: {line[:40]!r}"


# ── T3: false-positive lock (宁漏勿错) ────────────────────────────────────

def test_deleg_id_mention_without_tail_not_cut():
    """A line that references a deleg batch id but lacks the 'Full live
    transcript' tail must survive (the detector's hard-gate on the tail
    marker is the false-positive shield)."""
    assert not _is_relay_block(CLEAN_LINE_WITH_DELEG_ID)
    assert not _relay_pollution_fold({"content": CLEAN_LINE_WITH_DELEG_ID})
    assert ingress_reject_class(CLEAN_LINE_WITH_DELEG_ID) is None


# ── T4: read-side fold, both faces ─────────────────────────────────────────

def test_confirmed_poison_row_folded_out_of_prefetch(tmp_path):
    """The poison-row class (confirmed=True + failure_type=tool_error, content
    = a relay block) is folded OUT of the auto-inject face (prefetch) even
    though the confirmed-gate would otherwise keep it."""
    p = _make_provider(tmp_path)
    _write_memory_row(p, "clean confirmed deploy rule", sp="coder",
                      confirmed=True)
    _write_memory_row(p, RELAY_BLOCK, sp="coder", confirmed=True,
                      failure_type="tool_error")
    ctx = p.prefetch("deploy rule", session_id="s_relay", confidence=0.5)
    assert "clean confirmed deploy rule" in ctx, \
        "a legitimate confirmed row must survive the auto face"
    assert "Full live transcript" not in ctx and "deleg_4f93e18d" not in ctx, \
        "the confirmed relay-block poison row must be folded out"


def test_unconfirmed_relay_row_folded_out_of_prefetch(tmp_path):
    """An UNconfirmed relay-block row is also folded out (double safety: the
    confirmed-gate alone would already cut it, the relay fold is the
    belt-and-suspenders for any relay block that slipped through)."""
    p = _make_provider(tmp_path)
    _write_memory_row(p, RELAY_BLOCK, sp="coder")
    ctx = p.prefetch("transcript task", session_id="s_relay", confidence=0.5)
    assert "Full live transcript" not in ctx


# ── T5: isolation invariant (explicit search face unaffected) ──────────────

def test_folded_relay_row_still_visible_on_search_face(tmp_path):
    """The auto-inject face cut must NOT delete the row: the explicit search
    face (search_memories / filemem_search) still returns it for audit."""
    p = _make_provider(tmp_path)
    row = _write_memory_row(p, RELAY_BLOCK, sp="coder", confirmed=True,
                            failure_type="tool_error")
    # Auto face: folded out.
    ctx = p.prefetch("transcript task", session_id="s_relay", confidence=0.5)
    assert "Full live transcript" not in ctx
    # Explicit search face: still present.
    hits = p.search_memories("transcript task", limit=8, min_score=0.0)
    assert any(r.get("content") == RELAY_BLOCK for r in hits), \
        "the explicit search face must still return the folded row"


# ── T6: P3 replay re-gate lock ─────────────────────────────────────────────

def test_frozen_relay_snapshot_replayed_is_cut(tmp_path):
    """A P3 frozen snapshot written BEFORE the A leg (a relay line on disk) is
    re-gated through _filter_replayed_snapshot; the relay line is dropped so a
    legacy poison snapshot cannot keep re-injecting on cold-start replay.

    The relay row is seeded with a CLEAN content (the detector is marker-based,
    so a relay-shaped body is NOT required for the row to be a poison row),
    and its content carries the relay-block head + tail markers (the (d) leg's
    two features) while remaining a single-line, parseable recall line."""
    p = _make_provider(tmp_path, sid="s_replay")
    # A clean-content relay row: head marker + tail marker on ONE line.
    relay_line_content = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_4f93e18d] "
                          "task done; "
                          "Full live transcript: "
                          "/root/.hermes/cache/delegation/live/deleg_4f93e18d/task-0.log")
    assert _is_relay_block(relay_line_content), \
        "the fixture must be a relay block by the double-feature detector"
    _write_memory_row(p, relay_line_content, sp="coder", confirmed=True,
                      failure_type="tool_error")
    _write_memory_row(p, "clean confirmed rule", sp="coder", confirmed=True)
    # Simulate a legacy frozen block: one `[fact]` line per pool row. The relay
    # row's recall line is parseable (its content, though bracket-opening, is
    # still gated via the secondary content-key match), so the relay-fold
    # predicate can cut it on replay.
    block = "[fact] clean confirmed rule\n[fact] " + relay_line_content
    p._last_recall_query = "rule"
    p._save_injected_snapshot("s_replay", block)
    replayed = p._filter_replayed_snapshot(
        p._load_injected_snapshot("s_replay", "rule"))
    assert "clean confirmed rule" in replayed, \
        "a legitimate line in the frozen block must survive the re-gate"
    assert "Full live transcript" not in (replayed or ""), \
        "the legacy relay line must be cut from the replayed snapshot"


# ── non-dict / bad-shape fail-open on the fold predicate ──────────────────

def test_relay_pollution_fold_fail_open():
    assert _relay_pollution_fold("not a dict") is False
    assert _relay_pollution_fold(None) is False
    assert _relay_pollution_fold({"content": None}) is False
    assert _relay_pollution_fold({"content": 42}) is False
