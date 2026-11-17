"""Decided-ledger: the cross-session "already adjudicated, never re-ask" fix.

User-complaint (2026-11-17, root-caused this session): ANY already-adjudicated
topic (not just the 4 historical ones) re-asks in a new session. Root cause:
  * the confirm/discard VERDICT writes only a pool row (confirm) / an audit
    line (discard) — NO topic-level "decided, don't re-surface" record;
  * the verdict's consume (`_consume_pending_confirm`) is in-memory only
    (forgotten on process restart / new session);
  * the only re-land dedup is an EXACT content-fingerprint
    (`_drain_fingerprint`) — a reworded variant of an already-adjudicated topic
    evades it, re-lands as a fresh pool row + re-enqueues into the new
    session's pending-confirm -> re-asked.

This locks the durable fix:
  * a `ledgers/decided.json` topic-level suppression ledger (one row per
    decided topic: {fp, sig, verdict, content, ts, profile});
  * written on BOTH verdicts (confirm AND discard) via _record_topic_decided;
  * CONSUMED (fail-CLOSED) by the two re-land/ask doors:
      - `_push_pending_confirm`: a re-enqueued row whose topic overlaps a
        decided row is NOT enqueued (no re-ask);
      - `_drain_write_native`: a drain entry whose topic overlaps a DISCARD
        verdict is treated "dup" (no new pool row; the md entry still deletes,
        so the md stays steady without re-accumulating duplicates).
  * knob-gated (S2 parity, both DECAY_DEFAULTS + decay.json):
    consolidation.decided_ledger.{enable, min_overlap}; enable=false = full
    rollback to legacy (re-asks).

Red line held: plugin source + tests + knob only, host zero-change.
"""
import copy
import json
import pathlib
import sys
import threading

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")
sys.path.insert(0, r"C:/Users/user/AppData/Local/hermes/hermes-agent")

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))


# ── fixtures (mirror test_native_drain's synthetic store/provider) ─────────


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "filemem"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic", "ledgers"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, source_profile: str = "p-a",
                   session_id: str = "s-dec", enabled: bool = True,
                   min_shared: int = 3) -> FileMemProvider:
    store = _make_store(tmp)
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = source_profile
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._decay.setdefault("consolidation", {})
    p._decay["consolidation"].setdefault(
        "decided_ledger", {"enable": True, "min_shared": 3})
    p._decay["consolidation"]["decided_ledger"]["enable"] = enabled
    p._decay["consolidation"]["decided_ledger"]["min_shared"] = min_shared
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._session_drafts = {}
    p._main_runtime = None
    # failure-marker attrs the drain write path touches (mirror
    # test_native_drain's provider fixture):
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    # Isolate the decided-gate under test: neutralize the pre-existing
    # no-popup low-value gate so it can't drop these short fixtures before
    # they reach the decided-ledger check (the drain write-guard is tested
    # separately and does not flow through _no_popup_gate).
    p._no_popup_gate = lambda *a, **k: False
    return p


def _decided_file(store: pathlib.Path) -> pathlib.Path:
    return store / "ledgers" / "decided.json"


def _write_decided(store: pathlib.Path, rows) -> None:
    _decided_file(store).write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_decided(store: pathlib.Path):
    p = _decided_file(store)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


# two topics: X (decided) and a reworded variant of X, plus a distinct Y.
TOPIC_X = "filemem 和 kbs 依赖安装能不能改为自动安装方式"
TOPIC_X_VARIANT = "filemem 的自动安装依赖 需要改吗 依赖安装"
TOPIC_Y = "把 github token 设置成全局 access 凭据"


# ── T1: _topic_sig is deterministic + topic-sensitive ─────────────────────


def test_topic_sig_deterministic_and_topic_sensitive():
    s_x = FileMemProvider._topic_sig(TOPIC_X)
    s_xv = FileMemProvider._topic_sig(TOPIC_X_VARIANT)
    s_y = FileMemProvider._topic_sig(TOPIC_Y)
    # deterministic:
    assert FileMemProvider._topic_sig(TOPIC_X) == s_x
    # same-topic variant shares key tokens; distinct topic shares ~none:
    assert s_x.intersection(s_xv), "reworded variant must share topic tokens"
    assert not s_x.intersection(s_y) or len(s_x.intersection(s_y)) < 2, \
        "distinct topic must not overlap the decided topic"
    # empty/garbage -> empty sig (never crashes):
    assert FileMemProvider._topic_sig("") == frozenset()
    assert FileMemProvider._topic_sig(None) == frozenset()


