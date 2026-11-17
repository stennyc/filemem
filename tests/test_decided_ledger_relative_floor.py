"""Decided-ledger RELATIVE floor for short signatures (owner ruling 2026-11-17).

Problem (root-caused same day): min_shared=9 (the F2 ruling that kills CJK
false-positive suppression of DISTINCT long topics) is unreachable for SHORT
mixed-script topics — a short reworded variant of an adjudicated short topic
shares < 9 distinct tokens with the ledger row, so the re-ask loop survives
for exactly the short-latin topics (e.g. "github token 全局 access").

Fix: the per-comparison floor is RELATIVE for short sigs:
    eff = min(min_shared, max(3, min(len(sig_a), len(sig_b)) // 3))
  * long sigs (>= 27 tokens): eff = 9 = the owner's F2 floor, UNCHANGED —
    two distinct long CJK topics sharing 8 tokens must still NOT be suppressed;
  * short sigs: eff drops to min_len//3 (never below the absolute floor 3,
    so a 1-2 token coincidence can never suppress a DISTINCT topic);
  * the legacy min_shared=3 knob (rollback): eff = min(3, max(3, x)) = 3
    constant — byte-identical to the pre-change absolute behavior.

Symmetric: applied in BOTH the read side (_is_topic_decided) and the
write/fold side (_record_topic_decided) so short rewords FOLD into the
decided row instead of spawning parallel rows.

Red line: plugin source + tests + knob only; host zero-change.
"""
import copy
import json
import pathlib
import sys
import threading

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")
sys.path.insert(0, r"C:/Users/user/AppData/Local/hermes/hermes-agent")

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402


def _make_store(tmp: pathlib.Path) -> pathlib.Path:
    store = tmp / "filemem"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export",
                "local_semantic", "ledgers"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps([], ensure_ascii=False, indent=2), encoding="utf-8")
    return store


def _make_provider(tmp: pathlib.Path, min_shared: int = 9,
                   session_id: str = "s-rf") -> FileMemProvider:
    """Same fixture as test_decided_ledger, but min_shared=9 (production)."""
    store = _make_store(tmp)
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.source_profile = "rf"
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.pending_queue_path = store / "consolidated" / "pending_upgrades.jsonl"
    p._decay = copy.deepcopy(DECAY_DEFAULTS)
    p._decay.setdefault("consolidation", {})
    p._decay["consolidation"].setdefault(
        "decided_ledger", {"enable": True, "min_shared": 3})
    p._decay["consolidation"]["decided_ledger"]["enable"] = True
    p._decay["consolidation"]["decided_ledger"]["min_shared"] = min_shared
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._session_drafts = {}
    p._main_runtime = None
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._no_popup_gate = lambda *a, **k: False
    return p


# ── fixture texts (sig sizes computed by _topic_sig, asserted, not guessed) ─

# SHORT pair: 10-token sigs, sharing exactly 6 distinct tokens.
SHORT_A = "github token 全局 access 安装"
SHORT_B = "github token 全局 access 设置"

# distinct SHORT topic, shares exactly 1 token ("filemem") with SHORT_A's
# stamped row after the fold below — must NEVER be suppressed.
SHORT_DISTINCT = "python 脚本 解析 pdf 表格"

# LONG CJK pair (both sig >= 27): distinct topics sharing exactly 5 unigram
# tokens and ZERO bigrams — the F2 red line: at the owner's floor of 9 they
# must NOT be suppressed, and the relative floor must not loosen the 9 for
# long sigs.
LONG_X = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
LONG_Y = "戌天亥地辰人月卯山子石寅日丑光"

# LONG reword pair: near-permutation, must stay suppressed (old + new).
LONG_V1 = "依赖安装自动补装宿主环境缺失"
LONG_V2 = "依赖安装宿主环境缺失自动补装"


def test_fixture_sig_sizes_are_as_designed():
    """Guard the hand-derived token math the floor tests rely on."""
    sa = FileMemProvider._topic_sig(SHORT_A)
    sb = FileMemProvider._topic_sig(SHORT_B)
    assert len(sa) == 9 and len(sb) == 9, f"short sigs {len(sa)}/{len(sb)}"
    assert len(sa & sb) == 6, f"short shared {len(sa & sb)} (want 6)"
    sd = FileMemProvider._topic_sig(SHORT_DISTINCT)
    assert len(sa & sd) == 0 and len(sb & sd) == 0, \
        "distinct short topic must share no tokens"
    lx = FileMemProvider._topic_sig(LONG_X)
    ly = FileMemProvider._topic_sig(LONG_Y)
    assert len(lx) >= 27 and len(ly) >= 27, \
        f"long sigs {len(lx)}/{len(ly)} must both exceed 27 (F2 floor)"
    assert len(lx & ly) == 5, f"long distinct shared {len(lx & ly)} (want 5)"
    lv1 = FileMemProvider._topic_sig(LONG_V1)
    lv2 = FileMemProvider._topic_sig(LONG_V2)
    assert len(lv1 & lv2) >= 9, "long reword variant must share >= 9 tokens"


