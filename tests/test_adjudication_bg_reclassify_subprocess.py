"""2026-11-17 (owner ruling): adjudication reclassify in a CHILD PROCESS.

The ②+②a offload (f12c8b9) moved the adjudication path's ONLY slow op —
the budget-gated LLM reclassify of a confirmed fact-miss row — onto an
in-process daemon THREAD. The owner's 2026-11-17 ruling (parity with the
code_map subprocess build, 666fdbf): run it in a CHILD PYTHON instead.

Feasibility (verified against the source, not assumed):
  * the LLM face is host `agent.auxiliary_client.call_llm` with the
    EXPLICIT main_runtime DICT snapshot (JSON-safe; no in-process
    contextvar / panel dependency — the child carries the snapshot on
    stdin, secrets never ride the argv / ps surface);
  * the adjudication path never touches the in-memory `_pending_confirm`
    panel (the reclassify only advisory-stamps the on-disk row);
  * the single-shard RMW already serializes cross-process via
    `<shard>.json.lock` (fcntl/msvcrt, 0-lost-update), so the child
    reuses the EXISTING `_advisory_stamp_suggested_type` method.

DESIGN (mirrors code_map state._start_build_process):
  * `_start_reclassify_process`: Popen(entry, argv --shard/--row, payload
    JSON on STDIN). The parent pre-gates the per-session budget and
    reserves the slot AFTER a successful Popen (a Popen hiccup leaves
    the budget intact so the thread fallback can still call the LLM).
  * Registry `_reclassify_processes` (cap 8, trim oldest) + bounded
    quiesce reaping (`_quiesce_bg_reclassify`, called at shutdown — a
    hung child is left to die; the stamp is advisory, nothing is lost).
  * Fail-soft: flag-off / Popen hiccup / no host tree / non-JSON snapshot
    / over-cap -> the in-process THREAD path (the work is never lost to a
    spawn failure, only the isolation is abandoned).
  * Fail-visible: a child crash -> traceback on the host stderr, exit != 0.

Case map:
  1. real child: fake host tree (canned call_llm) + on-disk entry ->
     exit 0, advisory stamp lands in the regex shard, pid != parent,
     the audit row + llm_call telemetry row land in consolidation_log;
  2a. protocol violation (non-JSON stdin) -> exit != 0 + traceback,
     row untouched (fail-visible);
  2b. missing host call surface -> clean exit-0 no-op (the LLM face is
     fail-soft by design; the child never crashes on an unavailable host);
  3. spawn tracked + reaped + cap-8 trim (9 spawns -> 8 handles, all pids
     foreign, all exit 0);
  4. spawn hiccup (Popen raises) -> the thread fallback COMPLETES the
     stamp (work never lost, only isolation abandoned);
  5. flag-off (`_reclassify_subprocess=False`) -> pure thread path
     (clean rollback knob; the pre-2026-11-17 behavior the
     test_adjudication_bg_reclassify.py suite pins).

Store hygiene: tempfile-tree partial provider, ZERO writes to the live
store.
"""
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from _llm_host_fake import install  # noqa: E402
from test_adjudication_bg_reclassify import (  # noqa: E402
    FACT_MISS_CONTENT, _RECENT, _make_provider, _row_in,
)

_PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ENTRY = _PLUGIN_ROOT / "reclassify_subprocess_entry.py"


def _make_fake_host(tmp: pathlib.Path, reply=None, host_missing: bool = False) -> str:
    """A throwaway host tree: agent/__init__.py + agent/auxiliary_client.py
    with a canned call_llm (or a MISSING module = the clean no-op case)."""
    host = tmp / "fake_host"
    agent_dir = host / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "__init__.py").write_text("", encoding="utf-8")
    # the on-disk plugin __init__ top-level imports the host memory_provider
    # surface (unguarded, the import-audit allowlist) — the fake host must
    # carry it for the child's `import __init__` (the upgrade/stamp path)
    (agent_dir / "memory_provider.py").write_text(
        "class MemoryProvider:\n"
        "    pass\n"
        "\n"
        "class RecallStatus:\n"
        "    OK = 'ok'\n"
        "    SKIPPED = 'skipped'\n"
        "\n"
        "def is_trivial_prompt(prompt):\n"
        "    return False\n",
        encoding="utf-8")
    if host_missing:
        return str(host)
    reply = reply or {"type": "preference"}
    # bulletproof fake host: the canned LLM reply lands in a sibling JSON
    # file the fake call_llm reads back (no brace-counting in source text).
    (agent_dir / "canned_reply.json").write_text(json.dumps(reply), encoding="utf-8")
    (agent_dir / "auxiliary_client.py").write_text(
        "import json, os\n"
        "_P = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'canned_reply.json')\n"
        "def call_llm(main_runtime=None, messages=None, temperature=None,\n"
        "              max_tokens=None, route_info=None, **kw):\n"
        "    with open(_P, encoding='utf-8') as _f:\n"
        "        _reply = json.load(_f)\n"
        "    return {'choices': [{'message': {'content': json.dumps(_reply)}}]}\n",
        encoding="utf-8")
    return str(host)


def _write_fact_row(p, rid: str = "row-sp-1") -> str:
    """Land a fact row directly in fact.json (zero LLM involvement)."""
    row = {"id": rid, "content": FACT_MISS_CONTENT, "type": "fact",
           "level": "L0", "created_at": _RECENT, "updated_at": _RECENT,
           "metadata": {}}
    shard = p.memories_dir / "fact.json"
    items = json.loads(shard.read_text(encoding="utf-8")) if shard.exists() else []
    items.append(row)
    shard.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return rid


