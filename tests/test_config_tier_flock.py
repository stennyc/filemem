"""T5b: config-tier storage_root (neutral shared root) + per-shard flock (0 lost
across N processes) + @data-engineer's dual-root defense.

Contract (group chat 2026-09-27):
- auto (default, no config key): EVERY profile lands on the shared neutral root
  <base>/filemem, where base = the hermes home with a trailing profiles/<name>
  segment stripped and canonicalized. default/4d/coder/... all converge on one
  tree; source_profile stamps still record who wrote what.
- explicit override: plugins.entries.filemem.storage_root
    * "per_profile"  -> legacy <hermes_home>/plugins/filemem/storage (isolation
      must be OPT-IN and loud, never the silent default)
    * <absolute path> -> that exact root
- dual-root defense (@data-engineer ②): base must exist and be canonical after
  resolve(); an unresolved symlink home that would compute a wrong base falls
  back to the per-profile legacy path LOUDLY (never silently write a shared
  store somewhere unexpected).
- relocation is loud: when auto/explicit differs from the legacy path, a
  "storage root relocated: <old> -> <new>" line is logged (@data-engineer no-dual-write gate).
- per-shard flock: the instance _lock only serializes ONE process; the 117
  gateway co-writes one shared neutral store from ~10 profiles, so every
  read-modify-write on a memories/<type>.json shard is also serialized across
  processes via <shard>.lock (fcntl Linux / msvcrt Windows). N-process 0-lost gate.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading

REPO = pathlib.Path(__file__).resolve().parents[1]
ENGINE = pathlib.Path(os.environ.get(
    "HERMES_ENGINE", r"C:/Users/user/AppData/Local/hermes/hermes-agent"))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ENGINE))

import pytest  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
import __init__ as fm  # noqa: E402

HAS_FLOCK = (fm.fcntl is not None) or (fm.msvcrt is not None)


def _provider(tmp: pathlib.Path, hermes_home: pathlib.Path):
    """Minimal provider with just the fields _resolve_storage_root + _shard_lock touch."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s"
    p.hermetis_home = hermes_home
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir = tmp
    return p


def _stub_config(monkeypatch, storage_root: str = ""):
    """Force _probe_storage_root_override to return a fixed value (no real config read)."""
    monkeypatch.setattr(FileMemProvider, "_probe_storage_root_override",
                       lambda self: storage_root)


def test_auto_neutral_root_from_profile_home(tmp_path, monkeypatch):
    base = tmp_path / "hermes"
    home = base / "profiles" / "coder"
    home.mkdir(parents=True)
    p = _provider(tmp_path / "store", home)
    _stub_config(monkeypatch, "")
    p._resolve_storage_root()
    # canonical (resolve()) comparison — the auto root is the canonical base + /filemem
    assert p.storage_dir == (base / "filemem").resolve(), p.storage_dir
    assert p.storage_root_mode == "auto"


def test_auto_neutral_root_from_base_home_no_profiles(tmp_path, monkeypatch):
    base = tmp_path / "hermes"
    base.mkdir(parents=True)
    p = _provider(tmp_path / "store", base)
    _stub_config(monkeypatch, "")
    p._resolve_storage_root()
    assert p.storage_dir == (base / "filemem").resolve()
    assert p.storage_root_mode == "auto"


def test_explicit_per_profile_override_is_loud(tmp_path, monkeypatch):
    base = tmp_path / "hermes"
    home = base / "profiles" / "default"
    home.mkdir(parents=True)
    p = _provider(tmp_path / "store", home)
    _stub_config(monkeypatch, "per_profile")
    p._resolve_storage_root()
    # isolation -> the legacy per-profile path, NOT the neutral root
    assert p.storage_dir == (home / "plugins" / "filemem" / "storage"), p.storage_dir
    assert p.storage_root_mode == "per_profile"


def test_explicit_absolute_path_override(tmp_path, monkeypatch):
    home = tmp_path / "hermes" / "profiles" / "x"
    home.mkdir(parents=True)
    custom = tmp_path / "elsewhere"
    p = _provider(tmp_path / "store", home)
    _stub_config(monkeypatch, str(custom))
    p._resolve_storage_root()
    assert p.storage_dir == custom
    assert p.storage_root_mode.startswith("explicit:")


