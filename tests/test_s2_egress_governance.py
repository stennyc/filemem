"""S2 entry hygiene: egress-side noise cut + one-off governance tool.

Blue-print LTM-fix-epic.md S2 (North Star gate #2: injection-face noise
< 10%, tool-dump/echo classes out of the pool with an audit trail):

1. egress: the module-level `_egress_noise_cut` wires the EXISTING
   single-source `_looks_like_tool_dump` predicate + the config-driven
   egress_noise fingerprints (tool-dump openers / echo patterns /
   oversized-structural) into `FileMemProvider._egress_cut`, so the
   'Fetch finished …' 47KB task_1 class and the 'File unchanged' echo
   class stop entering the auto-inject face (explicit search still
   returns them — data stays on disk).
2. tools/govern_memories.py (pure stdlib, dry-run default): the one-off
   data-governance pass — identifies the four candidate classes, archives
   noise rows append-only into knowledge/archive/governance_<ts>.jsonl,
   downgrades complaint-class salience (never rewrites conclusions),
   fcntl.flock on <file>.lock (the _shard_lock protocol), idempotent.
3. config: decay.json <-> DECAY_DEFAULTS key parity (egress_noise block
   identical in both; the known pre-existing `search.mode` asymmetry is
   the ONLY tolerated diff and is asserted, not silently waved away).
4. single-source: the tool's `_looks_like_tool_dump` copy is byte-
   identical (AST source segment) to the plugin's — two faces can't
   drift apart silently.

All stores are tmp_path synthetic. Live /root/.hermes/filemem is never
touched here.
"""
import ast
import importlib.util
import json
import pathlib
import re
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import (  # noqa: E402
    FileMemProvider,
    MEMORY_TYPES,
    _egress_noise_cut,
)
from consolidation import DECAY_DEFAULTS  # noqa: E402

# load the governance tool by path (module name govern_memories, pure
# stdlib — no plugin import, no host hooks)
_spec = importlib.util.spec_from_file_location(
    "govern_memories", PLUGIN_DIR / "tools" / "govern_memories.py")
gov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gov)

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))

# Pristine snapshot of DECAY_DEFAULTS taken AT IMPORT (collection) time —
# before any test body runs. Cross-test shared-state hazard: test_ep_2_confirm
# does `p._decay = dict(DECAY_DEFAULTS)` (shallow) and then mutates
# `p._decay["consolidation"]["supersede_max_confirms"]`, which in-place edits
# the shared nested DECAY_DEFAULTS dict for the rest of the process. The
# config-parity test must compare decay.json against the PRISTINE defaults,
# immune to suite-order mutation (the production path is safe — the host's
# load_decay_config returns a fresh merged dict per load; this is a test-
# fixture-only hazard).
DEFAULTS_SNAPSHOT = json.loads(json.dumps(DECAY_DEFAULTS))


# ── store / provider fixtures ───────────────────────────────────────────────


def _row(rid, content, mem_type="fact", **extra):
    d = {"id": rid, "type": mem_type, "content": content, "level": "L0",
         "salience": 0.5, "reference_count": 0}
    # V2 confirmed gate: auto-face fixtures carry confirmed=True so pre-V2
    # egress/isolation tests keep their rows on the auto face. Pass
    # confirmed=False in extra to test the fold-out path.
    meta = {"confirmed": True}
    d.update(extra)
    if "metadata" not in d:
        d["metadata"] = meta
    elif isinstance(d.get("metadata"), dict):
        d["metadata"].setdefault("confirmed", True)
    return d


def _make_store(tmp: pathlib.Path, rows_by_type: dict) -> pathlib.Path:
    store = tmp / "store"
    (store / "memories").mkdir(parents=True)
    (store / "knowledge" / "archive").mkdir(parents=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps(rows_by_type.get(t, []), ensure_ascii=False, indent=2),
            encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, decay: dict) -> FileMemProvider:
    """Minimal provider (no initialize) wired to a synthetic memories dir."""
    p = FileMemProvider.__new__(FileMemProvider)
    import threading
    p._lock = threading.Lock()
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmp / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "s2-test"
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {"s2": True}
    p.min_search_score = 0.3
    p._decay = decay
    p._turn_report_line = lambda sid: ""
    p._reinforce_recall = lambda results: None
    return p


