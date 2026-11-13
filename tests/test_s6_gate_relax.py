"""S4-T2 (S6-note) alias-axis recall-gate relaxation switch.

Blueprint: specs/S4-lifespan-selfheal-impl-notes.md S4-T2 S6-note (North
Star #4, YAGNI). The new opt-in config key `search.gate_relax_alias`
(default **False** -> the legacy substring gate is byte-identical; regression
stays green) lets a pool item the legacy substring gate rejects STILL pass
when the RRF/bm25 axis genuinely ranked it (fused_scores[pos] >= min_score).

Root cause it addresses: a CJK paraphrase query ("浏览器刷新后白屏" — no
spaces, so `query_words` = one big non-substring token) gets legacy_match=False
and is cut BEFORE the min_score filter. Once the alias table (aliases.json)
mines a Chinese canonical topic + alias, `_expand_query_with_aliases` widens
the bm25 axis — but the off-gate alias-axis candidate is still cut by the
legacy gate unless this switch is ON. Because the live store has NO
aliases.json yet, the switch is inert by default; this slice only adds the
switch and the synthetic test that proves "switch ON + alias data present ->
a CJK paraphrase goes 0 -> >=1".

The gate change is confined to the RRF branch (`fused_scores is not None`);
the `else` legacy-keyword branch is byte-identical. The switch is
multi-lingual-neutral: it is read from the decay `search` block — no hardcoded
CJK string lives in the gate code.

ALL stores are tmp synthetic. The test MUST fabricate aliases.json itself
(the alias table absent -> the switch relaxes nothing, so no fabrication
cannot observe the relaxation).
"""
import copy
import json
import pathlib
import sys
import threading
import types

import pytest  # noqa: F401

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))

# CJK paraphrase query + Chinese canonical topic + alias. The content of the
# alias-axis candidate is deliberately REPHRASED so it contains NONE of the
# expanded query's space-split words as a substring (legacy gate fails on it).
Q = "浏览器刷新后白屏"
ALIASES = {
    "topics": {
        "浏览器白屏": {"aliases": ["浏览器刷新后白屏", "页面白屏"],
                       "source": "human", "ts": "t"},
    },
    "ts": "t",
}


def _fact(rid: str, content: str, ts: str = "2026-09-15T00:00:00") -> dict:
    """L2 row (ttl None) — never expired, always in the RRF pool."""
    return {"id": rid, "type": "fact", "content": content, "level": "L2",
            "metadata": {}, "created_at": ts, "updated_at": ts,
            "salience": 0.5, "reference_count": 0, "ttl_days": None,
            "source": "auto"}


