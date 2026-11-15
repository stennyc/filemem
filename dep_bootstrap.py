"""Single source of truth for splicing the shared dev-venv site-packages
dir onto sys.path, so filemem's transitive third-party import (psycopg2,
via shared_memory._PgConn) resolves under ANY calling face:

  - host in-process (a gateway loads filemem) -> shared_memory.py calls this
    on import;
  - a bare `import shared_memory` from a standalone interpreter (tests,
    ad-hoc scripts) -> this module is what shared_memory now imports FIRST,
    so the splice happens before psycopg2 is ever actually needed;
  - a future new calling face -> just import dep_bootstrap first (or import
    shared_memory, which already does).

Filemem's copy is deliberately self-contained (NOT an import of
knowledge-base-search/dep_bootstrap.py): the host loads each plugin via its
own spec_from_file_location in isolation — the two plugin dirs do not share
a sys.path, so cross-plugin imports would introduce a new coupling that
breaks the moment the kb plugin is renamed or removed. Same logic, owned
locally, per plugin.

Env override: a single FM_DEP_SITES variable ('='-separated candidate dirs)
supersedes the old FM_PG_SITE name — one knob for every third-party dep
this plugin needs. Supersedes nothing else; FM_PG_SITE has never been read
anywhere but the defining line in shared_memory.py (grep-verified, 2026-11-11)
so the rename carries no compatibility burden.

Rollback-safe: every target dir is appended to sys.path only if it isn't
already there, and every dir is only appended if it actually exists
(os.path.isdir) — a host that already resolves psycopg2 from its own
site-packages gets a no-op.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import sysconfig
from pathlib import Path
import glob


def _resolve_hermes_home() -> str:
    """Resolve HERMES_HOME with cross-platform auto-detection.

    Priority:
      1. ``HERMES_HOME`` env var (set by multiplex gateway per-profile)
      2. Windows: ``%LOCALAPPDATA%\\hermes`` (desktop app default on Win)
      3. Fallback: ``~/.hermes`` (POSIX / legacy)
    """
    hh = os.environ.get("HERMES_HOME", "").strip()
    if hh:
        return hh
    if sys.platform == "win32":
        localapp = os.environ.get("LOCALAPPDATA", "").strip()
        if localapp:
            candidate = os.path.join(localapp, "hermes")
            if os.path.isdir(candidate):
                return candidate
    return os.path.expanduser("~/.hermes")


def _find_hermes_managed_venv(hermes_home: str) -> str | None:
    """Return the venv path stored in Hermes' ``facts.json``.

    Hermes' package manager records the active venv in
    ``$HERMES_HOME/installs/<install_key>/facts.json`` under
    ``packages.venv.environment``.  This is the canonical source of truth —
    the path changes with each Hermes update and must never be hardcoded.

    Returns ``None`` when no facts file is found (fresh install, offline mode).
    """
    installs_dir = os.path.join(hermes_home, "installs")
    if not os.path.isdir(installs_dir):
        return None
    for key_dir in os.listdir(installs_dir):
        facts_path = os.path.join(installs_dir, key_dir, "facts.json")
        if not os.path.isfile(facts_path):
            continue
        try:
            with open(facts_path, encoding="utf-8-sig") as f:
                facts = json.load(f)
            venv = facts.get("packages", {}).get("venv", {}).get("environment")
            if venv and os.path.isdir(venv):
                return venv
        except (json.JSONDecodeError, OSError, KeyError):
            continue
    return None


def _find_dep_dirs() -> list[str]:
    """Every candidate site-packages dir to splice, highest priority first:
    1. the FM_DEP_SITES env override ('=' separated, if set);
    2. Hermes-managed venv from ``facts.json`` — canonical, auto-detected;
    3. the CURRENT interpreter's own site-packages dir (sysconfig
       "purelib") — derived live, never hardcoded, as last resort.
    """
    dirs: list[str] = []
    override = os.environ.get("FM_DEP_SITES")
    if override:
        dirs.extend(d for d in override.split("=") if d)

    hermes_home = _resolve_hermes_home()
    managed = _find_hermes_managed_venv(hermes_home)
    if managed:
        for pattern in [os.path.join(managed, "lib", "python*", "site-packages"),
                        os.path.join(managed, "Lib", "site-packages")]:
            found = glob.glob(pattern)
            dirs.extend(d for d in found if os.path.isdir(d))

    try:
        dirs.append(sysconfig.get_paths()["purelib"])
    except Exception:
        pass
    return dirs


def ensure_pg_dep_paths() -> None:
    """Make psycopg2 importable from the running interpreter. No-op when the
    host already resolves it; never blocks. Supersedes
    shared_memory.py::_ensure_pg_dep_paths (same logic, now shared)."""
    if importlib.util.find_spec("psycopg2"):
        return
    for site in _find_dep_dirs():
        if os.path.isdir(site) and site not in sys.path:
            sys.path.append(site)
