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

OS-awareness: delegates the discovery to `_os_detect.inject_agent_path()`,
which probes all known roots without assuming a single OS layout.
"""
import sys
from pathlib import Path

# Make the shared OS-detect helpers importable (tests/ is already on sys.path
# at conftest time because pytest adds the rootdir).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _os_detect import inject_agent_path  # noqa: E402

# Run the discovery; fail-soft — if no location is found, sys.path is unchanged
# and the legacy per-module errors stand (documented above).
inject_agent_path()

# NOTE: intentionally do NOT import `agent` here — the test modules import it
# after this runs; forcing the import at conftest time would surface a
# missing-dep error for `agent`'s own transitive imports as a conftest failure
# instead of the (more accurate) per-module skip/error.
