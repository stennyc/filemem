"""P1-1b: content-hash idempotent upsert in _save_memory.

The _save_memory path now stamps every NEW row with metadata.content_hash
(sha256 of raw content). On a subsequent save of the same content,
_find_duplicate_by_hash returns the existing row id before the exact-match
or keyword-overlap gates run — the row is updated in place instead of a
duplicate being created.

Legacy rows (without content_hash in metadata) are unaffected — they fall
through to the exact/overlap gates as before.
"""
import json
import tempfile
import os
from pathlib import Path
from unittest import mock

# Import the provider class directly — tests construct it manually.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __init__ import FileMemProvider, MEMORY_TYPES


def _make_provider(tmp_path: Path) -> FileMemProvider:
    """Build a minimal provider wired to tmp_path as storage_dir."""
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
    p._save_interval = 0.0  # no rate limit for tests
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    return p


def test_content_hash_stamped_on_new_row(tmp_path: Path):
    """New saves stamp metadata.content_hash on the row."""
    p = _make_provider(tmp_path)
    # Ensure fact.json exists as empty list
    f = tmp_path / "memories" / "fact.json"
    f.write_text("[]")

    result = p._save_memory("hello world", level="L0", source="manual")
    assert result  # got an id back

    data = json.loads(f.read_text())
    assert len(data) == 1
    md = data[0].get("metadata") or {}
    assert "content_hash" in md, "content_hash must be stamped on new rows"


def test_content_hash_upsert_sameshas_different_id(tmp_path: Path):
    """Same content saved twice → second call returns FIRST id, not a new row."""
    p = _make_provider(tmp_path)
    f = tmp_path / "memories" / "fact.json"
    f.write_text("[]")

    first_id = p._save_memory("same fact content", level="L0", source="manual")
    second_id = p._save_memory("same fact content", level="L0", source="manual")

    assert first_id == second_id, (
        f"content-hash upsert should return existing id; got {first_id!r} vs {second_id!r}"
    )
    data = json.loads(f.read_text())
    assert len(data) == 1, "should be exactly one row after idempotent upsert"


def test_content_hash_does_not_block_unchanged(tmp_path: Path):
    """Different content still creates distinct rows."""
    p = _make_provider(tmp_path)
    f = tmp_path / "memories" / "fact.json"
    f.write_text("[]")

    id1 = p._save_memory("fact alpha", level="L0", source="manual")
    id2 = p._save_memory("fact beta", level="L0", source="manual")

    assert id1 != id2
    data = json.loads(f.read_text())
    assert len(data) == 2


def test_legacy_rows_without_hash_fall_through(tmp_path: Path):
    """Rows without content_hash (pre-P1-1b) still participate in
    the exact-match and keyword-overlap dedup gates — the hash gate is
    fails-open (missing field = no match = continue to next gate)."""
    p = _make_provider(tmp_path)
    f = tmp_path / "memories" / "fact.json"
    # Write a legacy row WITHOUT content_hash
    legacy = [{
        "id": "fact_0_1234567890",
        "type": "fact",
        "content": "legacy fact",
        "level": "L0",
        "source": "auto",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "metadata": {"source_profile": "test_profile"},
    }]
    f.write_text(json.dumps(legacy, ensure_ascii=False))

    # Saving identical content should hit the exact-match gate (not hash gate)
    result = p._save_memory("legacy fact", level="L0", source="manual")
    assert result == "fact_0_1234567890", (
        "legacy row without content_hash should still dedup via exact-match"
    )
    data = json.loads(f.read_text())
    assert len(data) == 1, "should remain a single row"


def test_whitespace_different_content_different_hash(tmp_path: Path):
    """Content that differs in whitespace has a different hash — both rows kept.
    (Exact-match gate will catch truly identical whitespace-normalized strings
    from _find_duplicate_entry, but the hash gate itself is raw-bytes.)"""
    p = _make_provider(tmp_path)
    f = tmp_path / "memories" / "fact.json"
    f.write_text("[]")

    id1 = p._save_memory("hello  world", level="L0", source="manual")  # double space
    id2 = p._save_memory("hello world", level="L0", source="manual")   # single space

    # Different raw content → different hash → both saved
    assert id1 != id2
    data = json.loads(f.read_text())
    assert len(data) == 2, "different raw content should create two rows"


def test_content_hash_not_on_dedup_update(tmp_path: Path):
    """When a row is updated via the dup-path, its existing content_hash
    is NOT overwritten (the update preserves the original row's identity)."""
    p = _make_provider(tmp_path)
    f = tmp_path / "memories" / "fact.json"
    f.write_text("[]")

    first_id = p._save_memory("original content", level="L0", source="manual")
    first_hash = json.loads(f.read_text())[0]["metadata"]["content_hash"]

    # Save same content again → hits content-hash gate, updates in place
    second_id = p._save_memory("original content", level="L0", source="manual")
    assert second_id == first_id

    updated_hash = json.loads(f.read_text())[0]["metadata"]["content_hash"]
    assert updated_hash == first_hash, (
        "content_hash should be preserved on upsert, not recomputed"
    )
