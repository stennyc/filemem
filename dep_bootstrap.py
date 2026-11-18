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


# ---------------------------------------------------------------------------
# Gated PG-face auto-install (owner option 2, 2026-11-17)
#
# filemem's CORE face is stdlib-only — zero pip deps. The ONLY third-party
# deps the whole plugin can ever need are the OPTIONAL PG shared-memory face
# (shared_memory.py): psycopg2 (driver) + a yaml parser (ruamel.yaml
# preferred, PyYAML fallback — either satisfies the parse chain). This block
# is the kbs-parity gated auto-installer for exactly that pair:
#
#   - TRIGGER GATE: nothing here runs unless shared_memory._load_shared_memory_config
#     has read the plugins.entries.filemem shared-memory section AND its
#     enabled-gate passed — hosts that never enable PG pay zero cost
#     (no probe, no marker, no pip; owner requirement).
#   - INSTALL TARGET: the RUNNING interpreter (sys.executable -m pip), so the
#     wheels persist in its site-packages and the NEXT boot takes the fast
#     path (kbs 8ac1211 precedent); dep_bootstrap's splice model also keeps
#     resolving deps that live in the Hermes-managed venv.
#   - SPLIT CALLS: one pip invocation per missing package — a psycopg2
#     wheel-build failure must not block pyyaml (kbs first-cut lesson).
#   - 6h COOLDOWN MARKER under $HERMES_HOME (per-plugin, not shared with
#     kbs): an attempt (success OR failure) throttles retries against
#     flapping network.
#   - FAIL-SOFT: the trigger never raises; the local file face is byte-
#     unchanged no matter what the installer does.
#
# 2026-11-18 security retro F3 (supply chain + marker hardening):
#   - _PG_FACE_DEPS carries EXACT == version pins (proven on this host's
#     dev-venv: psycopg2-binary 2.9.13 / pyyaml 6.0.3 on py3.14) — a
#     floating install into the RUNNING gateway interpreter is a standing
#     PyPI supply-chain RCE surface; pins make the fetched artifact
#     auditable (same version the owner's venv already runs).
#   - marker writes are atomic (tmp + os.replace): two interpreters racing
#     the shared marker JSON no longer lose each other's updates.
#   - ok=None (in-flight / killed mid-pip) takes a SHORT retry window
#     (10 min), not the 6h anti-storm window — a killed install must not
#     waste the interpreter for 6h (2026-11-18 live 19:46 run).
#   - marker exe key normalizes python3(.exe) -> python(.exe) in the same
#     dir: one logical interpreter launched under two names must not
#     double-trigger under two marker keys.
# ---------------------------------------------------------------------------
_PG_FACE_DEPS = (("psycopg2", "psycopg2-binary==2.9.13"),
                 ("yaml", "pyyaml==6.0.3"))
_PG_AUTOINSTALL_COOLDOWN_HOURS = 6.0
_PG_INPROGRESS_RETRY_MINUTES = 10.0
_PG_PIP_TIMEOUT_S = 240
_PG_MARKER_NAME = ".filemem_pg_autoinstall.json"


def _pg_exe_key() -> str:
    """Interpreter identity for marker scoping: an install that satisfies ONE
    interpreter must not throttle ANOTHER's — the marker is keyed per
    sys.executable, because pip installs into the running interpreter's own
    site-packages (2026-11-17 live run: a marker written by the dev venv
    blocked the gateway interpreter's own needed install for 6h).

    2026-11-18 F3: normalize the two common exe names of ONE logical
    interpreter (python3.exe/python3 launched alongside python.exe/python in
    the same dir) to a single key — pre-fix they double-triggered."""
    if sys.executable:
        exe = os.path.normcase(os.path.abspath(sys.executable))
        d, b = os.path.dirname(exe), os.path.basename(exe)
        for _i3, _i1 in (("python3.exe", "python.exe"), ("python3", "python")):
            if b == _i3:
                exe = os.path.normcase(os.path.join(d, _i1))
                break
        return exe
    return "unknown"


def _pg_marker_path() -> Path:
    return Path(_resolve_hermes_home()) / _PG_MARKER_NAME


