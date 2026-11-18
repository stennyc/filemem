"""Shared OS detection + platform spec helpers for the filemem test suite.

Each test file can do:

    from _os_detect import IS_WINDOWS, IS_POSIX, HAS_FLOCK, LOCK_API,
                          resolve_plugin_dir, resolve_kbs_dir,
                          run_tool_cli, make_noop_shard_lock

Design:
  * No I/O at import time — every helper probes lazily / is pure.
  * Fail-soft: a missing optional surface (kbs dir, flock) degrades to None /
    empty rather than raising, so a single missing surface never breaks the
    whole suite's collection.
"""
import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ── platform constants ───────────────────────────────────────────────────────
IS_WINDOWS: bool = sys.platform == "win32"
IS_POSIX: bool = not IS_WINDOWS
IS_MACOS: bool = sys.platform == "darwin"

# ── lock API ───────────────────────────────────────────────────────────────────
# POSIX  -> fcntl.flock (the real cross-process lock)
# Windows -> msvcrt.locking
LOCK_API: str = "fcntl" if IS_POSIX else "msvcrt"


def has_flock() -> bool:
    """True when a real cross-process file-lock API is available.

    POSIX: fcntl always present (Linux/macOS).
    Windows: msvcrt present (Windows-only stdlib module).
    """
    if IS_WINDOWS:
        try:
            import msvcrt  # noqa: F401
            return True
        except ImportError:
            return False
    # POSIX (Linux / macOS / …): fcntl is in the standard library.
    return True


# ── path helpers ───────────────────────────────────────────────────────────────
_PLUGIN_DIR = Path(__file__).resolve().parent.parent


def resolve_plugin_dir() -> Path:
    """The filemem plugin root (…/plugins/filemem), OS-portable."""
    return _PLUGIN_DIR


def hermes_home() -> Path:
    """Best-effort HERMES_HOME: env > ~/.hermes.

    Used by test fixtures that seed the provider's hermetis_home attribute.
    The test itself overrides via monkeypatch; this is just the default.
    """
    hh = os.environ.get("HERMES_HOME", "").strip()
    if hh:
        return Path(hh)
    return Path.home() / ".hermes"


def resolve_kbs_dir() -> Path | None:
    """Locate the kbs (knowledge-base-search) plugin dir.

    Two name variants exist on disk:
      * ``kbs``                 — the upstream template name
      * ``knowledge-base-search`` — the installed name on this host

    Returns the first one that exists, or None when neither is installed
    (in which case the parity test should skip).
    """
    plugins_dir = _PLUGIN_DIR.parent
    for name in ("kbs", "knowledge-base-search"):
        d = plugins_dir / name
        if d.is_dir():
            return d
    return None


# ── subprocess helpers ─────────────────────────────────────────────────────────
def run_tool_cli(tool: str, store: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a tools/*.py CLI script with the right PYTHONPATH.

    The tools do ``from dep_bootstrap import _resolve_hermes_home`` at module
    top, so the plugin dir must be on the child's PYTHONPATH (parent
    ``sys.path.insert`` does NOT propagate to a subprocess).
    """
    plugin = str(resolve_plugin_dir())
    env = {**os.environ, "PYTHONPATH": plugin + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, str(resolve_plugin_dir() / "tools" / tool),
         "--store", str(store), *args],
        capture_output=True, text=True, env=env,
    )


# ── fixture helpers ────────────────────────────────────────────────────────────
def make_noop_shard_lock():
    """Return a no-op context-manager factory suitable for the fixture shell.

    The real ``_shard_lock`` signature gained a ``timeout`` kwarg (efc7f62).
    Fixtures that stub it with a bare ``lambda pth: …`` break when the
    caller passes ``timeout=…``. This factory absorbs **kwargs.
    """
    def _noop(path, *a, **kw):
        return contextlib.suppress()
    return _noop


# ── conftest auto-inject helper ───────────────────────────────────────────────
def inject_agent_path() -> bool:
    """Add the hermes-agent root to sys.path so ``import agent`` works.

    Call from conftest.py; idempotent, fail-soft (returns False when no
    known location exists — the legacy behavior stands).
    """
    here = Path(__file__).resolve()
    candidates = []
    try:
        home = here.parents[3]
        candidates.append(home / "hermes-agent")
    except IndexError:
        pass
    candidates.append(Path("/usr/local/lib/hermes-agent"))

    added = False
    for c in candidates:
        if (c / "agent" / "memory_provider.py").is_file():
            p = str(c)
            if p not in sys.path:
                sys.path.insert(0, p)
            added = True
    # Propagate to subprocess-based tests.
    if added:
        import os as _os
        for p in list(sys.path):
            if (_os.path.isdir(_os.path.join(p, "agent"))
                    and _os.path.isfile(_os.path.join(p, "agent", "memory_provider.py"))):
                existing = _os.environ.get("PYTHONPATH", "")
                if p not in existing.split(_os.pathsep):
                    _os.environ["PYTHONPATH"] = (p + _os.pathsep + existing).rstrip(_os.pathsep)
                break
    return added
