"""P0-2: read-side hard isolation + recall denoise (auto-retrieve face).

Root cause of post-compact task drift: the pre_llm_call auto-recall pulled
CROSS-PROFILE stale task rows (the 4d cron crawler) into the default session.
P0-1 fixed the WRITE face (ingress gate); P0-2 fixes the READ face.

Acceptance gates (spec):
  ① Cross-profile rows do NOT appear in auto-retrieve results, but DO
     appear in the explicit filemem_search results.
  ② Neutral unstamped rows (no source_profile key, or empty/None legacy row)
     ARE included in auto-retrieve.
  ③ The auto-inject row-count cap (search.auto_recall_max, default 3) is
     enforced on >3 hits.
  ④ After path-token normalization, host path tokens ("/root/.hermes", the
     bare word "hermes") no longer take part in matching — the 4d-crawler
     mis-recall vector is closed.

Plus:
  - profile resolution order: instance source_profile (most specific) then
    the process-global HERMES_PROFILE env; unresolvable = fail-open (full
    recall, no isolation) + a single warning.
  - The explicit face (profile_scope=None) always keeps the full pool.

All fixtures are L2 (ttl_days=None -> never expires) so the test is
deterministic on ANY run date; queries are designed to substring-match their
target rows (the legacy keyword gate binds; gate_relax_alias is off), and
every real search_memories call passes min_score=0.0 so the RRF rank gate
never cuts a qualifying row.
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from __init__ import (  # noqa: E402
    FileMemProvider,
    _resolve_session_profile,
    _normalize_search_text,
    _row_source_profile,
    _auto_recall_eligible,
    _auto_recall_filter,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402
import copy  # noqa: E402


# A recent date (L2 = no TTL, so it never expires on any run date).
_RECENT = "2026-10-01T00:00:00"


def _make_provider(tmpdir: Path, source_profile: str = "default") -> FileMemProvider:
    """Minimal provider: enough fields for the REAL search_memories + prefetch
    to run without initialize(). The P0-2 filter runs at pool assembly — we
    need the real memories_dir on disk."""
    import __init__ as fm
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = None
    p.episodic_dir = None
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = source_profile
    p.session_id = "s-p02-test"
    p._turn_active_sid = "s-p02-test"
    p.trigger_router = fm.MemoryTriggerRouter()
    p.auto_extract = False
    p.min_search_score = 0.0
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._last_save_time = 0
    p._save_interval = 0
    p._session_save_count = 0
    p._session_save_count_max = 100
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._session_drafts = {}
    p._pending_memories = []
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._turn_marker_index = {}
    p._turn_overflow_total = 0
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._llm_ctx = None
    p._distill_call_limit = 5
    p._distill_budget = {}
    p._last_recall = None
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {}
    p._pending_drafts_notified = {}
    p._main_runtime = None
    return p


def _row(row_id: str, content: str, source_profile=None, mem_type: str = "fact",
         confirmed: bool = True) -> dict:
    """A minimal L2 memory row (no TTL -> never expires). source_profile=None
    means the key is ABSENT (a neutral legacy row); pass "" for an explicitly
    empty legacy value.

    V2: the auto face now applies a confirmed gate, so test rows that are
    expected to survive the auto face carry confirmed=True by default.
    Pass confirmed=False to test the unconfirmed-fold path."""
    d = {
        "id": row_id,
        "type": mem_type,
        "content": content,
        "level": "L2",          # ttl_days None -> is_expired() is always False
        "salience": 0.5,
        "reference_count": 0,
        "created_at": _RECENT,
        "updated_at": _RECENT,
    }
    if source_profile is not None:
        d["source_profile"] = source_profile
    if confirmed:
        d["metadata"] = {"confirmed": True}
    return d


def _seed(tmp: Path, items: list, mem_type: str = "fact") -> None:
    (tmp / "memories" / f"{mem_type}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# ── ① Cross-profile rows are cut from the auto face, kept on the explicit ──

def test_cross_profile_rows_absent_from_auto_retrieve():
    """All three rows share the token 'sharedword' so every one passes the
    keyword gate; isolation is then the ONLY thing that separates them. The
    auto face (profile_scope=default) keeps the same-profile + neutral rows
    and cuts the cross-profile row; the explicit face (profile_scope=None)
    keeps all three."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [
        _row("row_cron4d", "sharedword cron4d crawler retry backoff", source_profile="cron-4d"),
        _row("row_default", "sharedword default session deploy", source_profile="default"),
        _row("row_neutral", "sharedword neutral legacy note"),  # no source_profile key
    ])

    auto_ids = {r.get("id") for r in p.search_memories(
        "sharedword", limit=8, min_score=0.0, profile_scope="default")}
    assert "row_default" in auto_ids, f"same-profile row must be present, got {auto_ids}"
    # owner 2026-11-08: neutral rows are CUT from the auto face under a
    # known session profile (cross-session / cross-profile leak vector).
    assert "row_neutral" not in auto_ids, f"neutral row must be CUT from auto face, got {auto_ids}"
    assert "row_cron4d" not in auto_ids, f"cross-profile row must be cut, got {auto_ids}"

    explicit_ids = {r.get("id") for r in p.search_memories(
        "sharedword", limit=8, min_score=0.0, profile_scope=None)}
    assert explicit_ids == {"row_cron4d", "row_default", "row_neutral"}, \
        f"explicit face must keep every row, got {explicit_ids}"