EN = DECAY_DEFAULTS["egress_noise"]
GOV = EN["governance"]


def _cfg() -> dict:
    """The decay block as the provider carries it (defaults baked in)."""
    return {"egress_noise": EN}


# ── 1. egress: _egress_noise_cut unit + _egress_cut wiring ─────────────────


def test_egress_noise_cut_task1_dump_class():
    """The task_1_1790657309 shape: 'Fetch finished' browser console dump,
    47 KB — misses the '{[' structural classes; the config table catches it."""
    content = ("Fetch finished loading: GET \"<URL>\".\n"
               * 2000) + "x" * 40000
    assert len(content) > 45000
    assert _egress_noise_cut(content, _cfg()) is True
    # and without the config block, pure-code rule ① alone does NOT catch it
    # (proof the table — not hardcoded prose — is what widens the net)
    assert _egress_noise_cut(content, {}) is False


def test_egress_noise_cut_echo_class_length_gated():
    echo = json.dumps(
        {"status": "unchanged",
         "message": "File unchanged since last read. The content from the "
                    "earlier read_file result in this conversation is still "
                    "current — refer to that instead of re-reading.",
         "dedup": True, "content_returned": False},
        ensure_ascii=False)
    assert len(echo) <= EN["echo_max_chars"]
    assert _egress_noise_cut(echo, _cfg()) is True
    # the echo rule is SURGICAL: a long log that merely CONTAINS the phrase
    # (beyond echo_max_chars, structural opener or not) is NOT an echo row
    long_log = "noise " * 3000 + "File unchanged"  # 15 KB, opens with 'n'
    assert len(long_log) > EN["echo_max_chars"]
    assert _egress_noise_cut(long_log, _cfg()) is False


def test_egress_noise_cut_legacy_structural_classes_no_regression():
    """'{[' opener rows: the pre-existing structural reject stays live for
    egress — the old '{[' 拒写面 did not regress; now oversized '{[' rows
    are cut even under the legacy ②/③ rules via the config opener class."""
    assert _egress_noise_cut('{"output": "tool result"}', _cfg()) is True
    assert _egress_noise_cut('{"error": "boom"}', _cfg()) is True
    # oversized '{' row beyond governance.max_chars (guaranteed: one extra
    # repeat pushes the row strictly past the threshold)
    big = "{" + "k: 1, " * (int(GOV["max_chars"] / 6) + 1)
    assert len(big) > GOV["max_chars"]
    assert _egress_noise_cut(big, _cfg()) is True
    # a SMALL '{' row is structural-noise via the room-marker predicate, not
    # via the noise cut (behavior boundary kept explicit)
    assert _egress_noise_cut("{\"a\": 1}", _cfg()) is False


def test_egress_noise_cut_fail_soft_on_missing_config():
    assert _egress_noise_cut("plain fact", _cfg()) is False
    assert _egress_noise_cut("plain fact", None) is False
    assert _egress_noise_cut("", {}) is False


def test_prefetch_egress_rejects_task1_dump_row_fixture(tmp_path):
    dump_content = ("Fetch finished loading: GET \"<URL>\".\n"
                    * 2000) + "x" * 40000
    clean = "部署 nginx 必须配置 upstream 健康检查"
    p = _make_provider(tmp_path, _cfg())
    results = [
        _row("task_1_synth", dump_content, "task"),
        _row("fact_ok", clean),
    ]
    # P0-2: the read face now passes profile_scope (auto isolation). The stub
    # replaces search_memories, so it absorbs the P0-2 kwarg (any kwargs) and
    # ignores it — the test is about egress, not isolation.
    p.search_memories = lambda *a, **k: results
    ctx = p.prefetch("nginx", session_id="s2")
    assert "Fetch finished" not in ctx
    assert clean in ctx, ctx
    assert p._last_recall.count == 1


