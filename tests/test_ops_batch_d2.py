"""D2②③ ops batch regression (one commit on top of fa5468e):

② the rate-limited queue is BOUNDED (cap 30; overflow degrades oldest items
   to raw_pending + a `queue_overflow` audit event — no silent drop), flushed
   at TURN END (not only shutdown), and the queue path logs "queued" — NOT
   "auto-saved" (the old semantic mislabel).

③ a bursty same-marker turn collapses near-duplicate auto-saves into the
   FIRST queued body (bumps its marker_count) instead of stacking 20+ rows;
   the collapsed item is stamped with metadata.marker_count on write.

Drivers build provider instances WITHOUT a real engine (same pattern as
test_pending_draft_notice) and call _save_memory / _flush_rate_limited_queue
directly.
"""
import json
import logging
import pathlib
import sys
import tempfile
import threading
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402

_PLUGIN_LOGGER = "__init__"  # `logger = logging.getLogger(__name__)` in the plugin


def _now_ts() -> float:
    return datetime.utcnow().timestamp()


def _provider(tmp: pathlib.Path, sid: str = "s_q") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = sid
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.knowledge_dir = tmp / "knowledge"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p._pending_drafts_notified = {}
    p._turn_marker_index = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 30
    p._save_interval = 30
    # 5s since last save -> inside the 30s window -> auto-saves queue
    p._last_save_time = _now_ts() - 5
    p._session_save_count = 0
    p._session_save_count_max = 50
    p.source_profile = "test"
    p._turn_self_test = False
    p._turn_canary = False
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._session_drafts = {}
    p.sessions_dir = None
    p.auto_extract = False
    p._pending_memories = []
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    return p


def test_2a_queue_is_bounded_by_cap():
    """② cap 30: 40 distinct queued items -> 30 held, 10 degrade to
    raw_pending (origin=queue_overflow) + a queue_overflow audit event."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    for i in range(40):
        p._save_memory(f"queued fact number {i}", level="L0", source="auto")
    assert len(p._rate_limited_queue) == p._rate_limited_queue_cap == 30
    rp = json.loads(p.knowledge_raw_pending_path.read_text(encoding="utf-8"))
    overflow = [it for it in rp if it.get("origin") == "queue_overflow"]
    assert len(overflow) == 10, [it.get("origin") for it in rp]
    log = tmp / "consolidation_log.jsonl"
    events = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    ovf = [e for e in events if e.get("action") == "queue_overflow"]
    # One audit event per overflow occurrence (each degrades exactly the excess
    # item) — 10 items over cap => 10 events summing to 10 discarded.
    assert len(ovf) == 10 and sum(e.get("discarded", 0) for e in ovf) == 10, events


def test_2b_turn_end_flush_writes_items():
    """② turn-end flush drains the queue and lands items on disk."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    for i in range(3):
        p._save_memory(f"flush me {i}", level="L0", source="auto")
    assert len(p._rate_limited_queue) == 3
    p._flush_rate_limited_queue()
    assert p._rate_limited_queue == []
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert len(items) == 3


def test_2c_flush_is_dedup_gated():
    """② flush is NOT a raw append: two content-equivalent L0s never both
    land (@data-engineer criterion: no two equivalent L0 rows in memories/)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    p._save_memory("same marker repeated A", level="L0", source="auto")
    p._save_memory("same marker repeated A", level="L0", source="auto")
    p._flush_rate_limited_queue()
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    contents = [it["content"] for it in items]
    assert contents.count("same marker repeated A") == 1, contents


def test_3a_bursty_turn_collapses_same_marker():
    """③ 20 identical burst auto-saves -> ONE queued body, marker_count=20,
    not 20 near-duplicate rows."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    body = "terminal command failed with exit code 1"
    for _ in range(20):
        p._save_memory(body, level="L0", source="auto")
    # Queue holds a single row; the 19 duplicates folded into it in place.
    assert len(p._rate_limited_queue) == 1, len(p._rate_limited_queue)
    assert p._rate_limited_queue[0]["marker_count"] == 20
    idx = p._turn_marker_index.get(p.session_id, {})
    assert len(idx) == 1 and idx[list(idx)[0]]["marker_count"] == 20


def test_3b_marker_count_stamped_on_write():
    """③ the collapsed item carries metadata.marker_count on disk
    (@data-engineer: first + marker_count, audit-visible)."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    p._save_memory("burst marker body", level="L0", source="auto")
    for _ in range(2):
        p._save_memory("burst marker body", level="L0", source="auto")
    p._flush_rate_limited_queue()
    items = json.loads((p.memories_dir / "fact.json").read_text(encoding="utf-8"))
    assert len(items) == 1
    assert items[0]["metadata"].get("marker_count") == 3, items[0]["metadata"]


def test_2d_queue_path_logs_queued_not_autosaved():
    """② semantic log: the queue path must say "queued", never "auto-saved"."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _provider(tmp)
    logs = []
    handler = logging.Handler()
    handler.emit = lambda record: logs.append(record.getMessage())
    lg = logging.getLogger(_PLUGIN_LOGGER)
    lg.addHandler(handler)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        p._save_memory("will be queued", level="L0", source="auto")
        p._save_memory("also queued", level="L0", source="auto")
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)
    joined = "\n".join(logs)
    assert "queued rate-limited save" in joined, joined
    assert "auto-saved L0 memory" not in joined, joined


if __name__ == "__main__":
    import importlib.util as _u, pathlib as _pl
    spec = _u.spec_from_file_location("test_ops_batch_d2",
                                      _pl.Path(__file__).resolve())
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _fails = 0
    for _name in dir(mod):
        if _name.startswith("test_"):
            try:
                getattr(mod, _name)()
                print("PASS", _name)
            except Exception as _e:
                _fails += 1
                print("FAIL", _name, "->", type(_e).__name__, _e)
    print("TOTAL-FAILS", _fails)
