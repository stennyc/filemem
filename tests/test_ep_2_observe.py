"""EP-2 Observation layer — T2 host-model observe face.

Blue-print: specs/EP-2-observation-impl-notes.md §『observe 作业面 (T2)』+
§『测试 T2』 (line-level blueprint). T1 data plane (upsert_observation /
_mark_stale_observations / pool demotion / decay config block) is landed
and green — this file covers ONLY the host-model face:

  1.  observe mock returns obs+supported -> entry lands kind=observation
      (raw_pending untouched — facts stay pending, I1/I2/I3 via T1)
  2.  observe mock returns empty (model failure) -> nothing written, raw
      facts stay in raw_pending, topic re-enters the next tick
  3.  observe raises (llm_caller face) -> fail-soft: tick survives,
      existing observations on disk intact
  4.  supported_by bad reference -> T1 gate rejects + quarantines the
      synthesized text to raw_pending (origin=obs_unresolved_provenance)
      + audit trace row
  5.  incremental: topic with an active NON-stale observation is never
      re-mined (LLM call counter proves zero calls)
  6.  stale topic re-synthesis: rebuild lands stale=False, the stale
      entry flips superseded (I1, no DELETE)
  7.  N-cap knowledge.observe_max_per_tick=3 with 5 fresh topics -> only
      3 written this tick, 2 roll to the next one
  8.  audit rows {action:observe,...} append-only across ticks
  9.  zero-hardcoded-model audit of the observe segment in llm_caller.py
  10. consolidation-boundary mount order rescore->confirm->mine->observe
      + fail-soft isolation of the observe call
  11. _main_runtime None -> skip silently (standalone, zero writes)
  12. in-flight guard skips a second concurrent tick
  13. per-topic fact cap N=8 (observe_max_facts_per_topic default)

Portability: synthetic fixtures (tempfile trees) + a scripted fake
ctx.llm — zero live data, zero real provider/host model. The REAL
consolidation/llm_caller.observe host path runs (unpatched) in tests 1/2/
5/6/7/8; tests 3/4 drive it through the fake LLM layer.
"""
import json
import pathlib
import re
import sys
import tempfile
import threading
import types

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
VALID_ID = "fact_100"


# ── fixtures ──────────────────────────────────────────────────────────────

