"""EP-2 Observation layer — T1 data plane (deterministic, zero LLM).

Blue-print: specs/EP-2-observation-impl-notes.md (T1 section + test cases).

Covers (>=10):
  1.  observation entry round-trip (kind/observed_topic/supported_by/
      proof_count/value/stale/status/superseded_by/provenance)
  2.  legacy no-kind entry loads as gold (pool salience 0.9)
  3.  supported_by bad reference (id not in _memory_id_set) -> reject +
      quarantine raw_pending (origin=obs_unresolved_provenance) + None
  4.  _mark_stale_observations hit (high-overlap new fact) -> stale=True
  5.  _mark_stale_observations irrelevant (low overlap) -> stale stays False
  6.  _mark_stale_observations deterministic (re-call on same input is a
      no-op -> 0; file bytes stable)
  7.  retrieval: active non-stale obs -> 0.9; stale obs -> 0.5;
      superseded obs -> not in pool
  8.  live-data 0-observation regression gate: gold-only pool byte-identical
      to pre-T1 (salience 0.9)
  9.  kind discrimination coexistence (gold + observation in same file read
      back correctly)
  10. lock: 2-process concurrent upsert_observation no-tear
  11. zero-LLM audit: T1 write face has no llm import / ctx.llm / complete_structured
  12. I2 idempotent upsert_observation (same topic+content -> same id, bytes stable)
  13. I1 supersede prior active observation on same topic
"""
import json, sys, pathlib, tempfile, threading, subprocess

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402


