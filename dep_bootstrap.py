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
import os
import sys
import sysconfig


def _find_dep_dirs() -> list[str]:
    """Every candidate site-packages dir to splice, highest priority first:
    1. the FM_DEP_SITES env override ('=' separated, if set) — the only way a
       host that genuinely lacks these packages can point us at the right
       venv without us guessing;
    2. the CURRENT interpreter's own site-packages dir (sysconfig
       "purelib") — derived live, never hardcoded, so it tracks whatever venv
       the running process actually lives in (survives any venv migration /
       Python minor upgrade without a code change). This is the only
       reliable "fallback" — a hardcoded path here (e.g. an old shared dev
       venv's location) would silently rot out the moment that venv moves,
       the same failure mode the kb plugin's 2026-11-11 flaky exposed.

    Existence is checked by the caller (the os.path.isdir guard inside
    ensure_pg_dep_paths) so a missing/rotten candidate dir is silently
    skipped rather than spliced as a dead path."""
    dirs: list[str] = []
    override = os.environ.get("FM_DEP_SITES")
    if override:
        dirs.extend(d for d in override.split("=") if d)
    try:
        dirs.append(sysconfig.get_paths()["purelib"])
    except Exception:
        pass  # sysconfig unavailable (shouldn't happen in any sane host) —
        # nothing sensible to do, just skip this candidate.
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
