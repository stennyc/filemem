"""Live main_runtime guarantee for all filemem LLM faces.

Proves the 100%-callable requirement: in a live host turn the contextvar
_runtime is always published, so _live_main_runtime() resolves a usable
snapshot for EVERY LLM face at execution time — no reliance on a lucky
tick rebind. Only a standalone interpreter (host helper absent / contextvar
empty) returns the module fallback or None, which is the single legitimate
skip path (no host to call).

Zero external deps; runs under the filemem runtime python.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import __init__ as fmm
from __init__ import FileMemProvider


@pytest.fixture(autouse=True)
def _isolate_module_main_runtime():
    """The real (a)-path of _live_main_runtime() PERSISTS a live snapshot
    into the module-level _HOST_MAIN_RUNTIME global (by design: a binder
    success benefits every other instance in this process). That write
    must never leak OUT of this test file into later test files — every
    "no runtime / standalone" assumption in test_multilingual_detect_type,
    test_recurring_dedupe, test_s1b_tick_wiring is predicated on that
    global being None between modules. Snapshot + restore it (and the
    per-home dict) around every test in this file, so collection order
    can never turn these tests into a global-state source for the rest
    of the suite. (The mock.patch.object(..., create=True) usages below
    already restore the SCALAR their with-block entered; what they cannot
    protect is a write made BY THE CODE UNDER TEST, which is what this
    fixture covers.)"""
    saved_scalar = fmm._HOST_MAIN_RUNTIME
    saved_by_home = fmm._HOST_MAIN_RUNTIME_BY_HOME
    yield
    fmm._HOST_MAIN_RUNTIME = saved_scalar
    fmm._HOST_MAIN_RUNTIME_BY_HOME = saved_by_home


def _mk():
    p = FileMemProvider.__new__(FileMemProvider)
    p._main_runtime = None
    return p


def test_live_read_success_writes_back_and_returns():
    """In a live turn the helper returns a snapshot -> 100% usable + written
    back to self._main_runtime so downstream llm.X(self._main_runtime, ...)
    sees it without re-resolving."""
    p = _mk()
    snap = {"model": "agnes-3.0-flash", "provider": "custom"}
    with mock.patch.object(fmm, "_normalize_main_runtime", return_value=snap):
        got = p._live_main_runtime()
    assert got == snap, "live read must return the published snapshot"
    assert p._main_runtime == snap, "live snapshot must be written back to the instance"


def test_no_clobber_when_already_bound():
    """A non-None bound value is returned as-is without calling the helper
    (fast path; never clobber a live snapshot with a stale read)."""
    p = _mk()
    p._main_runtime = {"model": "m", "provider": "custom"}
    with mock.patch.object(fmm, "_normalize_main_runtime",
                           side_effect=AssertionError("must not read live when bound")):
        got = p._live_main_runtime()
    assert got == {"model": "m", "provider": "custom"}


def test_module_fallback_when_contextvar_empty():
    """Helper returns None (out-of-turn / boot capture) but a module-level
    snapshot was captured at register() -> still usable, not a skip."""
    p = _mk()
    mod_snap = {"model": "mb", "provider": "custom"}
    with mock.patch.object(fmm, "_normalize_main_runtime", return_value=None), \
         mock.patch.object(fmm, "_HOST_MAIN_RUNTIME", mod_snap, create=True):
        got = p._live_main_runtime()
    assert got == mod_snap, "module fallback must be returned when live read is empty"


def test_fail_soft_none_only_in_standalone():
    """Helper absent (standalone) AND no module snapshot -> None; the LLM face
    gate then fires its existing `main_runtime missing` skip. The ONLY
    legitimate skip path — a live gateway turn never reaches this."""
    p = _mk()
    with mock.patch.object(fmm, "_normalize_main_runtime", None), \
         mock.patch.object(fmm, "_HOST_MAIN_RUNTIME", None, create=True):
        assert p._live_main_runtime() is None
        assert p._main_runtime is None


def test_helper_hiccup_fail_soft_no_crash():
    """A helper exception (old host build mid-upgrade) must not crash the
    LLM face — fall through to module fallback or None."""
    p = _mk()
    with mock.patch.object(fmm, "_normalize_main_runtime",
                           side_effect=RuntimeError("host hiccup")), \
         mock.patch.object(fmm, "_HOST_MAIN_RUNTIME", None, create=True):
        assert p._live_main_runtime() is None


def test_11_gate_sites_are_wired():
    """All LLM-face gate sites read through _live_main_runtime (not the
    raw getattr), so the live-read guarantee covers every face. Static
    check against the source (no execution of the faces themselves). The
    expected count tracks the registered face count (15 as of draft-refine
    face, 2026-11-08: the original 11 + handoff_extract gate + pin->tail
    summary gate + plan-probe LLM gate + draft-refine gate) — bump it in
    lock-step when a new LLM face lands."""
    src = (Path(__file__).resolve().parents[1] / "__init__.py").read_text()
    # 15 call sites (incl. handoff face 1 + pin-tail face 3 + plan-probe +
    # draft-refine face 1, 2026-11-08) + 1 method definition = 16 occurrences
    # (bump in lock-step when a new LLM face lands)
    assert src.count("self._live_main_runtime()") == 15, \
        f"expected 15 gate call sites, found {src.count('self._live_main_runtime()')}"
    assert src.count("def _live_main_runtime(") == 1
    # No bare main_runtime gate may remain on the LLM-face path
    assert 'if not getattr(self, "_main_runtime", None):' not in src, \
        "a bare main_runtime gate still bypasses the live-read guarantee"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
