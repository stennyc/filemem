"""EPIC card 3: per-call LLM telemetry rows in consolidation_log +
empty-response explicit marking — acceptance tests (t_ff034ab1).

Card scope (aligning with the compressor's engineering practice,
telemetry + explicit marking ONLY — the model resolve path is untouched):

  1. Every host LLM call (the 6 faces: score_salience / relevance /
     distill / confirm_supersede / suggest_aliases / observe) appends ONE
     face-level `llm_call` telemetry row to the EXISTING
     consolidation_log.jsonl:
       {ts, action: "llm_call", face, model, provider, duration_ms, ok,
        fallback, reason?}
     reusing the existing append + _shard_lock flock protocol (no new log
     file, no changed existing-row schema — the audit chain stays
     append-only and parseable).
  2. An empty / whitespace-only host response is manifested, not silent:
     the row carries fallback=True reason="empty_response". The
     caller's fail-soft verdict value is UNCHANGED (salience / relevance
     still fall back to 0.5, distill to _DISTILL_FAIL, ...) — only the
     log row now records that it went through the fallback.
  3. model / provider come from the route attribution the host call_llm
     fills into route_info (the host's concrete route), falling back to
     the main_runtime snapshot's declared values (card 1's explicit
     capture). Zero hardcoded model names, zero provider config keys,
     host files byte-untouched.

Host acceptance: one tick's consolidation_log line count = LLM call count
+ the existing tick rows; one face-level telemetry row per LLM call,
grep-able (`"face": "distill"` etc.); empty-response rows grep-able
(`"fallback": true`).
"""
import json
import sys
import threading
import types
import pathlib

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
import consolidation.llm_caller as lc  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]


def _llm_call_rows(p) -> list:
    """All `llm_call` telemetry rows in p's consolidation_log (card 3)."""
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("action") == "llm_call"]


def _all_log_rows(p) -> list:
    f = p.storage_dir / "consolidation_log.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _seed_custom_host_fake(monkeypatch, fake) -> None:
    """Seed a custom host call surface BOTH ways the faces resolve it:
    the re-exec path (provider-driven faces re-exec llm_caller fresh and
    rebind _HOST_CALL_LLM from sys.modules["agent.auxiliary_client"]) and
    the direct path (the already-imported module's seam)."""
    host_mod = types.ModuleType("agent.auxiliary_client")
    host_mod.call_llm = fake
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", host_mod)
    monkeypatch.setattr(lc, "_HOST_CALL_LLM", fake)


def _host_shaped(content: str):
    return {"choices": [{"message": {"content": content}}]}


# ── 1. face-level: the _last_telemetry seam records one row-data per call ──

def test_distill_success_records_telemetry_ok(tmp_path, monkeypatch):
    """Happy path: the seam carries ok=True fallback=False + the snapshot's
    model / provider attribution + a non-negative integer duration_ms."""
    fake, state = install(monkeypatch,
                          replies=[{"summary": "s", "value": "high", "confirmed": True}])
    res = lc.distill(_MAIN_RUNTIME, "some content")
    assert res["confirmed"] is True and res["summary"] == "s"  # verdict unchanged
    tel = lc._last_telemetry
    assert tel["ok"] is True and tel["fallback"] is False
    assert tel["model"] == _MAIN_RUNTIME["model"]        # snapshot attribution
    assert tel["provider"] == _MAIN_RUNTIME["provider"]
    assert isinstance(tel["duration_ms"], int) and tel["duration_ms"] >= 0
    assert "reason" not in tel
    assert state["calls"] == 1


def test_route_info_attribution_wins_over_snapshot(tmp_path, monkeypatch):
    """Card requirement: model / provider come from the route attribution
    the host call_llm RETURNS (route_info, the concrete route used),
    falling back to the snapshot only when the route left a key unset."""
    captured = {}

    def fake(main_runtime=None, messages=None, temperature=None,
             max_tokens=None, **kw):
        ri = kw.get("route_info")
        if isinstance(ri, dict):
            # simulate the host's _record_route_info filling the route
            ri["provider"] = "openrouter"
            ri["model"] = "some-aux-model"
        captured["route_info_kwarg"] = ri
        return _host_shaped(json.dumps({"summary": "s", "value": "high",
                                       "confirmed": True}))

    _seed_custom_host_fake(monkeypatch, fake)
    lc.distill(_MAIN_RUNTIME, "content")
    assert isinstance(captured["route_info_kwarg"], dict)  # plugin passed the dict
    tel = lc._last_telemetry
    assert tel["model"] == "some-aux-model"        # route attribution, not snapshot
    assert tel["provider"] == "openrouter"
    assert tel["ok"] is True and tel["fallback"] is False