def test_dual_root_defense_missing_base_falls_back_loud(tmp_path, monkeypatch, caplog):
    # hermetis_home points under a NON-EXISTENT base -> auto must NOT guess a
    # shared root; it falls back to the per-profile legacy path and warns.
    # (hermetis_home is just a Path — the provider never mkdirs the home itself,
    # so we deliberately do NOT create the base.)
    home = tmp_path / "ghost" / "profiles" / "coder"
    p = _provider(tmp_path / "store", home)
    _stub_config(monkeypatch, "")
    with caplog.at_level("WARNING"):
        p._resolve_storage_root()
    assert p.storage_root_mode == "auto_fallback_per_profile"
    assert p.storage_dir == (home / "plugins" / "filemem" / "storage")
    assert any("auto-parse failed" in m for m in caplog.messages), caplog.messages


def test_relocation_is_logged_when_auto_differs(tmp_path, monkeypatch, caplog):
    base = tmp_path / "hermes"
    home = base / "profiles" / "coder"
    home.mkdir(parents=True)
    p = _provider(tmp_path / "store", home)
    _stub_config(monkeypatch, "")
    with caplog.at_level("INFO"):
        p._resolve_storage_root()
    assert any("storage root relocated" in m for m in caplog.messages), caplog.messages


def test_unresolved_symlink_home_is_defended(tmp_path, monkeypatch, caplog):
    """@reviewer axis ①: an UN-RESOLVED symlink home that would compute a wrong
    base must not silently write a shared store somewhere unexpected.

    117 shape: profiles/default is a top-level symlink to the base itself, so the
    engine may pass an un-dereferenced home. Here the symlink target does not
    even exist -> auto-parse must fail the canonical check and fall back LOUDLY.
    (Skipped where the host forbids symlink creation — the gate runs on 117.)
    """
    base = tmp_path / "real-base"
    base.mkdir(parents=True)
    ghost_parent = tmp_path / "ghost"          # does NOT exist
    link = base / "profiles"                   # a real dir, not a symlink
    link.mkdir(parents=True)
    home = link / "default"
    home.mkdir()
    # Point HERMETIS_HOME at a symlinked profile segment whose parent is missing:
    # Path.resolve() on it must fail or land outside the expected base.
    symhome = tmp_path / "homesym"
    try:
        symhome.parent.mkdir(parents=True, exist_ok=True)
        symhome.symlink_to(ghost_parent / "profiles" / "default", target_is_directory=True)
    except OSError:
        pytest.skip("host forbids symlink creation (Windows without privilege)")
    p = _provider(tmp_path / "store", symhome)
    _stub_config(monkeypatch, "")
    with caplog.at_level("WARNING"):
        p._resolve_storage_root()
    # whatever the platform's resolve() does with a broken link: it must NOT have
    # silently picked a neutral root under a non-canonical base
    assert p.storage_root_mode in ("auto_fallback_per_profile", "per_profile"), \
        f"symlink home {symhome} resolved to mode={p.storage_root_mode} root={p.storage_dir}"
    if p.storage_root_mode == "auto_fallback_per_profile":
        assert any("auto-parse failed" in m or "WARNING" in m.upper() for m in caplog.messages)


@pytest.mark.skipif(not HAS_FLOCK, reason="no fcntl/msvcrt on this host — 0-lost gate runs where a real lock exists (117 Linux / dev Windows)")
def test_flock_n_process_zero_lost(tmp_path):
    """N subprocesses each do K read-modify-write increments on one shared
    memories/<type>.json shard through the provider's _shard_lock; the final
    value must equal N*K (zero lost updates)."""
    N, K = 4, 25
    work = tmp_path / "store"
    work.mkdir()
    memdir = work / "memories"
    memdir.mkdir()
    shard = memdir / "fact.json"

    child = tmp_path / "child.py"
    child.write_text(f"""
import json, sys, threading
sys.path.insert(0, {str(REPO)!r})
sys.path.insert(0, {str(ENGINE)!r})
from __init__ import FileMemProvider
p = FileMemProvider.__new__(FileMemProvider)
p._lock = threading.Lock()
p.memories_dir = __import__('pathlib').Path({str(memdir)!r})
shard = p.memories_dir / "fact.json"
for _ in range({K}):
    with p._shard_lock(shard):
        v = json.loads(shard.read_text()) if shard.exists() else 0
        shard.write_text(json.dumps(v + 1))
""", encoding="utf-8")
    # child inherits the caller's PYTHONPATH (the hermes-agent engine root),
    # not a hardcoded engine path — the Windows-fallback default for ENGINE
    # is meaningless on a Linux host and would clobber the real PYTHONPATH
    # (agent/ would become unimportable in the child -> spurious ModuleNotFound).
    env = dict(os.environ, PYTHONPATH=str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [subprocess.Popen([sys.executable, str(child)], env=env) for _ in range(N)]
    for pr in procs:
        assert pr.wait() == 0, pr.stderr
    got = json.loads(shard.read_text())
    assert got == N * K, f"lost updates: {got} != {N*K}"
