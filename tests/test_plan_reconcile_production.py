#!/usr/bin/env python3
"""PLAN-type PRD §8 — production default on-disk verifier for reconcile.

The reconcile op (slice 3c) uses an injectable `_reconcile_verifiers` seam
(default empty -> every anchored step lands unverifiable). This file tests
the PRODUCTION DEFAULT: when no seam is injected, reconcile falls back to
real on-disk re-checks that actually inspect the repo:
  * git_hash  -> `git cat-file -e <hash>` in source_cwd (+ rebase re-location)
  * file_path -> Path(value).exists() relative to source_cwd
  * test_pass -> shell out to the project's test runner (pytest by default,
    overridable) to re-run the specific test and re-verify it

Locked invariants:
  - production default only engages when NO _reconcile_verifiers seam is set
  - git_hash that exists in the repo -> synced (stays done)
  - git_hash that was REBASED (same patch, new hash) -> re-located, NOT behind
  - git_hash that truly never existed -> behind (roll back to pending)
  - file_path that exists on disk -> synced; missing -> behind (not diverged)
  - test_pass re-runs the test; a still-green test -> synced; a failing test
    -> behind (the claimed done is no longer true in reality)
  - a missing source_cwd / non-repo -> the verifier degrades to unverifiable
    (never fabricates a verdict)
"""
import sys
import json
import subprocess
import pathlib
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from __init__ import FileMemProvider


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True,
                          timeout=10)


def _init_repo(tmp: Path) -> Path:
    """Create a real tmp git repo with one tagged commit + a src file + a test."""
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@t")
    _git(tmp, "config", "user.name", "t")
    (tmp / "src").mkdir()
    (tmp / "src" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "src" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_mod.py").write_text(
        "def test_x():\n    from src.mod import X\n    assert X == 1\n",
        encoding="utf-8")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-q", "-m", "init")
    return tmp


def _provider(tmp: Path, source_cwd: Path = None, profile="coder"):
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp / "store"
    p.memories_dir = p.storage_dir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = p.storage_dir / "sessions"
    p.episodic_dir = p.storage_dir / "episodic"
    p.source_profile = profile
    p.session_id = "sess-1"
    p._last_save_time = 0.0
    p._rate_limited_queue = []
    if source_cwd is not None:
        p.source_cwd = source_cwd
    return p


def _create_and_done(p, pid_tmp, step_text, anchor, repo_tmp):
    out = json.loads(p.handle_tool_call("filemem_plan", {
        "action": "create", "title": "t", "steps": [step_text],
    }))
    pid = out["plan_id"]
    # force the step to done with the given anchor (fixture: skip the
    # continue+anchor op to control the anchor precisely)
    f = p.memories_dir / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for r in rows:
        for s in r["metadata"]["plan_steps"]:
            s["status"] = "done"
            s["anchor"] = anchor
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return pid


def _reconcile(p, pid):
    # NO _reconcile_verifiers seam -> production default engages
    return json.loads(p.handle_tool_call("filemem_plan",
                                         {"action": "advance", "plan_id": pid,
                                          "op": "reconcile"}))


def _step(pid, p):
    f = p.memories_dir / "plan.json"
    rows = json.loads(f.read_text(encoding="utf-8"))
    for r in rows:
        if r["id"] == pid:
            return r["metadata"]["plan_steps"][0]
    raise AssertionError("plan not found")


# ── git_hash: exists -> synced ───────────────────────────────────────────────