def test_route_info_partial_falls_back_to_snapshot(tmp_path, monkeypatch):
    """route_info leaving a key unset (host recorded only provider) ->
    model falls back to the main_runtime snapshot's declared value."""
    def fake(main_runtime=None, messages=None, **kw):
        ri = kw.get("route_info")
        if isinstance(ri, dict):
            ri["provider"] = "nous"
        return _host_shaped(json.dumps({"summary": "s", "value": "low",
                                       "confirmed": False}))
    _seed_custom_host_fake(monkeypatch, fake)
    lc.distill(_MAIN_RUNTIME, "content")
    tel = lc._last_telemetry
    assert tel["provider"] == "nous"
    assert tel["model"] == _MAIN_RUNTIME["model"]
    assert tel["ok"] is True and tel["fallback"] is False


# ── 2. empty / whitespace response: manifested, verdict unchanged ───────

def test_empty_response_distill_fallback_manifest(tmp_path, monkeypatch):
    """Empty host reply: distill still returns the EXACT pre-card-3
    fail-soft verdict (_DISTILL_FAIL — 宁可漏注, 不吞脏), while the seam
    records ok=False fallback=True reason="empty_response"."""
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.distill(_MAIN_RUNTIME, "content")
    assert res == lc._DISTILL_FAIL  # caller's fail-soft value UNCHANGED
    tel = lc._last_telemetry
    assert tel["ok"] is False and tel["fallback"] is True
    assert tel["reason"] == "empty_response"


def test_whitespace_response_salience_fallback_manifest(tmp_path, monkeypatch):
    """Whitespace-only reply: salience still falls back to 0.5 (the old
    silent default) but the seam marks fallback=True empty_response."""
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("   \n\t  ")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.score_salience(_MAIN_RUNTIME, "content")
    assert res["salience"] == 0.5  # unchanged fail-soft value
    tel = lc._last_telemetry
    assert tel["ok"] is False and tel["fallback"] is True
    assert tel["reason"] == "empty_response"


def test_empty_response_relevance_fallback_manifest(tmp_path, monkeypatch):
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.relevance_score(_MAIN_RUNTIME, "q", "text")
    assert res["relevance"] == 0.5 and res["model_used"] is True  # unchanged
    tel = lc._last_telemetry
    assert tel["fallback"] is True and tel["reason"] == "empty_response"


def test_unparseable_response_fallback_manifest(tmp_path, monkeypatch):
    """A non-JSON reply: parse degrades to the face defaults (不吞脏:
    nothing silently accepted) and the seam says unparseable_response."""
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("this is not json at all")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.confirm_supersede(_MAIN_RUNTIME, "new", "old")
    assert res["confirmed"] is False  # fail-soft verdict unchanged
    tel = lc._last_telemetry
    assert tel["ok"] is False and tel["fallback"] is True
    assert tel["reason"] == "unparseable_response"


def test_host_exception_fallback_manifest(tmp_path, monkeypatch):
    """Host transport exception: the face's except handler records
    fallback=True reason=model_unavailable:<exc> (duration_ms 0 — no
    call completed), verdict is the pre-card-3 fail-soft value."""
    def fake(main_runtime=None, messages=None, **kw):
        raise RuntimeError("host llm down")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.observe(_MAIN_RUNTIME, "topic", [{"id": "x", "content": "y"}])
    assert res["observation"] == "" and res["model_used"] is False  # unchanged
    tel = lc._last_telemetry
    assert tel["ok"] is False and tel["fallback"] is True
    assert tel["reason"].startswith("model_unavailable")
    assert "host llm down" in tel["reason"]


def test_missing_main_runtime_fallback_manifest(tmp_path, monkeypatch):
    """No snapshot (standalone / capture degrade): no host call at all —
    the face's RuntimeError path records fallback=True; verdict unchanged."""
    fake, state = install(monkeypatch, replies=[{"confirmed": True}])
    res = lc.confirm_supersede(None, "new", "old")
    assert res["confirmed"] is False and res["model_used"] is False
    assert state["calls"] == 0  # no call without a snapshot
    tel = lc._last_telemetry
    assert tel["ok"] is False and tel["fallback"] is True
    assert tel["reason"].startswith("model_unavailable")