def _pg_missing_probe() -> list:
    """(module, pip_name) pairs whose module is not importable right now.
    find_spec exceptions count as missing (hostile interpreter -> the
    installer gets a chance, fail-soft)."""
    missing = []
    for module, pip_name in _PG_FACE_DEPS:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append((module, pip_name))
        except (ImportError, ValueError, ModuleNotFoundError):
            missing.append((module, pip_name))
    return missing


def _pg_run_pip(pip_name: str, timeout: int = _PG_PIP_TIMEOUT_S):
    """One split pip install into the running interpreter. `pip_name` is a
    pip REQUIREMENT spec (2026-11-18 F3: == pinned, e.g.
    'psycopg2-binary==2.9.13' — the fetched artifact must match the
    audited version). Returns (ok: bool, detail: str); never raises."""
    import subprocess
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", pip_name],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode == 0:
            return True, "ok"
        return False, f"rc={proc.returncode} {proc.stderr.strip()[-200:]}"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _pg_thread_factory(target, name: str):
    """Seam for tests (synchronous runner); production = daemon thread so
    the call face never blocks on pip."""
    import threading
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _pg_read_marker() -> dict:
    """Marker = {exe_key: {"ts", "packages", "ok"}}. Legacy top-level shape
    ({"ts": ...}, pre-per-interpreter scoping) reads back as {} -> its
    shared entry is intentionally dropped (the next attempt rewrites the
    file in the new shape)."""
    try:
        data = json.loads(_pg_marker_path().read_text())
    except Exception:
        return {}
    if not isinstance(data, dict) or "ts" in data:
        return {}
    return data


def _pg_write_marker(ts: float, packages, ok) -> None:
    """Merge under the CURRENT interpreter's key (other interpreters'
    entries survive untouched). 2026-11-18 F3: atomic write (tmp +
    os.replace) — a plain write_text lost updates when two interpreters
    raced the shared marker file mid-write."""
    try:
        data = _pg_read_marker()
        data[_pg_exe_key()] = {"ts": ts, "packages": packages, "ok": ok}
        p = _pg_marker_path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(str(tmp), str(p))
    except Exception:
        pass  # marker is throttling sugar, never fatal


def _pg_entry_cooldown_hours(entry: dict) -> float:
    """Marker cooldown by OUTCOME (2026-11-18 F3): success/failure keep the
    6h anti-storm window; ok=None (in flight / process killed mid-pip)
    retries after the short window instead of wasting the interpreter for
    6h."""
    if entry.get("ok") is None:
        return _PG_INPROGRESS_RETRY_MINUTES / 60.0
    return _PG_AUTOINSTALL_COOLDOWN_HOURS


def ensure_pg_face_deps_installed(cooldown_hours: float | None = None) -> dict:
    """Gated auto-install for the optional PG face. See the section docstring
    above for the gate/cooldown/fail-soft contract. Returns
    {missing, triggered, reason}:
      reason in {'present', 'cooldown', 'installed', 'installing', 'pip_failed'}.
    NEVER raises (the local face must be unaffected by the optional
    installer, whatever it does)."""
    out = {"missing": [], "triggered": False, "reason": "present"}
    try:
        import time
        missing = _pg_missing_probe()
        if not missing:
            return out
        out["missing"] = [m for m, _ in missing]
        entry = _pg_read_marker().get(_pg_exe_key())
        if entry:
            cooldown = _pg_entry_cooldown_hours(entry)
            if cooldown_hours is not None:
                cooldown = min(cooldown, cooldown_hours)
            if time.time() - entry.get("ts", 0.0) < cooldown * 3600:
                out["reason"] = "cooldown"
                return out
        now = time.time()
        # record the attempt time BEFORE starting (anti-storm); the job
        # below rewrites the marker with the outcome
        _pg_write_marker(now, [p for _, p in missing], None)

        def _job():
            oks = []
            for _module, pip_name in missing:
                ok, _detail = _pg_run_pip(pip_name, _PG_PIP_TIMEOUT_S)
                oks.append(ok)
            _pg_write_marker(now, [p for _, p in missing], all(oks))

        _pg_thread_factory(_job, "filemem-pg-deps")
        out["triggered"] = True
        done = _pg_read_marker().get(_pg_exe_key())
        ok = done.get("ok") if done else None
        out["reason"] = ("pip_failed" if ok is False
                         else "installed" if ok is True else "installing")
    except Exception:
        out["triggered"] = False
        out["reason"] = "error"
    return out