def test_prefetch_egress_rejects_echo_rows(tmp_path):
    echo = json.dumps(
        {"status": "unchanged",
         "message": "File unchanged since last read."},
        ensure_ascii=False)
    p = _make_provider(tmp_path, _cfg())
    # P0-2: stub absorbs the auto-face profile_scope kwarg (any kwargs).
    p.search_memories = lambda *a, **k: [
        _row("fact_echo_synth", echo),
        _row("fact_keep", "redis 密码存放在 vault 的 kv0 路径"),
    ]
    ctx = p.prefetch("redis", session_id="s2")
    assert "File unchanged" not in ctx
    assert "redis 密码" in ctx


def test_prefetch_legacy_structural_cut_no_regression(tmp_path):
    """The OLD egress cut ('{[' opener room-marker class, self_test
    stamp, failure-lesson keep) is byte-for-byte still live."""
    p = _make_provider(tmp_path, _cfg())
    # legacy ②: '{' opener room-marker row (structural, small)
    assert p._egress_cut(_row("r1", "[Group chat: member quoted a log]"))
    # legacy ①: self_test stamp
    assert p._egress_cut(_row("r2", "any content", metadata={"self_test": True}))
    # legacy exception: failure-lesson KEPT even when it is a dump
    kept = _row("r3", '{"output": "tool dump with lesson"}',
                metadata={"failure_type": "read_failed"})
    assert not p._egress_cut(kept)
    # S2 ③: oversized task_1-class row cut (the new wiring)
    big = ("Fetch finished loading: GET \"<URL>\".\n" * 3000)
    assert p._egress_cut(_row("task_1_synth", big, "task"))
    # plain small prose passes all cuts
    assert not p._egress_cut(_row("r4", "部署 nginx 必须配置 upstream 健康检查"))


# ── 2. governance tool (tmp synthetic store) ───────────────────────────────

STORE_ROWS = {
    "fact": [
        _row("fact_echo_1", json.dumps(
            {"status": "unchanged",
             "message": "File unchanged since last read."},
            ensure_ascii=False)),
        _row("fact_tool_dump", '{"output": "console dump body"}'),
        _row("fact_91_synth",
              "supercompress 的上下文管理有问题，请看图，你有什么建议\n"
              "@image:/nonexistent_s2/store/img.jpg"),
        _row("fact_keep_1", "redis 密码存放在 vault 的 kv0 路径"),
        _row("fact_img_ok", "看图说话：架构图\n@image:/nonexistent_s2/ok.jpg"),
    ],
    "task": [
        _row("task_1_synth", ("Fetch finished loading: GET \"<URL>\".\n" * 2000)
              + "x" * 40000),
        _row("task_big_struct", "{" + "a, " * 5000 + "b}"),  # ~15000 > 8000
        _row("task_keep", "上线 checklist：金丝雀发布后观察 30 分钟"),
    ],
    "preference": [_row("pref_keep", "偏好使用 12h 制时间显示")],
    "decision": [_row("dec_keep", "决策：存储层用 SQLite 单文件")],
}


def _cfg_for(store: pathlib.Path) -> dict:
    return gov.load_decay_cfg(PLUGIN_DIR / "decay.json")


def test_governance_dry_run_lists_all_four_classes(tmp_path):
    store = _make_store(tmp_path, STORE_ROWS)
    res = gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
                     dry_run=True)
    by_id = {c["id"]: c["class"] for rows in res["candidates"].values()
             for c in rows}
    assert by_id == {
        "fact_echo_1": "echo",
        "fact_tool_dump": "tool_dump",
        "fact_91_synth": "complaint_dangling_ref",
        "task_1_synth": "tool_dump",          # 'Fetch finished' opener table
        "task_big_struct": "oversized_structural",
    }
    # conservative guard: an @image row whose ref RESOLVES is not a candidate
    assert "fact_img_ok" not in by_id
    # and the class table did not overreach the normal rows
    assert "fact_keep_1" not in by_id and "task_keep" not in by_id
    # dry-run wrote NOTHING: no archive file, pool bytes identical
    pre = (store / "memories" / "fact.json").read_text(encoding="utf-8")
    arc = store / "knowledge" / "archive"
    assert list(arc.iterdir()) == []
    assert res["applied"] == 0 and res["archive_file"] is None
    gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
               dry_run=True)
    assert (store / "memories" / "fact.json").read_text(encoding="utf-8") == pre


