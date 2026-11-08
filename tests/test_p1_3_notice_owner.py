"""P1-3 + P1-4: owner filter on the pending-draft NOTICE face.

`_pending_draft_notice` is pre_llm_call's ONLY user-visible face. Its glob
collected EVERY shared-store `drafts_*.json` (excluding the caller's own
session), so a sibling profile's undecided drafts would pop a clarify
question in THIS profile's session (the user-visible cross-profile memory
face, second source alongside P0-2's read-side isolation).

P1-3 owner-filtered the pending collection per row on
`metadata.source_profile` (stamped at `_add_draft`); P1-4 tightened it to
PER-ROW semantics (`_draft_eligible_rows`):
  - a file enters the pending list only if it has >=1 ELIGIBLE row — stamped
    with the provider's own `source_profile`, OR UNSTAMPED (legacy neutral
    row);
  - the reported COUNT is the eligible row count, not the file's total row
    count (a mixed file's cross-profile rows no longer ride along in the
    number; the user approved this line-format exception);
  - row-read failure / missing metadata / non-dict rows -> NEUTRAL (listed,
    fail-soft — a corrupt row never locks the host out of its notice);
  - provider with no resolved identity -> NEUTRAL-ROWS-ONLY (supersedes the
    P0-2 notice-face fail-open full-listing rule): unstamped rows listed,
    ownership-stamped rows NEVER listed + one auditable logger.warning
    ("filemem: pending-draft notice: provider profile identity unresolved,
    neutral-rows-only mode");
  - optional decay.json `notice.cross_profile_skip_hint` (default false):
    true -> a listed file's line appends "（另有 N 行跨 profile 不列）";
    false -> line bytes identical to the pre-P1-4 format;
  - the one-shot gate, base line text, turn_line increment and all other
    fail-soft semantics are UNCHANGED (only the pending-list condition and
    count moved).

Zero LLM; host source zero-change.
"""
import json, logging, pathlib, sys, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider, MemoryItem  # noqa: E402


def _make_provider(tmpdir: pathlib.Path, session_id: str = "s_cur",
                   source_profile: str = "test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = source_profile
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p._pending_confirm = {}
    p.search_memories = lambda *a, **k: []  # 0-hit path -> notice face
    return p


def _row(content: str, source_profile=None) -> dict:
    meta = {}
    if source_profile is not None:
        meta["source_profile"] = source_profile
    return MemoryItem(id="d", type="fact", content=content,
                      metadata=meta).to_dict()


def _write(tmpdir: pathlib.Path, name: str, payload) -> None:
    with open(tmpdir / name, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


# ① cross-profile draft file: NOT listed, no clarify pop
def test_01_cross_profile_draft_file_not_listed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_4d.json", [_row("4d's draft", source_profile="4d")])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" not in ctx, ctx
    assert "drafts_s_4d.json" not in ctx, ctx
    # the gate did NOT fire for a notice-less pass: a later eligible file
    # in the same session must still be able to announce
    assert not p._pending_drafts_notified.get("s_cur")


# ② same-profile file: listed as before
def test_02_same_profile_file_listed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [_row("mine", source_profile="test")])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "drafts_s_old.json" not in ctx, ctx
    assert p._pending_drafts_notified.get("s_cur") is True


# ③ unstamped legacy file: listed (neutral passthrough)
def test_03_legacy_unstamped_file_listed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_legacy.json", [_row("old row", source_profile=None)])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "drafts_s_legacy.json" not in ctx, ctx


# ④ mixed file (1 own row + 2 cross rows): P1-4 per-row count = 1
def test_04_mixed_file_per_row_count():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_mixed.json", [
        _row("other's", source_profile="4d"),
        _row("mine", source_profile="test"),
        _row("other's too", source_profile="4d"),
    ])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "drafts_s_mixed.json" not in ctx, ctx
    # P1-4: cross-profile rows are never listed (owner filter held);
    # D-site: the eligible row's CONTENT is what is listed
    assert "mine" in ctx, ctx
    assert "other's" not in ctx, ctx
    # hint OFF (default): no skip hint text on the line
    assert "跨 profile" not in ctx, ctx


