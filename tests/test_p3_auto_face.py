"""P3 host-face reactivation tests (S5 owner ruling 2026-11-19).

The P3 change lives in _prefetch_body, AFTER the O1 fold-out (L5029+):
  (1) BACKGROUND AUTO-RECLAIM: any archive-cascade hit that surfaced into
      the auto pool (S3 fold stamps `reclaimed=True`) is re-staged through
      _reclaim_from_archive(reason='auto_retrieve') -> a staging projection
      with reclaim_reason='auto_retrieve' (the background face; the S2
      data face stages explicit-search hits as 'explicit_search').
  (2) AUTO-FACE EXPIRY FILTER: live-pool rows whose top-level `expires_at`
      is past leave the auto-inject face ONLY. `reclaimed=True` rows are
      NEVER filtered (the staging projection is the sole entry point for
      the already-expired source — owner §3).
  (3) EXPLICIT FACE UN-TOUCHED: search_memories (the retrieve face) does
      NOT run the expiry filter — an expired live row still returns there
      (retrieve = full pool, owner ruling).
  (4) LEGACY FAIL-OPEN: live-pool rows WITHOUT `expires_at` are never
      filtered (undatable rows are kept, matching the S1 clean_reclaims
      undatable-keep red line).

Deterministic, zero LLM, all tmp stores (the same partial-provider shape
as tests/test_reclaim_reactivation.py::_make_provider — initialize() is
never run, the live /root/.hermes/filemem store is never touched).

Run (repo root /root/.hermes/plugins/filemem):
  /root/.hermes/plugin-venv/bin/python -m pytest tests/test_p3_auto_face.py -v
"""
import copy
import json
import pathlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest  # noqa: F401

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES, _utcnow  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


# ── fixture: partial provider (mirrors test_reclaim_reactivation.py) ──────