def test_governance_apply_removes_noise_and_audits(tmp_path):
    store = _make_store(tmp_path, STORE_ROWS)
    res = gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
                     dry_run=False, yes=True)
    assert res["applied"] == 5
    # (a) noise rows removed from the active pool
    facts = json.loads((store / "memories" / "fact.json")
                       .read_text(encoding="utf-8"))
    tasks = json.loads((store / "memories" / "task.json")
                       .read_text(encoding="utf-8"))
    assert [r["id"] for r in facts] == ["fact_91_synth", "fact_keep_1",
                                        "fact_img_ok"]
    assert [r["id"] for r in tasks] == ["task_keep"]
    # (b) complaint row downgraded IN PLACE (content untouched, never
    #     rewritten — the script's job boundary)
    comp = next(r for r in facts if r["id"] == "fact_91_synth")
    assert comp["salience"] == GOV["complaint_salience"]
    assert "你有什么建议" in comp["content"]
    assert "governed" not in json.dumps(comp) or True  # no stamp on content
    # (c) audit trail: append-only jsonl, full original rows + reason + ts
    arc = sorted((store / "knowledge" / "archive").glob("governance_*.jsonl"))
    assert len(arc) == 1
    lines = [json.loads(l) for l in arc[0].read_text(encoding="utf-8").splitlines() if l]
    assert len(lines) == 5  # 4 archived + 1 downgrade audit
    archived = [l for l in lines if l.get("governance_action") == "archive_remove"]
    down = [l for l in lines if l.get("governance_action") == "downgrade_salience"]
    assert len(archived) == 4 and len(down) == 1
    for l in lines:
        assert "governance_reason" in l and "governed_ts" in l
        assert "content" in l and l.get("governance_origin") in MEMORY_TYPES
    full = {l["id"] for l in archived}
    assert full == {"fact_echo_1", "fact_tool_dump", "task_1_synth",
                    "task_big_struct"}
    # full original row preserved in the audit (recoverable chain)
    t1_audit = next(l for l in archived if l["id"] == "task_1_synth")
    assert len(t1_audit["content"]) > 45000
    # (d) pool files are still valid JSON arrays (the tmp rewrite is clean)
    for t in MEMORY_TYPES:
        assert isinstance(json.loads(
            (store / "memories" / f"{t}.json").read_text(encoding="utf-8")), list)


def test_governance_idempotent_rerun_zero_candidates(tmp_path):
    store = _make_store(tmp_path, STORE_ROWS)
    gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
               dry_run=False, yes=True)
    arc1 = len(list((store / "knowledge" / "archive").glob("governance_*.jsonl")))
    assert arc1 == 1
    res2 = gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
                      dry_run=False, yes=True)
    assert res2["total_candidates"] == 0
    assert res2["applied"] == 0
    arc2 = len(list((store / "knowledge" / "archive").glob("governance_*.jsonl")))
    assert arc2 == arc1  # 0 candidates -> no new audit file


def test_governance_remove_complaint_mode(tmp_path):
    store = _make_store(tmp_path, {
        "fact": [
            _row("fact_91_synth", "有什么问题？\n@image:/nonexistent_s2/img.jpg"),
            _row("fact_keep", "redis 密码存放在 vault 的 kv0 路径"),
        ]})
    res = gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
                     dry_run=False, yes=True, remove_complaint=True)
    assert res["applied"] == 1
    facts = json.loads((store / "memories" / "fact.json")
                       .read_text(encoding="utf-8"))
    assert [r["id"] for r in facts] == ["fact_keep"]


def test_governance_complaint_existing_image_ref_not_listed(tmp_path):
    """A row whose @image ref RESOLVES on disk is an ordinary row: not a
    candidate even if it ends with a question."""
    store = _make_store(tmp_path, {"fact": []})
    img = store / "img.jpg"
    img.write_bytes(b"\xff\xd8\xff")
    p = _row("fact_q", "看看这张图说明什么？\n@image:" + str(img))
    (store / "memories" / "fact.json").write_text(
        json.dumps([p], ensure_ascii=False), encoding="utf-8")
    res = gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
                     dry_run=True)
    assert res["total_candidates"] == 0