# ── ② Neutral unstamped rows are kept by the auto face ─────────────────────

def test_neutral_rows_folded_out_of_auto_retrieve():
    """owner 2026-11-08 (cross-profile 串入收紧): a legacy row with NO
    source_profile key and one with an empty-string value are both NEUTRAL
    and are now CUT from the auto face under a KNOWN session profile
    (previously kept — the neutral leg leaked cross-session rows into
    every other profile). They still survive the EXPLICIT search face
    (全新 ≠ 失联: the row stays retrievable via filemem_search)."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [
        _row("row_legacy_no_key", "sharedword legacy api gateway", source_profile=None),
        _row("row_empty_sp", "sharedword empty legacy row", source_profile=""),
        _row("row_same", "sharedword same deploy note", source_profile="default"),
    ])
    ids = {r.get("id") for r in p.search_memories(
        "sharedword", limit=8, min_score=0.0, profile_scope="default")}
    assert "row_legacy_no_key" not in ids, f"neutral row must be CUT from auto face, got {ids}"
    assert "row_empty_sp" not in ids, f"empty-string source_profile = neutral, must be CUT, got {ids}"
    assert "row_same" in ids, f"same-profile row must be present, got {ids}"
    # 全新 ≠ 失联: the neutral rows are still retrievable on the EXPLICIT face.
    explicit_ids = {r.get("id") for r in p.search_memories(
        "sharedword", limit=8, min_score=0.0, profile_scope=None)}
    assert "row_legacy_no_key" in explicit_ids, f"neutral rows survive explicit search, got {explicit_ids}"
    assert "row_empty_sp" in explicit_ids, f"neutral rows survive explicit search, got {explicit_ids}"


def test_cross_profile_rows_visible_in_explicit_search():
    """The explicit face (profile_scope=None) returns a cross-profile row — a
    user's deliberate query is never isolation-cut."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [
        _row("row_other", "otherprofile task note for cron", source_profile="cron-4d"),
    ])
    ids = {r.get("id") for r in p.search_memories(
        "otherprofile", limit=8, min_score=0.0, profile_scope=None)}
    assert "row_other" in ids, f"explicit face must return cross-profile rows, got {ids}"
    # But the SAME query on the AUTO face (scoped to 'default') cuts it.
    auto_ids = {r.get("id") for r in p.search_memories(
        "otherprofile", limit=8, min_score=0.0, profile_scope="default")}
    assert "row_other" not in auto_ids, \
        f"auto face must cut the cross-profile row, got {auto_ids}"


# ── ③ Row-count cap (auto_recall_max) ─────────────────────────────────────

def test_auto_recall_max_caps_results():
    """search_memories returns all qualifying rows; prefetch applies the
    auto_recall_max cap afterward. With the cap at 3 and 6 matching rows,
    only the top-3 are injected (_last_recall.count == 3)."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [_row(f"row_{i}", f"deploynote number {i}", source_profile="default")
                for i in range(6)])
    # Real retrieval: all 6 rows are same-profile -> all kept.
    full = p.search_memories("deploynote", limit=8, min_score=0.0,
                             profile_scope="default")
    assert len(full) == 6, f"expected 6 rows before the cap, got {len(full)}"

    # Isolate the cap logic: stub the read face to return the 6, enable cap=3.
    p._decay["search"]["auto_recall_max"] = 3
    p.search_memories = lambda *a, **k: list(full)
    p._set_bringback_choice("s-p02-test", "clean_start")
    ctx = p.prefetch("deploynote", session_id="s-cap", confidence=0.5)
    assert p._last_recall.count == 3, \
        f"auto_recall_max=3 should inject 3 rows, got {p._last_recall.count}"
    present = [i for i in range(6) if f"deploynote number {i}" in ctx]
    assert len(present) == 3, f"expected 3 rows in the injected context, got {present}"


def test_auto_recall_max_zero_disables_cap():
    """auto_recall_max=0 disables the cap: all surviving rows are injected."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [_row(f"row_{i}", f"caprow marker {i}", source_profile="default")
                for i in range(4)])
    full = p.search_memories("caprow", limit=8, min_score=0.0,
                             profile_scope="default")
    assert len(full) == 4, f"expected 4 rows, got {len(full)}"

    p._decay["search"]["auto_recall_max"] = 0
    p.search_memories = lambda *a, **k: list(full)
    p._set_bringback_choice("s-p02-test", "clean_start")
    ctx = p.prefetch("caprow", session_id="s-cap0", confidence=0.5)
    assert p._last_recall.count == 4, \
        f"auto_recall_max=0 should disable the cap, got {p._last_recall.count}"
    assert all(f"caprow marker {i}" in ctx for i in range(4)), \
        "all 4 rows must be injected when the cap is disabled"