def _make_provider(tmp: pathlib.Path) -> FileMemProvider:
    """Partial provider over a synthetic tmp store (no initialize(),
    zero writes to live data)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s-p3"
    p.source_profile = "p3-test"
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = tmp / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = tmp / "knowledge" / "raw_pending.json"
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._corpus_pool_cache = None
    p._corpus_pool_sig = None
    p._corpus_pool_sections = None
    p.min_search_score = 0.0
    p._last_recall = None
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {}
    p._pending_drafts_notified = {}
    # P4 fresh-gate (2026-11-08): with NO recorded choice, _get_bringback_choice
    # degrades to 'fresh' -> the WHOLE auto face is folded to empty, before
    # anything downstream of the P3 block can assert on it. A test provider
    # is not a real session's first turn: record 'clean_start' (the V2
    # skeleton: confirmed global rows + current-session rows) so the P3
    # host-face logic under test actually sees its results. The P3 expiry
    # filter + auto-reclaim block runs BEFORE the fresh-gate, so the
    # reclaimed/expired-row assertions hold either way — clean_start only
    # keeps the confirmed skeleton + own-session rows visible downstream.
    p._set_bringback_choice("s-p3", "clean_start")
    p._turn_report_line = lambda sid: ""  # keep the visible face clean
    p._scheduled_due_block = lambda session_id=None: ""
    p._shared_mem_cfg = None  # _shared_referral_hint -> ''
    p._bringback_clarify_footer = lambda sid, user_query=None: ""
    return p


def _row(rid, content, mem_type="fact", **extra):
    d = {"id": rid, "type": mem_type, "content": content,
         "level": "L0", "salience": 0.5, "reference_count": 0,
         "created_at": _utcnow().isoformat(),
         "updated_at": _utcnow().isoformat(),
         "source_profile": "p3-test",
         "metadata": {"confirmed": True, "origin": {"kind": "user",
                                                    "trust": "high"}}}
    d.update(extra)
    return d


def _month_ago(n: int) -> str:
    return FileMemProvider._shift_months(_utcnow(), -n).strftime("%Y-%m")


def _write_buckets(p: FileMemProvider, spec):
    """spec: {bucket_name: [rows]} under p.memories_dir (append jsonl)."""
    for name, rows in spec.items():
        with open(p.memories_dir / name, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _empty_live_shards(p: FileMemProvider, keep: List[str] = None):
    """Write every MEMORY_TYPES shard: `keep` types get [] , the rest [].
    (The pool is assembled from memories/*.json; empty shards keep the
    cascade the only archive source.)"""
    for t in MEMORY_TYPES:
        (p.memories_dir / f"{t}.json").write_text("[]", encoding="utf-8")


def _reclaim_projections(p: FileMemProvider) -> List[Dict[str, Any]]:
    path = p.memories_dir / "reclaimed.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _seed_archive(p: FileMemProvider, word: str, months_ago: int = 6) -> str:
    """One archived source row in a far month bucket; returns the id."""
    rid = f"arch_{word}_{months_ago}"
    arch = _row(rid, f"{word} 归档独有行",
                created_at="2025-06-01T00:00:00",
                updated_at="2025-06-01T00:00:00")
    _write_buckets(p, {f"fact_{_month_ago(months_ago)}.jsonl": [arch]})
    return rid


def _seed_live(p: FileMemProvider, rows: List[Dict[str, Any]]):
    """Append live rows to the fact shard."""
    path = p.memories_dir / "fact.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.extend(rows)
    path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")


def _past_expires() -> str:
    return (_utcnow() - timedelta(days=1)).isoformat()


def _future_expires() -> str:
    return (_utcnow() + timedelta(days=30)).isoformat()


# ── 1. P3 background auto-reclaim (reclaimed=True rows -> staging
#    projection with reason='auto_retrieve') ─────────────────────────────

def test_auto_face_stages_archive_hit_with_auto_retrieve_reason(tmp_path):
    """S5: prefetch (auto face) hits an archive-cascade row -> the P3 block
    re-stages it with reason='auto_retrieve'. The projection lands in
    reclaimed.json with the new reason tag; the row still surfaces in the
    auto context (staging-浮出层 entries are the entry point, never cut)."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    rid = _seed_archive(p, "p3archhit")
    _o1_folded = p.prefetch("p3archhit", session_id="s-p3", confidence=0.5)
    ctx = _o1_folded
    # the archive hit surfaced on the auto face (the B口径 source row).
    assert "p3archhit 归档独有行" in ctx, \
        f"the staging-浮出层 row must stay on the auto face, got: {ctx!r}"
    # AND the P3 background auto-reclaim wrote the projection tagged
    # 'auto_retrieve' (NOT the S2 data-face 'explicit_search' default).
    projs = _reclaim_projections(p)
    mine = [r for r in projs if r["src_id"] == rid]
    assert len(mine) == 1
    assert mine[0]["reclaim_reason"] == "auto_retrieve"
    # the projection is a valid staging shape (the S1 schema is reused).
    assert mine[0]["id"].startswith("recl_" + rid + "_")
    assert mine[0]["src_shard"] == "fact"
    assert mine[0]["content"] == "p3archhit 归档独有行"
    # the projection source_profile falls back to the provider's own
    # (the archive row carries no source_profile stamp).
    assert mine[0]["source_profile"] == "p3-test"
    # expires_at = now + reclaim_ttl_days (3..5 clamp, default 3).
    now = datetime.now(timezone.utc)
    delta = (datetime.fromisoformat(mine[0]["expires_at"]) - now).total_seconds()
    assert 86400 - 10 < delta < 5 * 86400 + 10


def test_auto_face_staging_fail_soft_search_survives(tmp_path, monkeypatch):
    """P3 step (1) is fail-soft: a broken staging writer degrades the
    projection (not returned) but the auto face STILL surfaces the
    archive row and never raises."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    rid = _seed_archive(p, "p3softarch")
    staged = []

    def _capture(rows, reason="explicit_search"):
        staged.append(reason)
        return 0  # pretend nothing was written

    p._reclaim_from_archive = _capture
    ctx = p.prefetch("p3softarch", session_id="s-p3", confidence=0.5)
    assert "p3softarch 归档独有行" in ctx, \
        "the auto face must survive a staging miss (fail-soft)"
    # the P3 block DID route the reclaimed row through auto-reclaim with
    # the auto-retrieve reason tag.
    assert "auto_retrieve" in staged


def test_auto_face_live_rows_never_staged(tmp_path):
    """Red line (S2/S3): only RECLAIMED rows are staged. A pure live-pool
    hit writes ZERO staging projections, even when the row is expired."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    live = _row("p3liveonly", "p3liveword 活池独有行")
    live_expired = _row("p3liveexp", "p3liveexpword 活池过期行",
                        expires_at=_past_expires())
    _seed_live(p, [live, live_expired])
    p.prefetch("p3liveword", session_id="s-p3", confidence=0.5)
    assert _reclaim_projections(p) == [], \
        "live-pool rows (incl. expired ones) must never be staged"
    # no archive bucket -> the cascade itself had nothing to stage.


def test_explicit_staged_row_then_auto_renew_keeps_single_projection(tmp_path):
    """Same src_id across faces: the explicit face stages 'explicit_search',
    then the auto face re-claims it (S5 背景自动捞) -> RENEW in place:
    still ONE projection, reason flipped to 'auto_retrieve', expires_at
    refreshed from now."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    rid = _seed_archive(p, "p3dualface")
    # explicit retrieve face: S2 hook stages with the default reason.
    p.search_memories("p3dualface", limit=5, min_score=0.0)
    projs = _reclaim_projections(p)
    assert [r["reclaim_reason"] for r in projs] == ["explicit_search"]
    first_expires = projs[0]["expires_at"]
    # auto face: the P3 block re-stages the same src_id.
    p.prefetch("p3dualface", session_id="s-p3", confidence=0.5)
    projs = _reclaim_projections(p)
    assert len(projs) == 1, "renew, not a duplicate row"
    assert projs[0]["reclaim_reason"] == "auto_retrieve"
    assert projs[0]["src_id"] == rid
    assert projs[0]["expires_at"] >= first_expires


# ── 2. P3 auto-face expiry filter (live pool, auto face ONLY) ────────────

def test_auto_face_filters_expired_live_rows_keeps_live_and_reclaimed(tmp_path):
    """S5: live-pool rows with a past `expires_at` leave the AUTO face;
    live rows with a future `expires_at` stay; `reclaimed=True` rows are
    NEVER filtered (the staging-浮出层 is the expired source's sole
    entry point)."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    live_fresh = _row("p3exp_fresh", "p3expword fresh 活池未过期行",
                      expires_at=_future_expires())
    live_past = _row("p3exp_past", "p3expword past 活池已过期行",
                     expires_at=_past_expires())
    _seed_live(p, [live_fresh, live_past])
    rid = _seed_archive(p, "p3exp_arch")
    ctx = p.prefetch("p3expword p3exp_arch", session_id="s-p3",
                     confidence=0.5)
    assert "p3expword fresh 活池未过期行" in ctx, "future expires_at stays"
    assert "p3expword past 活池已过期行" not in ctx, \
        "expired live row must leave the auto face"
    assert "p3exp_arch 归档独有行" in ctx, \
        "reclaimed=True row must NEVER be expiry-filtered"
    # the archive hit was auto-staged (P3 step 1 ran on it).
    assert any(r["src_id"] == rid and r["reclaim_reason"] == "auto_retrieve"
               for r in _reclaim_projections(p))


def test_auto_face_all_expired_live_rows_zero_hits(tmp_path):
    """When EVERY live row is expired, the auto face 0-hits (early exit)
    and the staging file is NOT created (live rows are never staged).
    The explicit face below proves the rows are still retrievable."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    _seed_live(p, [
        _row("p3zero_past", "p3zeroword 全过期行",
             expires_at=_past_expires()),
    ])
    ctx = p.prefetch("p3zeroword", session_id="s-p3", confidence=0.5)
    assert "p3zeroword 全过期行" not in ctx
    assert not (p.memories_dir / "reclaimed.json").exists(), \
        "live rows are never staged — no staging file may appear"
    # the EXPLICIT face still surfaces the expired row (unfiltered).
    res = p.search_memories("p3zeroword", limit=5, min_score=0.0)
    assert any(r.get("id") == "p3zero_past" for r in res), \
        "retrieve = full pool, unfiltered (owner ruling)"


# ── 3. explicit face does NOT filter expiry (retrieve = full pool) ──────

def test_explicit_face_keeps_expired_live_row(tmp_path):
    """Owner ruling 2026-11-19: the EXPLICIT search face is unfiltered —
    the same expired live row that leaves the auto face still returns on
    search_memories (the reactivation entry point: an expired source can
    be retrieved explicitly and re-staged through the S2/S3 cascade)."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    expired = _row("p3explicit_exp", "p3expwordx 活池过期行",
                   expires_at=_past_expires())
    fresh = _row("p3explicit_fresh", "p3expwordx 活池未过期行",
                 expires_at=_future_expires())
    _seed_live(p, [expired, fresh])
    res = p.search_memories("p3expwordx", limit=5, min_score=0.0)
    ids = [r.get("id") for r in res]
    assert "p3explicit_exp" in ids, \
        f"expired live row must still be returned on the explicit face, got {ids}"
    assert "p3explicit_fresh" in ids
    # the explicit face NEVER auto-stages live rows (the staging door
    # exists for archive rows only — no cascade bucket here at all).
    assert not (p.memories_dir / "reclaimed.json").exists()


# ── 4. legacy fail-open: no `expires_at` key -> never filtered ──────────

def test_auto_face_legacy_row_without_expires_at_stays(tmp_path):
    """Legacy red line (S1 clean_reclaims undatable-keep, mirrored on the
    auto face): a live-pool row WITHOUT an `expires_at` key (pre-TTL
    corpus) is NEVER filtered — undatable rows are kept, the filter only
    cuts rows with a CONCRETE past date."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    legacy = _row("p3legacy_noexp", "p3legacyword 老行无过期字段")
    assert "expires_at" not in legacy
    fresh = _row("p3legacy_fresh", "p3legacyword 新行带过期字段",
                 expires_at=_future_expires())
    past = _row("p3legacy_past", "p3legacyword 新行已过期",
                expires_at=_past_expires())
    _seed_live(p, [legacy, fresh, past])
    ctx = p.prefetch("p3legacyword", session_id="s-p3", confidence=0.5)
    assert "p3legacyword 老行无过期字段" in ctx, \
        "no-expires_at row must stay (fail-open)"
    assert "p3legacyword 新行带过期字段" in ctx
    assert "p3legacyword 新行已过期" not in ctx, \
        "the concrete-past-date row is still filtered"


def test_reclaim_from_archive_default_reason_is_explicit_search(tmp_path):
    """Backward-compat red line: the NEW `reason` parameter defaults to
    'explicit_search', so every pre-P3 call site (the S2 data-face hook
    inside _archive_cascade_fold, which calls _reclaim_from_archive(rows)
    with no reason) stays byte-stable."""
    p = _make_provider(tmp_path)
    _empty_live_shards(p)
    rid = _seed_archive(p, "p3defaultreason")
    # default call (the S2 hook shape): no reason arg.
    n = p._reclaim_from_archive(
        [dict(r) for r in p._archive_cascade_recover("p3defaultreason")[0]])
    assert n >= 1
    projs = _reclaim_projections(p)
    assert all(r["reclaim_reason"] == "explicit_search" for r in projs)
    # the explicit reason tag wins when given (the P3 call site shape).
    rid2 = _seed_archive(p, "p3taggedreason", months_ago=5)
    rows2 = p._archive_cascade_recover("p3taggedreason")[0]
    p._reclaim_from_archive(rows2, reason="auto_retrieve")
    projs = _reclaim_projections(p)
    tagged = next(r for r in projs if r["src_id"] == rid2)
    assert tagged["reclaim_reason"] == "auto_retrieve"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