# ⑤ corrupt rows: fail-soft neutral (listed, host never locked out)
def test_05_row_corrupt_fail_soft_listed():
    # 5a: unparseable JSON file -> exception leg lists it (neutral)
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    with open(tmp / "drafts_s_bad.json", "w", encoding="utf-8") as f:
        f.write("{corrupt json")
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx, ctx
    assert "drafts_s_bad.json" not in ctx, ctx

    # 5b: parseable file whose rows are malformed shapes -> neutral -> listed
    p2 = _make_provider(tmp, "s_cur", "test")
    p2._pending_drafts_notified = {}
    _write(tmp, "drafts_s_shapes.json", [42, None, {"metadata": "not-a-dict"}])
    ctx2 = p2.prefetch("anything", session_id="s_cur")
    assert "[未决草稿]" in ctx2, ctx2
    # D-site: malformed rows have no content -> an honest unreadable line,
    # never a crash and never fabricated content
    assert "不可读" in ctx2, ctx2
    assert "drafts_s_shapes.json" not in ctx2, ctx2


# ⑥ one-shot gate semantics unchanged: same-session 2nd call does not re-fire
def test_06_one_shot_gate_semantics_unchanged():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_old.json", [_row("mine", source_profile="test")])
    assert "[未决草稿]" in p.prefetch("a", session_id="s_cur")
    assert "[未决草稿]" not in p.prefetch("b", session_id="s_cur")
    # a NEW caller session re-notifies (gate is sid-keyed, unchanged)
    assert "[未决草稿]" in p.prefetch("c", session_id="s_new")


# ⑦ P1-4 identity tightening (supersedes the P0-2 fail-open full-listing
#    rule on the notice face): no profile identity -> only NEUTRAL (unstamped)
#    rows are listed; ownership-stamped rows are NEVER listed; one auditable
#    warning line per shaped file.
def test_07_identity_unresolved_neutral_rows_only(caplog):
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", source_profile=None)
    _write(tmp, "drafts_s_stamped.json", [
        _row("4d's draft", source_profile="4d"),
        _row("test's draft", source_profile="test"),
    ])
    _write(tmp, "drafts_s_unstamped.json", [_row("neutral row")])
    with caplog.at_level(logging.WARNING):
        ctx = p.prefetch("anything", session_id="s_cur")
    # stamped file (every row ownership-stamped, no identity to match) ->
    # 0 eligible rows -> the whole file is NOT listed
    assert "drafts_s_stamped.json" not in ctx, ctx
    # unstamped file -> neutral rows listed (D-site: by CONTENT, not count)
    assert "drafts_s_unstamped.json" not in ctx, ctx
    assert "neutral row" in ctx, ctx
    # auditable rule-change warning, grep-able in the log
    assert any(
        "filemem: pending-draft notice: provider profile identity unresolved"
        in rec.message for rec in caplog.records
    ), [r.message for r in caplog.records]


# ⑧ empty draft files never listed (pre-existing semantics preserved)
def test_08_empty_draft_file_not_listed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    _write(tmp, "drafts_s_empty.json", [])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert ctx == "", repr(ctx)


# P1-4 ⑨ hint switch: decay.json notice.cross_profile_skip_hint=true ->
#    listed lines name the skipped cross-profile rows; false (default) ->
#    byte-identical base format (asserted in test_04 via "跨 profile" absent).
# D-site (2026-10-09): the hint switch is IGNORED on this face now — the
# per-row owner filter already hides cross-profile rows; the content face
# is byte-identical whether the hint is on or off.
def test_09_hint_on_names_skipped_cross_profile_rows():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    p._decay = {"notice": {"cross_profile_skip_hint": True}}
    _write(tmp, "drafts_s_mixed.json", [
        _row("other's", source_profile="4d"),
        _row("mine", source_profile="test"),
        _row("other's too", source_profile="4d"),
    ])
    ctx = p.prefetch("anything", session_id="s_cur")
    assert "drafts_s_mixed.json" not in ctx, ctx
    assert "mine" in ctx, ctx
    assert "other's" not in ctx, ctx
    assert "（另有 2 行跨 profile 不列）" not in ctx, ctx


def test_10_hint_off_default_is_byte_identical_base_format():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, "s_cur", "test")
    # no notice block at all (fresh {} _decay from the fixture) == default off
    _write(tmp, "drafts_s_mixed.json", [
        _row("other's", source_profile="4d"),
        _row("mine", source_profile="test"),
    ])
    ctx = p.prefetch("anything", session_id="s_cur")
    # D-site: the content face is identical regardless of the hint switch —
    # the eligible row's content line, cross-profile rows never listed.
    # Numbered list format (owner 2026-10-11): lines carry a 1-based
    # index so the clarify panel can reference items by number.
    # 2026-11-08: every row now carries a [来源: …] tag (judgment basis);
    # an unstamped row -> the heuristic default "inferred".
    line = [l for l in ctx.splitlines() if "条待处置" in l or "mine" in l]
    assert line == ["  1. [L0] [来源: inferred] mine"], ctx
