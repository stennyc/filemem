"""conftest: make the real `agent` package importable for this host's test run.

The plugin (`__init__.py`) does `from agent.memory_provider import ...` at module
top. Individual test files insert a hardcoded Linux path
(`/usr/local/lib/hermes-agent`) that does not exist on this host, so 7 test
modules failed to COLLECT with `ModuleNotFoundError: agent` even though their
logic is fine. This conftest runs before any test module imports and puts the
real `agent` package root on `sys.path` when it can find it, so the whole suite
collects. It is fail-soft: if no known location exists, it is a no-op and the
legacy behavior stands (those 7 modules still report the missing-dep error).

Cross-host discovery order (first existing wins, all are appended):
  1. `<hermes-home>/hermes-agent`  — discovered relative to this file: this
     conftest lives at `<hermes-home>/plugins/filemem/tests/`, so the hermes
     home is `Path(__file__).resolve().parents[3]` and the agent root sits
     next to `plugins/` (true on this Windows layout; also the macOS/Linux
     `~/.hermes` layout when hermes-agent is vendored alongside the plugins).
  2. the Linux system path the test files already assume.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_candidates = []
# 1. hermes-home relative (this file is <hermes-home>/plugins/filemem/tests/…)
try:
    _hermes_home = _here.parents[3]
    _candidates.append(_hermes_home / "hermes-agent")
except IndexError:
    pass
# 2. the Linux system install path the individual test files already insert.
_candidates.append(Path("/usr/local/lib/hermes-agent"))

_added = False
for _c in _candidates:
    try:
        if (_c / "agent" / "memory_provider.py").is_file():
            _p = str(_c)
            if _p not in sys.path:
                sys.path.insert(0, _p)
                _added = True
    except Exception:
        # path probing must never break collection of the other tests.
        continue

# ALSO export it into the environment: subprocess-based tests
# (test_s4_lifespan dual-process, test_sec_1_3_audit concurrent-prefetch,
# test_handoff_extract trace-replay, …) spawn a fresh interpreter that does
# NOT inherit a parent-process `sys.path.insert`. Setting PYTHONPATH makes the
# child interpreters resolve `agent` too. Idempotent + fail-soft.
import os as _os
if _added:
    for _p in list(sys.path):
        if (_os.path.isdir(_os.path.join(_p, "agent"))
                and _os.path.isfile(_os.path.join(_p, "agent", "memory_provider.py"))):
            _existing = _os.environ.get("PYTHONPATH", "")
            if _p not in _existing.split(_os.pathsep):
                _os.environ["PYTHONPATH"] = (_p + _os.pathsep + _existing).rstrip(_os.pathsep)
            break
# NOTE: intentionally do NOT import `agent` here — the test modules import it
# after this runs; forcing the import at conftest time would surface a
# missing-dep error for `agent`'s own transitive imports as a conftest failure
# instead of the (more accurate) per-module skip/error.
