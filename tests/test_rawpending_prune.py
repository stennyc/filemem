"""EP-2 prune layer — FIFO retention sweep over the C-slice raw_pending
auto-archive (knowledge/archive/raw_pending_auto_*.jsonl).

Blueprint: specs/RAWPENDING-recycle-bin-impl-notes.md §4 『prune 层』
(6 test items). All tests run on a synthetic tmp_path store — live data
(/root/.hermes/filemem) is NEVER touched. The prune face is pure stdlib
(os/json/hashlib/datetime), zero LLM, zero third-party SDK; it runs even
when _llm_ctx is None. The real _auto_prune_raw_pending_archive host path
runs (unpatched) in the behavioral tests; test 5 mirrors
test_consolidation_boundary_mount_order_and_failsoft_isolation.
"""
import hashlib
import json
import pathlib
import threading
from datetime import datetime, timedelta, timezone

import pytest  # noqa: F401

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
_TS = "%Y%m%dT%H%M%SZ"


def _naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_provider(tmpdir: pathlib.Path, knowledge_cfg: dict):
    """Partial provider (test_ep_2_observe shape, no LLM) with only the
    attributes the prune face reads: _decay, knowledge_archive_dir, _lock,
    and the bound _shard_lock. _llm_ctx is left unset (None) to prove the
    prune is decoupled from the LLM face."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_archive_dir = p.knowledge_dir / "archive"
    p.knowledge_archive_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p._decay = {"knowledge": knowledge_cfg}
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir = tmpdir
    return p


def _write_archive(p, ts: datetime, rows: list) -> pathlib.Path:
    """Write one C-slice archive file raw_pending_auto_<ts>.jsonl."""
    f = p.knowledge_archive_dir / f"raw_pending_auto_{ts.strftime(_TS)}.jsonl"
    with open(f, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return f


def _manifest_rows(p) -> list:
    f = p.knowledge_archive_dir / "pruned_index.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


def _row(content, topic="t", origin="queue_overflow", created_ts="2026-09-01T00:00:00"):
    return {"topic": topic, "content": content, "memory_ids": [],
            "origin": origin, "reason": "synthetic", "created_ts": created_ts}


# ── 1. stale file pruned + manifest written, recent kept, return = count ──

def test_prune_deletes_stale_writes_manifest(tmp_path):
    p = _make_provider(tmp_path, {"archive_prune_days": 90})
    now = _naive_utc()
    old_ts = now - timedelta(days=100)     # older than now-90d -> pruned
    new_ts = now - timedelta(days=5)       # inside the 90d window -> kept
    old_file = _write_archive(p, old_ts, [
        _row("old body 1", "alpha", created_ts="2020-01-01T00:00:00"),
        _row("old body 2", "beta",  created_ts="2020-01-02T00:00:00")])
    new_file = _write_archive(p, new_ts, [_row("new body", "gamma")])
    assert old_file.exists() and new_file.exists()

    n = p._auto_prune_raw_pending_archive()
    assert n == 1, f"exactly the 1 stale file pruned, got {n}"
    assert not old_file.exists(), "stale archive file must be unlinked"
    assert new_file.exists(), "recent archive file (inside 90d) must be kept"

    man = _manifest_rows(p)
    assert len(man) == 2, "every row of the pruned file is manifest-appended"
    for m in man:
        for field in ("origin", "topic", "created_ts", "content_sha1",
                      "char_len", "pruned_ts", "source_file"):
            assert field in m, f"manifest row missing {field}: {m}"
        assert m["source_file"] == old_file.name
    # content_sha1 / char_len reflect the pruned body (NOT the whole row)
    by_content = {json.dumps(m, sort_keys=True): m for m in man}
    sha1 = [m for m in man if m["topic"] == "alpha"]
    assert len(sha1) == 1
    a = sha1[0]
    assert a["content_sha1"] == hashlib.sha1("old body 1".encode()).hexdigest()
    assert a["char_len"] == len("old body 1")
    assert a["origin"] == "queue_overflow"


# ── 2. disabled when 0 / missing key / missing block ────────────────────

def test_prune_disabled_when_zero_or_missing(tmp_path):
    now = _naive_utc()
    old_ts = now - timedelta(days=100)
    stale = [
        _row("s1", "a", created_ts="2020-01-01T00:00:00")]

    # (a) archive_prune_days = 0 -> disabled
    p = _make_provider(tmp_path / "a", {"archive_prune_days": 0})
    _write_archive(p, old_ts, stale)
    assert p._auto_prune_raw_pending_archive() == 0
    assert list(p.knowledge_archive_dir.glob("raw_pending_auto_*.jsonl"))

    # (b) key absent from the knowledge block -> disabled
    p2 = _make_provider(tmp_path / "b", {"raw_pending_ttl_days": 30})
    _write_archive(p2, old_ts, stale)
    assert p2._auto_prune_raw_pending_archive() == 0

    # (c) knowledge block missing entirely -> disabled
    p3 = _make_provider(tmp_path / "c", None)
    p3.knowledge_archive_dir = tmp_path / "c" / "knowledge" / "archive"
    p3.knowledge_archive_dir.mkdir(parents=True, exist_ok=True)
    _write_archive(p3, old_ts, stale)
    assert p3._auto_prune_raw_pending_archive() == 0
    assert list(p3.knowledge_archive_dir.glob("raw_pending_auto_*.jsonl"))


# ── 3. unparseable filename ts -> skipped, fail-soft, not deleted ───────

def test_prune_skips_unparseable_filename_ts(tmp_path):
    p = _make_provider(tmp_path, {"archive_prune_days": 90})
    weird = p.knowledge_archive_dir / "raw_pending_auto_foo.jsonl"
    weird.write_text(json.dumps(_row("x")) + "\n", encoding="utf-8")
    # no parseable timestamp in the name -> skipped and left in place
    n = p._auto_prune_raw_pending_archive()
    assert n == 0
    assert weird.exists(), "unparseable-ts file must be left untouched"
    assert _manifest_rows(p) == []


# ── 4. quarantine + manifest + other archive files are never touched ────

def test_prune_does_not_touch_quarantine_or_manifest(tmp_path):
    p = _make_provider(tmp_path, {"archive_prune_days": 90})
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    # an OLD queue_overflow quarantine file: same age, but wrong prefix
    qfile = quarantine / "raw_pending_queue_overflow_20200101_000000.jsonl"
    qbytes = (json.dumps(_row("q")) + "\n").encode()
    qfile.write_bytes(qbytes)

    # a pre-existing pruned_index.jsonl (must survive byte-identical when
    # prune adds nothing new here, since we give it only a recent file)
    manifest = p.knowledge_archive_dir / "pruned_index.jsonl"
    mbytes = (json.dumps({"pre": "seed"}) + "\n").encode()
    manifest.write_bytes(mbytes)

    now = _naive_utc()
    recent = _write_archive(p, now - timedelta(days=3), [_row("keep me")])

    n = p._auto_prune_raw_pending_archive()
    assert n == 0, "nothing stale -> nothing deleted"
    # quarantine bytes untouched (glob prefix raw_pending_auto_* never matches)
    assert qfile.read_bytes() == qbytes
    # manifest bytes untouched (no row appended, no rewrite)
    assert manifest.read_bytes() == mbytes
    # recent archive file survived
    assert recent.exists()
    # no decision/dropped/fact/preference/task dirs were even created
    for d in ("decision", "dropped", "fact", "preference", "task"):
        assert not (quarantine / d).exists()


# ── 5. mount order rescore->confirm->mine->observe->archive->prune
#       + fail-soft isolation + _llm_ctx None decoupling ─────────────────

def test_prune_mounted_after_archive_failsoft():
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    block = src[src.index("def _spawn_llm_consolidation("):
                src.index("def _write_episodic_entry(")]
    i_rescore = block.index("self._llm_consolidate()")
    i_confirm = block.index("self._supersede_confirm_candidates()")
    i_mine = block.index("self._mine_alias_candidates()")
    i_observe = block.index("self._observe_consolidate()")
    i_archive = block.index("self._auto_archive_raw_pending()")
    i_prune = block.index("self._auto_prune_raw_pending_archive()")
    assert i_rescore < i_confirm < i_mine < i_observe < i_archive < i_prune
    # each of archive + prune sits in its own try/except Exception +
    # logger.warning (fail-soft isolation: one stage's failure never breaks
    # the later stages — the mount-order guarantee the caller relies on).
    for call_i, call in ((i_archive, "self._auto_archive_raw_pending()"),
                         (i_prune, "self._auto_prune_raw_pending_archive()")):
        site = block[call_i - 60:call_i + 120]
        tail = block[call_i:call_i + 400]
        assert "try:" in site
        assert "except Exception" in tail
        assert "logger.warning" in tail


def test_prune_runs_with_llm_ctx_none(tmp_path):
    """_llm_ctx None must not block the prune (pure file ops)."""
    p = _make_provider(tmp_path, {"archive_prune_days": 90})
    p._llm_ctx = None  # explicitly: the LLM face is absent
    now = _naive_utc()
    old_file = _write_archive(p, now - timedelta(days=100), [
        _row("stale A", "alpha", created_ts="2020-01-01T00:00:00")])
    n = p._auto_prune_raw_pending_archive()
    assert n == 1
    assert not old_file.exists()
    assert len(_manifest_rows(p)) == 1


# ── 6. decay.json and DECAY_DEFAULTS are key-consistent ──────────────────

def test_config_key_consistent():
    decay_file = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))
    file_k = decay_file["knowledge"]
    default_k = DECAY_DEFAULTS["knowledge"]
    assert set(file_k) == set(default_k), \
        f"key drift: file {sorted(file_k)} vs default {sorted(default_k)}"
    # the new key is present in BOTH and equal (default 90)
    assert "archive_prune_days" in file_k and "archive_prune_days" in default_k
    assert file_k["archive_prune_days"] == default_k["archive_prune_days"] == 90
    # every other knowledge key also matches value-for-value (C-slice rule)
    for k in ("stale_min_overlap", "observe_max_per_tick",
              "raw_pending_ttl_days", "raw_pending_max_rows"):
        assert file_k[k] == default_k[k], f"{k} drifts between the two sources"
