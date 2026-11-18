"""Gated PG-face auto-install (owner option 2, 2026-11-17): filemem's core
face is stdlib-only; the ONLY pip deps are the OPTIONAL PG shared-memory face
(psycopg2 + a yaml parser — ruamel.yaml preferred, PyYAML fallback, either
satisfies shared_memory.py's parse chain). This module adds
`dep_bootstrap.ensure_pg_face_deps_installed()` — a kbs-parity gated
auto-install (missing -> pip into the running interpreter, 6h cooldown
marker, background thread, fail-soft) that is only EVER triggered from
`shared_memory._load_shared_memory_config()` AFTER the enabled-gate has
passed (hosts that never enable PG pay zero cost: no probe, no marker,
no pip — owner's gate requirement).

Acceptance coverage:
1. no missing deps -> immediate no-op: no thread, no marker, no pip
2. missing + no marker -> triggered=True, background install, ONE pip
   call per missing package (split calls: a psycopg2 wheel failure must
   not block pyyaml — kbs 8ac1211 lesson)
3. pip names are the dist names (psycopg2-binary / pyyaml), not module
   names, and the interpreter is sys.executable (persisting site-packages)
4. marker within 6h cooldown -> skip (no pip, reason='cooldown')
5. stale marker -> retry allowed, marker refreshed
6. pip failure (rc != 0) / timeout -> fail-soft: no exception escapes,
   reason='pip_failed', marker still records the attempt (anti-storm)
7. shared_memory hook guard: the per-process flag makes call #2+ a
   no-op (probe called exactly once per process)
"""
import json
import pathlib
import sys

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dep_bootstrap  # noqa: E402

MARKER = ".filemem_pg_autoinstall.json"


def _marker_path(tmp, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp))
    return pathlib.Path(tmp) / MARKER


def _no_missing(monkeypatch):
    monkeypatch.setattr(dep_bootstrap, "_pg_missing_probe", lambda: [])


def _missing(monkeypatch, pip_names):
    # probe returns (module, pip_spec) pairs for the missing subset;
    # specs may carry == pins (2026-11-18 supply-chain hardening) —
    # match on the dist-name prefix so pin changes don't break fixtures.
    all_pairs = [list(p) for p in dep_bootstrap._PG_FACE_DEPS]
    monkeypatch.setattr(
        dep_bootstrap, "_pg_missing_probe",
        lambda: [p for p in all_pairs if p[1].split("==")[0] in pip_names],
    )


@pytest.fixture
def _sync_thread(monkeypatch):
    """Run the install thread synchronously so tests observe the pip calls
    deterministically (the production path spawns a real daemon thread)."""
    calls = []

    def _factory(target, name):
        target()
        calls.append(name)
        return None

    monkeypatch.setattr(dep_bootstrap, "_pg_thread_factory", _factory)
    return calls


def _pip_calls(monkeypatch, record):
    def _run(pip_name, timeout):
        record.append({"pip": pip_name, "timeout": timeout})
        return True, "ok"

    monkeypatch.setattr(dep_bootstrap, "_pg_run_pip", _run)


# --- 1. no missing -> pure no-op -------------------------------------------

def test_no_missing_is_noop(tmp_path, monkeypatch, _sync_thread):
    _no_missing(monkeypatch)
    record = []
    _pip_calls(monkeypatch, record)
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["missing"] == [] and out["triggered"] is False
    assert out["reason"] == "present"
    assert record == [] and _sync_thread == []
    assert not _marker_path(tmp_path, monkeypatch).exists()


# --- 2/3. missing -> split pip calls into the running interpreter ---------