def _payload(p, rid: str, host: str) -> dict:
    return {
        "storage_dir": str(p.storage_dir),
        "shard": "fact",
        "row_id": rid,
        "session_id": p.session_id,
        "source_profile": p.source_profile,
        "content": FACT_MISS_CONTENT,
        "main_runtime": p._main_runtime,
        "host_tree": host,
    }


def _run_entry(payload_text: str, rid: str) -> "subprocess.Popen":
    return subprocess.Popen(
        [sys.executable, str(_ENTRY), "--shard", "fact", "--row", rid],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True, cwd=str(_PLUGIN_ROOT))


# ── 1. real child: the advisory stamp lands through the child process ────
def test_child_process_lands_advisory_stamp(tmp_path):
    p = _make_provider(tmp_path)
    p._reclassify_subprocess = True
    p._reclassify_host_tree = _make_fake_host(tmp_path)
    rid = p._save_memory(FACT_MISS_CONTENT, level="L0",
                         source="confirmed_draft")
    procs = p._reclassify_processes
    assert len(procs) == 1, "the reclassify must run in a child process"
    proc = procs[0]
    assert proc.pid != os.getpid()
    assert proc.wait(timeout=90) == 0, "the child must exit clean"
    shard, row = _row_in(p, rid)
    assert shard == "fact", "single-shard invariant: the row stays put"
    assert row["metadata"].get("llm_suggested_type") == "preference", \
        "the child must advisory-stamp the upgrade verdict"
    log = (p.storage_dir / "consolidation_log.jsonl").read_text(
        encoding="utf-8")
    assert '"bg_reclassify_advisory_stamp"' in log, "the audit row must land"
    assert not getattr(p, "_reclassify_threads", []), \
        "no thread spawn when the child path is taken"


# ── 2a. protocol violation: fail-visible (traceback, exit != 0) ─────────
def test_entry_protocol_violation_is_fail_visible(tmp_path):
    p = _make_provider(tmp_path)
    rid = _write_fact_row(p)
    host = _make_fake_host(tmp_path)
    proc = _run_entry("not-json{{", rid)
    out, err = proc.communicate(input="not-json{{", timeout=90)
    assert proc.returncode != 0, "a protocol violation must be visible"
    assert "Traceback" in err
    assert _row_in(p, rid)[1]["metadata"].get("llm_suggested_type") is None, \
        "the row must stay untouched"


# ── 2b. missing host call surface: clean exit-0 no-op (fail-soft LLM) ───
def test_entry_missing_host_surface_is_clean_noop(tmp_path):
    p = _make_provider(tmp_path)
    rid = _write_fact_row(p)
    host = _make_fake_host(tmp_path, host_missing=True)
    payload = json.dumps(_payload(p, rid, host))
    proc = _run_entry(payload, rid)
    out, err = proc.communicate(input=payload, timeout=90)
    assert proc.returncode == 0, \
        "an unavailable host LLM surface is a no-op, not a crash"
    assert _row_in(p, rid)[1]["metadata"].get("llm_suggested_type") is None


# ── 3. spawn tracked + reaped + cap-8 trim (foreign pids, all exit 0) ───
def test_spawn_tracked_reaped_cap8(tmp_path):
    p = _make_provider(tmp_path)
    p._reclassify_subprocess = True
    p._type_classify_limit = 20  # headroom for 9 spawns (default cap 5)
    p._reclassify_host_tree = _make_fake_host(tmp_path, reply={"type": "fact"})
    rid = _write_fact_row(p)
    for _ in range(9):
        p._spawn_bg_reclassify_advisory("fact", rid, FACT_MISS_CONTENT,
                                        p.session_id)
    assert len(p._reclassify_processes) == 8, "the registry trims at cap 8"
    for proc in p._reclassify_processes:
        assert proc.pid != os.getpid()
        assert proc.wait(timeout=90) == 0
    p._quiesce_bg_reclassify()  # bounded wait, no exception on the healthy path


# ── 4. Popen hiccup -> the thread fallback COMPLETES the work ────────────
def test_spawn_hiccup_thread_fallback_completes(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    fake, state = install(monkeypatch, replies={
        "*": {"type": "preference", "model_used": True}})
    p._reclassify_subprocess = True
    p._reclassify_host_tree = _make_fake_host(tmp_path)

    def _raising_popen(*a, **kw):
        raise RuntimeError("simulated Popen hiccup")

    monkeypatch.setattr(fm.subprocess, "Popen", _raising_popen)
    rid = _write_fact_row(p)
    p._spawn_bg_reclassify_advisory("fact", rid, FACT_MISS_CONTENT, p.session_id)
    assert not getattr(p, "_reclassify_processes", []), \
        "a Popen hiccup must not register a process"
    assert len(p._reclassify_threads) == 1, \
        "the thread fallback must take over (work never lost)"
    p._reclassify_threads[-1].join(timeout=10)
    assert state["calls"] >= 1, (
        "the fallback thread still runs the LLM call (budget was NOT "
        "pre-reserved on a failed spawn)")
    assert _row_in(p, rid)[1]["metadata"].get("llm_suggested_type") == "preference"


# ── 5. flag-off = the pre-2026-11-17 pure-thread path (clean rollback) ───
def test_flag_off_pure_thread_path(tmp_path, monkeypatch):
    p = _make_provider(tmp_path)
    p._reclassify_subprocess = False
    fake, state = install(monkeypatch, replies={
        "*": {"type": "preference", "model_used": True}})
    rid = _write_fact_row(p)
    p._spawn_bg_reclassify_advisory("fact", rid, FACT_MISS_CONTENT, p.session_id)
    assert not getattr(p, "_reclassify_processes", [])
    assert len(p._reclassify_threads) == 1
    p._reclassify_threads[-1].join(timeout=10)
    assert state["calls"] >= 1
    assert _row_in(p, rid)[1]["metadata"].get("llm_suggested_type") == "preference"