# ── 3. provider sites: one llm_call row per call into the existing log ──

def _make_confirm_provider(tmpdir) -> FileMemProvider:
    """test_ep_2_confirm shape: the confirm face's provider surface."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = _MAIN_RUNTIME
    p._supersede_confirming = False
    p.knowledge_entries_path = None
    return p


def _old_row(rid, content, new_id):
    return {
        "id": rid, "type": "fact", "content": content, "level": "L0",
        "metadata": {"origin": {"kind": "user", "trust": "medium"},
                     "superseded": True, "superseded_by": new_id},
        "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.5, "last_referenced": "", "ttl_days": None,
    }


def _new_row(rid, content, old_ids, state="candidate"):
    return {
        "id": rid, "type": "fact", "content": content, "level": "L0",
        "metadata": {"origin": {"kind": "user", "trust": "medium"},
                     "supersedes": list(old_ids), "supersede_state": state,
                     "superseded": False},
        "created_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T00:00:00",
        "occurred_at": None, "source": "auto", "reference_count": 0,
        "salience": 0.6, "last_referenced": "", "ttl_days": None,
    }


def _write_shard(p, rows):
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


def test_confirm_site_writes_one_llm_call_row(tmp_path, monkeypatch):
    """confirm_supersede site: one LLM call -> exactly ONE llm_call row
    (face=confirm_supersede, ok, attribution) appended to the SAME log as
    the verdict row — line count = LLM call count + verdict rows."""
    fake, state = install(monkeypatch,
                          replies=[{"confirmed": True, "reason": "same fact updated"}])
    p = _make_confirm_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "deploy on host 117 uses vm", "fact_1"),
                     _new_row("fact_1", "deploy on host 117 uses k8s", ["fact_0"])])
    p._supersede_confirm_candidates()
    assert state["calls"] == 1
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    row = tel[0]
    assert row["face"] == "confirm_supersede"
    assert row["ok"] is True and row["fallback"] is False
    assert row["model"] == _MAIN_RUNTIME["model"]
    assert row["provider"] == _MAIN_RUNTIME["provider"]
    assert isinstance(row["duration_ms"], int) and "ts" in row
    # grep-able face attribution in the raw file
    raw = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    assert '"face": "confirm_supersede"' in raw
    # acceptance: total lines = LLM call count (1) + verdict rows (1)
    all_rows = _all_log_rows(p)
    assert len(all_rows) == 2
    verdicts = [r for r in all_rows if r.get("action") == "supersede_confirm"]
    assert len(verdicts) == 1 and verdicts[0]["confirmed"] is True


def test_confirm_site_empty_response_fallback_row(tmp_path, monkeypatch):
    """Empty host reply at the confirm site: the verdict row keeps its
    pre-card-3 fail-soft values (confirmed=False, model_used=True — the
    call DID happen), while the llm_call row marks fallback=True
    reason=empty_response (grep-able)."""
    def fake(main_runtime=None, messages=None, temperature=None,
             max_tokens=None, **kw):
        return _host_shaped("   ")
    _seed_custom_host_fake(monkeypatch, fake)
    p = _make_confirm_provider(tmp_path)
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "b", ["fact_0"])])
    p._supersede_confirm_candidates()
    rows = {r["id"]: r for r in json.loads(
        (p.memories_dir / "fact.json").read_text(encoding="utf-8"))}
    assert rows["fact_1"]["metadata"]["supersede_state"] == "candidate"  # unchanged
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "confirm_supersede"
    assert tel[0]["ok"] is False and tel[0]["fallback"] is True
    assert tel[0]["reason"] == "empty_response"
    verdicts = [r for r in _all_log_rows(p) if r.get("action") == "supersede_confirm"]
    assert len(verdicts) == 1
    assert verdicts[0]["confirmed"] is False and verdicts[0]["model_used"] is True
    raw = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    assert '"fallback": true' in raw          # grep-able fallback mark


def test_confirm_site_no_call_zero_rows(tmp_path, monkeypatch):
    """Standalone gate (_main_runtime None): no LLM call -> NO llm_call
    row and no verdict row (the call-level gate precedes the scan)."""
    install(monkeypatch, replies=[{"confirmed": True}])
    p = _make_confirm_provider(tmp_path)
    p._main_runtime = None
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "b", ["fact_0"])])
    p._supersede_confirm_candidates()
    assert _llm_call_rows(p) == []
    assert _all_log_rows(p) == []


def _make_rescore_provider(tmpdir, n_entries: int) -> FileMemProvider:
    """test_s1b shape trimmed to the rescore face: episodic candidates +
    the main_runtime gate + the existing log path."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    with open(p.episodic_dir / "s.jsonl", "w", encoding="utf-8") as fh:
        for i in range(n_entries):
            fh.write(json.dumps({"content": f"entry {i} detail",
                                 "session_id": f"s{i}", "salience": 0.5}) + "\n")
    p.source_profile = "test"
    p.session_id = "s-test"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = _MAIN_RUNTIME
    p._llm_consolidating = False
    return p


