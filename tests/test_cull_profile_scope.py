"""Cull-face ownership closure (t_0411ac0b): per-profile scoping of the
raw_pending cull face (--list / --promote / --restore), the write-side
source_profile backfill, and the one-off --sweep-unstamped quarantine move.

All tests run on a synthetic tmp store or a __new__ provider instance in a
temp dir — live data (/root/.hermes/filemem) is NEVER touched (the live
probe is baseline-only; sweep logic is exercised here on tmp stores).
"""
import importlib.util
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "cleanup_raw_pending.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("cleanup_raw_pending", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli_env(profile=None):
    env = dict(os.environ)
    env.pop("HERMES_PROFILE", None)
    if profile is not None:
        env["HERMES_PROFILE"] = profile
    return env


def _row(topic, content, origin="queue_overflow", reason="synthetic",
         ts="2026-09-01T00:00:00", profile=None):
    r = {"topic": topic, "content": content, "memory_ids": [],
         "origin": origin, "reason": reason, "created_ts": ts}
    if profile is not None:
        r["source_profile"] = profile
    return r


def _build_store(root, pool_rows=None, quarantine_rows=None, auto_rows=None):
    knowledge = root / "knowledge"
    archive = knowledge / "archive"
    quarantine = root / "quarantine"
    knowledge.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    pool = knowledge / "raw_pending.json"
    pool.write_text(json.dumps(pool_rows or [], ensure_ascii=False, indent=2),
                    encoding="utf-8")
    if quarantine_rows:
        (quarantine / "raw_pending_queue_overflow_20260929_213643.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n"
                    for r in quarantine_rows), encoding="utf-8")
    if auto_rows:
        (archive / "raw_pending_auto_20260929T000000Z.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n"
                    for r in auto_rows), encoding="utf-8")
    return pool


def _run(root, *extra, profile=None):
    r = subprocess.run([sys.executable, str(TOOL), "--root", str(root), *extra],
                       capture_output=True, text=True, env=_cli_env(profile))
    return r


def _snap(root):
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ── ① stamped own rows: list/promote pass; foreign rows: hidden/refused ──

def test_list_shows_only_own_profile_rows(tmp_path):
    _build_store(
        tmp_path,
        pool_rows=[_row("t1", "mine", profile="coder")],
        quarantine_rows=[_row("t2", "theirs", profile="orchestrator"),
                         _row("t3", "mine2", profile="coder")],
        auto_rows=[_row("t4", "untouched")],
    )
    r = _run(tmp_path, "--list", profile="coder")
    assert r.returncode == 0
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 2, lines
    assert "mine" in lines[0] and "mine2" in lines[1]
    assert "theirs" not in r.stdout and "untouched" not in r.stdout


def test_list_without_profile_is_global_read(tmp_path):
    _build_store(
        tmp_path,
        pool_rows=[_row("t1", "mine", profile="coder")],
        quarantine_rows=[_row("t2", "theirs", profile="orchestrator")],
        auto_rows=[_row("t3", "unstamped")],
    )
    r = _run(tmp_path, "--list")
    assert r.returncode == 0
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 3, "no profile env = full global read (harmless)"


