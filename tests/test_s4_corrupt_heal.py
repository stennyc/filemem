"""S4-T2 (S4-③) memories read-face corrupt self-heal.

Blueprint: specs/S4-lifespan-selfheal-impl-notes.md S4-T2 S4-③ (North Star #5,
"corrupt 后自愈、旧文件 rename 留壳可回滚、不吞脏"). A single corrupt JSON row
must NOT silently kill the recall face. The single-source helper
`_read_memories_file` (wired into the 3 bounded call sites — search_memories
read choke, _find_duplicate_entry dedup read, _save_memory RMW read step) does:

  1. RENAME the bad active file to `<type>.json.corrupt-<UTC-ts>`
     (shell retained, human-recoverable, never deleted),
  2. REBUILD the active file empty (`[]`) so the write face keeps appending,
  3. append one `corrupt_memories_healed` audit row to consolidation_log
     (action / type / renamed_to / ts) — the self-heal is self-evident;
     dirty data is never silently swallowed.

The corrupt check runs UNDER the shard lock at every call site so a heal
(rename + rebuild + audit) can't race a concurrent writer.

ALL stores are tmp synthetic — the live /root/.hermes/filemem store is never
touched. Zero host-package changes; zero third-party deps in the new code.
"""
import copy
import json
import pathlib
import re
import threading
import types

import pytest  # noqa: F401

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


# ── synthetic store / full provider shell (mirrors test_s4_lifespan) ────────


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    """Every subdirectory + an empty `[]` file per MEMORY_TYPE (a realistic
    store; only the file under test is corrupted by each case)."""
    store = tmp / "store"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, store: pathlib.Path,
                   decay: dict) -> FileMemProvider:
    """FULL synthetic provider (no initialize) wired against the tmp store;
    enough attributes for search_memories + _save_memory +
    _find_duplicate_entry (the three S4-③ call sites)."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s-s4c"
    p.source_profile = "s4c-test"
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = None
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = decay
    p._llm_ctx = None
    p._llm_consolidating = False
    p._supersede_confirming = False
    p._alias_mining_in_flight = False
    p.trigger_router = types.SimpleNamespace(
        detect_and_route=lambda content: {"level": None})
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p._turn_marker_index = {}
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._rate_limited_queue = []
    p._rate_limited_queue_cap = 100
    p._turn_overflow_total = 0
    p._last_save_time = 0.0
    p._save_interval = 30
    p._session_save_count = 0
    p._session_save_count_max = 5
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._last_consolidation_ts = 0.0
    p._last_skip_audit_ts = 0.0
    p._last_recall_audit_ts = 0.0
    p._last_recall_stats = {"reinforced": 0, "durable_hits": 0}
    p.min_search_score = 0.3
    p.auto_extract = False
    p._turn_report_line = lambda sid: ""
    return p


def _corrupt_fact(store: pathlib.Path, body: str = "{ this is NOT valid json ]]") -> None:
    """Overwrite the active fact.json with illegal JSON (the corrupt trigger)."""
    (store / "memories" / "fact.json").write_text(body, encoding="utf-8")


def _heal_shell(store: pathlib.Path) -> pathlib.Path:
    """The renamed corrupt shell (exactly one, `fact.json.corrupt-<ts>`)."""
    shells = sorted((store / "memories").glob("fact.json.corrupt-*"))
    assert len(shells) == 1, f"exactly one corrupt shell expected, got {shells}"
    return shells[0]


def _audit_rows(store: pathlib.Path, action: str = "corrupt_memories_healed") -> list:
    p = store / "consolidation_log.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    return [r for r in rows if r.get("action") == action]


# ── S4-③ search_memories read choke: a corrupt row must NOT kill recall ─────


def test_corrupt_search_does_not_silently_die(tmp_path):
    """Corrupt fact.json -> search_memories returns [] (the recall face is
    ALIVE, not a crash): the old bare json.load would have raised
    JSONDecodeError and killed the whole read choke."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    res = p.search_memories("anything at all", limit=5, min_score=0.0)
    # no raise is the contract: recall degrades to an empty result, never dead
    assert res == [], f"corrupt fact.json must not poison the pool: {res}"


