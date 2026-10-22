"""S6 read-side: referral hint (local recall < 2 -> suggest shared_memory_search)
+ shared_memory_search tool (read-only central query, no local write).

Owner rulings (2026-10-22):
- D1 = (b): program only ADDS A HINT LINE (never auto-injects central data);
  the model (or a human) decides whether to call shared_memory_search.
- D2 = "read results stay in THIS turn's tool return only" — never written to
  the local shard, never injected into the auto face, never re-landed locally.
- Threshold is a FIXED 2 (not auto_recall_max).
- Machines without a shared_memory config block get a byte-identical auto face
  (hint returns "") and shared_memory_search returns an "not enabled" error.
"""
import json
import sys
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __init__ import FileMemProvider


def _make_provider(tmp_path: Path) -> FileMemProvider:
    p = FileMemProvider()
    p.storage_dir = tmp_path
    p.memories_dir = tmp_path / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.session_id = "test_session"
    p.source_profile = "test_profile"
    p._decay = {}
    p._turn_marker_index = {}
    p._turn_active_sid = "test_session"
    p._last_save_time = 0.0
    p._save_interval = 0.0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    return p


def _cfg() -> dict:
    return {
        "enabled": True,
        "agent_id": "local",
        "pg": {"host": "127.0.0.1", "port": 5432, "db": "sharedmem",
                "user": "sharedmem", "password": "pw"},
        "token": "tok",
    }


class TestReferralHint:
    def test_hint_empty_when_config_absent(self, tmp_path):
        """No shared_memory cfg -> hint returns "" (byte-identical auto face)."""
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = None
        p._sm_mod = None
        assert p._shared_referral_hint(0) == ""
        assert p._shared_referral_hint(1) == ""
        assert p._shared_referral_hint(5) == ""

    def test_hint_shown_below_threshold_when_enabled(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = mock.MagicMock()
        for n in (0, 1):
            h = p._shared_referral_hint(n)
            assert "shared_memory_search" in h, h
            assert str(n) in h, h
        assert p._shared_referral_hint(2) == ""
        assert p._shared_referral_hint(3) == ""

    def test_hint_must_not_carry_central_content(self, tmp_path):
        """The hint is a pointer only — it must not embed any central row data
        (D1=b: program never auto-injects central data into the auto face)."""
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = mock.MagicMock()
        h = p._shared_referral_hint(0)
        assert "content" not in h.lower() or "content_hash" not in h
        # it names the tool and the count, nothing else


class TestSharedMemorySearchTool:
    def test_not_enabled_returns_error_not_exception(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = None
        p._sm_mod = None
        out = json.loads(p._tool_shared_search({"query": "postgres"}))
        assert out["success"] is False
        assert "not enabled" in out.get("error", "").lower() or "enabled" in out["error"]

    def test_delegates_to_search_central_and_returns_json(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        captured = {}
        def _fake_search(cfg, query, confirmed_only=True, limit=50):
            captured["cfg"] = cfg
            captured["query"] = query
            captured["confirmed_only"] = confirmed_only
            return [{"id": "r1", "content": "row", "agent_id": "remote",
                     "confirmed": True, "updated_at": "2026-10-20T00:00:00Z",
                     "version": 1, "mem_type": "fact", "level": "L0",
                     "source_profile": "remote_profile", "metadata": {}}]
        p._sm_mod = mock.MagicMock()
        p._sm_mod.search_central.side_effect = _fake_search
        out = json.loads(p._tool_shared_search({"query": "postgres", "confirmed_only": True, "limit": 10}))
        assert out["success"] is True
        assert captured["query"] == "postgres"
        assert captured["confirmed_only"] is True
        assert captured["cfg"] is p._shared_mem_cfg  # cfg passed straight through
        assert out["results"][0]["agent_id"] == "remote"

    def test_failed_push_marker_never_in_results(self, tmp_path):
        """search_central must NOT surface the push-only bookkeeping table;
        results are memory_all rows only."""
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = mock.MagicMock()
        p._sm_mod.search_central.return_value = [
            {"id": "a", "content": "x", "agent_id": "b", "confirmed": True,
             "updated_at": None, "version": 1, "mem_type": "fact",
             "level": "L0", "source_profile": None, "metadata": {}}]
        out = json.loads(p._tool_shared_search({"query": "x"}))
        assert out["success"] is True
        assert len(out["results"]) == 1
