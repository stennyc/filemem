"""Owner 2026-10-12 ruling: draft-decision surface polish.

The [未决草稿] / [记忆报告] panels were judged (by the owner) too opaque to
act on:
  1. bare session ids with no topic — the user cannot tell which session
     `20261011_053536_609fc4` was;
  2. L0/L1/L2 tier codes with no explanation — the user cannot tell what a
     tier means before confirming/discarding;
  3. raw tool-output JSON fragments listed as "memories to judge" — the
     user cannot make a confirm/discard decision about a JSON blob.

This locks the three fixes:
  - `[session <id> · <title>]` header on the historical face: title = first
    user message of sessions/<id>.jsonl (fail-soft: unreadable/absent
    trace -> bare-id header, unchanged legacy shape);
  - LEVEL_LEGEND footer line on BOTH faces ([未决草稿] + [记忆报告])
    explaining L0/L1/L2 in one line;
  - noise-row filter: a pending row whose content is a JSON blob /
    truncated JSON fragment is NOT surfaced on either face (stays in
    draft storage for audit; the panel just skips it). A file whose rows
    are ALL noise/superseded yields NO notice at all (an empty body
    claiming "drafts await confirmation" is the same defect we're fixing).

Red line held: plugin source + tests only, host zero-change.
"""
import json, pathlib, sys, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402

NOISE_JSON = '{"status": "success", "output": "/usr/local/lib/hermes-agent/gateway/…'
TRUNCATED_JSON = '{"tasks": [{"id": "t_08c56714", "title": "P3-1 filemem…'
HUMAN_TEXT = "你这个拆的方式就已经补正确 -> root=/root/.hermes/plugins, path=knowledge-base-search"


def _make_provider(tmpdir: pathlib.Path, session_id: str = "s_cur",
                   profile: str = "test") -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = session_id
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(parents=True, exist_ok=True)
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = profile
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    p._decay = {}
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    return p


# ── _is_noise_confirm_row ──────────────────────────────────────────────────
def test_noise_json_blob_is_noise():
    assert FileMemProvider._is_noise_confirm_row(NOISE_JSON) is True


def test_noise_truncated_json_is_noise():
    assert FileMemProvider._is_noise_confirm_row(TRUNCATED_JSON) is True


def test_noise_complete_json_array_is_noise():
    assert FileMemProvider._is_noise_confirm_row('[1, 2, 3]') is True


def test_human_text_is_not_noise():
    assert FileMemProvider._is_noise_confirm_row(HUMAN_TEXT) is False


def test_empty_content_is_not_noise():
    # empty is skipped upstream, not mis-classified as noise
    assert FileMemProvider._is_noise_confirm_row("") is False


def test_human_text_starting_with_parenthesis_is_not_noise():
    assert FileMemProvider._is_noise_confirm_row("(见上文) 结论已确认") is False


# ── noise rule tightening (reviewer round 2, finding #3) ───────────────────
def test_cjk_brace_mark_human_text_is_kept():
    # "{注意} 模板要注意转义" starts with { but is a CJK human marker, not JSON
    assert FileMemProvider._is_noise_confirm_row("{注意} 这个模板要注意转义") is False


def test_cjk_bracket_mark_human_text_is_kept():
    assert FileMemProvider._is_noise_confirm_row("[注意] 这是人话说明") is False


def test_cjk_brace_metaphor_human_text_is_kept():
    assert FileMemProvider._is_noise_confirm_row("{这个括号是比喻，不是JSON}") is False


def test_truncated_json_with_ascii_key_pattern_is_noise():
    # truncated tool output still carries the "key": structure -> noise
    assert FileMemProvider._is_noise_confirm_row('{"file": "x.py", "lines": [1, 2, 3]') is True


def test_truncated_json_with_cjk_key_pattern_is_noise():
    # tool output truncated at a CJK-quoted key still carries the key pattern
    assert FileMemProvider._is_noise_confirm_row('{"任务": "P3-1 filemem 低价值草稿静默…') is True


def test_complete_json_dict_is_noise():
    assert FileMemProvider._is_noise_confirm_row('{"status": "dispatched", "count": 1}') is True


# ── _session_title_hint ────────────────────────────────────────────────────
def _write_trace(p: FileMemProvider, sid: str, first_user: str,
                 assistant_first: bool = False) -> None:
    lines = []
    if assistant_first:
        lines.append(json.dumps({"role": "assistant", "content": "assistant reply"}))
    lines.append(json.dumps({"role": "user", "content": first_user}))
    (p.sessions_dir / f"{sid}.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def test_title_hint_reads_first_user_message():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _write_trace(p, "abc", "filemem 挂账处理")
    assert p._session_title_hint("abc") == "filemem 挂账处理"


def test_title_hint_skips_assistant_first_line():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _write_trace(p, "abc", "filemem 挂账处理", assistant_first=True)
    assert p._session_title_hint("abc") == "filemem 挂账处理"


def test_title_hint_missing_trace_is_empty():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    assert p._session_title_hint("nope") == ""


def test_title_hint_empty_sid_is_empty():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    assert p._session_title_hint("") == ""


def test_title_hint_truncates_long_topics():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    _write_trace(p, "abc", "x" * 100)
    hint = p._session_title_hint("abc")
    assert hint == "x" * 40 + "…"


def test_title_hint_malformed_trace_line_is_skipped():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp)
    (p.sessions_dir / "abc.jsonl").write_text(
        "not-json-line\n" + json.dumps({"role": "user", "content": "真主题"}) + "\n",
        encoding="utf-8")
    assert p._session_title_hint("abc") == "真主题"


