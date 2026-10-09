"""EP-1 story: 谱系字段 supersedes / related_to / origin（确定性候选层，零 LLM）.

Acceptance coverage (spec EP-1, four gates):
1. 谱系字段写入/读取 round-trip 单测；向后兼容（存量条目无字段 = 正常检索）
   — MemoryItem constructor/from_dict round-trip + legacy row compat.
2. 跨进程锁沿用 <path>.lock；确定排序可 diff
   — _stamp_lineage_candidates deterministic ranking (overlap desc, id asc)
   + cap, asserted byte-identical across two calls.
3. 回归门：无谱系数据时检索行为与 EP 前一致
   — search_memories with a NO-superseded corpus returns zero
     lineage_expansion-tagged rows; the 谱系出池 split is a no-op.
4. 零 LLM（确定性规则建候选，确认交 EP-2）
   — the whole story (mapping + save door + search pool) keeps the host
     distill counter at 0; LLM 计数 = 0 asserted explicitly.

Portability: no absolute paths — fixtures live in tempfile trees; the
plugin module is loaded from a parents[1]-relative path (zero hardcoded
/root, /usr/local, C:\\). Fail-soft: the provider is partial (__new__),
the way test_sec_1_1_origin / test_sa2_1_occurred_at do it.
"""
import ast
import json
import re
import sys
import tempfile
import threading
import pathlib
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    MemoryItem,
    lineage_of,
    _stamp_lineage_candidates,
    _content_overlap_score,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402

# Portability audit: no hardcoded absolute paths in this test or the new
# save-door code path's test file (same pattern as SEC-1-1's gate).
_FORBIDDEN_PATH_PATTERNS = [
    re.compile(r'["\'](/root|/usr/local|C:\\\\)'),
    re.compile(r'Path\([\'"]/root'),
]