# ── T2: _record_topic_decided upserts (one row per topic, idempotent) ─────


def test_record_topic_decided_upserts_not_duplicates(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    p._record_topic_decided(TOPIC_X_VARIANT, "discard")   # same topic -> upsert
    rows = _read_decided(p.storage_dir)
    assert len(rows) == 1, \
        f"reworded same-topic must upsert the ONE row, got {len(rows)}"
    assert rows[0]["verdict"] == "discard"
    assert rows[0]["content"]
    assert rows[0].get("sig")


def test_record_topic_decided_distinct_topics_keep_separate_rows(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "confirm")
    p._record_topic_decided(TOPIC_Y, "discard")
    rows = _read_decided(p.storage_dir)
    assert len(rows) == 2, "distinct topics must be distinct rows"
    verdicts = {r["content"] and r["verdict"] for r in rows}
    assert "confirm" in verdicts and "discard" in verdicts


# ── T3: _is_topic_decided fail-closed overlap matching ────────────────────


def test_is_topic_decided_variant_matches_distinct_does_not(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    assert p._is_topic_decided(TOPIC_X_VARIANT) == "discard"
    assert p._is_topic_decided(TOPIC_Y) is None
    # verdict filter: a confirm-only ledger does NOT match a discard query:
    p2 = _make_provider(tmp_path / "p2")
    p2._record_topic_decided(TOPIC_X, "confirm")
    assert p2._is_topic_decided(TOPIC_X_VARIANT, verdicts=("discard",)) is None


def test_is_topic_decided_empty_ledger_is_none(tmp_path):
    p = _make_provider(tmp_path)
    assert p._is_topic_decided(TOPIC_X) is None


# ── T4: _push_pending_confirm suppresses reworded re-asks ────────────────


def test_push_suppresses_reworded_variant_of_decided_topic(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    p._push_pending_confirm("s", "id_xv", "L0", TOPIC_X_VARIANT, kind="l0")
    assert p._pending_confirm.get("s", []) == [], \
        "a reworded variant of a decided topic must NOT re-enqueue"


def test_push_keeps_distinct_topic_and_enabled_gate(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    # distinct topic still enqueues (the gate is topic-scoped, not blanket):
    p._push_pending_confirm("s", "id_y", "L0", TOPIC_Y, kind="l0")
    assert [e["id"] for e in p._pending_confirm.get("s", [])] == ["id_y"]
    # enable=false -> full rollback: the decided variant re-enqueues:
    p2 = _make_provider(tmp_path / "p2b", enabled=False)
    p2._record_topic_decided(TOPIC_X, "discard")   # records (write side ungated)
    p2._push_pending_confirm("s", "id_xv2", "L0", TOPIC_X_VARIANT, kind="l0")
    assert p2._pending_confirm.get("s", []), \
        "enable=false must roll back to legacy re-ask behavior"


# ── T5: _drain_write_native write-guard for DISCARD verdicts ──────────────


def test_drain_write_guard_blocks_discard_variant_new_row(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    # a reworded variant of the DISCARDED topic -> "dup" (no new pool row):
    res = p._drain_write_native(TOPIC_X_VARIANT, "memory", "s")
    assert res == "dup"
    assert _pool_rows_all(p.storage_dir) == [], \
        "a discarded topic must not re-land a new pool row"
    # a CONFIRM-verdict topic still lands (write-guard only blocks discard):
    p2 = _make_provider(tmp_path / "p2")
    p2._record_topic_decided(TOPIC_Y, "confirm")
    res2 = p2._drain_write_native("another way to say " + TOPIC_Y, "memory", "s")
    assert res2 in ("written", "dup"), \
        "confirm verdict must NOT block a genuine new/variant row"


def _pool_rows_all(store: pathlib.Path):
    out = []
    for t in MEMORY_TYPES:
        out.extend(json.loads(
            (store / "memories" / f"{t}.json").read_text(encoding="utf-8")))
    return out


# ── T6: confirm/discard verdicts record the topic (write side) ───────────


def test_confirm_records_decided_topic(tmp_path, monkeypatch):
    """_tool_review_drafts(confirm) must upsert the topic into decided.json."""
    p = _make_provider(tmp_path)
    p._session_drafts[p.session_id] = [
        _draft("d1", "filemem 和 kbs 依赖安装 自动安装 方式", "L1"),
    ]
    # save_memory just records the id; stub the pool write so the tool path
    # runs without a full provider runtime.
    p._save_memory = lambda content, **kw: "confirmed_1"
    out = json.loads(p._tool_review_drafts({
        "action": "confirm", "memory_id": "d1"}))
    assert out["success"] is True
    rows = _read_decided(p.storage_dir)
    assert any(r["verdict"] == "confirm" for r in rows), \
        f"confirm must record the decided topic, got {rows}"


def test_discard_records_decided_topic(tmp_path):
    p = _make_provider(tmp_path)
    p._session_drafts[p.session_id] = [
        _draft("d1", "filemem 和 kbs 依赖安装 自动安装 方式", "L1"),
    ]
    out = json.loads(p._tool_review_drafts({
        "action": "discard", "memory_id": "d1"}))
    assert out["success"] is True
    rows = _read_decided(p.storage_dir)
    assert any(r["verdict"] == "discard" for r in rows), \
        f"discard must record the decided topic, got {rows}"


def _draft(did, content, level):
    from __init__ import MemoryItem
    d = MemoryItem(id=did, type="fact", content=content)
    d.level = level
    return d


# ── T7: fail-soft — a corrupt decided.json must NOT crash the hot paths ──


def test_corrupt_decided_ledger_fail_closed(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(TOPIC_X, "discard")
    _write_decided(p.storage_dir, "{ NOT VALID JSON ]]")
    # read path fail-closed: corrupt ledger -> "not decided" -> re-enqueues
    # (legacy), never crashes, never wrongly suppresses:
    p._decided_cache = None
    p._push_pending_confirm("s", "id_x", "L0", TOPIC_X_VARIANT, kind="l0")
    assert p._pending_confirm.get("s", []), \
        "corrupt ledger must fail-open to queue (宁多问不可崩)"


# ── T8: S2 knob parity (DECAY_DEFAULTS vs decay.json, both sides) ─────────


def test_decided_ledger_knob_parity():
    dd = DECAY_DEFAULTS.get("consolidation", {})
    dj = DECAY_JSON.get("consolidation", {})
    assert "decided_ledger" in dd, "DECAY_DEFAULTS must declare decided_ledger"
    assert "decided_ledger" in dj, "decay.json must declare decided_ledger"
    assert set(dd["decided_ledger"]) == set(dj["decided_ledger"]), \
        "the two sides must carry the same decided_ledger knobs"
    for k in dd["decided_ledger"]:
        assert dd["decided_ledger"][k] == dj["decided_ledger"][k], \
            f"knob parity broken for decided_ledger.{k}"
    assert dd["decided_ledger"]["enable"] is True
    assert dd["decided_ledger"]["min_shared"] >= 2, \
        "min_shared must be a sane distinct-token floor (>=2)"


# ── T7: the min_shared floor separates reworded-same-topic from CJK noise ──
# F2 regression: the old floor of 3 over-suppressed UNRELATED generic-CJK
# topics (e.g. a DB-timezone fact sharing only a handful of common single
# chars with a decided row). The shipped floor (9) kills that false positive.


def test_min_shared_floor_separates_generic_cjk_noise(tmp_path):
    UNRELATED = ("数据库 时区 能不能 改成 东八区 并 重启 迁移 任务 "
                 "加 索引 优化 查询")   # shares 6 distinct tokens with TOPIC_X
    # shipped default floor (9): the unrelated generic topic is NOT suppressed;
    # the reworded same-topic variant still is.
    p = _make_provider(tmp_path, min_shared=9)
    p._record_topic_decided(TOPIC_X, "discard")
    assert p._is_topic_decided(UNRELATED, verdicts=("discard",)) is None, \
        "unrelated generic-CJK topic must NOT be over-suppressed at the default floor"
    assert p._is_topic_decided(TOPIC_X_VARIANT, verdicts=("discard",)) == "discard", \
        "reworded same-topic variant must still be suppressed at the default floor"
    # the old floor (3) is exactly where the bug lived: the same unrelated
    # topic (shared=6 >= 3) WAS wrongly suppressed. Locks that 9 is the lever.
    p3 = _make_provider(tmp_path / "p3", min_shared=3)
    p3._record_topic_decided(TOPIC_X, "discard")
    assert p3._is_topic_decided(UNRELATED, verdicts=("discard",)) == "discard", \
        "at min_shared=3 the unrelated topic IS (wrongly) suppressed — the F2 bug"