# ── helper unit tests (the floor function itself) ──────────────────────────


def test_eff_floor_caps_at_min_shared_for_long_sigs():
    # sig >= 27 -> owner's F2 floor 9 stands untouched.
    assert fm._decided_eff_floor(9, 27, 29) == 9
    assert fm._decided_eff_floor(9, 40, 80) == 9
    # one step below the 27-token hand-off: 26//3 = 8.
    assert fm._decided_eff_floor(9, 26, 26) == 8


def test_eff_floor_is_relative_for_short_sigs():
    assert fm._decided_eff_floor(9, 10, 10) == 3      # 10//3 = 3
    assert fm._decided_eff_floor(9, 13, 13) == 4      # 13//3 = 4
    assert fm._decided_eff_floor(9, 19, 19) == 6      # 19//3 = 6
    assert fm._decided_eff_floor(9, 19, 6) == 3       # min-side 6//3 = 2 -> floor 3
    assert fm._decided_eff_floor(9, 4, 40) == 3       # absolute floor 3


def test_eff_floor_preserves_legacy_min_shared_3_knob():
    # the enable=false / min_shared=3 rollback path stays byte-identical:
    # eff = min(3, max(3, x)) = 3 for every sig size.
    for a, b in ((4, 4), (10, 10), (27, 29), (80, 80)):
        assert fm._decided_eff_floor(3, a, b) == 3


# ── read side: _is_topic_decided ───────────────────────────────────────────


def test_short_reword_is_suppressed_at_production_floor(tmp_path):
    """RED against absolute-9 code: 6 shared tokens < 9 was a re-ask leak."""
    p = _make_provider(tmp_path)  # min_shared=9
    p._record_topic_decided(SHORT_A, "discard")
    assert p._is_topic_decided(SHORT_B) == "discard"


def test_distinct_short_topic_is_not_misjudged(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(SHORT_A, "discard")
    assert p._is_topic_decided(SHORT_DISTINCT) is None


def test_f2_invariant_distinct_long_cjk_topics_still_need_9(tmp_path):
    """The owner's 2026-11-17 F2 ruling: distinct long CJK topics sharing
    5-8 tokens must NOT be suppressed — the relative floor must not loosen
    the 9-token floor for long sigs."""
    p = _make_provider(tmp_path)
    p._record_topic_decided(LONG_X, "discard")
    assert p._is_topic_decided(LONG_Y) is None, \
        "distinct long topics sharing < 9 tokens must not be suppressed"


def test_long_reword_variant_still_suppressed(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(LONG_V1, "discard")
    assert p._is_topic_decided(LONG_V2) == "discard"


def test_verdict_filter_still_respected_under_relative_floor(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(SHORT_A, "confirm")
    # confirm-only ledger must not answer a discard-filtered query:
    assert p._is_topic_decided(SHORT_B, verdicts=("discard",)) is None
    # ...but the any-verdict gate (the re-ask door) still suppresses:
    assert p._is_topic_decided(SHORT_B) == "confirm"


def test_legacy_min_shared_3_knob_behavior_unchanged(tmp_path):
    """Rollback knob: with min_shared=3 the floor is the constant 3 —
    the pre-change absolute semantics (a 1-2 token overlap never suppresses,
    a 3-token overlap does)."""
    p = _make_provider(tmp_path, min_shared=3)
    p._record_topic_decided(SHORT_A, "discard")
    assert p._is_topic_decided(SHORT_B) == "discard"
    assert p._is_topic_decided(SHORT_DISTINCT) is None


# ── write side: _record_topic_decided folds symmetrically ─────────────────


def test_short_reword_folds_into_decided_row_not_new_row(tmp_path):
    """RED against absolute-9 code: 6 < 9 spawned a parallel row; under the
    relative floor the reword UPSERTS the one row (sig union, no dup)."""
    p = _make_provider(tmp_path)  # min_shared=9
    p._record_topic_decided(SHORT_A, "discard")
    p._record_topic_decided(SHORT_B, "discard")
    rows = json.loads(
        (p.storage_dir / "ledgers" / "decided.json").read_text(encoding="utf-8"))
    assert len(rows) == 1, f"short reword must upsert, got {len(rows)} rows"
    assert len(rows[0]["sig"]) > len(FileMemProvider._topic_sig(SHORT_A)), \
        "fold must union the sigs"


def test_distinct_short_topic_keeps_separate_row(tmp_path):
    p = _make_provider(tmp_path)
    p._record_topic_decided(SHORT_A, "discard")
    p._record_topic_decided(SHORT_DISTINCT, "confirm")
    rows = json.loads(
        (p.storage_dir / "ledgers" / "decided.json").read_text(encoding="utf-8"))
    assert len(rows) == 2, "distinct short topic must NOT fold into the row"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