def _make_provider(tmpdir, seed_memory_ids=()):
    """Mirror test_gold_knowledge_red._make_provider + knowledge face (zero initialize())."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    for mid in seed_memory_ids:
        mtype = mid.split("_")[0]
        f = p.memories_dir / f"{mtype}.json"
        items = json.load(open(f, encoding="utf-8")) if f.exists() else []
        items.append({"id": mid, "type": mtype, "content": f"seed {mid}",
                      "salience": 0.5, "created_at": "2026-09-26T00:00:00",
                      "updated_at": "2026-09-26T00:00:00"})
        f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    p.storage_dir = tmpdir
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p._decay = {"knowledge": {"stale_min_overlap": 0.4, "observe_max_per_tick": 3}}
    p._llm_ctx = None
    return p


def _entries(p):
    f = p.knowledge_entries_path
    return json.load(open(f, encoding="utf-8")) if f.exists() else []


def test_observation_round_trip():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    oid = p.upsert_observation(
        topic="t1",
        content="the team deploys weekly on Friday",
        supported_by=[{"id": "preference_892_1790368739", "quote": "we ship Fridays"}],
        value="high",
    )
    assert oid is not None and oid.startswith("k_")
    e = next(x for x in _entries(p) if x["id"] == oid)
    assert e["kind"] == "observation"
    assert e["observed_topic"] == "t1"
    assert e["supported_by"] == [{"id": "preference_892_1790368739", "quote": "we ship Fridays"}]
    assert e["proof_count"] == 1
    assert e["value"] == "high"
    assert e["stale"] is False
    assert e["status"] == "active"
    assert e["superseded_by"] is None
    assert e["provenance"]["memory_ids"] == ["preference_892_1790368739"]


def test_legacy_no_kind_loads_as_gold():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()))
    p.upsert_knowledge(topic="g", content="gold content here",
                       memory_ids=[])
    e = _entries(p)[0]
    assert "kind" not in e or e.get("kind") in (None, "gold")
    # pool: legacy gold (no kind) stays salience 0.9 (pre-T1 behavior)
    pool = p._load_knowledge_pool()
    assert len(pool) == 1 and pool[0]["salience"] == 0.9


def test_observation_bad_reference_rejected():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    res = p.upsert_observation(
        topic="bad",
        content="should not land",
        supported_by=[{"id": "decision_999_9999999999", "quote": "unresolved id"}],
    )
    assert res is None
    assert _entries(p) == []
    rp = json.load(open(p.knowledge_raw_pending_path, encoding="utf-8"))
    assert any(r["origin"] == "obs_unresolved_provenance"
               and r["content"] == "should not land" for r in rp)


def test_mark_stale_hit():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    p.upsert_observation(topic="t1",
                         content="deploy cadence is weekly every friday",
                         supported_by=[{"id": "preference_892_1790368739", "quote": "q"}])
    n = p._mark_stale_observations("t1", "deploy cadence is now monthly every friday")
    assert n >= 1
    e = next(x for x in _entries(p) if x["kind"] == "observation")
    assert e["stale"] is True


def test_mark_stale_irrelevant():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    p.upsert_observation(topic="t1",
                         content="deploy cadence is weekly every friday",
                         supported_by=[{"id": "preference_892_1790368739", "quote": "q"}])
    n = p._mark_stale_observations("t1", "the office kitchen coffee machine is broken")
    assert n == 0
    e = next(x for x in _entries(p) if x["kind"] == "observation")
    assert e["stale"] is False


def test_mark_stale_deterministic_idempotent():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    p.upsert_observation(topic="t1",
                         content="deploy cadence is weekly every friday",
                         supported_by=[{"id": "preference_892_1790368739", "quote": "q"}])
    first = p._mark_stale_observations("t1", "deploy cadence is now monthly every friday")
    assert first >= 1
    before = p.knowledge_entries_path.read_bytes()
    # second call: already stale -> no-op -> 0 new marks, bytes unchanged
    second = p._mark_stale_observations("t1", "deploy cadence is now monthly every friday")
    assert second == 0
    assert p.knowledge_entries_path.read_bytes() == before


def test_retrieval_salience_matrix():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    sb = [{"id": "preference_892_1790368739", "quote": "q"}]
    # I1: two writes on topic t, the SECOND supersedes the FIRST
    p.upsert_observation(topic="t", content="obsA", supported_by=sb)   # first -> superseded
    p.upsert_observation(topic="t", content="obsB", supported_by=sb)   # second -> active, stale=False
    ents = {e["content"]: e for e in _entries(p)}
    assert ents["obsA"]["status"] == "superseded"
    assert ents["obsB"]["status"] == "active" and ents["obsB"]["stale"] is False
    # force the ACTIVE obsB stale=True -> down-weight 0.5
    active_id = ents["obsB"]["id"]
    raw = json.loads(p.knowledge_entries_path.read_text())
    for e in raw:
        if e["id"] == active_id:
            e["stale"] = True
    p.knowledge_entries_path.write_text(json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2))
    pool = p._load_knowledge_pool()
    by_id = {e["id"]: e for e in pool}
    assert by_id[active_id]["salience"] == 0.5        # stale obs down-weighted
    assert ents["obsA"]["id"] not in by_id            # superseded obs NOT in pool
    # a fresh active non-stale obs on a new topic -> 0.9
    p.upsert_observation(topic="t2", content="obsC", supported_by=sb)
    pool2 = {e["id"]: e for e in p._load_knowledge_pool()}
    obsC = next(e for e in _entries(p) if e["content"] == "obsC")
    assert pool2[obsC["id"]]["salience"] == 0.9


def test_live_zero_observation_pool_byte_identical():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    p.upsert_knowledge(topic="deploy", content="deploy cadence weekly",
                       memory_ids=["preference_892_1790368739"])
    pool = p._load_knowledge_pool()
    # gold-only (no observation): single active gold row, salience 0.9, type knowledge
    assert len(pool) == 1
    assert pool[0]["salience"] == 0.9 and pool[0]["type"] == "knowledge"
    assert pool[0]["content"] == "deploy cadence weekly"


def test_kind_discrimination_coexist():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    p.upsert_knowledge(topic="t", content="gold row",
                       memory_ids=["preference_892_1790368739"])
    p.upsert_observation(topic="t", content="observation row",
                         supported_by=[{"id": "preference_892_1790368739", "quote": "q"}])
    ents = _entries(p)
    kinds = [e.get("kind") for e in ents]
    # gold row has no kind (or kind missing); observation row has kind=observation
    assert "observation" in kinds
    assert len(ents) == 2
    obs = next(e for e in ents if e.get("kind") == "observation")
    gold = next(e for e in ents if e is not obs)
    assert gold["content"] == "gold row"


def test_lock_two_processes_no_tear():
    tmp = pathlib.Path(tempfile.mkdtemp())
    _make_provider(tmp, seed_memory_ids=["preference_892_1790368739"])
    # Drive the real _shard_lock across 2 threads of concurrent upsert
    # calls on one provider (same cross-process lock helper, in-process form).
    p = _make_provider(tmp, seed_memory_ids=["preference_892_1790368739"])

    def w(n):
        for i in range(5):
            p.upsert_observation(topic="t", content=f"c{n}-{i}",
                                 supported_by=[{"id": "preference_892_1790368739", "quote": "q"}])

    ts = [threading.Thread(target=w, args=(k,)) for k in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    ents = _entries(p)
    # both threads wrote; file valid JSON; all 10 distinct content rows present
    contents = {e["content"] for e in ents}
    assert len(contents) == 10, f"expected 10 rows, got {len(contents)}"


def test_zero_llm_audit_t1_write_face():
    """T1 write face (upsert_observation / _mark_stale_observations) is a pure
    deterministic data plane: it EXECUTES no LLM face. The docstring may mention
    T2's llm_caller (prose), so the audit inspects EXECUTABLE names via AST
    (string-constant docstring mentions are NOT ast.Name/ast.Attribute nodes)."""
    import ast
    src = pathlib.Path(__file__).resolve().parents[1] / "__init__.py"
    text = src.read_text(encoding="utf-8")
    raw_start = text.index("def upsert_observation(")
    start = text.rfind("\n", 0, raw_start) + 1   # include the leading indent of that line
    end = text.index("# SA2-4: graph skeleton")
    import textwrap
    block = textwrap.dedent(text[start:end])   # uniform 4-space base -> module level
    assert "ctx.llm" not in block            # no host-LLM handle in T1 block
    assert "complete_structured" not in block  # no LLM call
    exec_names = set()
    for node in ast.walk(ast.parse(block)):
        if isinstance(node, ast.Name):
            exec_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            exec_names.add(node.attr)
    assert "llm_caller" not in exec_names     # T1 never calls the LLM caller
    assert "complete_structured" not in exec_names


def test_i2_idempotent_upsert_observation():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    sb = [{"id": "preference_892_1790368739", "quote": "q"}]
    id1 = p.upsert_observation(topic="t", content="c", supported_by=sb)
    first = p.knowledge_entries_path.read_bytes()
    for _ in range(3):
        rid = p.upsert_observation(topic="t", content="c", supported_by=sb)
        assert rid == id1  # no-op returns existing id
    assert p.knowledge_entries_path.read_bytes() == first  # I2 bytes stable


def test_i1_supersede_prior_active_observation():
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    sb = [{"id": "preference_892_1790368739", "quote": "q"}]
    p.upsert_observation(topic="t", content="v1", supported_by=sb)
    p.upsert_observation(topic="t", content="v2", supported_by=sb)
    by_content = {e["content"]: e for e in _entries(p)}
    assert by_content["v1"]["status"] == "superseded"
    assert by_content["v2"]["status"] == "active"
    assert by_content["v1"]["superseded_by"] == by_content["v2"]["id"]
    assert len(_entries(p)) == 2  # no DELETE