def _make_provider(tmp: pathlib.Path, decay: dict) -> FileMemProvider:
    """FULL synthetic provider wired against the tmp store (mirrors the S4
    suite): enough attributes for search_memories + the alias read face."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s-s6"
    p.source_profile = "s6-test"
    p.storage_dir = tmp
    p.memories_dir = tmp / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmp / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmp / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = None
    p._decay = decay
    p._llm_ctx = None
    p._alias_mining_in_flight = False
    return p


def _write_facts(p: FileMemProvider, rows: list) -> None:
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _write_aliases(p: FileMemProvider, data: dict) -> None:
    (p.storage_dir / "aliases.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _decay_with(relax: bool) -> dict:
    d = copy.deepcopy(DECAY_DEFAULTS)
    d["search"]["gate_relax_alias"] = relax
    return d


# ── default OFF: byte-identical to the legacy gate (regression green) ──────


def test_default_false_byte_identical_to_legacy(tmp_path):
    """gate_relax_alias=False (and key ABSENT, the pre-S6 decay shape) ->
    search_memories is byte-identical to the legacy gate: the alias-axis
    candidate the substring gate rejects is cut BEFORE the min_score filter,
    so a CJK paraphrase returns 0. This is the regression door — the existing
    golden behavior must not drift when the switch is off."""
    cand = _fact("ali", "刷新之后页面出现白屏")  # alias-axis only, off-gate
    rows = [cand]

    p_off = _make_provider(tmp_path, _decay_with(False))
    _write_facts(p_off, rows)
    _write_aliases(p_off, ALIASES)
    off = p_off.search_memories(Q, limit=5, min_score=0.3)
    assert off == [], f"OFF must stay 0-hit the off-gate CJK paraphrase: {off}"

    # key ABSENT (the pre-S6 decay block shape) is byte-identical to explicit
    # False — the switch default and the missing-key path must agree.
    d_absent = copy.deepcopy(DECAY_DEFAULTS)
    d_absent["search"].pop("gate_relax_alias", None)
    p_absent = _make_provider(tmp_path, d_absent)
    _write_facts(p_absent, rows)
    _write_aliases(p_absent, ALIASES)
    absent = p_absent.search_memories(Q, limit=5, min_score=0.3)
    assert absent == off == [], "absent-key and explicit-False must be identical"


def test_default_false_no_alias_table_noop(tmp_path):
    """OFF + no aliases.json (the live store's shape today) -> the expand
    is a no-op and the CJK paraphrase 0-hits, matching the pre-S6 result."""
    p = _make_provider(tmp_path, _decay_with(False))
    _write_facts(p, [_fact("ali", "刷新之后页面出现白屏")])
    assert not (p.storage_dir / "aliases.json").exists()
    assert p.search_memories(Q, limit=5, min_score=0.3) == []


# ── ON + alias data present: a CJK paraphrase goes 0 -> >=1 ─────────────────


def test_relax_on_cjk_paraphrase_zero_to_one(tmp_path):
    """The blueprint's core synthetic gate: gate_relax_alias=True + a
    fabricated aliases.json (Chinese canonical topic + alias). The alias-axis
    candidate the legacy substring gate rejects is admitted because the RRF/
    bm25 axis ranked it >= min_score -> the CJK paraphrase top-5 goes from 0
    (off) to >=1 (on)."""
    cand = _fact("ali", "刷新之后页面出现白屏")
    off_p = _make_provider(tmp_path, _decay_with(False))
    _write_facts(off_p, [cand])
    _write_aliases(off_p, ALIASES)
    off = off_p.search_memories(Q, limit=5, min_score=0.3)
    assert off == [], "precondition: OFF 0-hits the off-gate candidate"

    on_p = _make_provider(tmp_path, _decay_with(True))
    # re-materialize: search_mutates the pool dicts in place, so a fresh
    # provider + fresh file for the ON leg.
    on_p = _make_provider(tmp_path, _decay_with(True))
    _write_facts(on_p, [_fact("ali", "刷新之后页面出现白屏")])
    _write_aliases(on_p, ALIASES)
    on = on_p.search_memories(Q, limit=5, min_score=0.3)
    ids = [r["id"] for r in on]
    assert len(on) >= 1 and "ali" in ids, \
        f"ON must recall the off-gate alias-axis candidate: {ids}"
    # the admitted candidate is the alias-axis one, carrying its RRF score
    ali = next(r for r in on if r["id"] == "ali")
    assert ali.get("score", 0.0) >= 0.3, f"admitted candidate passes the gate: {ali}"


def test_relax_on_only_grows_never_tightens(tmp_path):
    """Relaxation is monotone-growth: a row the LEGACY gate already admits
    (its content is a substring of the expanded query) returns IDENTICALLY
    when the switch flips on -> the switch only ADs alias-axis candidates,
    it never removes a legacy-eligible hit."""
    sub = _fact("sub", "浏览器白屏 the browser white screen fix")  # legacy-eligible
    off_p = _make_provider(tmp_path, _decay_with(False))
    _write_facts(off_p, [sub])
    _write_aliases(off_p, ALIASES)
    off = off_p.search_memories(Q, limit=5, min_score=0.0)
    assert [r["id"] for r in off] == ["sub"], "the control row is legacy-eligible"

    on_p = _make_provider(tmp_path, _decay_with(True))
    _write_facts(on_p, [sub])
    _write_aliases(on_p, ALIASES)
    on = on_p.search_memories(Q, limit=5, min_score=0.0)
    assert on == off, "a legacy-eligible row must be unchanged by the switch"


def test_relax_on_requires_alias_data_or_matching_axis(tmp_path):
    """Boundary (blueprint key note): the switch only releases off-gate
    candidates the fused axis actually ranked. With NO aliases.json the
    expand is a no-op — so whether the off-gate CJK paraphrase recalls
    depends on the bm25/decay axis alone, not on the alias table. The
    switch never INVENTS a candidate; it just stops cutting ones the fused
    axis already scored >= min_score. (The live store's aliases.json is
    absent today, so this slice ships the switch inert by default — the
    0->1 demonstration above requires the fabricated alias table.)"""
    p = _make_provider(tmp_path, _decay_with(True))
    _write_facts(p, [_fact("ali", "刷新之后页面出现白屏")])
    assert not (p.storage_dir / "aliases.json").exists()
    on = p.search_memories(Q, limit=5, min_score=0.3)
    # the switch is a monotone GATE, not a recall source: it can only keep
    # a candidate the fused axis already ranked >= min_score. Assert the
    # result is a subset of what the fused axis admitted (never a NEW row
    # invented by the switch itself) — every returned row carries a
    # fused-derived score at-or-above the gate.
    for r in on:
        assert r.get("score", 0.0) >= 0.3, \
            f"the switch only admits fused-ranked rows: {r}"
    # and it never tightens: the same query with the switch OFF is a subset
    off_p = _make_provider(tmp_path, _decay_with(False))
    _write_facts(off_p, [_fact("ali", "刷新之后页面出现白屏")])
    off = off_p.search_memories(Q, limit=5, min_score=0.3)
    assert {r["id"] for r in off}.issubset({r["id"] for r in on}), \
        "switch ON must be a superset of switch OFF (growth, never tightening)"


# ── gate change is confined to the RRF branch (else branch byte-identical) ──


def test_gate_change_confined_to_rrf_branch(tmp_path):
    """The S6 edit is a single-line gate rewrite in the `fused_scores is not
    None` branch. The `else` legacy-keyword branch (ranking that falls back
    when the RRF axes are unavailable) is untouched — it still does the
    keyword-overlap `hits` qualification exactly as before."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    sm = src.split("def search_memories", 1)[1].split(
        "\n    def _memories_rotate_mb", 1)[0]
    # the opt-in flag is read ONCE, outside the loop (from the decay search block)
    assert '_gate_relax = bool(' in sm and 'gate_relax_alias' in sm
    # the RRF branch gate is the rewritten conditional (legacy_match AND the
    # opt-in fused-axis pass), not the bare `if not legacy_match: continue`
    assert "if not legacy_match and not (" in sm, \
        "the RRF branch gate must carry the opt-in fused-axis pass"
    assert "_gate_relax and fused_scores[pos] >= min_score" in sm
    # the else (legacy-keyword) branch keeps its ORIGINAL `hits` qualification
    else_seg = sm.split("if fused_scores is not None:", 1)[1]
    else_body = else_seg.split("else:", 1)[1]
    assert "hits = sum(1 for w in query_words if w and w in content_lower)" in else_body
    assert "if not hits:" in else_body
    # the opt-in flag is NOT introduced into the else branch
    assert "_gate_relax" not in else_body