# ── ④ Path-token normalization ─────────────────────────────────────────────

def test_normalize_search_text_drops_host_path_tokens():
    """/root/.hermes, ~/.hermes, ./hermes, and the bare host word 'hermes'
    are all stripped; ordinary tokens survive unchanged."""
    assert _normalize_search_text("/root/.hermes some_query") == "some_query"
    assert "hermes" not in _normalize_search_text("/root/.hermes some_query")
    assert _normalize_search_text("hermes deploy note") == "deploy note"
    assert _normalize_search_text("~/.hermes config") == "config"
    assert _normalize_search_text("./hermes something") == "something"
    # non-noise tokens (including a bare 'port' or '8080') are kept:
    assert _normalize_search_text("deploy nginx port 8080") == "deploy nginx port 8080"
    # empty input is returned unchanged (fail-soft).
    assert _normalize_search_text("") == ""


def test_path_token_normalization_stops_hermes_misrecall():
    """End-to-end: the 4d-crawler vector is a host-path row. Normalizing the
    query drops the path tokens, and the cross-profile row is isolation-cut on
    the auto face anyway — so 'hermes' inside a path can no longer drag a 4d
    row into the auto-inject face."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [
        _row("row_4d_path",
             "hermes filemem plugin at /root/.hermes/plugins/filemem init",
             source_profile="cron-4d"),
        _row("row_real", "the api deploy uses nginx upstream port 8080",
             source_profile="default"),
    ])
    # Normalize the raw (path-bearing) query the way prefetch does.
    q = _normalize_search_text("/root/.hermes plugins")
    assert q == "plugins", f"normalization must strip the path tokens, got {q!r}"
    results = p.search_memories(q, limit=8, min_score=0.0, profile_scope="default")
    ids = {r.get("id") for r in results}
    assert "row_4d_path" not in ids, \
        f"a 4d host-path row must not be mis-recalled on the auto face, got {ids}"


# ── Resolution + fail-open + predicates (pure functions) ───────────────────

def test_fail_open_when_profile_unresolvable():
    """_auto_recall_filter with session_sp=None keeps every row (no
    isolation) — an unresolvable profile must never drop recall."""
    pool = [
        {"id": "a", "content": "x", "source_profile": "cron-4d"},
        {"id": "b", "content": "y", "source_profile": "other"},
        {"id": "c", "content": "z"},
    ]
    kept = _auto_recall_filter(pool, None)
    assert len(kept) == 3, f"fail-open must keep all rows, got {len(kept)}"


def _set_profile_env(value):
    """Context helper: set or remove HERMES_PROFILE for the duration."""
    had = "HERMES_PROFILE" in os.environ
    old = os.environ.get("HERMES_PROFILE")
    if value is None:
        os.environ.pop("HERMES_PROFILE", None)
    else:
        os.environ["HERMES_PROFILE"] = value
    return (had, old)


def test_resolve_session_profile_instance_wins_over_env():
    """Instance source_profile (the per-session identity) is the most specific
    signal and wins over the process-global HERMES_PROFILE env."""
    had, old = _set_profile_env("env-profile")
    try:
        assert _resolve_session_profile(fallback_profile="instance") == "instance"
    finally:
        if had:
            os.environ["HERMES_PROFILE"] = old
        else:
            os.environ.pop("HERMES_PROFILE", None)


def test_resolve_session_profile_env_when_no_instance():
    """With no instance identity, the HERMES_PROFILE env is the fallback."""
    had, old = _set_profile_env("env-profile")
    try:
        assert _resolve_session_profile(fallback_profile=None) == "env-profile"
        assert _resolve_session_profile(fallback_profile="") == "env-profile"
    finally:
        if had:
            os.environ["HERMES_PROFILE"] = old
        else:
            os.environ.pop("HERMES_PROFILE", None)


def test_resolve_session_profile_unresolvable_returns_none():
    """Neither instance nor env -> None (the caller fails open)."""
    had, old = _set_profile_env(None)
    try:
        assert _resolve_session_profile(fallback_profile=None) is None
        assert _resolve_session_profile(fallback_profile="") is None
    finally:
        if had:
            os.environ["HERMES_PROFILE"] = old
        else:
            os.environ.pop("HERMES_PROFILE", None)


def test_row_source_profile_reads_both_locations():
    """_row_source_profile reads metadata.source_profile first, then the
    top-level source_profile. Neither present -> None (neutral). Non-dict /
    non-dict-metadata inputs degrade to None (never raise — a row-shape
    hiccup must not drop recall)."""
    assert _row_source_profile({"metadata": {"source_profile": "alpha"}}) == "alpha"
    assert _row_source_profile({"source_profile": "beta"}) == "beta"
    assert _row_source_profile({}) is None
    assert _row_source_profile({"source_profile": ""}) is None
    assert _row_source_profile({"metadata": {"source_profile": "a"},
                               "source_profile": "b"}) == "a"
    # fail-soft on bad shapes:
    assert _row_source_profile(None) is None
    assert _row_source_profile("not-a-dict") is None
    assert _row_source_profile({"metadata": "oops", "source_profile": "c"}) == "c"
    assert _row_source_profile({"metadata": None, "source_profile": "d"}) == "d"


def test_auto_recall_eligible_predicate():
    """session None -> all eligible (fail-open); row None (neutral) -> cut
    under a known session profile (owner 2026-11-08); equal -> eligible;
    cross -> cut."""
    assert _auto_recall_eligible(None, None) is True
    assert _auto_recall_eligible("a", None) is True
    assert _auto_recall_eligible(None, "a") is False   # neutral cut under known profile
    assert _auto_recall_eligible("a", "a") is True
    assert _auto_recall_eligible("b", "a") is False


# ── Auto face wiring: prefetch passes the resolved profile as profile_scope ──

def test_prefetch_passes_profile_scope_to_search():
    """prefetch resolves the session's owning profile (from the instance
    source_profile here) and threads it into search_memories as
    profile_scope, so the auto face is hard-isolated while the explicit face
    (profile_scope=None) is not."""
    tmp = Path(tempfile.mkdtemp())
    p = _make_provider(tmp, source_profile="default")
    _seed(tmp, [
        _row("row_cross", "wired cross profile stale task", source_profile="cron-4d"),
        _row("row_self", "wired self profile deploy note", source_profile="default"),
    ])
    captured = {}
    original = p.search_memories

    def spy(*a, **k):
        captured["profile_scope"] = k.get("profile_scope")
        return original(*a, **k)

    p.search_memories = spy
    p.prefetch("wired", session_id="s-int", confidence=0.5)
    assert captured.get("profile_scope") == "default", \
        f"prefetch must thread the session profile as profile_scope, got {captured}"

    # The auto face actually cut the cross row from the injected content:
    p2 = _make_provider(tmp, source_profile="default")
    p2._set_bringback_choice("s-p02-test", "clean_start")
    ctx = p2.prefetch("wired", session_id="s-int2", confidence=0.5)
    assert "wired self profile deploy note" in ctx, \
        f"same-profile row must inject on the auto face, got {ctx!r}"
    assert "wired cross profile stale task" not in ctx, \
        f"cross-profile row must NOT inject on the auto face, got {ctx!r}"


# ── Config key parity: decay.json carries auto_recall_max ─────────────────

def test_decay_json_has_auto_recall_max():
    """The P0-2 denoise constant auto_recall_max is present in both decay.json
    and DECAY_DEFAULTS (key parity, S2 pattern) and defaults to 3."""
    decay_json = json.loads(
        (Path(__file__).resolve().parents[1] / "decay.json").read_text(encoding="utf-8"))
    assert decay_json.get("search", {}).get("auto_recall_max") == 3, \
        f"decay.json search block must carry auto_recall_max=3, got {decay_json.get('search')}"
    assert DECAY_DEFAULTS["search"]["auto_recall_max"] == 3
    assert decay_json["search"]["auto_recall_max"] == DECAY_DEFAULTS["search"]["auto_recall_max"]


if __name__ == "__main__":
    import traceback
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
                passed += 1
            except Exception:
                print(f"FAIL  {name}")
                traceback.print_exc()
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