def test_promote_own_row_passes(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t1", "seed", origin="catch_filter", profile="coder")],
        quarantine_rows=[_row("t2", "mine", profile="coder")],
        auto_rows=[_row("t3", "theirs", profile="orchestrator")],
    )
    # scoped numbering: #1 pool seed, #2 quarantine mine, #3 unaddressable
    r = _run(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    hit = [x for x in rows if x.get("content") == "mine"]
    assert len(hit) == 1 and hit[0]["origin"] == "manual_promote"
    assert hit[0]["source_profile"] == "coder"


def test_promote_foreign_row_refused(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t1", "seed", origin="catch_filter", profile="coder")],
        auto_rows=[_row("t3", "theirs", profile="orchestrator")],
    )
    # the caller's scoped space holds only row #1; a foreign row is
    # simply out of range for this profile
    r = _run(tmp_path, "--promote", "2", profile="coder")
    assert r.returncode == 2
    assert "out of range" in r.stderr
    assert "valid row range: 1..1" in r.stderr
    assert len(json.loads(pool.read_text(encoding="utf-8"))) == 1, \
        "foreign row must not enter the pool"


# ── ② no env: cull actions fail closed ───────────────────────────────────

def test_promote_without_profile_env_fail_closed(tmp_path):
    _build_store(
        tmp_path,
        pool_rows=[_row("t1", "mine", profile="coder")],
        quarantine_rows=[_row("t2", "mine2", profile="coder")],
    )
    before = _snap(tmp_path)
    r = _run(tmp_path, "--promote", "1")
    assert r.returncode == 2
    assert "refused" in r.stderr and "HERMES_PROFILE" in r.stderr
    out = json.loads(r.stdout)
    assert out["error"].startswith("profile identity unrecognized")
    assert _snap(tmp_path) == before, "fail-closed must touch nothing"


def test_restore_without_profile_env_fail_closed(tmp_path):
    _build_store(tmp_path, quarantine_rows=[_row("t2", "x", profile="coder")])
    before = _snap(tmp_path)
    r = _run(tmp_path, "--restore")
    assert r.returncode == 2
    assert "refused" in r.stderr
    assert _snap(tmp_path) == before


def test_restore_only_merges_own_rows(tmp_path):
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter", profile="coder")],
        quarantine_rows=[_row("t2", "mine", profile="coder"),
                         _row("t3", "theirs", profile="orchestrator"),
                         _row("t4", "unstamped")],
    )
    r = _run(tmp_path, "--restore", profile="coder")
    assert r.returncode == 0
    rows = json.loads(pool.read_text(encoding="utf-8"))
    assert {x["content"] for x in rows} == {"seed", "mine"}, \
        "only own stamped rows merge back"
    q = (tmp_path / "quarantine" / "raw_pending_queue_overflow_20260929_213643.jsonl")
    qrows = [json.loads(l) for l in q.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert {x["content"] for x in qrows} == {"mine", "theirs", "unstamped"}


# ── ③ sweep: idempotent, unstamped rows leave, archive file exists ──────

def test_sweep_moves_unstamped_and_is_idempotent(tmp_path):
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("p1", "mine", profile="coder"), _row("p2", "legacy")],
        auto_rows=[_row("a1", "legacy2"),
                   _row("a2", "theirs", profile="orchestrator")],
    )
    r1 = mod.sweep_unstamped(tmp_path)
    assert r1["moved_total"] == 2, r1
    assert r1["moved_per_source"] == {"pool": 1,
                                      "raw_pending_auto_20260929T000000Z.jsonl": 1}
    archive = tmp_path / r1["archive_file"]
    assert archive.name.startswith("raw_pending_unstamped_sweep_")
    swept = [json.loads(l) for l in
             archive.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert {s["content"] for s in swept} == {"legacy", "legacy2"}
    # pool: only the stamped row remains; auto file: only the foreign row
    prow = json.loads(pool.read_text(encoding="utf-8"))
    assert {x["content"] for x in prow} == {"mine"}
    afile = (tmp_path / "knowledge" / "archive"
             / "raw_pending_auto_20260929T000000Z.jsonl")
    arow = [json.loads(l) for l in afile.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert {x["content"] for x in arow} == {"theirs"}
    # audit row appended to the shared consolidation log
    log = [json.loads(l) for l in
           (tmp_path / "consolidation_log.jsonl").read_text(encoding="utf-8")
           .splitlines() if l.strip()]
    assert len(log) == 1 and log[0]["action"] == "sweep_unstamped"
    assert log[0]["moved_total"] == 2
    # idempotent re-run: 0 moved, no second archive, no second audit row
    r2 = mod.sweep_unstamped(tmp_path)
    assert r2["moved_total"] == 0 and r2["archive_file"] is None
    assert (tmp_path / "consolidation_log.jsonl").read_text(
        encoding="utf-8").count('"sweep_unstamped"') == 1
    assert len(list((tmp_path / "knowledge" / "archive")
                    .glob("raw_pending_unstamped_sweep_*.jsonl"))) == 1
    # dry-run on fresh unstamped state: counts without writing anything
    _build_store(tmp_path, pool_rows=[_row("n1", "fresh-legacy")])
    rd = mod.sweep_unstamped(tmp_path, dry_run=True)
    assert rd["dry_run"] is True and rd["would_move"] == 1
    assert {x["content"] for x in
            json.loads(pool.read_text(encoding="utf-8"))} == {"fresh-legacy"}
    assert (tmp_path / "consolidation_log.jsonl").read_text(
        encoding="utf-8").count('"sweep_unstamped"') == 1, "dry-run adds no audit"


def test_sweep_fully_unstamped_auto_file_left_empty_not_unlinked(tmp_path):
    mod = _load_tool()
    _build_store(tmp_path, auto_rows=[_row("a1", "legacy")])
    r = mod.sweep_unstamped(tmp_path)
    assert r["moved_total"] == 1
    afile = (tmp_path / "knowledge" / "archive"
             / "raw_pending_auto_20260929T000000Z.jsonl")
    assert afile.exists() and afile.read_text(encoding="utf-8") == ""


# ── ④ write side: new raw_pending rows carry the stamp ───────────────────

def _new_provider(tmp):
    sys.path.insert(0, str(REPO))
    from __init__ import FileMemProvider  # noqa: E402
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_dir.mkdir(parents=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    return p


def test_new_raw_pending_write_carries_source_profile():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _new_provider(tmp)
        p.source_profile = "coder"
        p._quarantine_raw_pending(topic="rate_limited_overflow_L0",
                                  content="a fresh degraded row",
                                  memory_ids=[], origin="queue_overflow",
                                  reason="cap exceeded")
        rows = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
        assert len(rows) == 1
        assert rows[0]["source_profile"] == "coder"
        assert rows[0]["origin"] == "queue_overflow"


def test_write_side_without_profile_attr_writes_none_stamp():
    """Pre-resolve instances (no source_profile attribute) still degrade:
    the row is written with source_profile=None -> it stays cull-unstamped
    until swept, never a crash at the write point."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        p = _new_provider(tmp)
        p._quarantine_raw_pending(topic="t", content="c", memory_ids=[],
                                  origin="llm_failed_raw", reason="x")
        rows = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
        assert rows[0].get("source_profile") is None


# ── ⑤ grep hard-acceptance (host 0 / pure stdlib / no hardcoded rules) ───

def test_grep_tool_pure_stdlib_no_new_third_party():
    tool_src = TOOL.read_text(encoding="utf-8")
    for bad in ("openai", "anthropic", "requests", "urllib", "httpx",
                "from __init__", "import __init__"):
        assert bad not in tool_src, f"forbidden token {bad!r} in tool"
    allowed = {"argparse", "hashlib", "json", "os", "sys", "datetime", "pathlib",
               "fcntl", "__future__"}
    for line in tool_src.splitlines():
        m = re.match(r"^(?:import|from)\s+(\S+)", line)
        if m:
            top = m.group(1).split(".")[0]
            assert top in allowed, f"non-stdlib import in tool: {line!r}"


def test_grep_write_side_stamp_at_degradation_point_only():
    """The __init__.py delta is the one-line write backfill at the
    degradation write point; nothing else in the plugin changed."""
    sys.path.insert(0, str(REPO))
    from __init__ import FileMemProvider  # noqa: E402
    src = inspect.getsource(FileMemProvider._quarantine_raw_pending)
    assert '"source_profile": getattr(self, "source_profile", None)' in src
    # the tool carries no hardcoded profile names and no hardcoded CJK
    # scoping rules — scoping is a pure equality check on row stamps
    tool_src = TOOL.read_text(encoding="utf-8")
    for name in ("coder", "orchestrator", "researcher", "reviewer"):
        assert name not in tool_src, f"hardcoded profile {name!r} in tool"
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in tool_src), \
        "no hardcoded Chinese rules in the cull face"


# ── ⑥ restore: idempotent + flock + audit (t_335b5093 G1 fix) ──────────

def test_restore_idempotent_second_run_no_duplicate(tmp_path):
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        quarantine_rows=[_row("t2", "mine", profile="coder"),
                         _row("t3", "theirs", profile="orchestrator")],
    )
    r1 = mod.restore(pool, tmp_path / "quarantine", profile="coder")
    assert r1 == {"restored_n": 1, "archive": str(tmp_path / "quarantine"
                                                   / "raw_pending_queue_overflow_20260929_213643.jsonl")}
    assert [x["content"] for x in json.loads(pool.read_text(encoding="utf-8"))] \
        == ["mine"], "first restore inserts own row exactly once"
    log_lines = _restore_log_lines(tmp_path)
    assert len(log_lines) == 1, "exactly one audit row after the only move"
    # second run: the row is already pooled -> deduped by content+topic,
    # zero pool write, zero new audit rows
    r2 = mod.restore(pool, tmp_path / "quarantine", profile="coder")
    assert r2["restored_n"] == 0
    assert [x["content"] for x in json.loads(pool.read_text(encoding="utf-8"))] \
        == ["mine"], "repeated restore must not re-dump the row"
    assert len(_restore_log_lines(tmp_path)) == 1, "no audit row on a zero-move re-run"
    assert mod.restore(pool, tmp_path / "quarantine", profile=None) \
        == {"error": "profile identity unrecognized, cull refused"}


def _restore_log_lines(root):
    p = root / "consolidation_log.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("action") == "restore_quarantine"]


def test_restore_writes_audit_row_with_required_fields(tmp_path):
    mod = _load_tool()
    pool = _build_store(tmp_path, quarantine_rows=[_row("t2", "mine", profile="coder")])
    mod.restore(pool, tmp_path / "quarantine", profile="coder")
    log = _restore_log_lines(tmp_path)
    assert len(log) == 1
    row = log[0]
    assert row["action"] == "restore_quarantine"
    assert row["profile"] == "coder"
    assert row["restored_n"] == 1
    assert row["archive"].endswith("raw_pending_queue_overflow_20260929_213643.jsonl")
    assert row["ts"], "audit row must carry the naive-UTC ts convention"
    # audit is sort_keys + append-only (one json per line, keys ordered)
    p = tmp_path / "consolidation_log.jsonl"
    for l in p.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        r = json.loads(l)
        assert list(r) == sorted(r), f"audit row keys not sorted: {list(r)}"


def test_restore_pool_rmw_inside_flock_no_lost_update(tmp_path):
    """restore's pool read-modify-write must hold the exclusive
    raw_pending.json.lock (inter-PROCESS flock semantics):

    (a) a peer process holding that lock blocks restore (pool unchanged
        until the peer releases — if restore did not take the lock it would
        write immediately, unblocked);
    (b) after release, a peer's RMW row survives the restore (no lost
        update) — the pool re-read happens INSIDE the lock."""
    holder = """
import fcntl, json, sys, time
from pathlib import Path
pool = Path(sys.argv[1]); done = Path(sys.argv[2])
fh = open(str(pool) + ".lock", "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
done.write_text("held", encoding="utf-8")
while not Path(sys.argv[3]).exists():
    time.sleep(0.05)
rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
rows.append({"topic": "t9", "content": "concurrent", "memory_ids": [],
             "origin": "manual_promote", "reason": "synthetic",
             "created_ts": "2026-09-01T00:00:00", "source_profile": "coder"})
import os as _os
_tmp = str(pool) + ".tmp"
with open(_tmp, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
_os.replace(_tmp, str(pool))
fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
done.write_text("released", encoding="utf-8")
"""
    import time as _time
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", origin="catch_filter", profile="coder")],
        quarantine_rows=[_row("t2", "mine", profile="coder")],
    )
    done = tmp_path / "lock_done"
    release = tmp_path / "release_marker"
    holder_pid = subprocess.Popen([sys.executable, "-c", holder,
                                   str(pool), str(done), str(release)],
                                  env=dict(os.environ))
    assert _wait_text(done, "held"), "lock holder never acquired the pool lock"
    # (a) restore must block while the peer holds the exclusive lock
    rows_before = json.loads(pool.read_text(encoding="utf-8"))
    assert [x["content"] for x in rows_before] == ["seed"]
    res = {}

    def _call():
        res["r"] = mod.restore(pool, tmp_path / "quarantine", profile="coder")

    rt = threading.Thread(target=_call, daemon=True)
    rt.start()
    _time.sleep(0.5)
    still = json.loads(pool.read_text(encoding="utf-8"))
    assert [x["content"] for x in still] == ["seed"], \
        "pool written while the peer held the lock -> restore is NOT under flock"
    assert rt.is_alive()
    release.write_text("go", encoding="utf-8")
    rt.join(20)
    assert res["r"]["restored_n"] == 1
    rows = json.loads(pool.read_text(encoding="utf-8"))
    # (b) the peer's RMW row and the restored row coexist: no lost update
    assert {x["content"] for x in rows} == {"seed", "mine", "concurrent"}
    assert len([x for x in rows if x["content"] == "concurrent"]) == 1
    assert len(_restore_log_lines(tmp_path)) == 1
    holder_pid.wait(5)
    assert _wait_text(done, "released")


def _wait_text(p: Path, want: str, timeout: float = 10.0) -> bool:
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if p.exists() and p.read_text(encoding="utf-8") == want:
            return True
        time.sleep(0.05)
    return False


def test_restore_concurrent_runs_serialize_no_duplicate(tmp_path):
    """Three simultaneous restores of the same row: exactly one moves it
    (the others dedupe on re-read inside the flock); the pool ends with
    exactly one copy and one audit row."""
    import threading
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t0", "seed", profile="coder")],
        quarantine_rows=[_row("t2", "mine", profile="coder")],
    )
    results = []
    gate = threading.Lock()

    def _call():
        r = mod.restore(pool, tmp_path / "quarantine", profile="coder")
        with gate:
            results.append(r)

    threads = [threading.Thread(target=_call) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(results) == 3
    assert sum(r["restored_n"] for r in results) == 1, \
        "exactly one concurrent restore actually moved the row"
    rows = json.loads(pool.read_text(encoding="utf-8"))
    assert len([x for x in rows if x["content"] == "mine"]) == 1, \
        "no duplicate under concurrency"
    assert len(_restore_log_lines(tmp_path)) == 1


def test_restore_no_audit_row_on_zero_move_rerun(tmp_path):
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("t2", "mine", profile="coder")],
        quarantine_rows=[_row("t2", "mine", profile="coder")],
    )
    r = mod.restore(pool, tmp_path / "quarantine", profile="coder")
    assert r["restored_n"] == 0, "content already in pool -> deduped, not re-inserted"
    assert _restore_log_lines(tmp_path) == [], "zero-move restore writes no audit row"
    assert [x["content"] for x in json.loads(pool.read_text(encoding="utf-8"))] == ["mine"]


def test_sweep_module_function_contract():
    mod = _load_tool()
    for fn, params in (("list_rows", {"profile"}),
                       ("promote", {"profile"}),
                       ("sweep_unstamped", {"dry_run"}),
                       ("sweep_quarantine_unstamped", {"dry_run", "hard", "confirm"})):
        sig = inspect.signature(getattr(mod, fn))
        assert params <= set(sig.parameters), f"{fn} missing {params}"


# ── ⑦ quarantine-unstamped sweep: retain-trail default + --hard (t_744a74dc) ──
# The live quarantine file (1064 unstamped legacy rows) is NEVER touched:
# every case below runs on the synthetic tmp store built by _build_store.

def _q_file(root):
    return root / "quarantine" / "raw_pending_queue_overflow_20260929_213643.jsonl"


def _q_rows(root):
    p = _q_file(root)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def _q_sweep_logs(root, mode=None):
    p = root / "consolidation_log.jsonl"
    if not p.exists():
        return []
    out = []
    for l in p.read_text(encoding="utf-8").splitlines():
        if not l.strip():
            continue
        r = json.loads(l)
        if r.get("action") == "quarantine_unstamped_sweep" and (
                mode is None or r.get("mode") == mode):
            out.append(r)
    return out


def _manifest_dir(root):
    return list((root / "knowledge" / "archive")
                .glob("raw_pending_quarantine_unstamped_*_archive_manifest.json"))


def test_quarantine_sweep_archive_mode_moves_rows_to_terminal_archive(tmp_path):
    """Retain-trail mode: unstamped rows leave the quarantine files, land in
    the terminal archive (recoverable), the stamped rows stay in place, the
    manifest hashes verify, and the pool is untouched."""
    mod = _load_tool()
    pool = _build_store(
        tmp_path,
        pool_rows=[_row("p0", "pool-seed", origin="catch_filter", profile="coder")],
        quarantine_rows=[_row("q1", "legacy-a"),
                         _row("q2", "mine", profile="coder"),
                         _row("q3", "legacy-b")],
    )
    r = mod.sweep_quarantine_unstamped(tmp_path)
    assert r["moved_total"] == 2 and r["mode"] == "archive", r
    assert r["moved_per_source"] == {"raw_pending_queue_overflow_20260929_213643.jsonl": 2}
    # unstamped rows gone from quarantine; stamped row stays
    qrows = _q_rows(tmp_path)
    assert {x["content"] for x in qrows} == {"mine"}
    # terminal archive: both unstamped rows recoverable, canonical form
    arc = tmp_path / r["archive_file"]
    assert arc.name.startswith("raw_pending_quarantine_unstamped_")
    assert arc.name.endswith(".jsonl")
    swept = [json.loads(l) for l in arc.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    assert {s["content"] for s in swept} == {"legacy-a", "legacy-b"}
    # manifest: per-row sha256 of the canonical row verifies
    m = json.loads((tmp_path / r["manifest"]).read_text(encoding="utf-8"))
    assert m["action"] == "quarantine_unstamped_sweep" and m["moved_n"] == 2
    entries = {e["source_file"]: e for e in m["source_files"]}
    e = entries["quarantine/raw_pending_queue_overflow_20260929_213643.jsonl"]
    assert e["moved_n"] == 2 and len(e["hashes"]) == 2
    for row, h in zip(swept, e["hashes"]):
        assert hashlib.sha256(
            json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest() == h, "manifest hash must match the archived row"
    # audit row: append-only, sorted keys, required fields
    log = _q_sweep_logs(tmp_path)
    assert len(log) == 1
    a = log[0]
    assert a["mode"] == "archive" and a["moved_n"] == 2
    assert a["manifest"] == r["manifest"] and a["ts"]
    assert a["archive_file"] == r["archive_file"]
    p_log = tmp_path / "consolidation_log.jsonl"
    for l in p_log.read_text(encoding="utf-8").splitlines():
        if l.strip():
            rr = json.loads(l)
            assert list(rr) == sorted(rr), f"audit row keys not sorted: {list(rr)}"
    # pool untouched
    assert {x["content"] for x in json.loads(
        pool.read_text(encoding="utf-8"))} == {"pool-seed"}


def test_quarantine_sweep_idempotent_rerun_moves_zero_writes_nothing(tmp_path):
    mod = _load_tool()
    _build_store(tmp_path, quarantine_rows=[_row("q1", "legacy")])
    r1 = mod.sweep_quarantine_unstamped(tmp_path)
    assert r1["moved_total"] == 1
    before = _snap(tmp_path)
    r2 = mod.sweep_quarantine_unstamped(tmp_path)
    assert r2["moved_total"] == 0, r2
    assert r2["archive_file"] is None and r2["manifest"] is None
    assert _q_sweep_logs(tmp_path, mode="archive") and \
        len(_q_sweep_logs(tmp_path)) == 1, "rerun adds no second audit row"
    assert _snap(tmp_path) == before, "zero-move rerun writes nothing"


def test_quarantine_sweep_dry_run_counts_without_writing(tmp_path):
    mod = _load_tool()
    _build_store(tmp_path, quarantine_rows=[_row("q1", "legacy"),
                                            _row("q2", "mine", profile="coder")])
    before = _snap(tmp_path)
    r = mod.sweep_quarantine_unstamped(tmp_path, dry_run=True)
    assert r["dry_run"] is True and r["would_move"] == 1
    assert r["would_move_per_source"] == \
        {"raw_pending_queue_overflow_20260929_213643.jsonl": 1}
    assert _snap(tmp_path) == before, "dry-run writes nothing (no manifest, " \
        "no lock file, no audit)"


def test_quarantine_sweep_hard_deletes_after_manifest_and_keeps_no_archive(tmp_path):
    mod = _load_tool()
    _build_store(
        tmp_path,
        quarantine_rows=[_row("q1", "legacy-a"),
                         _row("q2", "mine", profile="coder"),
                         _row("q3", "legacy-b")],
    )
    preview = mod.sweep_quarantine_unstamped(tmp_path, dry_run=True)
    assert preview["would_move"] == 2
    r = mod.sweep_quarantine_unstamped(tmp_path, hard=True, confirm=2)
    assert r["moved_total"] == 2 and r["mode"] == "hard", r
    # manifest landed FIRST (retain trail even in hard mode)
    m = json.loads((tmp_path / r["manifest"]).read_text(encoding="utf-8"))
    assert m["mode"] == "hard" and m["moved_n"] == 2
    e = m["source_files"][0]
    assert e["moved_n"] == 2 and len(e["hashes"]) == 2
    # rows physically gone from quarantine, stamped row survives
    assert {x["content"] for x in _q_rows(tmp_path)} == {"mine"}
    # NO archive jsonl (manifest only)
    assert r["archive_file"] is None
    assert list((tmp_path / "knowledge" / "archive")
                .glob("raw_pending_quarantine_unstamped_*[!.j]*.jsonl")) == []
    assert not any(f.name.endswith(".jsonl")
                   for f in _manifest_dir(tmp_path))
    # audit row in hard mode
    log = _q_sweep_logs(tmp_path, mode="hard")
    assert len(log) == 1 and log[0]["moved_n"] == 2
    assert "archive_file" not in log[0]


def test_quarantine_sweep_hard_without_confirm_refused(tmp_path):
    mod = _load_tool()
    _build_store(tmp_path, quarantine_rows=[_row("q1", "legacy")])
    before = _snap(tmp_path)
    r = mod.sweep_quarantine_unstamped(tmp_path, hard=True)
    assert "error" in r, r
    assert "--confirm" in r["error"]
    assert _snap(tmp_path) == before, "refused hard run touches nothing"
    # mismatched confirm is refused too
    r2 = mod.sweep_quarantine_unstamped(tmp_path, hard=True, confirm=99)
    assert "error" in r2 and "does not match" in r2["error"]
    assert _snap(tmp_path) == before


def test_quarantine_sweep_zero_unstamped_is_clean_noop(tmp_path):
    """Quarantine present but fully stamped (or absent rows): moved_total=0,
    zero writes — no manifest, no archive, no audit row, no new files."""
    mod = _load_tool()
    _build_store(tmp_path, quarantine_rows=[_row("q1", "mine", profile="coder")])
    before = _snap(tmp_path)
    r = mod.sweep_quarantine_unstamped(tmp_path)
    assert r == {"moved_total": 0, "archive_file": None, "manifest": None,
                 "moved_per_source": {}, "mode": "archive"}, r
    assert _snap(tmp_path) == before
    assert not (tmp_path / "consolidation_log.jsonl").exists()
    assert _manifest_dir(tmp_path) == []
    # hard no-op is equally clean
    r2 = mod.sweep_quarantine_unstamped(tmp_path, hard=True, confirm=0)
    assert r2["moved_total"] == 0 and _snap(tmp_path) == before


def test_quarantine_sweep_cli_archive_and_hard_gates(tmp_path):
    _build_store(tmp_path, quarantine_rows=[_row("q1", "legacy"),
                                            _row("q2", "mine", profile="coder")])
    # archive mode via CLI: works WITHOUT HERMES_PROFILE (global face)
    r = _run(tmp_path, "--sweep-quarantine-unstamped")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["moved_total"] == 1 and out["mode"] == "archive"
    assert {x["content"] for x in _q_rows(tmp_path)} == {"mine"}
    # --hard via CLI without --confirm: exit 2, refused, quarantine intact
    _build_store(tmp_path, quarantine_rows=[_row("q1", "legacy2")])
    r2 = _run(tmp_path, "--sweep-quarantine-unstamped", "--hard")
    assert r2.returncode == 2, r2.stdout + r2.stderr
    assert "refused" in r2.stdout or "refused" in r2.stderr
    assert {x["content"] for x in _q_rows(tmp_path)} == {"legacy2"}
    # --hard with the two-step confirm (count taken from a dry run)
    rd = _run(tmp_path, "--sweep-quarantine-unstamped", "--hard", "--dry-run")
    assert json.loads(rd.stdout)["would_move"] == 1
    r3 = _run(tmp_path, "--sweep-quarantine-unstamped", "--hard", "--confirm", "1")
    assert r3.returncode == 0, r3.stderr
    out3 = json.loads(r3.stdout)
    assert out3["mode"] == "hard" and out3["moved_total"] == 1
    assert not _q_file(tmp_path).exists() or \
        _q_rows(tmp_path) == [], "hard sweep physically removed the unstamped rows"