# ── config surface: decay.json <-> DECAY_DEFAULTS per-key dual sync ─────────


def test_config_gate_relax_alias_dual_sync_per_key():
    """The new key is dual-synced (改表不改码): decay.json and DECAY_DEFAULTS
    carry the SAME value for `search.gate_relax_alias`, defaulting to False
    (OFF = regression-green, no production recall behavior change by default)."""
    assert DECAY_JSON["search"]["gate_relax_alias"] == \
        DECAY_DEFAULTS["search"]["gate_relax_alias"], \
        "search.gate_relax_alias must match between decay.json and DECAY_DEFAULTS"
    # multi-lingual-neutral + off by default: a Python bool False
    assert DECAY_DEFAULTS["search"]["gate_relax_alias"] is False
    assert DECAY_JSON["search"]["gate_relax_alias"] is False
    # the key lives ONLY in the search block (no stray top-level / other block)
    assert "gate_relax_alias" not in DECAY_JSON.get("consolidation", {})
    assert "gate_relax_alias" not in DECAY_JSON.get("aliases", {})


def test_gate_flag_multilingual_neutral_no_hardcoded_cjk(tmp_path):
    """Multi-lingual-neutral hard constraint: the gate reads the flag from the
    decay `search` block — the new gate code does NOT hardcode any CJK string
    (the CJK content lives only in the test fixtures, never in production)."""
    src = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    sm = src.split("def search_memories", 1)[1].split(
        "\n    def _memories_rotate_mb", 1)[0]
    gate_line = next(l for l in sm.splitlines()
                     if "_gate_relax and fused_scores[pos] >= min_score" in l)
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in gate_line), \
        f"the S6 gate line must not hardcode a CJK string: {gate_line!r}"
    # and the flag read itself is a pure decay-block lookup, no literal CJK
    flag_lines = [l for l in sm.splitlines() if "gate_relax_alias" in l]
    assert flag_lines and not any(
        "\u4e00" <= ch <= "\u9fff" for l in flag_lines for ch in l), \
        "the gate_relax_alias read must be multi-lingual-neutral"


def test_no_host_package_changes_in_diff(tmp_path):
    """S4-T2 is plugin-surface only: the changed-file set is confined to the
    plugin tree (__init__.py, consolidation/, decay.json, tests/) — no
    /usr/local/lib/hermes-agent path is touched."""
    import subprocess
    # 🔴 守卫 (同 S1b/S4-T2/S4-T1): 非 git 仓库 -> git diff 无意义 (exit 129),
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
              "tests/", "spec", "tools/", "shared_memory.py",
              "search_index.py", "graph_autorebuild.py", "plugin.yaml")), \
            f"unexpected changed path (non-plugin-surface): {c}"
