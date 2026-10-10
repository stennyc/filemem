"""Session isolation fixes (owner 2026-10-10): two defect classes found by
tracing auto-inject session/profile isolation.

Fix 1  P3 cold-start snapshot replay runs BEFORE the P0-2 hard-isolation path
       and returns the frozen block verbatim, so a cross-profile / superseded
       / settled row once frozen re-injects every later prefetch and the
       snapshot is never refreshed (the replay branch skips
       _save_injected_snapshot).
       -> replay must pass the frozen block through the P0-2 auto-face
          predicate (same-profile + neutral rows only, no superseded /
          settled / lapsed rows); cut rows are re-frozen so the stale block
          stops replaying.

Fix 2  _resolve_profile_name falls back to the STRING "default" when the home
       has no profiles/<name> segment, so the launch-profile's (default)
       writes are stamped "default" instead of NEUTRAL and become invisible
       to every other profile's P0-2 isolated auto-inject.
       -> a home without a profiles/ segment resolves to None (neutral,
          unstamped rows visible to every profile); a profiles/<name> home
          still resolves to <name>.

Run:
    cd /root/.hermes/plugins/filemem
    /usr/bin/python3 -m pytest tests/test_session_isolation_fix.py -q \
      -p no:cacheprovider
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import FileMemProvider, _row_source_profile  # noqa: E402


# ── minimal offline provider (same shape as the P1-1 suite fixture) ────────

def _make_provider(tmp: Path, sid: str = "s_iso", profile=None) -> FileMemProvider:
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
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.auto_extract = False
    p._decay = {}
    p.min_search_score = 0.3
    p._last_save_time = time.time()
    p._save_interval = 30
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p._pending_memories = []
    p._session_drafts = {}
    p._main_runtime = None
    p._llm_ctx = None
    p._last_recall = None
    return p


def _write_row(tmp: Path, mem_type: str, row_id: str, content: str,
               sp=None, **meta_extra):
    meta = {}
    if sp is not None:
        meta["source_profile"] = sp
    meta.update(meta_extra)
    row = {
        "id": row_id,
        "type": mem_type,
        "content": content,
        "level": "L0",
        "source": "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "metadata": meta,
    }
    f = tmp / "memories" / f"{mem_type}.json"
    items = json.loads(f.read_text()) if f.exists() else []
    items.append(row)
    f.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    return row


# ── Fix 1: P3 replay must respect P0-2 isolation ────────────────────────────

def test_p3_replay_strips_cross_profile_and_superseded_rows(tmp_path):
    """A frozen block is re-gated at replay time through the V2 auto-face
    predicate (P0-2 profile isolation + O1 fold + confirmed gate):
    cross-profile rows are cut, superseded rows are cut, unconfirmed rows
    are cut (V2 confirmed gate). Confirmed own/neutral rows survive.
    The cut block is re-frozen so the stale rows stop replaying."""
    p = _make_provider(tmp_path, profile="coder")
    # Confirmed own-profile row -> survives (per-profile global skeleton)
    _write_row(tmp_path, "fact", "f_own", "coder own fact", sp="coder", confirmed=True)
    # Unconfirmed own-profile row -> cut by the V2 confirmed gate
    _write_row(tmp_path, "fact", "f_own_unconf", "unconfirmed coder fact", sp="coder")
    # Cross-profile row -> cut by P0-2 profile isolation
    _write_row(tmp_path, "fact", "f_cross", "orchestrator cross fact", sp="orchestrator", confirmed=True)
    # Superseded row -> cut by O1 auto-face fold
    _write_row(tmp_path, "fact", "f_sup", "superseded coder fact", sp="coder",
               superseded=True, confirmed=True)
    block = "\n".join([
        "[task] coder own fact",
        "[task] unconfirmed coder fact",
        "[task] orchestrator cross fact",
        "[task] superseded coder fact",
    ])
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_iso", block)
    snap = p._load_injected_snapshot("s_iso", "facts")
    assert snap == block  # raw loader is unchanged
    replayed = p._filter_replayed_snapshot(snap)
    assert "coder own fact" in replayed            # confirmed own -> kept
    assert "unconfirmed coder fact" not in replayed # unconfirmed -> cut (V2)
    assert "orchestrator cross fact" not in replayed # cross-profile -> cut
    assert "superseded coder fact" not in replayed # superseded -> cut


def test_p3_replay_keeps_neutral_rows(tmp_path):
    """Neutral (unstamped) confirmed rows survive replay on every profile.
    An unconfirmed neutral row is cut by the V2 confirmed gate even though
    the profile leg is fail-open."""
    p = _make_provider(tmp_path, profile="coder")
    _write_row(tmp_path, "fact", "f_neu", "shared neutral fact", confirmed=True)
    _write_row(tmp_path, "fact", "f_neu_unconf", "unconfirmed neutral fact")
    block = "\n".join([
        "[task] shared neutral fact",
        "[task] unconfirmed neutral fact",
    ])
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_iso", block)
    replayed = p._filter_replayed_snapshot(p._load_injected_snapshot("s_iso", "facts"))
    assert "shared neutral fact" in replayed
    assert "unconfirmed neutral fact" not in replayed  # V2 confirmed gate


def test_p3_replay_emptied_block_stops_replaying(tmp_path):
    """When isolation cuts every line of a frozen block, the replay yields an
    empty block (the prefetch branch then drops the snapshot file and
    re-prefetches live, so the stale content never re-replays)."""
    p = _make_provider(tmp_path, profile="coder")
    # Only row in the pool is cross-profile -> every line gets cut.
    _write_row(tmp_path, "fact", "f_cross", "orchestrator cross fact", sp="orchestrator")
    block = "[task] orchestrator cross fact"
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_iso", block)
    snap = p._load_injected_snapshot("s_iso", "facts")
    replayed = p._filter_replayed_snapshot(snap)
    assert replayed == ""


def test_p3_prefetch_emptied_block_drops_snapshot(tmp_path, monkeypatch):
    """prefetch()'s P3 branch: a fully-cut frozen block is dropped from disk
    (txt + .tokens) and the live re-prefetch runs instead."""
    p = _make_provider(tmp_path, profile="coder")
    _write_row(tmp_path, "fact", "f_cross", "orchestrator cross fact", sp="orchestrator")
    block = "[task] orchestrator cross fact"
    # Snapshot tokens come from _last_recall_query: keep the replay-query token
    # set identical so the P3 Jaccard gate lets the (stale) block through —
    # the production cold-start shape this test emulates.
    p._last_recall_query = "orchestrator cross fact"
    p._save_injected_snapshot("s_iso", block)
    # Spy on the live search face: the live re-prefetch must run (the snapshot
    # was dropped, so a normal search_memories call happens).
    live_calls = []
    orig_search = p.search_memories

    def spy_search(*a, **k):
        live_calls.append((a, k))
        return orig_search(*a, **k)

    monkeypatch.setattr(p, "search_memories", spy_search)
    p.prefetch("orchestrator cross fact", session_id="s_iso")
    # The live face actually ran for this session (re-prefetch happened).
    assert live_calls
    snap_path = p._injected_snapshot_path("s_iso")
    # Stale block gone from disk: the fully-cut branch unlinks txt + .tokens
    # up front, and the 0-hit tail skips the snapshot save.
    if snap_path is not None:
        assert not snap_path.exists() or not snap_path.read_text().strip()
        tok = snap_path.with_suffix(".tokens")
        assert not tok.exists()


def test_p3_replay_unresolvable_profile_fails_open(tmp_path):
    """V2 C-b invariant: when the provider profile is UNRESOLVED (None), the
    P0-2 profile leg fails open (a cross-profile row is kept, not cut), but
    the confirmed leg STILL applies — an unconfirmed cross-profile row is
    cut even with no resolvable session profile."""
    p = _make_provider(tmp_path, profile=None)
    # Confirmed cross-profile row: kept (profile leg fails open -> all rows pass)
    _write_row(tmp_path, "fact", "f_cross", "orchestrator cross fact", sp="orchestrator", confirmed=True)
    block = "[task] orchestrator cross fact"
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_iso", block)
    replayed = p._filter_replayed_snapshot(p._load_injected_snapshot("s_iso", "facts"))
    assert "orchestrator cross fact" in replayed

    # Now the same block but the row is UNCONFIRMED: the confirmed leg cuts
    # it even though the profile leg fails open. Use a separate store so the
    # pool contains ONLY the unconfirmed row (the content-key match in
    # _filter_replayed_snapshot keeps a line if ANY matching row passes).
    import tempfile
    tmp2 = Path(tempfile.mkdtemp())
    p2 = _make_provider(tmp2, profile=None)
    _write_row(tmp2, "fact", "f_cross_unconf", "orchestrator cross fact", sp="orchestrator")
    p2._last_recall_query = "facts"
    p2._save_injected_snapshot("s_iso2", "[task] orchestrator cross fact")
    replayed2 = p2._filter_replayed_snapshot(p2._load_injected_snapshot("s_iso2", "facts"))
    assert "orchestrator cross fact" not in replayed2  # confirmed gate still applies


def test_p3_prefetch_replay_uses_filtered_block(tmp_path, monkeypatch):
    """prefetch()'s P3 branch must return the FILTERED block, not the raw
    frozen block."""
    p = _make_provider(tmp_path, profile="coder")
    _write_row(tmp_path, "fact", "f_cross", "orchestrator cross fact", sp="orchestrator")
    block = "[task] orchestrator cross fact"
    p._last_recall_query = "facts"
    p._save_injected_snapshot("s_iso", block)
    # Trivial-prompt gate + pending-draft notice must not fire here: the
    # query is non-trivial and no drafts exist.
    out = p.prefetch("orchestrator cross fact", session_id="s_iso")
    assert "orchestrator cross fact" not in out


# ── Fix 2: neutral resolution for no-profiles homes ─────────────────────────

def test_resolve_profile_name_profiles_segment_returns_name():
    assert FileMemProvider._resolve_profile_name(
        Path("/root/.hermes/profiles/coder")) == "coder"
    assert FileMemProvider._resolve_profile_name(
        Path("/x/y/profiles/reviewer/deep")) == "reviewer"


def test_resolve_profile_name_no_profiles_segment_is_neutral():
    # A base home without a profiles/ segment is the NEUTRAL profile — its
    # rows must NOT be stamped "default" (invisible to every other
    # profile's isolated auto face). None = neutral, unstamped.
    assert FileMemProvider._resolve_profile_name(
        Path("/root/.hermes")) is None
    assert FileMemProvider._resolve_profile_name(
        Path.home() / ".hermes") is None


def test_row_neutral_visible_across_profiles():
    """A neutral row (no source_profile key) is auto-recall-eligible under
    ANY session profile — the whole point of Fix 2."""
    row = {"id": "f1", "type": "fact", "content": "x", "metadata": {}}
    assert _row_source_profile(row) is None
    assert _row_source_profile(row) is None  # neutral
    # The P0-2 predicate keeps neutral rows for every session profile:
    from __init__ import _auto_recall_eligible
    assert _auto_recall_eligible(None, "coder")
    assert _auto_recall_eligible(None, "orchestrator")


# ── Fix 4: settled/superseded draft rows leave the pending-draft notice ─────

def _make_notice_provider(tmp: Path, sid: str = "s_cur",
                          profile: str = "test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
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
    p.pending_queue_path = tmp / "consolidated" / "pending.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = profile
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p._pending_confirm = {}
    p.min_search_score = 0.3
    p._last_save_time = time.time()
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p.search_memories = lambda *a, **k: []
    return p


def _draft_row(content: str, sp=None, **meta_extra) -> dict:
    meta = {}
    if sp is not None:
        meta["source_profile"] = sp
    meta.update(meta_extra)
    return {
        "id": "d1",
        "type": "fact",
        "content": content,
        "level": "L0",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "metadata": meta,
    }


def test_settled_and_superseded_draft_rows_excluded_from_notice(tmp_path):
    p = _make_notice_provider(tmp_path)
    (tmp_path / "drafts_s_old.json").write_text(json.dumps([
        _draft_row("pending row", sp="test"),
        _draft_row("settled row", sp="test", settled=True),
        _draft_row("superseded row", sp="test", superseded=True),
    ]))
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "pending row" in ctx
    assert "settled row" not in ctx
    assert "superseded row" not in ctx


def test_malformed_metadata_shape_does_not_kill_notice(tmp_path):
    """A dict row whose metadata is a non-dict (e.g. a bare string) is
    malformed-shape: the settled/superseded check must NOT crash on it, the
    notice must still fire (fail-soft), and the row must not fabricate a
    content line (no readable content)."""
    p = _make_notice_provider(tmp_path)
    (tmp_path / "drafts_s_shapes.json").write_text(json.dumps([
        42, None, {"metadata": "not-a-dict", "content": "nope"},
        _draft_row("pending row", sp="test"),
    ]))
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    # The malformed rows surface as the honest unreadable line, not content:
    assert "不可读" in ctx, ctx
    # The well-formed row still renders:
    assert "pending row" in ctx
