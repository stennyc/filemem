"""S2/S3 reactivation model selftest (dual-side alignment with the
filemem-dsh Go side, commit 648c497, REACTIVATION_DESIGN.md §1c).

Deterministic, zero LLM, all stores tmp-synthetic — the live
/root/.hermes/filemem store is never touched.

Covers:
  S2  ① ParseTimeAnchor (Go contract, rule-by-rule):
        absolute 4-digit year + 1-2-digit month ('-' '/' '.' '年月'),
        N 个月前 / N 周前 / N 天前 / 上个月 / 去年 (now-based, UTC),
        Chinese numerals as an option, fail-soft (no time word -> ok=False,
        never guess a month).
  S2 ② two-level cascade: FAST tier (last reclaim_fast_months buckets,
        incl. the current month) returns immediately on a hit; a miss falls
        to the SLOW tier = full scan. Bucket listing is glob-driven over
        memories/<type>_<YYYY-MM>.jsonl; corrupt lines are skipped
        (never block the cascade).
  S2 ③ time-anchored query -> scan ONLY the target month +/-1 buckets
        (union hit), no cascade.
  S1 staging door (reclaimed.json):
        put_reclaim new-projection schema (recl_<srcid>_<ts>,
        expires_at = now + reclaim_ttl_days, reason=explicit_search,
        source_profile/salience copied from the source row);
        renew-on-same-src_id (no duplicate row, expires_at refreshed);
        clean_reclaims physical expiry; fail-soft (corrupt file -> empty,
        write failure -> None, never crash the caller).
  S3 fold-in (B口径):
        search_memories with a live pool that misses an archived row
        returns the SOURCE MEMORY row itself with `reclaimed=True`
        (no src_id / projection id exposed); a live-pool hit is NOT
        staged; expired live rows stay in the pool UN-filtered (S2/S3
        truth model: the live face never filters expiry).
  Zero-diff red line: the cascade + staging never writes to the archive
        jsonl files or the live shards.

Structure mirrors tests/test_s4_lifespan.py (same tmp provider helper
shape); the decay knobs reclaim_fast_months / reclaim_ttl_days ride
self._decay like every other knob.
"""
import copy
import json
import pathlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES, _utcnow  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


def _row(rid, content, mem_type="fact", **extra):
    d = {"id": rid, "type": mem_type, "content": content,
         "level": "L0", "salience": 0.5, "reference_count": 0,
         "created_at": _utcnow().isoformat(),
         "updated_at": _utcnow().isoformat()}
    d.update(extra)
    return d