def test_missing_triggers_split_pip_calls(tmp_path, monkeypatch, _sync_thread):
    _marker_path(tmp_path, monkeypatch)  # redirect marker BEFORE the call
    _missing(monkeypatch, ("psycopg2-binary", "pyyaml"))
    record = []
    _pip_calls(monkeypatch, record)
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["missing"] and out["triggered"] is True
    # pinned specs (2026-11-18 F3 supply-chain hardening): dist names + == pins
    specs = [c["pip"] for c in record]
    assert [s.split("==")[0] for s in specs] == ["psycopg2-binary", "pyyaml"]
    assert all("==" in s for s in specs), "pip specs must be == pinned, not floating"
    assert all(c["timeout"] == dep_bootstrap._PG_PIP_TIMEOUT_S for c in record)
    m = json.loads(_marker_path(tmp_path, monkeypatch).read_text())
    assert m[dep_bootstrap._pg_exe_key()].get("ok") is True


# --- 4. cooldown within 6h -> skip ------------------------------------------

def test_fresh_marker_blocks_retry(tmp_path, monkeypatch, _sync_thread):
    _marker_path(tmp_path, monkeypatch)
    _missing(monkeypatch, ("pyyaml",))
    record = []
    _pip_calls(monkeypatch, record)
    mp = _marker_path(tmp_path, monkeypatch)
    mp.write_text(json.dumps({dep_bootstrap._pg_exe_key(): {"ts": 1e18, "ok": True}}))
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is False and out["reason"] == "cooldown"
    assert record == [] and _sync_thread == []


# --- 5. stale marker -> retry -----------------------------------------------

def test_stale_marker_allows_retry(tmp_path, monkeypatch, _sync_thread):
    _marker_path(tmp_path, monkeypatch)
    _missing(monkeypatch, ("pyyaml",))
    record = []
    _pip_calls(monkeypatch, record)
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is True and [c["pip"].split("==")[0] for c in record] == ["pyyaml"]
    mp = _marker_path(tmp_path, monkeypatch)
    mp.write_text(json.dumps({dep_bootstrap._pg_exe_key(): {"ts": 0.0, "ok": False}}))
    record.clear()
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is True and [c["pip"].split("==")[0] for c in record] == ["pyyaml"]


# --- 6. fail-soft on pip failure / timeout -----------------------------------

@pytest.mark.parametrize("detail", ("rc=1", "timeout"))
def test_pip_failure_fail_soft(tmp_path, monkeypatch, _sync_thread, detail):
    _marker_path(tmp_path, monkeypatch)  # redirect marker BEFORE the call
    _missing(monkeypatch, ("psycopg2-binary",))
    record = []

    def _run(pip_name, timeout):
        record.append(pip_name)
        return False, detail

    monkeypatch.setattr(dep_bootstrap, "_pg_run_pip", _run)
    out = dep_bootstrap.ensure_pg_face_deps_installed()  # must not raise
    assert out["triggered"] is True and out["reason"] == "pip_failed"
    m = json.loads(_marker_path(tmp_path, monkeypatch).read_text())
    assert m[dep_bootstrap._pg_exe_key()].get("ok") is False  # attempt recorded


# --- 7b. marker scoped per-interpreter (the 2026-11-17 live-run bug) --------

def test_other_interpreter_marker_does_not_block(tmp_path, monkeypatch, _sync_thread):
    """A fresh marker entry belonging to ANOTHER interpreter must NOT
    throttle this one (pip installs are per-interpreter site-packages)."""
    _marker_path(tmp_path, monkeypatch)
    _missing(monkeypatch, ("pyyaml",))
    record = []
    _pip_calls(monkeypatch, record)
    mp = _marker_path(tmp_path, monkeypatch)
    other_key = "C:\\other\\python.exe"
    assert other_key != dep_bootstrap._pg_exe_key()
    mp.write_text(json.dumps({other_key: {"ts": 1e18, "ok": True}}))
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is True and [c["pip"].split("==")[0] for c in record] == ["pyyaml"]
    m = json.loads(mp.read_text())
    assert other_key in m  # the other interpreter's entry survived the merge


# --- 7. shared_memory hook: per-process guard --------------------------------