def test_rescore_tick_line_count_equals_calls_plus_tick_rows(tmp_path, monkeypatch):
    """Acceptance criterion: after one tick, consolidation_log line count
    = LLM call count + the existing tick rows — each of the N rescore
    calls writes ONE llm_call row plus its llm_rescore verdict row."""
    fake, state = install(monkeypatch, replies={"*": {"salience": 0.9}})
    p = _make_rescore_provider(tmp_path, 3)
    p._llm_consolidate()
    assert state["calls"] == 3
    tel = _llm_call_rows(p)
    assert len(tel) == 3
    assert all(r["face"] == "score_salience" for r in tel)
    assert all(r["ok"] is True and r["fallback"] is False for r in tel)
    assert all(r["model"] == _MAIN_RUNTIME["model"] for r in tel)
    all_rows = _all_log_rows(p)
    # 3 llm_call rows + 3 llm_rescore verdict rows = 6 (zero other rows:
    # the tick ran the rescore face directly, no tick_skip audit)
    assert len(all_rows) == 6
    assert len([r for r in all_rows if r.get("action") == "llm_rescore"]) == 3


def test_rescore_tick_empty_responses_marked(tmp_path, monkeypatch):
    """All-empty replies: 3 calls -> 3 llm_call rows, each
    fallback=True reason=empty_response; the verdict rows still carry the
    unchanged fail-soft salience 0.5 (salience_source stays 'llm' — the
    call happened, it just produced nothing to parse)."""
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    p = _make_rescore_provider(tmp_path, 3)
    p._llm_consolidate()
    tel = _llm_call_rows(p)
    assert len(tel) == 3
    assert all(r["fallback"] is True and r["ok"] is False for r in tel)
    assert all(r["reason"] == "empty_response" for r in tel)
    verdicts = [r for r in _all_log_rows(p) if r.get("action") == "llm_rescore"]
    assert len(verdicts) == 3
    assert all(v["after"]["salience"] == 0.5 for v in verdicts)  # unchanged value


def _make_observe_provider(tmpdir) -> FileMemProvider:
    """test_ep_2_observe shape: knowledge paths + seeded memory id so the
    observation's supported_by resolves."""
    VALID_ID = "fact_100"
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    (p.memories_dir / "fact.json").write_text(
        json.dumps([{"id": VALID_ID, "type": "fact",
                     "content": "seed fact_100", "salience": 0.5,
                     "created_at": "2026-09-26T00:00:00",
                     "updated_at": "2026-09-26T00:00:00"}]),
        encoding="utf-8")
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = _MAIN_RUNTIME
    p._observing_in_flight = False
    return p, VALID_ID


def _obs_reply(topic, supported_id):
    return {"observation": f"the facts establish topic {topic}",
            "value": "high",
            "supported": [{"id": supported_id, "quote": f"q for {topic}"}]}


def test_observe_site_writes_llm_call_row(tmp_path, monkeypatch):
    """observe success: one llm_call row (face=observe) + the existing
    observe verdict row — the entry write semantics are untouched."""
    p, VALID_ID = _make_observe_provider(tmp_path)
    fake, state = install(monkeypatch,
                          replies={"i1": _obs_reply("i1", VALID_ID),
                                   "*": _obs_reply("i1", VALID_ID)})
    (p.knowledge_raw_pending_path).write_text(
        json.dumps([{"topic": "i1", "content": "fact i1",
                     "memory_ids": [VALID_ID], "origin": "catch_filter",
                     "reason": "synthetic", "created_ts": "2026-10-01T00:00:00"}]),
        encoding="utf-8")
    p._observe_consolidate()
    assert state["calls"] == 1
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "observe" and tel[0]["ok"] is True
    entries = json.loads(p.knowledge_entries_path.read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0]["kind"] == "observation"
    verdicts = [r for r in _all_log_rows(p) if r.get("action") == "observe"]
    assert len(verdicts) == 1 and verdicts[0]["observation_id"] is not None