# ── [未决草稿] header: session title attached ───────────────────────────────
def test_unresolved_header_carries_session_title():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    # historical draft file for a DIFFERENT (closed) session
    _write_trace(p, "hist1", "code_map root 拆分讨论")
    df = p.storage_dir / "drafts_hist1.json"
    df.write_text(json.dumps([{"id": "d1", "type": "fact",
                               "content": HUMAN_TEXT, "level": "L2",
                               "metadata": {}}]), encoding="utf-8")
    ctx = p._pending_draft_notice("", session_id="s_cur")
    assert "[未决草稿]" in ctx
    assert "[session hist1 · code_map root 拆分讨论]" in ctx
    # 2026-11-08: the [来源: …] tag is now always shown on each row (the
    # user's judgment basis). HUMAN_TEXT is unstamped -> heuristic default.
    assert f"  1. [L2] [来源: inferred] {HUMAN_TEXT}" in ctx


def test_unresolved_header_falls_back_to_bare_id_without_trace():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    df = p.storage_dir / "drafts_hist2.json"
    df.write_text(json.dumps([{"id": "d1", "type": "fact",
                               "content": HUMAN_TEXT, "level": "L1",
                               "metadata": {}}]), encoding="utf-8")
    ctx = p._pending_draft_notice("", session_id="s_cur")
    assert "[session hist2]" in ctx
    assert "·" not in ctx.split("[session hist2]")[0]


# ── [未决草稿] noise rows are not surfaced ─────────────────────────────────
def test_noise_draft_rows_are_not_listed():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    _write_trace(p, "hist3", "工具输出会话")
    rows = [
        {"id": "n1", "type": "fact", "content": NOISE_JSON,
         "level": "L0", "metadata": {}},
        {"id": "h1", "type": "fact", "content": HUMAN_TEXT,
         "level": "L1", "metadata": {}},
    ]
    (p.storage_dir / "drafts_hist3.json").write_text(
        json.dumps(rows), encoding="utf-8")
    ctx = p._pending_draft_notice("", session_id="s_cur")
    assert "[未决草稿]" in ctx
    assert "h1" not in ctx  # ids never surfaced (A-site red line)
    assert HUMAN_TEXT in ctx
    assert NOISE_JSON[:20] not in ctx  # the noise row is skipped


def test_all_noise_drafts_yield_no_notice():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    _write_trace(p, "hist4", "纯工具输出会话")
    rows = [
        {"id": "n1", "type": "fact", "content": NOISE_JSON,
         "level": "L0", "metadata": {}},
        {"id": "n2", "type": "fact", "content": TRUNCATED_JSON,
         "level": "L0", "metadata": {}},
    ]
    (p.storage_dir / "drafts_hist4.json").write_text(
        json.dumps(rows), encoding="utf-8")
    ctx = p._pending_draft_notice("", session_id="s_cur")
    assert "[未决草稿]" not in ctx  # empty body must not claim pending drafts


# ── level legend on both faces ─────────────────────────────────────────────
def test_legend_on_unresolved_notice():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    _write_trace(p, "hist5", "带图例会话")
    (p.storage_dir / "drafts_hist5.json").write_text(json.dumps([
        {"id": "d1", "type": "fact", "content": HUMAN_TEXT,
         "level": "L2", "metadata": {}}
    ]), encoding="utf-8")
    ctx = p._pending_draft_notice("", session_id="s_cur")
    assert FileMemProvider.LEVEL_LEGEND in ctx
    assert "L0 环境事实" in ctx and "L2 架构决策" in ctx


def test_legend_on_turn_report():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._push_pending_confirm("s_cur", "id_x", "L1", HUMAN_TEXT, kind="l0")
    panel = p._turn_report_line("s_cur")
    assert "[记忆报告]" in panel
    assert FileMemProvider.LEVEL_LEGEND in panel


# ── [记忆报告] noise rows skipped, human rows kept ─────────────────────────
def test_turn_report_skips_noise_rows():
    tmp = pathlib.Path(tempfile.mkdtemp())
    p = _make_provider(tmp, session_id="s_cur")
    p._push_pending_confirm("s_cur", "id_noise", "L0", NOISE_JSON, kind="l0")
    panel = p._turn_report_line("s_cur")
    assert panel == "" or "[记忆报告]" not in panel  # nothing left to judge
    # push a human row: now the panel surfaces it (and not the noise row)
    p._push_pending_confirm("s_cur", "id_human", "L1", HUMAN_TEXT, kind="l0")
    panel = p._turn_report_line("s_cur")
    assert "[记忆报告]" in panel
    assert HUMAN_TEXT in panel
    assert NOISE_JSON[:20] not in panel
    assert FileMemProvider.LEVEL_LEGEND in panel
