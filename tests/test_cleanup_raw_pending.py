"""Path B: raw_pending recycle-bin human face (--list / --promote) tests.

Blueprint: specs/RAWPENDING-recycle-bin-impl-notes.md §测试面 (10 items).
All tests run on a synthetic tmp_path store — live data
(/root/.hermes/filemem) is NEVER touched. The tool is imported directly
(pure stdlib, zero plugin __init__, zero LLM); CLI behaviors (exit codes,
stderr) go through subprocess.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest  # noqa: F401

TOOL = Path(__file__).resolve().parents[1] / "tools" / "cleanup_raw_pending.py"

try:
    import fcntl as _fcntl  # noqa: F401
    HAS_FLOCK = True
except ImportError:
    HAS_FLOCK = False


def _cli_env(profile=None):
    """CLI test env for the cull face. The ownership closure reads the
    caller's profile from HERMES_PROFILE: profile=None runs the pre-closure
    env (list = global read; cull actions fail closed), profile=<name>
    scoping tests mirror the row stamps built by _row()."""
    env = dict(os.environ)
    env.pop("HERMES_PROFILE", None)
    if profile is not None:
        env["HERMES_PROFILE"] = profile
    return env


def _load_tool():
    spec = importlib.util.spec_from_file_location("cleanup_raw_pending", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_cli(root, *extra, profile=None):
    return subprocess.run(
        [sys.executable, str(TOOL), "--root", str(root), *extra],
        capture_output=True, text=True, env=_cli_env(profile))


def _row(topic, content, origin="queue_overflow", reason="synthetic",
         ts="2026-09-01T00:00:00", profile="coder"):
    return {"topic": topic, "content": content, "memory_ids": [],
            "origin": origin, "reason": reason, "created_ts": ts,
            "source_profile": profile}


def _build_store(root, pool_rows=None, quarantine_rows=None, auto_rows=None,
                 corrupt_quarantine=False):
    """Synthetic store: knowledge/raw_pending.json (+lock), quarantine/,
    knowledge/archive/ — mirrors real layout, all under tmp_path."""
    knowledge = root / "knowledge"
    archive = knowledge / "archive"
    quarantine = root / "quarantine"
    knowledge.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    pool = knowledge / "raw_pending.json"
    pool.write_text(json.dumps(pool_rows or [], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    qfile = quarantine / "raw_pending_queue_overflow_20260929_213643.jsonl"
    if corrupt_quarantine:
        qfile.write_text("not json {{{{", encoding="utf-8")
    elif quarantine_rows:
        qfile.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n"
                    for r in quarantine_rows), encoding="utf-8")
    if auto_rows:
        (archive / "raw_pending_auto_20260929_213643.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n"
                    for r in auto_rows), encoding="utf-8")
    return pool


def _all_bytes(root):
    """Byte snapshot of every regular file under root (for no-mutation checks)."""
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _audit_files(root):
    return sorted((root / "knowledge" / "archive").glob("manual_promote_*.jsonl"))


def _audit_rows(root):
    out = []
    for f in _audit_files(root):
        out.extend(json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip())
    return out


# ── 1. --list enumerates all three sources ───────────────────────────────

def test_list_enumerates_all_three_sources(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t1", "pool fact", origin="catch_filter", ts="2026-09-02T00:00:00")],
        quarantine_rows=[_row("t2", "quar fact", ts="2026-09-01T00:00:00")],
        auto_rows=[_row("t3", "auto fact", ts="2026-08-30T00:00:00")],
    )
    before = _all_bytes(tmp_path)
    r = _run_cli(tmp_path, "--list")
    assert r.returncode == 0
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 3
    assert lines[0].startswith("#1  [pool]") and "origin=catch_filter" in lines[0]
    assert lines[1].startswith("#2  [quarantine]") and "origin=queue_overflow" in lines[1]
    assert lines[2].startswith("#3  [auto-archive]") and "origin=queue_overflow" in lines[2]
    # source order pool -> quarantine -> auto-archive; within-source created_ts asc
    assert "pool fact" in lines[0] and "quar fact" in lines[1] and "auto fact" in lines[2]
    assert _all_bytes(tmp_path) == before, "--list must be a pure read"


# ── 2. corrupt source skipped, not fatal ─────────────────────────────────

def test_list_corrupt_source_skipped_not_fatal(tmp_path):
    _build_store(
        tmp_path,
        pool_rows=[_row("t1", "pool fact")],
        auto_rows=[_row("t3", "auto fact")],
        corrupt_quarantine=True,
    )
    r = _run_cli(tmp_path, "--list")
    assert r.returncode == 0, "corrupt source must not be fatal"
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 2
    assert "pool fact" in lines[0] and "auto fact" in lines[1]
    assert "warning" in r.stderr.lower() and "skipping" in r.stderr.lower()


# ── 3. promote from quarantine ───────────────────────────────────────────

def test_promote_from_quarantine(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter")],
        quarantine_rows=[_row("t2", "quar fact", reason="overflow", ts="2026-09-05T01:02:03")],
    )
    r = _run_cli(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    hit = [x for x in rows if x.get("content") == "quar fact" and x.get("topic") == "t2"]
    assert len(hit) == 1
    assert hit[0]["origin"] == "manual_promote"
    assert len(rows) == 2, "pool gains exactly the promoted row"
    aud = _audit_rows(tmp_path)
    assert len(aud) == 1
    a = aud[0]
    assert a["row"] == 2 and a["source"] == "quarantine"
    assert a["origin"] == "queue_overflow"          # pre-promote origin recorded
    assert a["created_ts"] == "2026-09-05T01:02:03"
    assert "promoted_ts" in a


# ── 4. promote from auto-archive ─────────────────────────────────────────

def test_promote_from_auto_archive(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter")],
        auto_rows=[_row("t3", "auto fact", ts="2026-08-30T00:00:00")],
    )
    r = _run_cli(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    hit = [x for x in rows if x.get("content") == "auto fact"]
    assert len(hit) == 1 and hit[0]["origin"] == "manual_promote"
    aud = _audit_rows(tmp_path)
    assert len(aud) == 1 and aud[0]["source"] == "auto-archive" and aud[0]["row"] == 2


# ── 5. pool row: mark only, no duplicate ─────────────────────────────────

def test_promote_pool_row_marks_not_dup(tmp_path):
    pool = _build_store(tmp_path, pool_rows=[_row("t1", "pool fact",
                                                   origin="catch_filter")])
    before_len = 1
    r = _run_cli(tmp_path, "--promote", "1", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    assert len(rows) == before_len, "pool row must not be duplicated"
    assert rows[0]["origin"] == "manual_promote"
    assert _audit_rows(tmp_path)[0]["source"] == "pool"


# ── 6. idempotent on content+topic ───────────────────────────────────────

def test_promote_idempotent_content_topic(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t2", "quar fact", origin="catch_filter",
                        ts="2026-09-09T00:00:00")],
        quarantine_rows=[_row("t2", "quar fact", ts="2026-09-05T00:00:00")],
    )
    assert len(json.loads(pool.read_text(encoding="utf-8"))) == 1
    r = _run_cli(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    assert len(rows) == 1, "already-in-pool content+topic must not be re-inserted"
    hit = [x for x in rows if x.get("content") == "quar fact" and x.get("topic") == "t2"]
    assert hit and hit[0]["origin"] == "manual_promote"
    assert _audit_rows(tmp_path)[0]["row"] == 2


# ── 7. out-of-range row -> exit 2, files untouched ──────────────────────

def test_promote_missing_row_exit2(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t1", "pool fact")],
        quarantine_rows=[_row("t2", "quar fact")],
    )
    before = _all_bytes(tmp_path)
    r = _run_cli(tmp_path, "--promote", "999", profile="coder")
    assert r.returncode == 2
    assert "out of range" in r.stderr and "1..2" in r.stderr
    assert _all_bytes(tmp_path) == before, "out-of-range must not touch any file"
    # negative row is also out of range
    r2 = _run_cli(tmp_path, "--promote", "0", profile="coder")
    assert r2.returncode == 2
    assert _all_bytes(tmp_path) == before


# ── 8. --dry-run promotes nothing ─────────────────────────────────────────

def test_promote_dry_run_no_mutation(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t1", "pool fact")],
        quarantine_rows=[_row("t2", "quar fact")],
    )
    before = _all_bytes(tmp_path)
    r = _run_cli(tmp_path, "--promote", "2", "--dry-run", profile="coder")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["dry_run"] is True and out["row"] == 2 and out["source"] == "quarantine"
    assert out["row_data"]["origin"] == "manual_promote"
    assert len(json.loads(pool.read_text(encoding="utf-8"))) == 1, "pool unchanged"
    assert _audit_files(tmp_path) == [], "dry-run must not write audit"
    after = dict(_all_bytes(tmp_path))
    after[pool.as_posix()] = pool.read_bytes()
    assert after == before, "dry-run must not mutate any file"


# ── 9. only origin changes; reason/created_ts preserved ──────────────────

def test_promote_respects_origin_preserved_reason_created_ts(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter")],
        quarantine_rows=[_row("t2", "quar fact", origin="queue_overflow",
                               reason="overflow 9f13", ts="2026-09-05T01:02:03")],
    )
    r = _run_cli(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 0
    hit = json.loads(pool.read_text(encoding="utf-8"))
    p = [x for x in hit if x.get("content") == "quar fact"][0]
    assert p["origin"] == "manual_promote", "origin is the only field rewritten"
    assert p["reason"] == "overflow 9f13", "reason preserved verbatim"
    assert p["created_ts"] == "2026-09-05T01:02:03", "created_ts preserved"
    assert p["topic"] == "t2"


# ── 10. flock serializes concurrent pool RMW ─────────────────────────────

def test_lock_serializes_rmw(tmp_path):
    if not HAS_FLOCK:
        pytest.skip("fcntl/flock unavailable (non-POSIX environment)")
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter")],
        quarantine_rows=[_row("q1", "quar fact A"), _row("q2", "quar fact B")],
    )
    # Deterministic no-lost-update proof: BOTH processes promote the SAME
    # global row (#2 = quarantine "quar fact A"). Same row => the pre-lock
    # list_rows enumeration resolves identically in both processes, so the
    # only thing the exclusive flock protects is the pool read-modify-write.
    # Without it, two "insert A" writes race and one is lost (pool still
    # missing A, or two A's if the mark was lost). With it, both serialize:
    # final pool has exactly ONE "quar fact A", both audit rows written.
    #
    # NOTE on scope: the same-row case is the deterministic lock guarantee.
    # A *different*-row concurrency (--promote 2 vs 3) is NOT stable, because
    # the pool is source #1 in the global row space: process A's in-pool
    # insert re-numbers the archive rows, so process B's pre-emptively-read
    # row #3 can resolve to a different target than intended. That is a
    # property of pool-first enumeration, not of the lock; it is out of scope
    # for this test (which asserts the lock's no-lost-update on a shared RMW).
    p1 = subprocess.Popen([sys.executable, str(TOOL), "--root", str(tmp_path),
                           "--promote", "2"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          env=_cli_env("coder"))
    p2 = subprocess.Popen([sys.executable, str(TOOL), "--root", str(tmp_path),
                           "--promote", "2"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True,
                          env=_cli_env("coder"))
    assert p1.wait() == 0 and p2.wait() == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    a_rows = [r for r in rows if r.get("content") == "quar fact A"]
    assert len(a_rows) == 1, f"lost/dup update: expected exactly one A, got {len(a_rows)}"
    assert a_rows[0]["origin"] == "manual_promote"
    # only row #2 was promoted -> B stays in quarantine; pool = {seed, A}
    assert {r["content"] for r in rows} == {"seed", "quar fact A"}
    aud = _audit_rows(tmp_path)
    assert len(aud) == 2, "both serialised RMWs must append an audit row"
    assert all(a["row"] == 2 for a in aud)
    # pool file is valid JSON after the concurrent RMW (no torn write)
    json.loads(pool.read_text(encoding="utf-8"))


# ── tool-level: module is pure stdlib, no plugin imports (grep-equivalent) ─

def test_tool_module_pure_stdlib_no_plugin_imports():
    src = TOOL.read_text(encoding="utf-8")
    for bad in ("openai", "anthropic", "requests", "urllib", "httpx",
                "from __init__", "import __init__"):
        assert bad not in src, f"forbidden token {bad!r} found in tool"
    mod = _load_tool()
    assert hasattr(mod, "list_rows") and hasattr(mod, "promote")