def _assert_no_hardcoded_paths(rel: str) -> None:
    text = (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
    for pat in _FORBIDDEN_PATH_PATTERNS:
        m = pat.search(text)
        assert not m, f"hardcoded-path pattern {pat.pattern!r} in {rel}: {m.group(0)}"


_NOW = datetime.now()


def _ts(days_ago: int) -> str:
    """Relative fixture timestamp (days ago) — TTL-safe, no hardcoded date."""
    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _make_provider(tmpdir: pathlib.Path) -> FileMemProvider:
    """Partial provider (same shape as test_sec_1_1_origin): no initialize().
    _decay is merged with DECAY_DEFAULTS so the supersede_* search keys
    exist (the live default is load_decay_config's merge result)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._decay = dict(DECAY_DEFAULTS)
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._session_drafts = {}
    # knowledge layer: None -> _read_knowledge_entries fail-soft []
    p.knowledge_entries_path = None
    return p


def _old_row(rid: str, content: str, created_at: str = None) -> dict:
    """A legacy-shaped old row: NO lineage keys at all (the 227-row live
    data shape — occurred_at + metadata.origin present, no 谱系 fields).
    Default timestamp is _ts(3) — 3 days ago, RELATIVE to the run time so
    the fixture can never silently cross the L0 30-day TTL boundary (the
    hardcoded 2026-09-01 default expired on 2026-10-01 and dropped these
    rows out of the search pool, breaking two regression gates)."""
    ts = created_at if created_at is not None else _ts(3)
    return {
        "id": rid,
        "type": "fact",
        "content": content,
        "level": "L0",
        "metadata": {"source_profile": "test", "origin": {"kind": "user", "trust": "medium"}},
        "created_at": ts,
        "updated_at": ts,
        "occurred_at": None,
        "source": "auto",
        "reference_count": 0,
        "salience": 0.5,
        "last_referenced": "",
        "ttl_days": None,
    }


# ── 1. 谱系字段 round-trip + 向后兼容（acceptance #1）──────────────────────

def test_lineage_round_trip():
    """supersedes/related_to write -> to_dict -> from_dict -> equal."""
    item = MemoryItem(
        id="fact_0",
        type="fact",
        content="deploy uses docker on host 117 now",
        supersedes=["fact_9"],
        related_to=["fact_3"],
    )
    d = item.to_dict()
    assert d["metadata"]["supersedes"] == ["fact_9"]
    assert d["metadata"]["related_to"] == ["fact_3"]
    back = MemoryItem.from_dict(d)
    assert back.supersedes == ["fact_9"]
    assert back.related_to == ["fact_3"]
    assert back.metadata["supersedes"] == ["fact_9"]
    # lineage_of read-side view
    lv = lineage_of(d)
    assert lv["supersedes"] == ["fact_9"]
    assert lv["related_to"] == ["fact_3"]
    assert lv["superseded"] is False


def test_lineage_legacy_backward_compat():
    """存量条目无字段 = 正常：legacy row (no supersedes/related_to/superseded
    keys at all) loads cleanly, lineage view = empty, search unaffected."""
    legacy = _old_row("legacy_1", "some completely unrelated legacy note")
    item = MemoryItem.from_dict(legacy)
    assert item.supersedes == []
    assert item.related_to == []
    lv = lineage_of(legacy)
    assert lv["supersedes"] == [] and lv["related_to"] == []
    assert lv["superseded"] is False
    assert lv["supersede_state"] is None


# ── 2. 确定性候选 stamp：排序可 diff + 候选只标记不判定（acceptance #2/#4）──

def _candidate_fixture() -> tuple:
    """3 older same-type rows: high / mid / low overlap vs the new content.
    Overlaps are computed against the new content 'deploy uses docker on
    host 117 now': old_a shares 5/6 tokens (~0.83), old_b 4/8 (~0.44),
    old_c is disjoint (~0.0)."""
    new_row = {
        "id": "fact_new",
        "type": "fact",
        "content": "deploy uses docker on host 117 now",
        "created_at": _ts(0),
        "updated_at": _ts(0),
    }
    rows = [
        _old_row("old_c", "zeta omega completely different topic"),
        _old_row("old_b", "deploy uses docker on host 117 today"),
        _old_row("old_a", "deploy uses docker on host 117"),
    ]
    return new_row, rows


def test_stamp_deterministic_and_capped():
    """Two independent stamp runs diff byte-identical; ranking = overlap
    desc / id asc; capped at supersede_max_candidates (default 2); the
    new row carries candidate links + state, old rows get the
    superseded marker — all deterministic, zero LLM."""
    new_row, rows = _candidate_fixture()
    out_a = _stamp_lineage_candidates(new_row, list(rows), dict(DECAY_DEFAULTS))
    # second run on FRESH copies — diff must be byte-identical (跨进程可 diff)
    new_row2, rows2 = _candidate_fixture()
    out_b = _stamp_lineage_candidates(new_row2, rows2, dict(DECAY_DEFAULTS))
    assert json.dumps(new_row, sort_keys=True) == json.dumps(new_row2, sort_keys=True)
    assert json.dumps(rows, sort_keys=True) == json.dumps(rows2, sort_keys=True)
    assert out_a == out_b
    # cap = 2 (DECAY_DEFAULTS default); old_a (high) + old_b (mid) win
    assert [r["id"] for r in out_a] == ["old_a", "old_b"]
    # new row: candidate links + candidate state (EP-2 confirms later)
    assert new_row["metadata"]["supersedes"] == ["old_a", "old_b"]
    assert new_row["metadata"]["supersede_state"] == "candidate"
    assert "related_to" not in new_row["metadata"]  # not invented
    # old rows: superseded marker + superseded_by back-link (谱系展开)
    by_id = {r["id"]: r for r in rows}
    assert by_id["old_a"]["metadata"]["superseded"] is True
    assert by_id["old_a"]["metadata"]["superseded_by"] == "fact_new"
    assert by_id["old_b"]["metadata"]["superseded"] is True
    assert by_id["old_c"]["metadata"].get("superseded") is None  # under threshold
    # determinism under cap override: min_overlap=0.0 picks all 3, cap still 2
    new_row3, rows3 = _candidate_fixture()
    decay3 = dict(DECAY_DEFAULTS)
    decay3["search"] = dict(DECAY_DEFAULTS["search"],
                            supersede_min_overlap=0.0, supersede_max_candidates=3)
    out3 = _stamp_lineage_candidates(new_row3, rows3, decay3)
    assert [r["id"] for r in out3] == ["old_a", "old_b", "old_c"]


def test_stamp_same_second_not_older():
    """严格更旧 rule: an old row born in the SAME second is NOT a candidate.
    rows[2] is old_a (the high-overlap row) — bump its ts to the new row's."""
    new_row, rows = _candidate_fixture()
    rows[2]["created_at"] = new_row["created_at"]  # same ts as the new row
    out = _stamp_lineage_candidates(new_row, rows, dict(DECAY_DEFAULTS))
    ids = [r["id"] for r in out]
    assert "old_a" not in ids  # same-second row excluded
    assert "old_b" in ids     # strictly older row still in


def test_stamp_fail_soft_corrupt_row():
    """fail-soft: a corrupt old row (missing metadata dict) never raises;
    the save door 不炸宿主 turn."""
    new_row, _ = _candidate_fixture()
    corrupt = {"id": "old_x", "type": "fact",
               "content": "deploy uses docker on host 117",
               "created_at": _ts(60)}  # corrupt rows are skipped before the ts check
    corrupt["metadata"] = "garbage-not-a-dict"
    out = _stamp_lineage_candidates(new_row, [corrupt], dict(DECAY_DEFAULTS))
    assert out == []  # corrupt row skipped, no exception


def test_overlap_score_pure():
    """_content_overlap_score is a pure symmetric deterministic function."""
    assert _content_overlap_score("a b c", "a b c") == 1.0
    assert _content_overlap_score("a b c", "x y z") == 0.0
    assert _content_overlap_score("", "anything") == 0.0
    assert _content_overlap_score("a b", "a b c") == _content_overlap_score("a b c", "a b")
    # CJK goes through the same ngram tokenizer family as the search axis
    assert _content_overlap_score("使用 容器 部署", "容器 部署") > 0.5


# ── 3. 保存门 stamp 集成（save path, deterministic candidates）─────────────

def test_save_door_stamps_candidates(tmp_path=None):
    """_save_memory (single-save door) stamps the new row with candidate
    links and the old row with superseded — deterministic zero-LLM, and
    the host distill counter stays at 0 for the stamping itself."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    calls = {"n": 0}
    p._distill_item = lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1)
    # seed an older row in the fact shard (the save door's dedup/stamp scope)
    (p.memories_dir / "fact.json").write_text(
        json.dumps([_old_row("seed_1", "deploy uses docker on host 117")],
                   ensure_ascii=False), encoding="utf-8")
    p._save_memory("deploy uses docker on host 117 now", level="L0", source="manual")
    assert calls["n"] == 0, f"candidate stamping made LLM calls: {calls}"
    rows = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert len(rows) == 2
    new, old = rows[1], rows[0]
    assert new["metadata"]["supersedes"] == ["seed_1"]
    assert new["metadata"]["supersede_state"] == "candidate"
    assert old["metadata"]["superseded"] is True
    assert old["metadata"]["superseded_by"] == new["id"]
    # I1 无 DELETE: both rows still on disk (谱系留存)


# ── 4. 检索面：superseded 出池 + 回归门（acceptance #3）────────────────────

def test_search_pool_drops_superseded():
    """superseded rows leave the RRF main pool and return ONLY as a
    lineage-expansion backfill (谱系展开, gate-bypassed)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    active = _old_row("active_1", "deploy uses docker on host 117")
    superseded = _old_row("sup_1", "deploy uses docker on host 117 (old variant)")
    superseded["metadata"]["superseded"] = True
    superseded["metadata"]["superseded_by"] = "active_1"
    (p.memories_dir / "fact.json").write_text(
        json.dumps([superseded, active], ensure_ascii=False), encoding="utf-8")
    res = p.search_memories("docker deploy", limit=5)
    ids = [r["id"] for r in res]
    assert "active_1" in ids
    # superseded row only reachable via the expansion backfill, tagged
    sup = [r for r in res if r["id"] == "sup_1"]
    assert len(sup) == 1
    assert sup[0].get("lineage_expansion") is True
    # 出池: the backfill row is NOT in the scored main pool (its score is
    # the flat 0.0 backfill value, never an RRF-normalized one)
    assert sup[0].get("score") == 0.0


def test_search_regression_no_lineage(tmp_path=None):
    """回归门（acceptance #3）：无谱系数据时检索行为与 EP 前一致 —
    a corpus with NO superseded rows yields zero lineage_expansion tags
    and the 出池 split is a no-op (byte-identical main-pool behavior)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    (p.memories_dir / "fact.json").write_text(
        json.dumps([_old_row("a1", "alpha deploy pipeline"),
                    _old_row("a2", "beta deploy pipeline")],
                   ensure_ascii=False), encoding="utf-8")
    res = p.search_memories("deploy pipeline", limit=5)
    assert [r["id"] for r in res] == ["a1", "a2"]  # deterministic order
    assert all(r.get("lineage_expansion") is not True for r in res)
    assert all("supersede_candidates" not in (r.get("metadata") or {}) for r in res)


# ── 5. 零 LLM（acceptance #4：测试断言 LLM 计数=0）────────────────────────

def test_zero_llm_calls(tmp_path=None):
    """The ENTIRE EP-1 story keeps the host LLM counter at 0: candidate
    stamping (pure stdlib), save-door integration, and search-pool 出池
    are all zero-LLM — 确认交 EP-2 (the only decision face)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    calls = {"n": 0}
    p._distill_item = lambda *a, **kw: calls.__setitem__("n", calls["n"] + 1)
    p._llm_ctx = object()  # host ctx present — the story must still be 0

    # ① deterministic stamp function itself
    new_row, rows = _candidate_fixture()
    _stamp_lineage_candidates(new_row, rows, dict(DECAY_DEFAULTS))
    assert calls["n"] == 0, f"① stamp function made LLM calls: {calls}"

    # ② save door (manual source = no distill target; stamping is the only
    # new code on this path)
    (p.memories_dir / "fact.json").write_text(
        json.dumps([_old_row("seed_9", "deploy uses docker on host 117")],
                   ensure_ascii=False), encoding="utf-8")
    p._save_memory("deploy uses docker on host 117 now", level="L0", source="manual")
    assert calls["n"] == 0, f"② save-door stamping made LLM calls: {calls}"

    # ③ search pool 出池 (pure read-side dict split + backfill)
    res = p.search_memories("docker deploy", limit=5)
    assert calls["n"] == 0, f"③ search-pool split made LLM calls: {calls}"
    assert res  # sanity: the corpus is retrievable

    # ④ import audit: the new code paths only touch stdlib + host agent face
    # (llm_caller is never imported by any line in this story — the
    # consolidation.distill face is the ONLY LLM door, and it is budgeted
    # to EP-2, not this one). Audit = real LLM SYMBOL USAGE (call +
    # attribute + import names), NOT docstring prose — zero-LLM means the
    # deterministic layer never touches the host LLM interface.
    _src_path = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"
    src = _src_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    _LLM_SYMBOLS = {"_load_llm_caller", "llm_caller", "distill",
                    "relevance_score"}  # host LLM faces (consolidation door)
    for fn in ("_stamp_lineage_candidates", "_content_overlap_score",
               "lineage_of", "_lineage_token_set"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        used = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                used.add(sub.func.id)
            elif isinstance(sub, ast.Attribute):
                used.add(sub.attr)
            elif isinstance(sub, ast.Name) and sub.id in _LLM_SYMBOLS:
                used.add(sub.id)
        bad = used & _LLM_SYMBOLS
        assert not bad, f"{fn} touched host LLM face(s) {bad} — EP-1 is zero-LLM"


if __name__ == "__main__":
    _assert_no_hardcoded_paths("tests/test_ep_1_lineage.py")
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