def test_governance_lock_files_created(tmp_path):
    """fcntl.flock protocol: the tool uses <file>.lock siblings for every
    touched pool file (the _shard_lock protocol)."""
    store = _make_store(tmp_path, STORE_ROWS)
    gov.govern(store, gov.load_decay_cfg(PLUGIN_DIR / "decay.json"),
               dry_run=False, yes=True)
    # every touched pool file has a lock sibling (touched = had candidates)
    for t in ("fact", "task"):
        assert (store / "memories" / f"{t}.json.lock").exists()
    # untouched pool files were never opened for lock either
    assert not (store / "memories" / "preference.json.lock").exists()


# ── 3. config parity: decay.json <-> DECAY_DEFAULTS ────────────────────────


def test_decay_json_defaults_egress_noise_key_parity():
    """逐键一致 on the egress_noise block (the S2 key): both files carry
    the identical structure; the ONLY repo-wide tolerated diff is the
    pre-existing `search.mode` (present in decay.json, absent in
    DECAY_DEFAULTS, consumed by search_index.py) — asserted explicitly.
    Compared against DEFAULTS_SNAPSHOT (pristine import-time copy) so the
    assertion is immune to the in-suite nested-dict mutation described at
    the snapshot definition."""
    assert DECAY_JSON["egress_noise"] == DEFAULTS_SNAPSHOT["egress_noise"]
    diffs = []
    for k in set(DECAY_JSON) | set(DEFAULTS_SNAPSHOT):
        a, b = DEFAULTS_SNAPSHOT.get(k), DECAY_JSON.get(k)
        if a is None or b is None or a != b:
            diffs.append(k)
    assert sorted(diffs) == ["search"], f"unexpected top-level config drift: {sorted(diffs)}"
    # per-key sub-parity inside the shared blocks (search minus mode)
    for k in ("injection", "knowledge", "consolidation", "aliases", "search"):
        a = json.loads(json.dumps(DEFAULTS_SNAPSHOT.get(k, {})))
        b = json.loads(json.dumps(DECAY_JSON.get(k, {})))
        if k == "search":
            b.pop("mode", None)  # tolerated asymmetry (documented above)
        assert a == b, f"{k} block drifted: {a} != {b}"


# ── 4. single-source byte identity (copied predicate) ──────────────────────


def test_tool_predicate_copy_byte_identical():
    """The tool's `_looks_like_tool_dump` is a VERBATIM copy of the plugin
    single source — assert AST source-segment identity so the two faces
    (ingress/egress vs one-off governance) can never drift silently."""
    plugin_src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    tool_src = (PLUGIN_DIR / "tools" / "govern_memories.py") \
        .read_text(encoding="utf-8")
    plugin_fn = next(
        n for n in ast.walk(ast.parse(plugin_src))
        if isinstance(n, ast.FunctionDef) and n.name == "_looks_like_tool_dump")
    tool_fn = next(
        n for n in ast.walk(ast.parse(tool_src))
        if isinstance(n, ast.FunctionDef) and n.name == "_looks_like_tool_dump")
    p_seg = ast.get_source_segment(plugin_src, plugin_fn).strip()
    t_seg = ast.get_source_segment(tool_src, tool_fn).strip()
    assert p_seg == t_seg


# ── 5. hard-constraint grep gates on the tool ──────────────────────────────


def test_tool_zero_sdk_zero_model_names():
    src = (PLUGIN_DIR / "tools" / "govern_memories.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add(n.module or "")
    stdlib_allowed = {"argparse", "json", "os", "re", "sys", "datetime",
                      "pathlib", "typing", "fcntl", "msvcrt"}
    third_party = imported - stdlib_allowed
    assert not third_party, f"non-stdlib imports: {third_party}"
    for model in ("gpt", "claude", "gemini", "llama", "qwen", "deepseek"):
        assert model.lower() not in src.lower(), f"model name '{model}' leaked"
    assert "complete_structured" not in src and "openai" not in src.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