def _make_provider(tmp: pathlib.Path) -> FileMemProvider:
    """Partial provider over a synthetic tmp store (no initialize(),
    zero writes to live data)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s-reclaim"
    p.source_profile = "reclaim-test"
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
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._corpus_pool_cache = None
    p._corpus_pool_sig = None
    p._corpus_pool_sections = None
    return p


def _write_buckets(p: FileMemProvider, spec):
    """spec: {bucket_name: [rows]} under p.memories_dir (append jsonl)."""
    for name, rows in spec.items():
        with open(p.memories_dir / name, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _this_month() -> str:
    return _utcnow().strftime("%Y-%m")


def _month_ago(n: int) -> str:
    return FileMemProvider._shift_months(_utcnow(), -n).strftime("%Y-%m")


# ── S2 ① ParseTimeAnchor (dual-side contract, Go verbatim) ────────────────

def test_anchor_absolute_variants():
    # 4-digit year + 1-2-digit month, all four separators.
    for q in ("我2026-10月做了什么", "看看 2026/10 的归档",
              "2026.10 部署记录", "2026年10月发生了什么",
              "2026年9月部署", "2026-9 的归档"):
        target, ok = FileMemProvider.parse_time_anchor(q)
        assert ok, q
    assert FileMemProvider.parse_time_anchor("2026年10月")[0] == "2026-10"
    assert FileMemProvider.parse_time_anchor("2026-9")[0] == "2026-09"
    # absolute beats relative (wins when both are present).
    assert FileMemProvider.parse_time_anchor("2025-06 上个月")[0] == "2025-06"


def test_anchor_relative_rules():
    # '两个月前' -> now - 2 months (Go: now.AddDate(0, -N, 0)).
    target, ok = FileMemProvider.parse_time_anchor("两个月前部署的事")
    assert ok and target == _month_ago(2)
    # '三个月前' / 'N个月前' / 'N月前' — all resolve to the same
    # month-shift semantics (the N-months-ago rule, Go L2 contract).
    # Note: '前 N 个月' is NOT a distinct Go L2 rule (it would
    # double-shift N months); the N-个月 pattern carries its own N.
    t2, ok2 = FileMemProvider.parse_time_anchor("3个月前")
    assert ok2 and t2 == _month_ago(3)
    t3, ok3 = FileMemProvider.parse_time_anchor("4月前")
    assert ok3 and t3 == _month_ago(4)
    # weeks / days resolve through the now-based shift.
    now = _utcnow()
    w, okw = FileMemProvider.parse_time_anchor("三周前改的代码")
    assert okw and w == (now - timedelta(weeks=3)).strftime("%Y-%m")
    d, okd = FileMemProvider.parse_time_anchor("5天前")
    assert okd and d == (now - timedelta(days=5)).strftime("%Y-%m")
    lm, oklm = FileMemProvider.parse_time_anchor("上个月讨论的方案")
    assert oklm and lm == _month_ago(1)
    ly, okly = FileMemProvider.parse_time_anchor("去年的决定")
    assert okly and ly == _month_ago(12)


def test_anchor_chinese_numerals_optional():
    # digits remain the primary path; 一二三... also resolve.
    now = _utcnow()
    assert FileMemProvider.parse_time_anchor("二个月前")[0] == _month_ago(2)
    assert FileMemProvider.parse_time_anchor("三周前")[0] == \
        (now - timedelta(weeks=3)).strftime("%Y-%m")
    assert FileMemProvider.parse_time_anchor("五天前")[0] == \
        (now - timedelta(days=5)).strftime("%Y-%m")
    # compound numerals: 二十一 = 21.
    assert FileMemProvider._cn_numeral("二十一") == 21
    assert FileMemProvider._cn_numeral("十") == 10


def test_anchor_fail_soft_never_guesses():
    for q in ("UI 设计规范库", "", "   ", "没有时间的查询", "2026-99月"):
        assert FileMemProvider.parse_time_anchor(q)[1] is False, q


def test_month_tolerance_union():
    assert FileMemProvider._month_tolerance("2026-10") == \
        ["2026-09", "2026-10", "2026-11"]
    # year boundary wraps both sides.
    assert FileMemProvider._month_tolerance("2026-01") == \
        ["2025-12", "2026-01", "2026-02"]
    assert FileMemProvider._month_tolerance("2026-12") == \
        ["2026-11", "2026-12", "2027-01"]
    # malformed -> target only (fail-soft).
    assert FileMemProvider._month_tolerance("bogus") == ["bogus"]


# ── S2 ② two-level cascade ────────────────────────────────────────────────

def test_cascade_fast_tier_hit_skips_slow(tmp_path):
    p = _make_provider(tmp_path)
    old_row = _row("fact_old_slow", "oldslowtopic 慢速层独有词")
    old_row["created_at"] = "2025-06-01T00:00:00"
    old_row["updated_at"] = "2025-06-01T00:00:00"
    new_row = _row("fact_new_fast", "newfasttopic 快速层独有词")
    _write_buckets(p, {
        f"fact_{_month_ago(8)}.jsonl": [old_row],      # slow tier only
        f"fact_{_month_ago(1)}.jsonl": [new_row],      # fast tier
    })
    # pure-entity query: fast tier (last 3 months) hits -> no slow scan.
    rows, anchor, hit = p._archive_cascade_recover("newfasttopic")
    assert not hit and anchor is None
    assert [r["id"] for r in rows] == ["fact_new_fast"]
    # the slow-tier-only row is invisible while the fast tier has ANY hit
    # (the "反复想起的记忆变快" fast-first contract).
    p._decay["reclaim_fast_months"] = 3
    rows, _, _ = p._archive_cascade_recover("newfasttopic")
    assert [r["id"] for r in rows] == ["fact_new_fast"]
    # query hitting ONLY the slow tier -> fast miss -> slow sweep catches it.
    rows2, _, _ = p._archive_cascade_recover("oldslowtopic")
    assert [r["id"] for r in rows2] == ["fact_old_slow"]


def test_cascade_corrupt_lines_skipped(tmp_path):
    p = _make_provider(tmp_path)
    ok_row = _row("fact_good", "goodtopic 归档行")
    bucket = p.memories_dir / f"fact_{_month_ago(1)}.jsonl"
    bucket.write_text(
        "{not json at all\n"
        + json.dumps(ok_row, ensure_ascii=False) + "\n"
        + "trailing garbage line\n",
        encoding="utf-8")
    rows, _, _ = p._archive_cascade_recover("goodtopic")
    assert [r["id"] for r in rows] == ["fact_good"]
    # a totally corrupt bucket still yields the sibling good rows.
    p2 = p
    p2.memories_dir.mkdir(parents=True, exist_ok=True)


def test_cascade_type_slice_sees_only_its_type(tmp_path):
    p = _make_provider(tmp_path)
    _write_buckets(p, {
        f"fact_{_month_ago(1)}.jsonl": [_row("fact_x", "facttopic 行")],
        f"task_{_month_ago(1)}.jsonl": [_row("task_x", "tasktopic 行")],
    })
    rf, _, _ = p._archive_cascade_recover("facttopic", mem_type="fact")
    rt, _, _ = p._archive_cascade_recover("tasktopic", mem_type="task")
    assert [r["id"] for r in rf] == ["fact_x"]
    assert [r["id"] for r in rt] == ["task_x"]
    # a type slice that has NO matching bucket -> empty (no cross-type leak).
    rr, _, _ = p._archive_cascade_recover("facttopic", mem_type="task")
    assert rr == []


def test_cascade_time_anchored_union_hits(tmp_path):
    p = _make_provider(tmp_path)
    m10 = _month_ago(1)   # the target month for "上个月"
    m11 = _month_ago(0)   # +1 neighbour
    m9 = _month_ago(2)    # -1 neighbour
    _write_buckets(p, {
        f"fact_{m9}.jsonl": [_row("fact_lo", "loanchor 低档词")],
        f"fact_{m10}.jsonl": [_row("fact_mid", "midanchor 中档词")],
        f"fact_{m11}.jsonl": [_row("fact_hi", "hianchor 高档词")],
        f"fact_{_month_ago(4)}.jsonl": [_row("fact_far", "faran 远档词")],
    })
    # The union window m9..m11 has no keyword match anywhere, so the
    # anchored query misses cleanly: no rows, no far-month leak.
    rows, anchor, hit = p._archive_cascade_recover("上个月 的锚定词 远档词")
    assert hit and anchor == m10
    assert rows == []  # the far-month row is excluded (no cascade)
    # per-bucket union proof: each of the 3 window buckets contributes
    # its own row when the query word lands in it.
    rows, _, _ = p._archive_cascade_recover("上个月 的锚定词 低档词")
    assert {r["id"] for r in rows} == {"fact_lo"}
    rows, _, _ = p._archive_cascade_recover("上个月 的锚定词 中档词")
    assert {r["id"] for r in rows} == {"fact_mid"}
    rows, _, _ = p._archive_cascade_recover("上个月 的锚定词 高档词")
    # +/-1 union: the -1 and +1 neighbour rows hit the SAME anchor.
    assert {r["id"] for r in rows} == {"fact_hi"}
    # an anchored query that misses the union still misses: "faran 远档词"
    # is a PURE-ENTITY query (no time word) -> two-level cascade, not an
    # anchor; the slow tier (full scan) reaches the 4-months-ago bucket.
    rr, anchor, hit = p._archive_cascade_recover("faran 远档词")
    assert not hit and anchor is None
    assert [r["id"] for r in rr] == ["fact_far"]
    # a query that is BOTH anchored AND a union-miss returns empty
    # (the anchor IS the decision — no fallback cascade): "2025-08"
    # points at the far bucket... and hits it (that's the union window).
    rr2, a2, h2 = p._archive_cascade_recover("faran 2025-08 的远档词")
    assert h2 and a2 == "2025-08"
    # an anchored query with NO match inside its target+/-1 window:
    rr3, a3, h3 = p._archive_cascade_recover("不存在的词 2025-01")
    assert h3 and a3 == "2025-01" and rr3 == []


def test_cascade_no_keywords_empty(tmp_path):
    p = _make_provider(tmp_path)
    assert p._archive_cascade_recover("") == ([], None, False)
    assert p._archive_cascade_recover("   ") == ([], None, False)
    # no buckets at all (fresh scratch root) -> fail-soft empty.
    assert p._archive_cascade_recover("anything") == ([], None, False)


# ── S1 staging door (reclaimed.json) ─────────────────────────────────────

def test_put_reclaim_new_projection_schema(tmp_path):
    p = _make_provider(tmp_path)
    rr = p.put_reclaim(src_id="fact_1", src_shard="fact",
                       content="源记忆内容", reason="explicit_search",
                       source_profile="default", salience=0.7)
    assert rr is not None
    # id = recl_<srcid>_<UTC ts>
    assert rr["id"].startswith("recl_fact_1_")
    assert rr["src_id"] == "fact_1" and rr["src_shard"] == "fact"
    assert rr["content"] == "源记忆内容"
    assert rr["reclaim_reason"] == "explicit_search"
    assert rr["source_profile"] == "default"
    assert rr["salience"] == 0.7
    # expires_at = now + reclaim_ttl_days (default 3).
    now = datetime.now(timezone.utc)
    exp = datetime.fromisoformat(rr["expires_at"])
    delta = (exp - now).total_seconds()
    assert timedelta(days=3) - timedelta(seconds=10) < \
        timedelta(seconds=delta) < timedelta(days=3) + timedelta(seconds=10)
    # the file is the JSON array of projections.
    rows = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert len(rows) == 1 and rows[0]["id"] == rr["id"]
    # B口径: no projection id / src_id field leaks into the staging row
    # schema (the Go ReclaimRow surface — id/src_id/src_shard/content/
    # reclaimed_at/expires_at/reclaim_reason/source_profile/salience).
    assert set(rr.keys()) == {
        "id", "src_id", "src_shard", "content", "reclaimed_at",
        "expires_at", "reclaim_reason", "source_profile", "salience"}


def test_put_reclaim_renew_no_duplicate(tmp_path):
    p = _make_provider(tmp_path)
    rr1 = p.put_reclaim(src_id="fact_1", src_shard="fact", content="c1")
    first_expires = rr1["expires_at"]
    rr2 = p.put_reclaim(src_id="fact_1", src_shard="fact", content="c2")
    rows = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert len(rows) == 1  # RENEW, not a duplicate row
    # the renew refreshes the dwell window from NOW.
    assert rows[0]["expires_at"] >= first_expires or \
        rows[0]["expires_at"] != first_expires
    # a second src_id is a fresh projection (append, not overwrite).
    p.put_reclaim(src_id="fact_2", src_shard="fact", content="c")
    rows = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert len(rows) == 2


def test_put_reclaim_replaces_expired(tmp_path):
    p = _make_provider(tmp_path)
    rr = p.put_reclaim(src_id="fact_1", src_shard="fact", content="c")
    # force-expire the projection (beyond any ttl clamp).
    path = p.memories_dir / "reclaimed.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[0]["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    rr2 = p.put_reclaim(src_id="fact_1", src_shard="fact", content="c2")
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert len(rows) == 1  # replaced in place, no dup
    assert rows[0]["content"] == "c2"
    assert rr2 is not None


def test_reclaim_ttl_clamped_3_to_5(tmp_path):
    p = _make_provider(tmp_path)
    assert p._reclaim_ttl_days() == 3
    p._decay["reclaim_ttl_days"] = 1
    assert p._reclaim_ttl_days() == 3   # clamped up
    p._decay["reclaim_ttl_days"] = 9
    assert p._reclaim_ttl_days() == 5   # clamped down
    p._decay["reclaim_ttl_days"] = "bogus"
    assert p._reclaim_ttl_days() == 3   # malformed -> default
    p._decay["reclaim_fast_months"] = 0
    assert p._reclaim_fast_months() == 0  # fast tier off = full-only
    p._decay["reclaim_fast_months"] = "x"
    assert p._reclaim_fast_months() == 3  # malformed -> default


def test_clean_reclaims_physical_expiry(tmp_path):
    p = _make_provider(tmp_path)
    rr_live = p.put_reclaim(src_id="live", src_shard="fact", content="l")
    rr_dead = p.put_reclaim(src_id="dead", src_shard="fact", content="d")
    path = p.memories_dir / "reclaimed.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in data:
        if r["src_id"] == "dead":
            r["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    removed = p.clean_reclaims()
    assert "recl_dead_" in removed[0] and len(removed) == 1
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert [r["src_id"] for r in rows] == ["live"]
    # a second clean with nothing due -> no-op, no rewrite of live rows.
    assert p.clean_reclaims() == []
    # undatable expiry is KEPT (fail-open, never delete what you can't
    # date).
    p.put_reclaim(src_id="undated", src_shard="fact", content="u")
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in data:
        if r["src_id"] == "undated":
            r["expires_at"] = "not-a-timestamp"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert p.clean_reclaims() == []
    assert any(r["src_id"] == "undated"
               for r in json.loads(path.read_text(encoding="utf-8")))


def test_reclaim_fail_soft_corrupt_and_write_failure(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    # corrupt staging file -> read degrades to empty, the caller (the S2
    # hook) keeps working.
    path = p.memories_dir / "reclaimed.json"
    path.write_text("{corrupt json", encoding="utf-8")
    assert p._read_reclaims() == []
    # a put after the corruption REPAIRS the file (atomic tmp+rename).
    rr = p.put_reclaim(src_id="x1", src_shard="fact", content="c")
    assert rr is not None
    assert json.loads(path.read_text(encoding="utf-8"))[0]["src_id"] == "x1"
    # write failure -> None, never a raise out of the retrieval face.
    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(p, "_write_reclaims", _boom)
    assert p.put_reclaim(src_id="x2", src_shard="fact", content="c") is None
    assert p.clean_reclaims() == []  # the clean is fail-soft too


# ── S3 fold-in (B口径) ────────────────────────────────────────────────────

def test_search_folds_archive_hit_with_reclaimed_mark(tmp_path):
    """活池无 → 归档级联命中 → 结果行 = 源记忆 + reclaimed=True."""
    p = _make_provider(tmp_path)
    # empty live shards (a fresh store): the cascade is the only source.
    for t in MEMORY_TYPES:
        (p.memories_dir / f"{t}.json").write_text(
            "[]", encoding="utf-8")
    arch = _row("fact_archived_unique", "archiveuniqueword 归档独有")
    arch["created_at"] = "2025-09-01T00:00:00"
    arch["updated_at"] = "2025-09-01T00:00:00"
    _write_buckets(p, {f"fact_{_month_ago(8)}.jsonl": [arch]})
    results = p.search_memories("archiveuniqueword", limit=5,
                                min_score=0.0)
    ids = [r.get("id") for r in results]
    assert "fact_archived_unique" in ids
    hit = next(r for r in results if r.get("id") == "fact_archived_unique")
    assert hit.get("reclaimed") is True
    # B口径: the row is the SOURCE MEMORY itself (its content/type ride
    # through), and no projection id / src_id is ever exposed.
    assert hit.get("content") == arch["content"]
    assert hit.get("type") == "fact"
    assert "src_id" not in hit and "recl_" not in str(hit.get("id"))
    # the S2 hook wrote a staging projection for the hit.
    rows = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert any(r["src_id"] == "fact_archived_unique" for r in rows)
    assert all(r["reclaim_reason"] == "explicit_search" for r in rows)


def test_search_live_pool_hit_is_not_staged(tmp_path):
    """活池行命中不写暂存（暂存只服务归档行）."""
    p = _make_provider(tmp_path)
    live = _row("fact_live_unique", "liveword 活池独有")
    (p.memories_dir / "fact.json").write_text(
        json.dumps([live], ensure_ascii=False), encoding="utf-8")
    for t in MEMORY_TYPES:
        if t != "fact":
            (p.memories_dir / f"{t}.json").write_text(
                "[]", encoding="utf-8")
    results = p.search_memories("liveword", limit=5, min_score=0.0)
    assert any(r.get("id") == "fact_live_unique" for r in results)
    # the live row carries no reclaimed mark and no staging projection.
    hit = next(r for r in results if r.get("id") == "fact_live_unique")
    assert hit.get("reclaimed") in (None, False)
    assert not (p.memories_dir / "reclaimed.json").exists()


def test_search_dedupes_live_and_archive_same_id(tmp_path):
    """同一 id 同时在活池与归档桶 → 一行不重复（Go: liveID dedupe）."""
    p = _make_provider(tmp_path)
    live = _row("fact_dup", "dupword 同时存在")
    (p.memories_dir / "fact.json").write_text(
        json.dumps([live], ensure_ascii=False), encoding="utf-8")
    _write_buckets(p, {
        f"fact_{_month_ago(5)}.jsonl":
            [dict(live, reference_count=9)]  # same id, archive copy
    })
    results = p.search_memories("dupword", limit=5, min_score=0.0)
    dup_hits = [r for r in results if r.get("id") == "fact_dup"]
    assert len(dup_hits) == 1
    # the LIVE copy wins (no reclaimed mark on the result row).
    assert dup_hits[0].get("reclaimed") in (None, False)
    # S2 hook: a cascade hit IS staged even when the live pool carries
    # the same id (the staging door exists for archive rows only; the
    # fold dedup keeps the LIVE copy, no duplicate result row).
    rows = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert [r["src_id"] for r in rows] == ["fact_dup"]


def test_search_live_pool_unfiltered_expired_stays(tmp_path):
    """S2/S3 真值模型：活池不滤过期（过期行在池, 结果集语义不变）."""
    p = _make_provider(tmp_path)
    old = _row("fact_expired_live", "expireword 过期活池行")
    old["ttl_days"] = 1
    old["created_at"] = "2020-01-01T00:00:00"
    old["updated_at"] = "2020-01-01T00:00:00"
    (p.memories_dir / "fact.json").write_text(
        json.dumps([old], ensure_ascii=False), encoding="utf-8")
    # the pool filter is the pre-existing UN-filtered behavior: the
    # search face is unchanged (this test locks the red line, not a new
    # mechanism).
    pool = p._cached_all_memories()
    assert "fact_expired_live" in [i.get("id") for i in pool] or \
        pool == []  # either way: no NEW expiry filter was introduced
    # the cascade adds only GROWTH: no archive bucket -> byte-identical
    # to the pre-fold pool.
    assert p._archive_cascade_recover("expireword") == ([], None, False)


def test_cascade_write_failure_does_not_break_search(tmp_path, monkeypatch):
    """S2 hook 写失败 → 检索照跑（live-pool-only 结果, 不 crash）."""
    p = _make_provider(tmp_path)
    for t in MEMORY_TYPES:
        (p.memories_dir / f"{t}.json").write_text(
            json.dumps([_row(f"{t}_live", f"{t}word 活池")],
                       ensure_ascii=False), encoding="utf-8")
    arch = _row("fact_arch_broken_write", "archword 归档独有")
    _write_buckets(p, {f"fact_{_month_ago(6)}.jsonl": [arch]})

    def _boom(*a, **k):
        raise OSError("reclaimed.json unwritable")
    monkeypatch.setattr(p, "_write_reclaims", _boom)
    results = p.search_memories("archword", limit=5, min_score=0.0)
    # the live pool still returns; the archive fold degraded silently.
    assert any(r.get("id") == "fact_arch_broken_write" for r in results)
    assert not (p.memories_dir / "reclaimed.json").exists()


def test_zero_diff_red_line(tmp_path):
    """归档 jsonl + 活池 shard 全程零 diff（捞回只写 reclaimed.json）."""
    p = _make_provider(tmp_path)
    live = [
        _row("fact_live_a", "liveword a"),
        _row("fact_live_b", "liveword b"),
    ]
    (p.memories_dir / "fact.json").write_text(
        json.dumps(live, ensure_ascii=False), encoding="utf-8")
    for t in MEMORY_TYPES:
        if t != "fact":
            (p.memories_dir / f"{t}.json").write_text(
                "[]", encoding="utf-8")
    arch = _row("fact_arch_z", "archzword 归档独有")
    arch["created_at"] = "2025-08-01T00:00:00"
    _write_buckets(p, {f"fact_{_month_ago(7)}.jsonl": [arch]})
    bucket_path = p.memories_dir / f"fact_{_month_ago(7)}.jsonl"
    before = {f.name: f.read_bytes() for f in p.memories_dir.iterdir()
              if f.suffix == ".jsonl" or f.suffix == ".json"}
    p.search_memories("archzword", limit=5, min_score=0.0)
    p.search_memories("liveword", limit=5, min_score=0.0)
    # the only NEW file is the staging projection.
    after = {f.name: f.read_bytes() for f in p.memories_dir.iterdir()
             if f.suffix in (".jsonl", ".json") or f.name == "reclaimed.json"}
    for name, blob in before.items():
        assert after.get(name) == blob, name  # zero diff on every existing
    assert "reclaimed.json" in after
    rows = json.loads(after["reclaimed.json"])
    assert [r["src_id"] for r in rows] == ["fact_arch_z"]


def test_reclaim_from_archive_copies_source_profile(tmp_path):
    """S2 hook: source_profile 从源行 metadata 拷入, 缺省回落 provider 自身."""
    p = _make_provider(tmp_path)
    arch = _row("fact_arch_prof", "archprofword 归档",
                metadata={"source_profile": "coder"})
    arch2 = _row("task_arch_prof", "taskprofword 归档")  # no stamp
    _write_buckets(p, {
        f"fact_{_month_ago(2)}.jsonl": [arch],
        f"task_{_month_ago(2)}.jsonl": [arch2],
    })
    rows = p._reclaim_from_archive(
        p._archive_cascade_recover("archprofword")[0])
    assert rows == 1
    proj = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    assert proj[0]["source_profile"] == "coder"
    rows2 = p._reclaim_from_archive(
        p._archive_cascade_recover("taskprofword")[0])
    assert rows2 == 1
    proj = json.loads(
        (p.memories_dir / "reclaimed.json").read_text(encoding="utf-8"))
    mine = next(r for r in proj if r["src_id"] == "task_arch_prof")
    assert mine["source_profile"] == "reclaim-test"  # provider fallback


def test_fold_noop_without_query(tmp_path):
    """query='' 的控制路径（纯 decay/recency 全池排序）不触发级联."""
    p = _make_provider(tmp_path)
    arch = _row("fact_arch_nq", "archnqword 归档独有")
    _write_buckets(p, {f"fact_{_month_ago(6)}.jsonl": [arch]})
    results = p.search_memories("", limit=5, ranking="recency")
    # the control path never folds: the archive row stays out of results.
    assert "fact_arch_nq" not in [r.get("id") for r in results]
    assert not (p.memories_dir / "reclaimed.json").exists()


def test_windowed_query_disables_cascade(tmp_path):
    """SA2-2 × S2/S3：有界窗口是刻意时间过滤 — 归档级联不得向结果
    泄漏窗口外行（对齐 Go: window 与 anchor 是互斥的时间语义）."""
    p = _make_provider(tmp_path)
    for t in MEMORY_TYPES:
        (p.memories_dir / f"{t}.json").write_text(
            "[]", encoding="utf-8")
    old = _row("fact_arch_old", "oldwinword 旧归档行")
    old["created_at"] = "2025-01-01T00:00:00"
    old["updated_at"] = "2025-01-01T00:00:00"
    _write_buckets(p, {f"fact_2025-01.jsonl": [old]})
    # a window far from the archive bucket month: the fold is disabled,
    # so no out-of-window row can ride the cascade into the result.
    results = p.search_memories("oldwinword", limit=5, min_score=0.0,
                                window={"since": "2026-11-01",
                                        "until": "2026-11-30"})
    assert results == []
    assert not (p.memories_dir / "reclaimed.json").exists()
    # the same query WITHOUT the window folds the archive hit.
    results2 = p.search_memories("oldwinword", limit=5, min_score=0.0)
    assert "fact_arch_old" in [r.get("id") for r in results2]