def _make_provider(tmpdir: pathlib.Path, llm, seed_ids=(VALID_ID,)) -> FileMemProvider:
    """Partial provider (test_ep_2_observation_data shape) + host gate:
    a truthy ``llm`` marker turns the provider's _main_runtime snapshot ON
    (the real host face is scripted per-test via _llm_host_fake.install —
    zero provider/model config, zero hardcoded model names)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for mid in seed_ids:
        mtype = str(mid).split("_")[0]
        rows.append({"id": mid, "type": mtype, "content": f"seed {mid}",
                     "salience": 0.5, "created_at": "2026-09-26T00:00:00",
                     "updated_at": "2026-09-26T00:00:00"})
    if rows:
        by_type = {}
        for r in rows:
            by_type.setdefault(r["type"], []).append(r)
        for mtype, items in by_type.items():
            (p.memories_dir / f"{mtype}.json").write_text(
                json.dumps(items, ensure_ascii=False), encoding="utf-8")
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = None if llm is None else _MAIN_RUNTIME
    return p


def _write_raw_pending(p, rows: list) -> None:
    p.knowledge_raw_pending_path.write_text(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8")


def _raw_row(topic: str, content: str, mids, i: int = 0) -> dict:
    return {"topic": topic, "content": content, "memory_ids": list(mids),
            "origin": "catch_filter", "reason": "synthetic",
            "created_ts": f"2026-10-01T00:00:{i:02d}"}


def _entries(p) -> list:
    f = p.knowledge_entries_path
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def _audit_rows(p) -> list:
    """The append-only AUDIT-CHAIN rows (observe verdict rows) — epic card 3
    adds a SEPARATE `llm_call` telemetry row type to the SAME
    consolidation_log.jsonl; audit-chain assertions filter it out so the
    existing verdict-row schema / counts stay the pre-card-3 surface."""
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("action") != "llm_call"]


class FakeLLM:
    """Gate-on marker (epic card 2): the observe face now rides the host
    call surface with an explicit _main_runtime snapshot; per-test script
    replies are installed via _llm_host_fake.install (topic-substring
    matching, "*" fallback, Exception-valued replies raise)."""

    def __init__(self, replies: dict = None):
        self.replies = dict(replies or {})
        self.calls = 0
        self.prompts = []


def _obs_reply(topic: str, supported_id=VALID_ID) -> dict:
    return {"observation": f"the facts establish topic {topic}",
            "value": "medium",
            "supported": [{"id": supported_id, "quote": f"q for {topic}"}]}


# ── 1. observe mock 返回 → 落盘 kind=observation ────────────────────────

def test_observe_success_lands_kind_observation(tmp_path, monkeypatch):
    llm = FakeLLM({"alpha": _obs_reply("alpha"), "*": _obs_reply("alpha")})
    install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    _write_raw_pending(p, [_raw_row("alpha", "deploy cadence is weekly", [VALID_ID])])
    p._observe_consolidate()  # must not raise
    ents = _entries(p)
    obs = [e for e in ents if e.get("kind") == "observation"]
    assert len(obs) == 1
    e = obs[0]
    assert e["observed_topic"] == "alpha"
    assert e["status"] == "active" and e["stale"] is False
    assert e["value"] == "medium"
    assert e["proof_count"] == 1
    assert e["supported_by"] == [{"id": VALID_ID, "quote": "q for alpha"}]
    assert e["content"] == "the facts establish topic alpha"
    assert e["id"].startswith("k_")
    # raw_pending is READ-ONLY to T2: the source facts are still pending
    rp = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    assert len(rp) == 1 and rp[0]["topic"] == "alpha"
    # audit row
    aud = _audit_rows(p)
    assert len(aud) == 1
    a = aud[0]
    assert a["action"] == "observe" and a["topic"] == "alpha"
    assert a["observation_id"] == e["id"] and a["proof_count"] == 1
    assert a["model_used"] is True


# ── 2. observe mock 返回空 (LLM 失败) → 不落盘, raw 保持 pending, 下 tick 可重试 ──

def test_observe_empty_llm_failure_keeps_raw_pending_and_retries(tmp_path, monkeypatch):
    llm = FakeLLM({"beta": RuntimeError("host model unavailable"),
                   "*": RuntimeError("host model unavailable")})
    fake, state = install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    _write_raw_pending(p, [_raw_row("beta", "something new on beta", [VALID_ID])])
    p._observe_consolidate()
    assert _entries(p) == []                      # nothing written
    rp1 = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    assert len(rp1) == 1 and rp1[0]["topic"] == "beta"   # raw kept
    assert _audit_rows(p) == []                  # no verdict, no audit
    # next tick: the topic re-enters the job set (no active obs) -> retry
    p._observe_consolidate()
    assert state["calls"] == 2 and _entries(p) == []


# ── 3. observe 抛异常 → fail-soft 不炸 tick, 旧 observation 不动 ────────

def test_observe_raise_failsoft_existing_observations_intact(tmp_path, monkeypatch):
    p = _make_provider(tmp_path, FakeLLM({"*": _obs_reply("gamma")}))
    # an existing observation on a DIFFERENT topic, plus a fresh topic
    p.upsert_observation(topic="gamma", content="existing gamma synthesis",
                         supported_by=[{"id": VALID_ID, "quote": "q"}])
    _write_raw_pending(p, [_raw_row("gamma", "more gamma facts", [VALID_ID]),
                           _raw_row("delta", "delta fact", [VALID_ID])])
    before = p.knowledge_entries_path.read_bytes()

    class BoomModule:
        @staticmethod
        def observe(ctx, topic, facts):
            raise RuntimeError("llm caller exploded mid-topic")

    monkeypatch.setattr(fm, "_load_llm_caller", lambda: BoomModule())
    p._observe_consolidate()  # per-topic catch -> batch survives, no raise
    assert p.knowledge_entries_path.read_bytes() == before  # existing obs intact
    assert _audit_rows(p) == []
    # raw pending untouched too (fail-soft: nothing consumed)
    rp = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    assert {r["topic"] for r in rp} == {"gamma", "delta"}


# ── 4. supported_by 坏引用 → 拒写 + 降级 raw_pending (T1 gate) ──────────

def test_bad_supported_reference_rejected_and_quarantined(tmp_path, monkeypatch):
    llm = FakeLLM({"*": {"observation": "synthesis over a ghost id",
                         "value": "high",
                         "supported": [{"id": "decision_404_99999", "quote": "q"}]}})
    install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    _write_raw_pending(p, [_raw_row("epsilon", "fact backing epsilon", [VALID_ID])])
    p._observe_consolidate()
    assert _entries(p) == []                     # 整条拒写
    rp = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    # original row (1) + the synthesized text quarantined by the T1 I3 gate
    assert len(rp) == 2
    q = [r for r in rp if r.get("origin") == "obs_unresolved_provenance"]
    assert len(q) == 1
    assert q[0]["content"] == "synthesis over a ghost id"
    assert q[0]["topic"] == "epsilon"
    aud = _audit_rows(p)
    assert len(aud) == 1 and aud[0]["action"] == "observe"
    assert aud[0]["observation_id"] is None
    assert aud[0]["result"] == "rejected_bad_reference"


# ── 5. 增量: 已有 active 非 stale observation 的 topic 不重挖 ────────────

def test_incremental_skips_topic_with_fresh_active_observation(tmp_path, monkeypatch):
    llm = FakeLLM({"zeta": _obs_reply("zeta"), "*": _obs_reply("zeta")})
    fake, state = install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    p.upsert_observation(topic="zeta", content="already synthesized zeta",
                         supported_by=[{"id": VALID_ID, "quote": "q"}])
    _write_raw_pending(p, [_raw_row("zeta", "new zeta fact", [VALID_ID]),
                           _raw_row("eta", "eta fact", [VALID_ID])])
    p._observe_consolidate()
    assert state["calls"] == 1                    # only eta mined
    prompt = state["prompts"][0]
    assert "zeta" not in prompt and "eta" in prompt
    ents = {e["content"]: e for e in _entries(p)}
    assert "already synthesized zeta" in ents     # untouched
    assert ents["already synthesized zeta"]["status"] == "active"
    # second tick: zeta still fresh-active -> still skipped; eta now has an
    # active obs -> also skipped -> zero calls (incremental steady state)
    p._observing_in_flight = False
    p._observe_consolidate()
    assert state["calls"] == 1


# ── 6. stale topic 重综合: 重建 stale=False, 旧条 superseded ─────────────

def test_stale_topic_rebuild_supersedes_old_entry(tmp_path, monkeypatch):
    llm = FakeLLM({"theta": _obs_reply("theta"), "*": _obs_reply("theta")})
    install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    p.upsert_observation(topic="theta", content="theta deploy cadence is weekly on friday",
                         supported_by=[{"id": VALID_ID, "quote": "q"}])
    # deterministic T1 marking: an overlapping new fact flips the active obs
    # stale (Jaccard(theta deploy cadence is weekly on friday,
    #  theta deploy cadence is monthly on friday) = 6/8 = 0.75 >= 0.4)
    marked = p._mark_stale_observations("theta", "theta deploy cadence is monthly on friday")
    assert marked == 1
    old = _entries(p)[0]
    assert old["stale"] is True and old["status"] == "active"
    _write_raw_pending(p, [_raw_row("theta", "theta deploy cadence is monthly on friday", [VALID_ID])])
    p._observe_consolidate()
    ents = _entries(p)
    obs = {e["content"]: e for e in ents}
    new_e = obs["the facts establish topic theta"]
    old_e = obs["theta deploy cadence is weekly on friday"]
    assert new_e["stale"] is False and new_e["status"] == "active"
    assert old_e["status"] == "superseded"        # I1: no DELETE
    assert old_e["superseded_by"] == new_e["id"]
    # stale cleared -> search pool: new obs 0.9, old gone from pool
    pool = {e["id"]: e for e in p._load_knowledge_pool()}
    assert new_e["id"] in pool and old_e["id"] not in pool
    assert pool[new_e["id"]]["salience"] == 0.9


# ── 7. N-cap observe_max_per_tick 生效 (超的滚下 tick) ───────────────────

def _make_dynamic_replies():
    """Per-topic canned reply as a dynamic host-fake payload (epic card 2):
    the callable receives the prompt content and returns that topic's
    observation (so I2 idempotency never merges distinct topics)."""
    def dyn(content):
        m = re.search(r"Topic: (\S+)", content or "")
        topic = m.group(1) if m else "dyn"
        return _obs_reply(topic)
    return dyn


def test_n_cap_overflow_rolls_to_next_tick(tmp_path, monkeypatch):
    fake, state = install(monkeypatch,
                          replies={"*": _make_dynamic_replies()})
    p = _make_provider(tmp_path, FakeLLM())
    p._decay["knowledge"]["observe_max_per_tick"] = 3
    topics = [f"topic_{i}" for i in range(5)]
    _write_raw_pending(p, [_raw_row(t, f"fact about {t}", [VALID_ID]) for t in topics])
    p._observe_consolidate()
    assert state["calls"] == 3                    # N-cap = 3 topics this tick
    # deterministic: the 3 written are the topic-ascending first three
    written = {e["observed_topic"] for e in _entries(p)}
    assert written == set(topics[:3])
    p._observing_in_flight = False
    p._observe_consolidate()                     # overflow rolls next tick
    assert state["calls"] == 5
    written = {e["observed_topic"] for e in _entries(p)}
    assert written == set(topics)


# ── 8. 审计行 {action:observe} append-only ────────────────────────────────

def test_audit_rows_append_only_across_ticks(tmp_path, monkeypatch):
    llm = FakeLLM({"i1": _obs_reply("i1"), "i2": _obs_reply("i2"),
                   "*": _obs_reply("i1")})
    install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm)
    _write_raw_pending(p, [_raw_row("i1", "fact i1", [VALID_ID])])
    p._observe_consolidate()
    first = _audit_rows(p)
    assert len(first) == 1 and first[0]["action"] == "observe"
    first_bytes = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    # next tick: a second topic; the FIRST row must survive verbatim
    _write_raw_pending(p, [_raw_row("i1", "fact i1", [VALID_ID]),
                           _raw_row("i2", "fact i2", [VALID_ID])])
    p._observe_consolidate()
    after = _audit_rows(p)
    assert len(after) == 2
    assert all(a["action"] == "observe" for a in after)
    assert {a["topic"] for a in after} == {"i1", "i2"}
    text = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    assert text.startswith(first_bytes)           # append-only, no rewrite


# ── 9. 零硬编码模型审计 (observe 段) ─────────────────────────────────────

def test_no_hardcoded_model_or_provider_in_observe_segment():
    text = (PLUGIN_DIR / "consolidation" / "llm_caller.py").read_text(encoding="utf-8")
    seg = text.split("EP-2 T2: observation synthesis", 1)[1]
    # 蓝图验收 grep: model 赋值 / 第三方 SDK / task 键 全部 = 0
    assert not re.search(r'model\s*[:=]\s*["a-z]|openai|anthropic|httpx|requests', seg), \
        "hardcoded model/provider/SDK leaked into the EP-2 observe segment"
    # 宿主主模型面: 唯一入口 host call surface (_structured_call -> _HOST_CALL_LLM,
    # epic card 2), 调用不带 task= 键 (显式 main_runtime, 宿主主模型落点)
    assert "_structured_call(" in seg
    assert "task=" not in seg
    # 段内无新增 import 语句 (stdlib + host call surface only)
    assert not re.search(r"^\s*import |^\s*from ", seg, re.M)


# ── 10. 巩固边界挂载顺序 rescore->confirm->mine->observe + fail-soft 隔离 ──

def test_consolidation_boundary_mount_order_and_failsoft_isolation():
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    block = src[src.index("def _spawn_llm_consolidation("):
                src.index("def _write_episodic_entry(")]
    i_rescore = block.index("self._llm_consolidate()")
    i_confirm = block.index("self._supersede_confirm_candidates()")
    i_mine = block.index("self._mine_alias_candidates()")
    i_observe = block.index("self._observe_consolidate()")
    assert i_rescore < i_confirm < i_mine < i_observe   # 行级顺序
    # 挂载是独立 try/except (fail-soft 隔离): 一个 face 异常不炸后续
    observe_call_site = block[i_observe - 60:i_observe + 120]
    assert "try:" in observe_call_site
    assert "except Exception" in block[i_observe:i_observe + 400]
    assert "logger.warning" in block[i_observe:i_observe + 400]


def test_boundary_failsoft_simulated_does_not_break_host_path(tmp_path, monkeypatch):
    """Boundary isolation (rescore->confirm->mine->observe, one try/except
    per face in _run): simulate the containment — every face raising must be
    swallowed by its own except and never break the others' host path. Then
    verify _observe_consolidate ITSELF fail-soft-swallows a broken
    llm_caller module (whole-tick posture)."""
    def _boom():
        raise RuntimeError("face exploded (boundary's except swallows this)")

    p = _make_provider(tmp_path, FakeLLM({"*": _obs_reply("k")}))
    _write_raw_pending(p, [_raw_row("k", "fact k", [VALID_ID])])
    swallowed = []
    for name in ("_llm_consolidate", "_supersede_confirm_candidates",
                 "_mine_alias_candidates", "_observe_consolidate"):
        monkeypatch.setattr(p, name, _boom)
        try:
            getattr(p, name)()
        except RuntimeError:
            swallowed.append(name)  # the boundary's per-face except swallows
    assert swallowed == ["_llm_consolidate", "_supersede_confirm_candidates",
                         "_mine_alias_candidates", "_observe_consolidate"]

    # whole-tick fail-soft inside the REAL observe face (fresh provider,
    # broken caller module -> tick survives, disk + pending untouched)
    p2 = _make_provider(tmp_path, FakeLLM({"*": _obs_reply("k")}))
    _write_raw_pending(p2, [_raw_row("k", "fact k", [VALID_ID])])

    class BoomModule:
        @staticmethod
        def observe(ctx, topic, facts):
            raise RuntimeError("boom")
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: BoomModule())
    p2._observe_consolidate()  # no raise (per-topic catch)
    assert _entries(p2) == []
    assert {r["topic"] for r in json.loads(
        p2.knowledge_raw_pending_path.read_text(encoding="utf-8"))} == {"k"}


# ── 11/12. standalone skip + in-flight guard ─────────────────────────────

def test_main_runtime_none_skips_silently(tmp_path):
    llm = FakeLLM({"*": _obs_reply("m")})
    p = _make_provider(tmp_path, None)
    p._main_runtime = None                     # standalone: no host main_runtime
    _write_raw_pending(p, [_raw_row("m", "fact m", [VALID_ID])])
    p._observe_consolidate()                 # must not raise
    assert _entries(p) == []
    assert _audit_rows(p) == []              # zero writes (gate before scan)


def test_in_flight_guard_skips_second_tick(tmp_path):
    p = _make_provider(tmp_path, FakeLLM({"*": _obs_reply("n")}))
    _write_raw_pending(p, [_raw_row("n", "fact n", [VALID_ID])])
    p._observing_in_flight = True            # 另一 tick 在飞 -> 幂等 guard
    p._observe_consolidate()
    assert _entries(p) == [] and _audit_rows(p) == []


# ── 13. per-topic fact cap N=8 (prompt 超限防护) ─────────────────────────

def test_per_topic_fact_cap_limits_prompt(tmp_path, monkeypatch):
    llm = FakeLLM({"o": _obs_reply("o"), "*": _obs_reply("o")})
    fake, state = install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm,
                       seed_ids=[VALID_ID] + [f"fact_{i:02d}" for i in range(12)])
    _write_raw_pending(p, [
        _raw_row("o", f"fact about o row {i}", [f"fact_{i:02d}"], i=i)
        for i in range(12)])
    p._observe_consolidate()
    prompt = state["prompts"][0]
    for i in range(8):
        assert f"fact_{i:02d}" in prompt       # 前 8 行进 prompt
    assert "fact_08" not in prompt             # 后 4 行被 N=8 cap 掉
    # entry written; supported_by only cites the canned reply's id (seeded)
    e = _entries(p)[0]
    assert e["proof_count"] == 1 and e["supported_by"][0]["id"] == VALID_ID
    assert set(e["provenance"]["memory_ids"]) <= {VALID_ID}


# helper (used by test 7): per-topic dynamic canned reply
# (installed directly in test_n_cap_overflow_rolls_to_next_tick)


# ── 14. D core: dangling memory_ids never feed the observe face ───────────
# _memory_id_set() membership test (L4575/L4694), NOT naive truthiness.
# A row whose memory_ids are all DANGLING (not in memories/*.json) must:
#   * NOT burn observe LLM budget (calls stay 0 for that topic)
#   * NEVER be fed to llm.observe (no supported_by built over dangling ids)
#   * stay in raw_pending (not swallowed, not re-dropped) — the circular
#     source=sink is killed.

def test_d_dangling_ids_never_feed_observe(tmp_path, monkeypatch):
    llm = FakeLLM({"*": {}})
    fake, state = install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm, seed_ids=[VALID_ID])
    # a topic whose ONLY raw row carries a dangling id (ghost, not seeded)
    p._decay["knowledge"]["raw_pending_ttl_days"] = 0   # disable C so we isolate D
    p._decay["knowledge"]["raw_pending_max_rows"] = 0
    _write_raw_pending(p, [_raw_row("dang", "ghost fact", ["ghost_999"])])
    p._observe_consolidate()
    assert state["calls"] == 0, "dangling-id topic must burn zero LLM budget"
    assert _entries(p) == [], "no observation may be built over a dangling id"
    rp = _raw_rows(p)
    assert any(r.get("topic") == "dang" for r in rp), "row must stay pending"


def test_d_mixed_valid_and_dangling_feeds_only_valid(tmp_path, monkeypatch):
    llm = FakeLLM({"mix": _obs_reply("mix", supported_id=VALID_ID),
                   "*": _obs_reply("mix", supported_id=VALID_ID)})
    fake, state = install(monkeypatch, replies=llm.replies)
    p = _make_provider(tmp_path, llm, seed_ids=[VALID_ID])
    p._decay["knowledge"]["raw_pending_ttl_days"] = 0
    p._decay["knowledge"]["raw_pending_max_rows"] = 0
    # one row mixes a valid id + a dangling one: only the valid one may back
    # a supported_by reference (D-2 drops the dangling id from the facts).
    _write_raw_pending(p, [_raw_row("mix", "deploy cadence", [VALID_ID, "ghost_999"])])
    p._observe_consolidate()
    prompt = state["prompts"][0]
    assert VALID_ID in prompt
    assert "ghost_999" not in prompt, "dangling id must not reach the LLM prompt"
    e = _entries(p)[0]
    assert e["supported_by"][0]["id"] == VALID_ID


def _raw_rows(p):
    f = p.knowledge_raw_pending_path
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


# ── 15. C slice: bounded auto-archive of raw_pending ──────────────────────

def test_c_ttl_archives_old_rows_only(tmp_path):
    from datetime import datetime, timezone
    p = _make_provider(tmp_path, None, seed_ids=[VALID_ID])
    p.knowledge_archive_dir = p.knowledge_dir / "archive"
    p._decay["knowledge"]["raw_pending_ttl_days"] = 30
    p._decay["knowledge"]["raw_pending_max_rows"] = 0
    fresh = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    old_rows = [_raw_row("t", f"old {i}", [VALID_ID]) for i in range(3)]
    for r in old_rows:
        r["created_ts"] = "2020-01-01T00:00:00"      # ~6 years old -> past ttl
    keep_rows = [_raw_row("t", f"fresh {i}", [VALID_ID], i=10 + i) for i in range(2)]
    for r in keep_rows:
        r["created_ts"] = fresh                        # age ~0 -> under ttl
    _write_raw_pending(p, old_rows + keep_rows)
    n = p._auto_archive_raw_pending()
    assert n == 3, f"only the 3 old rows archive, got {n}"
    remaining = _raw_rows(p)
    assert all("fresh" in r.get("content", "") for r in remaining)
    assert len(remaining) == 2
    arch = list((p.knowledge_archive_dir).glob("raw_pending_auto_*.jsonl"))
    assert len(arch) == 1
    archived = [json.loads(l) for l in arch[0].read_text().splitlines() if l.strip()]
    assert len(archived) == 3 and all("old" in r.get("content", "") for r in archived)


def test_c_max_rows_archives_overflow(tmp_path):
    p = _make_provider(tmp_path, None, seed_ids=[VALID_ID])
    p.knowledge_archive_dir = p.knowledge_dir / "archive"
    p._decay["knowledge"]["raw_pending_ttl_days"] = 0
    p._decay["knowledge"]["raw_pending_max_rows"] = 3
    _write_raw_pending(p, [_raw_row("t", f"row {i}", [VALID_ID], i=i) for i in range(5)])
    n = p._auto_archive_raw_pending()
    assert n == 2, "keep 3, archive the 2 overflow rows"
    assert len(_raw_rows(p)) == 3


def test_c_fail_soft_missing_block_moves_nothing(tmp_path):
    p = _make_provider(tmp_path, None, seed_ids=[VALID_ID])
    p.knowledge_archive_dir = p.knowledge_dir / "archive"
    p._decay["knowledge"] = {}                         # no knobs -> no archive
    _write_raw_pending(p, [_raw_row("t", "row", [VALID_ID])])
    n = p._auto_archive_raw_pending()
    assert n == 0
    assert len(_raw_rows(p)) == 1                       # pool untouched