def test_git_hash_exists_stays_synced(tmp_path):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    p = _provider(tmp_path, source_cwd=repo)
    pid = _create_and_done(p, tmp_path, "init commit",
                           {"kind": "git_hash", "value": head}, repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    assert s["status"] == "done", s  # synced, no rollback
    assert out.get("behind") == [], out


# ── git_hash: rebase (same patch, new hash) -> re-located, NOT behind ───────

def test_git_hash_rebased_relocated(tmp_path):
    repo = _init_repo(tmp_path)
    old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # amend the commit (a new hash, same patch content) to simulate a rebase
    (repo / "src" / "mod.py").write_text("X = 1  # tweaked\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--amend", "-m", "init")
    new_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert old_head != new_head, "fixture must actually change the hash"

    p = _provider(tmp_path, source_cwd=repo)
    pid = _create_and_done(p, tmp_path, "init commit",
                           {"kind": "git_hash", "value": old_head}, repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    # NOT behind: the work is still in the repo, just under a new hash
    assert s["status"] == "done", s
    assert out.get("behind") == [], out
    # The anchor was refreshed off the orphaned old hash to the live
    # re-located commit. Assert it moved to a different live hash (the new
    # head), and that the refreshed value IS the current HEAD (the active
    # commit that carries the same tree).
    s = _step(pid, p)
    assert s["status"] == "done", s
    assert out.get("behind") == [], out
    live_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert s["anchor"]["value"] == live_head, (s["anchor"], live_head)


# ── git_hash: never existed -> behind ────────────────────────────────────────

def test_git_hash_missing_rolls_back(tmp_path):
    repo = _init_repo(tmp_path)
    p = _provider(tmp_path, source_cwd=repo)
    pid = _create_and_done(p, tmp_path, "phantom commit",
                           {"kind": "git_hash", "value": "deadbeef"}, repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    assert s["status"] == "pending", s  # rolled back
    assert 1 in out.get("behind", []), out


# ── file_path: exists -> synced; missing -> behind ──────────────────────────

def test_file_path_exists_synced(tmp_path):
    repo = _init_repo(tmp_path)
    p = _provider(tmp_path, source_cwd=repo)
    pid = _create_and_done(p, tmp_path, "add module",
                           {"kind": "file_path", "value": "src/mod.py"}, repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    assert _step(pid, p)["status"] == "done"
    assert out.get("behind") == [], out


def test_file_path_missing_rolls_back(tmp_path):
    repo = _init_repo(tmp_path)
    p = _provider(tmp_path, source_cwd=repo)
    pid = _create_and_done(p, tmp_path, "add module",
                           {"kind": "file_path", "value": "src/nope.py"}, repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    assert _step(pid, p)["status"] == "pending"
    assert 1 in out.get("behind", []), out


# ── test_pass: re-run the specific test, green -> synced, failing -> behind ─

def test_test_pass_green_stays_synced(tmp_path):
    repo = _init_repo(tmp_path)
    p = _provider(tmp_path, source_cwd=repo)
    # point the runner at the venv python that ACTUALLY has pytest: the
    # production default `["python", "-m", "pytest"]` resolves via the
    # PATH python, which in this host's venv has no pytest module
    # (pre-existing environment fact, not a reconcile bug).
    import sys as _sys
    p._test_runner = [_sys.executable, "-m", "pytest"]
    pid = _create_and_done(p, tmp_path, "add test",
                          {"kind": "test_pass", "value": "tests/test_mod.py::test_x"},
                          repo)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    assert s["status"] == "done", s  # the test still passes
    assert out.get("behind") == [], out


def test_test_pass_failing_rolls_back(tmp_path):
    repo = _init_repo(tmp_path)
    p = _provider(tmp_path, source_cwd=repo)
    # same sys.executable runner as the green-path sibling: the failure
    # must come from the ACTUAL broken test (X=999 != 1), not from a
    # missing pytest module in the PATH python (false-green on the
    # failing leg).
    import sys as _sys
    p._test_runner = [_sys.executable, "-m", "pytest"]
    pid = _create_and_done(p, tmp_path, "add test",
                          {"kind": "test_pass", "value": "tests/test_mod.py::test_x"},
                          repo)
    # break the module so the test now fails
    (repo / "src" / "mod.py").write_text("X = 999\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "break")
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    assert s["status"] == "pending", s  # claimed done is no longer true
    assert 1 in out.get("behind", []), out


# ── no source_cwd / non-repo -> degrade to unverifiable (never fabricate) ──

def test_no_source_cwd_degrades_unverifiable(tmp_path):
    p = _provider(tmp_path)  # NO source_cwd set
    pid = _create_and_done(p, tmp_path, "a step",
                           {"kind": "git_hash", "value": "abc123"}, tmp_path)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    # no source_cwd -> the git verifier cannot inspect reality; the step is
    # UNVERIFIABLE, not silently rolled back to behind (PRD §8.2: never
    # fabricate a verdict on a host you cannot inspect).
    assert s["status"] == "done", s
    assert 1 in out.get("unverifiable", []), out
    assert out.get("behind") == [], out


def test_non_repo_git_hash_degrades_unverifiable(tmp_path):
    # source_cwd exists but is NOT a git repo -> the git check can't run
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    p = _provider(tmp_path, source_cwd=not_a_repo)
    pid = _create_and_done(p, tmp_path, "a step",
                           {"kind": "git_hash", "value": "abc123"}, tmp_path)
    out = _reconcile(p, pid)
    assert out.get("success") is True, out
    s = _step(pid, p)
    assert s["status"] == "done", s
    assert 1 in out.get("unverifiable", []), out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