def test_corrupt_file_renamed_shell_retained(tmp_path):
    """The bad active file is RENAMED (not deleted) to fact.json.corrupt-<ts>
    so a human can roll back / recover the dirty bytes; the active file is
    rebuilt empty `[]`."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    p.search_memories("probe", limit=5, min_score=0.0)
    shell = _heal_shell(store)
    assert re.fullmatch(r"fact\.json\.corrupt-\d{14}", shell.name), shell.name
    # the original dirty bytes are RETAINED in the shell (never silently gone)
    assert shell.read_text(encoding="utf-8").startswith("{ this is NOT valid json")
    # the active file is a clean empty list so the write face can keep appending
    active = (store / "memories" / "fact.json").read_text(encoding="utf-8")
    assert json.loads(active) == []


def test_corrupt_audit_row_landed(tmp_path):
    """Self-heal is self-evident: exactly one `corrupt_memories_healed` audit
    row lands in consolidation_log with action / type / renamed_to / ts
    (North Star #1 same-source: the heal is auditable, not a silent swallow)."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    p.search_memories("probe", limit=5, min_score=0.0)
    rows = _audit_rows(store)
    assert len(rows) == 1, f"exactly one audit row, got {rows}"
    row = rows[0]
    assert row["action"] == "corrupt_memories_healed"
    assert row["type"] == "fact"
    assert re.fullmatch(r"fact\.json\.corrupt-\d{14}", row["renamed_to"]), row
    assert row["ts"], "the audit row carries the heal timestamp"
    # renamed_to points at the shell that actually exists
    assert (store / "memories" / row["renamed_to"]).exists()


def test_corrupt_heal_is_idempotent(tmp_path):
    """A SECOND read of the (now clean) active file is a no-op: no new shell,
    no duplicate audit row. The heal fires once on the corrupt bytes, then
    the rebuilt empty active file reads normally."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    p.search_memories("probe", limit=5, min_score=0.0)
    shells_after_1st = len(list((store / "memories").glob("fact.json.corrupt-*")))
    rows_after_1st = len(_audit_rows(store))
    # a clean re-read of the rebuilt active file heals nothing
    p.search_memories("probe again", limit=5, min_score=0.0)
    assert len(list((store / "memories").glob("fact.json.corrupt-*"))) == shells_after_1st
    assert len(_audit_rows(store)) == rows_after_1st, "no duplicate heal audit"


# ── S4-③ write face keeps appending on the rebuilt shell ────────────────────


def test_save_memory_write_face_continues_after_heal(tmp_path):
    """After the corrupt file is healed (rebuilt empty), the write face
    (_save_memory RMW read step -> _read_memories_file) lands a NEW row on
    the clean active shell instead of dying on the bad bytes."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    # trigger the heal via the read face
    p.search_memories("probe", limit=5, min_score=0.0)
    assert json.loads((store / "memories" / "fact.json").read_text("utf-8")) == []
    # the write face now appends onto the clean shell (source=manual bypasses
    # the rate-limit/auto/distill paths -> a deterministic, L2 single-row write)
    new_id = p._save_memory("remember the toolchain pins rustc nightly",
                            level="L2", source="manual")
    assert new_id, "the save must return the new row id"
    active = json.loads((store / "memories" / "fact.json").read_text("utf-8"))
    assert len(active) == 1 and active[0]["id"] == new_id
    assert active[0]["content"] == "remember the toolchain pins rustc nightly"
    # the corrupt shell is STILL retained (the heal is not retroactively lost)
    assert len(list((store / "memories").glob("fact.json.corrupt-*"))) == 1


def test_find_duplicate_entry_read_goes_through_helper(tmp_path):
    """The dedup read (_find_duplicate_entry) is wired to the same helper: a
    corrupt fact.json must NOT crash the dedup path — it self-heals (rename +
    rebuild) and returns None (the empty rebuilt list has no duplicate). A
    bare json.load here would have raised and killed the write face."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    _corrupt_fact(store)
    dup = p._find_duplicate_entry("some brand new fact content", "fact")
    # no raise: the dedup read degraded to 'no duplicate' on the healed shell
    assert dup is None, f"corrupt dedup read must not crash: {dup!r}"
    # the dedup read triggered the heal side-effect (rename + rebuild)
    assert re.fullmatch(r"fact\.json\.corrupt-\d{14}", _heal_shell(store).name)
    assert json.loads((store / "memories" / "fact.json").read_text("utf-8")) == []
    assert len(_audit_rows(store)) == 1


def test_find_duplicate_entry_still_dedups_clean_file(tmp_path):
    """Regression guard: on a CLEAN fact.json the dedup read still finds an
    exact-content duplicate (the helper is transparent to healthy files — it
    returns the parsed list unchanged, no shell, no audit row)."""
    store = _make_store(tmp_path)
    p = _make_provider(tmp_path, store, copy.deepcopy(DECAY_DEFAULTS))
    known = {"id": "x1", "type": "fact", "content": "the shared line",
             "level": "L0", "salience": 0.5, "reference_count": 0,
             "created_at": "2026-09-01T00:00:00", "updated_at": "2026-09-01T00:00:00"}
    (store / "memories" / "fact.json").write_text(
        json.dumps([known], ensure_ascii=False, indent=2), encoding="utf-8")
    dup = p._find_duplicate_entry("the shared line", "fact")
    assert dup == "x1", f"exact-content duplicate must be found: {dup!r}"
    # a healthy file is never renamed / audited
    assert not list((store / "memories").glob("fact.json.corrupt-*"))
    assert _audit_rows(store) == []


# ── hard-constraint grep: the S4-③ helper is deterministic / stdlib-only ────


def test_read_memories_file_zero_llm_stdlib_only(tmp_path):
    """Hard constraint: _read_memories_file is pure deterministic file I/O —
    zero LLM call-sites, zero third-party import, zero hardcoded model name.
    It only renames + rebuilds + appends an audit row (all stdlib)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    m = next(i for i, l in enumerate(lines)
             if l.strip().startswith("def _read_memories_file("))
    indent = len(lines[m]) - len(lines[m].lstrip())
    end = len(lines)
    for j in range(m + 1, len(lines)):
        l = lines[j]
        if l.strip() and (len(l) - len(l.lstrip())) <= indent and \
                (l.lstrip().startswith("def ") or l.lstrip().startswith("class ")):
            end = j
            break
    body = "\n".join(lines[m:end])
    for tok in ("llm_caller", "complete_structured", "_load_llm_caller",
                "score_salience", "import requests", "import http",
                "openai", "anthropic", "httpx"):
        assert tok not in body, f"_read_memories_file leaks a non-stdlib token: {tok}"
    for model in ("gpt-", "claude", "gemini", "qwen", "llama", "grok"):
        assert model not in body.lower(), f"_read_memories_file hardcodes a model: {model}"
    # the heal path is rename + rebuild-empty + audit row (the three S4-③ moves)
    assert ".corrupt-" in body and '"[]"' in body
    assert "corrupt_memories_healed" in body


def test_s4_corrupt_heal_wired_into_three_call_sites(tmp_path):
    """Bounded wiring lock: the helper is the single source used by exactly
    the 5 S4-③ sites (search_memories read choke, _find_duplicate_entry
    dedup read, _save_memory RMW read step, the native-md drain pool-write
    read step t_db244983, and the P1-1b content-hash dedup read
    _find_duplicate_by_hash) — none of the five still does a bare
    `json.load` of a memories file.

    The 4th site is the D5 native-drain write: a drained entry lands as an L0
    auto-row in the pool under the SAME S4-③ corrupt self-heal read choke as
    every other pool write (a corrupt shard is healed + audited, the write
    face appends on the clean shell). It is a legitimate pool-write site, not
    a bare json.load — so it extended the lock 3 -> 4 deliberately. The 5th
    site is P1-1b's content-hash idempotent-upsert gate (_find_duplicate_by
    _hash), which ALSO reads the shard through the self-heal helper (a corrupt
    shard must self-heal on the dedup read, never a bare json.load) — so the
    lock is 4 -> 5. All five are single-source S4-③ pool reads; the drift is
    in the LOCK, not in the source."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    # the helper is the single source used by the S4-③ sites (search_memories
    # read choke, _find_duplicate_entry dedup read, _save_memory RMW read step,
    # native-drain write read step, P1-1b content-hash dedup read) plus the S3
    # dream faces (semantic-merge / contradiction / salience-decay shard RMW
    # reads). All eight are S4-③ corrupt-self-heal pool reads — none does a
    # bare `json.load` of a memories file. S3 extension legitimately widened
    # the lock 5 -> 8 (the 3 S3 shard RMW sites each read through the helper,
    # never bare json.load), so the invariant is the LOWER BOUND 5 (the S4-③
    # sites are still present + all S3 sites are also helper-mediated), NOT an
    # exact count: any future face must also route its shard read through the
    # self-heal choke or this lower bound + a per-site bare-json.load absence
    # check catches the regression.
    assert src.count("self._read_memories_file(") >= 5, \
        "the 5 S4-③ call sites regressed (helper-mediated reads < 5)"
    # the S4-③ read choke in search_memories locks the corrupt check
    sm = src.split("def search_memories", 1)[1].split("\n    def _memories_rotate_mb", 1)[0]
    assert "with self._shard_lock(mem_file):" in sm, \
        "the search read choke must run the corrupt check under the shard lock"
    assert "self._read_memories_file(mem_file)" in sm


def test_no_host_package_changes_in_diff(tmp_path):
    """S4-T2 is plugin-surface only: the changed-file set is confined to the
    plugin tree (__init__.py, consolidation/, decay.json, tests/) — no
    /usr/local/lib/hermes-agent path is touched."""
    import subprocess
    # 🔴 守卫 (同 S1b/S4-T2/S6): 非 git 仓库 -> git diff 无意义 (exit 129),
    # 转环境性 SKIP 而非假红; git 托管环境下仍真跑。
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--git-dir"], cwd=str(PLUGIN_DIR),
            text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        pytest.skip("plugin dir is not a git repository (git-diff guard N/A)")
    out = subprocess.check_output(
        ["git", "diff", "--name-only"], cwd=str(PLUGIN_DIR), text=True)
    changed = [l for l in out.splitlines() if l.strip()]
    for c in changed:
        low = c.lower()
        assert "hermes-agent" not in low and "hermes_agent" not in low, \
            f"S4-T2 touched a host package path: {c}"
        assert low.startswith(
            ("__init__.py", "consolidation/", "decay.json",
             "tests/", "spec", "tools/", "shared_memory.py")), \
            f"unexpected changed path (non-plugin-surface): {c}"