def test_observe_site_empty_response_no_entry_but_fallback_row(
        tmp_path, monkeypatch):
    """Empty host reply: the observation entry is NOT written (raw facts
    stay pending, unchanged 不吞脏 semantics) — but the call is still
    manifested: one llm_call row fallback=True reason=empty_response."""
    p, VALID_ID = _make_observe_provider(tmp_path)

    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    (p.knowledge_raw_pending_path).write_text(
        json.dumps([{"topic": "i1", "content": "fact i1",
                     "memory_ids": [VALID_ID], "origin": "catch_filter",
                     "reason": "synthetic", "created_ts": "2026-10-01T00:00:00"}]),
        encoding="utf-8")
    p._observe_consolidate()
    assert not p.knowledge_entries_path.exists() or \
        json.loads(p.knowledge_entries_path.read_text(encoding="utf-8")) == []
    rp = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    assert len(rp) == 1 and rp[0]["topic"] == "i1"  # raw kept (unchanged)
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "observe"
    assert tel[0]["fallback"] is True and tel[0]["reason"] == "empty_response"
    assert not [r for r in _all_log_rows(p) if r.get("action") == "observe"] \
        or all(r.get("observation_id") is None
               for r in _all_log_rows(p) if r.get("action") == "observe")


def _make_alias_provider(tmpdir) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = None
    p.source_profile = "test"
    p.session_id = "s1"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = _MAIN_RUNTIME
    p._alias_mining_in_flight = False
    return p


def _fact(rid, content, ts="2026-09-15T00:00:00"):
    return {"id": rid, "type": "fact", "content": content, "level": "L2",
            "metadata": {}, "created_at": ts, "updated_at": ts,
            "salience": 0.5, "reference_count": 0, "ttl_days": None,
            "source": "auto"}


