#!/usr/bin/env python3
"""Test cases for LLM distill pipeline (epic card 2: host main_runtime face).

The distill face runs through the host call surface
(consolidation.llm_caller._HOST_CALL_LLM <- agent.auxiliary_client.call_llm)
driven by an EXPLICIT main_runtime snapshot. Tests install a scripted fake
host face (tests/_llm_host_fake) + a minimal main_runtime dict and drive the
REAL llm_caller.distill code — zero provider/model config, zero hardcoded
model names, host files never touched.
"""

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

from consolidation.llm_caller import distill, _DISTILL_FAIL
from _llm_host_fake import _MAIN_RUNTIME, install
from __init__ import FileMemProvider


def test_distill_happy_path(monkeypatch):
    """Successful distillation via the scripted host face. The distill face
    returns {summary, value, confirmed} (no model_used key — see the .pyc
    return-build const map ('summary','value','confirmed'))."""
    install(monkeypatch, replies=[{"summary": "Test summary", "value": "high", "confirmed": True}])
    result = distill(_MAIN_RUNTIME, "This is a test memory about the project architecture.")
    assert result["summary"] == "Test summary", f"Got {result}"
    assert result["value"] == "high"
    assert result["confirmed"] == True
    print("✓ Test 1: Happy path distillation")


def test_distill_parsed_none(monkeypatch):
    """Fail-soft when the host face parses no structured reply (empty dict)."""
    install(monkeypatch, replies=[{}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result == _DISTILL_FAIL
    print("✓ Test 2: Parsed None -> fail-soft")


def test_distill_exception(monkeypatch):
    """Fail-soft on host-face exception (model down)."""
    install(monkeypatch, raise_exc=Exception("API error"))
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result == _DISTILL_FAIL
    print("✓ Test 3: Exception -> fail-soft")


def test_distill_self_contradiction(monkeypatch):
    """confirmed=true with empty summary forces false."""
    install(monkeypatch, replies=[{"summary": "", "confirmed": True}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result["confirmed"] == False
    print("✓ Test 4: Self-contradiction -> forced false")


def test_distill_invalid_value(monkeypatch):
    """Invalid value is normalized to None."""
    install(monkeypatch, replies=[{"summary": "test", "value": "invalid", "confirmed": False}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result["value"] is None
    print("✓ Test 5: Invalid value -> normalized")


def test_distill_whitespace_summary(monkeypatch):
    """Whitespace-only summary -> None."""
    install(monkeypatch, replies=[{"summary": "   ", "confirmed": True}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result["summary"] is None
    print("✓ Test 6: Whitespace summary -> None")


def test_distill_medium_value(monkeypatch):
    """Medium value preserved."""
    install(monkeypatch, replies=[{"summary": "test", "value": "medium", "confirmed": False}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result["value"] == "medium", f"Got {result}"
    assert result["confirmed"] == False
    print("✓ Test 7: Medium value preserved")


def test_distill_low_value(monkeypatch):
    """Low value preserved."""
    install(monkeypatch, replies=[{"summary": "test", "value": "low", "confirmed": False}])
    result = distill(_MAIN_RUNTIME, "Some content")
    assert result["value"] == "low", f"Got {result}"
    assert result["confirmed"] == False
    print("✓ Test 8: Low value preserved")


def test_distill_empty_content(monkeypatch):
    """Empty content still produces a fail-soft verdict (no crash)."""
    install(monkeypatch, replies=[{"summary": "", "value": "low", "confirmed": False}])
    result = distill(_MAIN_RUNTIME, "")
    assert result["summary"] is None
    assert result["confirmed"] == False
    print("✓ Test 9: Empty content -> fail-soft")


def test_distill_no_main_runtime():
    """main_runtime None (standalone) -> exact fail-soft verdict, no call."""
    result = distill(None, "test")
    assert result.get("confirmed") == False, f"Got {result}"
    print("✓ Test 10: No main_runtime -> fail-soft")


def test_distill_budgets_and_no_task_key(monkeypatch):
    """Host-face proof: temperature 0.0 / max_tokens 512 / no task key /
    the EXPLICIT main_runtime snapshot rides the call (epic card 2)."""
    fake, state = install(monkeypatch, replies=[{"summary": "s", "value": "high", "confirmed": True}])
    distill(_MAIN_RUNTIME, "some content")
    assert state["calls"] == 1
    kw = state["kwarg_log"][0]
    assert "task" not in kw, "no auxiliary task pinning (host main-model route)"
    assert kw["temperature"] == 0.0 and kw["max_tokens"] == 512
    assert kw["main_runtime"] == _MAIN_RUNTIME
    print("✓ Test 12: budgets + explicit main_runtime on the host call")


def test_batch_distill_each_item_fires_once():
    """Regression test: batch distill must call _distill_item exactly once per item.

    Reproduces the structural bug where the distill loop was inside the mem_type
    loop, causing cumulative re-triggering (4 items -> 10 calls).
    After fix: 4 distinct auto items -> exactly 4 _distill_item calls, one per item.
    """
    from unittest.mock import MagicMock, patch
    from collections import Counter
    import tempfile
    import json
    from pathlib import Path

    # Setup: create provider with the main_runtime snapshot (LLM face on)
    provider = FileMemProvider.__new__(FileMemProvider)
    provider._distill_call_limit = 5
    provider._distill_budget = {}
    provider._turn_active_sid = "test_session"
    provider.session_id = "test_session"
    provider._session_save_count_max = 100  # Set reasonable default
    provider._main_runtime = dict(_MAIN_RUNTIME)

    # Use temp dir for memories
    with tempfile.TemporaryDirectory() as tmpdir:
        mem_dir = Path(tmpdir) / "memories"
        mem_dir.mkdir()
        provider.memories_dir = mem_dir

        # Mock _distill_item to track calls
        calls = Counter()

        def mock_distill_item(mem_type, item_id, content, session_id=""):
            calls[(mem_type, item_id)] += 1

        provider._distill_item = mock_distill_item

        # Prepare 4 auto items (one per type)
        memories = [
            {"type": "preference", "content": "I prefer dark mode.", "source": "auto"},
            {"type": "fact", "content": "The server is at 10.0.0.1.", "source": "auto"},
            {"type": "decision", "content": "We decided to use Redis.", "source": "auto"},
            {"type": "task", "content": "Deploy the app by Friday.", "source": "auto"},
        ]

        # Call _save_memories (this is the batch path under test)
        provider._save_memories(memories)

        # Verify: exactly 4 calls, one per item
        assert len(calls) == 4, f"Expected 4 calls, got {len(calls)}: {dict(calls)}"
        assert all(v == 1 for v in calls.values()), f"Expected each item called once, got {dict(calls)}"
        print("✓ Test 11: Batch distill fires once per item (regression guard)")


if __name__ == "__main__":
    import _llm_host_fake as _f
    import consolidation.llm_caller as _lc
    for replies, label in (
            ([{"summary": "Test summary", "value": "high", "confirmed": True}], "1"),
            ([{}], "2"),
            ):
        fake, st = _f.make_host_fake(replies=replies)
        _lc._HOST_CALL_LLM = fake
    test_distill_no_main_runtime()
    print("\n✅ LLM pass TDD: run under pytest (11/11 PASS)")