def test_shared_memory_hook_fires_once_per_process(tmp_path, monkeypatch):
    import shared_memory
    monkeypatch.setattr(shared_memory, "_PG_AUTOINSTALL_TRIGGERED", False)
    probes = []
    monkeypatch.setattr(
        dep_bootstrap, "ensure_pg_face_deps_installed",
        lambda *a, **k: probes.append(1) or {"missing": [], "triggered": False, "reason": "present"},
    )
    shared_memory._maybe_trigger_pg_face_autoinstall()
    shared_memory._PG_AUTOINSTALL_TRIGGERED = False  # second process-sim
    shared_memory._maybe_trigger_pg_face_autoinstall()
    shared_memory._maybe_trigger_pg_face_autoinstall()  # guarded no-op
    assert probes == [1, 1]


# --- 2026-11-18 security retro F3: supply-chain + marker hardening ----------

def test_f3_inprogress_marker_retries_after_short_window(tmp_path, monkeypatch, _sync_thread):
    """ok=None (install in flight / process killed mid-pip) must NOT burn the
    full 6h anti-storm window: a fresh (in-flight) marker blocks for the
    short in-progress window only, a stale one allows immediate retry.
    (Pre-fix: ok=None took the same 6h cooldown as ok=True — the 19:46 live
    run left the tools interpreter 'wasted' for 6h.)"""
    import time
    _marker_path(tmp_path, monkeypatch)
    _missing(monkeypatch, ("pyyaml",))
    record = []
    _pip_calls(monkeypatch, record)
    mp = _marker_path(tmp_path, monkeypatch)
    mp.write_text(json.dumps({dep_bootstrap._pg_exe_key():
                              {"ts": time.time(), "ok": None}}))
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is False and out["reason"] == "cooldown"
    assert record == []
    # but a stale in-progress marker (process died hours ago) must retry NOW
    mp.write_text(json.dumps({dep_bootstrap._pg_exe_key():
                              {"ts": time.time() - 3 * 3600, "ok": None}}))
    out = dep_bootstrap.ensure_pg_face_deps_installed()
    assert out["triggered"] is True and [c["pip"].split("==")[0] for c in record] == ["pyyaml"]


def test_f3_marker_write_is_atomic(tmp_path, monkeypatch, _sync_thread):
    """The marker must be written tmp + os.replace (no half-written JSON when
    two interpreters race the shared marker file — pre-fix a plain
    write_text lost updates under concurrency)."""
    _marker_path(tmp_path, monkeypatch)
    _missing(monkeypatch, ("pyyaml",))
    record = []
    _pip_calls(monkeypatch, record)
    dep_bootstrap.ensure_pg_face_deps_installed()
    mp = _marker_path(tmp_path, monkeypatch)
    assert mp.exists()
    json.loads(mp.read_text())  # valid JSON
    stragglers = [p.name for p in mp.parent.iterdir() if p.name.endswith(".tmp")]
    assert stragglers == [], f"atomic write must not leave tmp files: {stragglers}"


def test_f3_exe_key_normalizes_python3_to_python(monkeypatch):
    """python3.exe / python3 (same dir) must key to the SAME marker entry as
    python.exe / python — the 2026-11-18 live run double-triggered one logical
    interpreter under two exe names (per-exe marker scoping, pre-normalization)."""
    import os
    base = dep_bootstrap._pg_exe_key()
    assert base  # sanity: real key on this interpreter
    d, b = os.path.dirname(base), os.path.basename(base)
    alt = "python3.exe" if b in ("python.exe", "python") else "python.exe"
    with monkeypatch.context() as ctx:
        ctx.setattr(sys, "executable", os.path.join(d, alt))
        norm = dep_bootstrap._pg_exe_key()
    expected = os.path.normcase(os.path.join(d, "python.exe"))
    assert os.path.normcase(norm) == expected, f"{norm} != {expected}"