def test_alias_site_writes_llm_call_row(tmp_path, monkeypatch):
    """suggest_aliases success: one llm_call row (face=suggest_aliases)
    + the existing alias_mine verdict row."""
    p = _make_alias_provider(tmp_path)
    fake, state = install(monkeypatch,
                          replies={"toolchain": {"aliases": ["pip", "poetry"]},
                                   "*": {"aliases": []}})
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as fh:
        json.dump([_fact("a1", "toolchain alpha"),
                   _fact("a2", "toolchain beta"),
                   _fact("a3", "toolchain gamma")], fh, ensure_ascii=False)
    p._mine_alias_candidates()
    assert state["calls"] == 1
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "suggest_aliases" and tel[0]["ok"] is True
    data = json.loads((p.storage_dir / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["toolchain"]["aliases"] == ["pip", "poetry"]
    verdicts = [r for r in _all_log_rows(p) if r.get("action") == "alias_mine"]
    assert len(verdicts) == 1


def test_alias_site_empty_response_no_alias_but_fallback_row(
        tmp_path, monkeypatch):
    """Empty reply: the topic gets an EMPTY alias entry (an empty entry
    is the VERDICT of no-alias, unchanged 不吞脏 semantics) — but the
    call is manifested: llm_call row fallback=True reason=empty_response."""
    p = _make_alias_provider(tmp_path)

    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as fh:
        json.dump([_fact("a1", "toolchain alpha"),
                   _fact("a2", "toolchain beta"),
                   _fact("a3", "toolchain gamma")], fh, ensure_ascii=False)
    p._mine_alias_candidates()
    data = json.loads((p.storage_dir / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["toolchain"]["aliases"] == []  # unchanged verdict
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "suggest_aliases"
    assert tel[0]["fallback"] is True and tel[0]["reason"] == "empty_response"


# ── 4b. distill site (_distill_item): one llm_call row per call ─────────

def _make_distill_provider(tmpdir, item_content: str = "old content") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.source_profile = "test"
    p.session_id = "s1"
    p._turn_active_sid = "s1"
    p._decay = json.loads(json.dumps(DECAY_DEFAULTS))
    p._main_runtime = _MAIN_RUNTIME
    p._distill_budget = {}
    p._distill_call_limit = 5
    (p.memories_dir / "fact.json").write_text(json.dumps(
        [{"id": "i1", "type": "fact", "content": item_content, "level": "L0",
          "metadata": {}, "created_at": "2026-09-26T00:00:00",
          "updated_at": "2026-09-26T00:00:00", "salience": 0.5,
          "reference_count": 0, "ttl_days": None, "source": "auto"}],
        ensure_ascii=False), encoding="utf-8")
    return p


def test_distill_site_writes_llm_call_row(tmp_path, monkeypatch):
    """distill success: one llm_call row (face=distill) + the item's
    metadata gets the LLM verdict (confirmed=True) — the write semantics
    are unchanged, the log gains exactly one telemetry row."""
    p = _make_distill_provider(tmp_path)
    fake, state = install(monkeypatch,
                         replies=[{"summary": "new summary", "value": "high",
                                    "confirmed": True}])
    p._distill_item("fact", "i1", "some fact content")
    assert state["calls"] == 1
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "distill" and tel[0]["ok"] is True
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert items[0]["metadata"]["confirmed"] is True
    assert items[0]["metadata"]["summary"] == "new summary"


def test_distill_site_empty_response_fallback_row(tmp_path, monkeypatch):
    """Empty host reply at the distill site: the item's metadata keeps the
    pre-card-3 fail-soft verdict (confirmed=False, no summary — 宁可漏注),
    while the llm_call row manifests fallback=True reason=empty_response."""
    p = _make_distill_provider(tmp_path)

    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    p._distill_item("fact", "i1", "some fact content")
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert items[0]["metadata"]["confirmed"] is False      # unchanged verdict
    assert "summary" not in items[0]["metadata"]
    tel = _llm_call_rows(p)
    assert len(tel) == 1
    assert tel[0]["face"] == "distill"
    assert tel[0]["fallback"] is True and tel[0]["reason"] == "empty_response"


# ── 4c. relevance site (search B3 model mode): one row per re-rank call ─

def _s1b_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_s1b_helpers", PLUGIN_DIR / "tests" / "test_s1b_tick_wiring.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_relevance_site_writes_llm_call_row(tmp_path, monkeypatch):
    """B3 model-assist re-ranking site: each LLM relevance call that
    completes writes ONE llm_call row (face=relevance) via the shared
    flock-protected append.

    KNOWN PRE-EXISTING LATENT BUG (out of card-3 scope, follow-up task
    filed): the re-rank line computes item["score"] * (0.5 + r) where r
    is the face's verdict DICT — a guaranteed TypeError on the first
    item, caught by the site's fail-soft except ("model re-rank failed,
    keeping keyword order"). So under the real caller exactly one call
    completes per search -> exactly one telemetry row; the row-per-call
    guarantee this card adds is unaffected. The one-token fix
    (0.5 + r["relevance"]) would CHANGE the search-path behavior the card
    must not touch (fail-soft semantics frozen by card 2), so it ships
    as its own task, not here."""
    s1b = _s1b_module()
    store = s1b._make_store(
        tmp_path, {"fact": [
            {"id": "f1", "type": "fact", "content": "ingress 502 errors",
             "level": "L2", "metadata": {}, "created_at": "2026-09-26T00:00:00",
             "updated_at": "2026-09-26T00:00:00", "salience": 0.5,
             "reference_count": 1, "last_referenced": "2026-10-01T00:00:00",
             "ttl_days": None, "source": "auto"},
            {"id": "f2", "type": "fact", "content": "ingress routing table",
             "level": "L2", "metadata": {}, "created_at": "2026-09-26T00:00:00",
             "updated_at": "2026-09-26T00:00:00", "salience": 0.5,
             "reference_count": 1, "last_referenced": "2026-10-01T00:00:00",
             "ttl_days": None, "source": "auto"},
        ]})
    decay = s1b._decay_copy()
    decay.setdefault("search", {})["mode"] = "model"
    p = s1b._make_provider(tmp_path, store, decay)
    p._main_runtime = _MAIN_RUNTIME  # B3 gate (card 2 surface)
    fake, state = install(monkeypatch, replies={"*": {"relevance": 0.9}})
    p.search_memories("ingress", ranking="decay")  # must not raise (fail-soft)
    # pre-existing bug caps the loop at 1 completed call (TypeError on
    # the dict-multiply after the first item's telemetry row is written):
    assert state["calls"] == 1
    tel = _llm_call_rows(p)
    assert len(tel) == 1                     # one row per COMPLETED call
    row = tel[0]
    assert row["face"] == "relevance" and row["ok"] is True
    assert row["model"] == _MAIN_RUNTIME["model"]
    assert row["provider"] == _MAIN_RUNTIME["provider"]


def test_relevance_face_direct_empty_response_fallback_row(tmp_path, monkeypatch):
    """Face-level (direct seam) relevance empty-response manifestation —
    the dict-multiply site bug does not interfere with the face itself:
    empty reply -> relevance 0.5 (unchanged fail-soft) + seam fallback."""
    def fake(main_runtime=None, messages=None, **kw):
        return _host_shaped("")
    _seed_custom_host_fake(monkeypatch, fake)
    res = lc.relevance_score(_MAIN_RUNTIME, "q", "text")
    assert res["relevance"] == 0.5            # unchanged value
    assert lc._last_telemetry["fallback"] is True
    assert lc._last_telemetry["reason"] == "empty_response"


# ── 5. the zero-LLM test doubles stay quiet (no phantom rows) ────────────

def test_zero_llm_test_double_writes_no_llm_call_row(tmp_path, monkeypatch):
    """A caller module without the telemetry seam (the sibling tests'
    zero-host doubles / CLI paths) must NOT produce llm_call rows — the
    helper fail-softs on a missing seam (no row, no error)."""
    p = _make_confirm_provider(tmp_path)
    import types as _t

    class BoomModule:
        @staticmethod
        def confirm_supersede(ctx, new_text, old_text):
            return {"confirmed": False, "model_used": False}
    monkeypatch.setattr(fm, "_load_llm_caller", lambda: BoomModule())
    _write_shard(p, [_old_row("fact_0", "a", "fact_1"),
                     _new_row("fact_1", "b", ["fact_0"])])
    p._supersede_confirm_candidates()  # must not raise
    assert _llm_call_rows(p) == []
    # but the verdict row still lands (existing schema, unchanged)
    verdicts = [r for r in _all_log_rows(p) if r.get("action") == "supersede_confirm"]
    assert len(verdicts) == 1 and verdicts[0]["model_used"] is False


# ── 5. existing-row schema / audit-chain integrity ───────────────────────

def test_existing_row_schemas_unchanged_and_all_lines_parse(
        tmp_path, monkeypatch):
    """The card constraint: existing consolidation_log row schemas are
    untouched — card 3 only APPENDS the new llm_call row type. Every line
    in the shared log stays JSON-parseable (the audit chain is intact),
    verdict rows keep their exact pre-card-3 key sets."""
    p, VALID_ID = _make_observe_provider(tmp_path)
    fake, _state = install(monkeypatch,
                           replies={"i1": _obs_reply("i1", VALID_ID),
                                    "*": _obs_reply("i1", VALID_ID)})
    (p.knowledge_raw_pending_path).write_text(
        json.dumps([{"topic": "i1", "content": "fact i1",
                     "memory_ids": [VALID_ID], "origin": "catch_filter",
                     "reason": "synthetic", "created_ts": "2026-10-01T00:00:00"}]),
        encoding="utf-8")
    p._observe_consolidate()
    raw = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    lines = [l for l in raw.splitlines() if l.strip()]
    rows = [json.loads(l) for l in lines]  # every line parses (chain intact)
    verdict = [r for r in rows if r.get("action") == "observe"][0]
    # the pre-card-3 observe verdict row key set, verbatim
    assert set(verdict.keys()) == {"ts", "action", "topic", "observation_id",
                                   "proof_count", "model_used", "source_profile"}
    tel = [r for r in rows if r.get("action") == "llm_call"][0]
    # the new row type carries the card's field set
    assert set(tel.keys()) == {"ts", "action", "face", "model", "provider",
                               "duration_ms", "ok", "fallback"}


def test_hardcoded_model_audit_llm_caller_segment(tmp_path, monkeypatch):
    """Zero-hardcoded-model audit over the card-3 telemetry segment of
    llm_caller.py (same posture as the sibling face segments): no model
    assignment literals, no SDK imports, no task= key on the host call."""
    text = (PLUGIN_DIR / "consolidation" / "llm_caller.py").read_text(encoding="utf-8")
    seg = text.split("epic card 3: per-call telemetry seam", 1)[1]
    import re
    assert not re.search(r'model\s*[:=]\s*["\'][a-z]|openai|anthropic|httpx|requests', seg)
    assert "task=" not in seg
    # no new imports in the segment
    assert not re.search(r"^\s*import |^\s*from ", seg, re.M)
