"""S3⑥ recurring-repetition (near-synonym) consolidate face — TDD spec.

User-complaint (10-09): the same standing instruction (e.g. "every write
needs my approval; reviewer after each fix; NEVER touch hermes native
code — this red line is not crossable") gets RE-SPOKEN in every new
session because the store accumulated N near-synonym rows under the same
directive, and the auto-retrieve face only injects the top-3 — so the
fresh, most-authoritative reworded copy doesn't reliably surface.

S3⑥ fixes the WORDING, not the decision: a dream tick (same async
consolidation thread as S3①-④, same 30-min timer + water-level trigger)
LLM-identifies such recurring near-synonym directive rows and pushes a
CLARIFY panel (kind="recurring") into _pending_confirm — the user's next
session turn lists it under [记忆报告] and the user decides. This is
ASYNCHRONOUS on purpose: the dream tick runs in a background thread, the
panel surfaces in the NEXT session of the SAME profile (storage is
per-profile — a "global" sediment IS the current profile's memory, so
there is no cross-profile option to ask about).

Spec (locked in, tests pin it):
  * A. _recurring_dedupe_consolidate():
      - deterministic coarse pre-filter (zero LLM): same source_profile
        stamp (or both unstamped-neutral), live rows (not superseded /
        stale), >=2 rows, AND pairwise Jaccard overlap >= supersede_min_overlap
        (the EP-1 knob, reused — no new threshold), AND at least one
        directive signal (row content OR its origin kind) — only DIRECTIVE
        clusters (standing rules / red lines) ask the user; plain fact
        clusters are left to S3① semantic-merge (scope split, no overlap).
      - LLM confirmation (one call per candidate cluster, capped by the
        new knob recurring_max_per_tick, default 2): the verdict says
        "these restate the SAME standing directive" (+ which row is the
        fullest/authoritative). NOT same_fact (S3①) and NOT
        contradiction (S3②) — this is the third, directive-specific lane.
      - on YES: push EXACTLY ONE clarify panel entry (kind="recurring")
        per cluster with options ["sediment", "dismiss"] — two options
        (用户拍板: 要/不要, no default/recommended/prefilled/hint field,
        裁决权 100% 用户). Panel payload names the cluster member ids +
        a one-line directive summary; the [记忆报告] turn line picks it
        up like any other pending item.
      - NO auto-merge, NO supersede stamping, NO DELETE at dream time.
        The dream face only ASKS. Applying the user's "sediment" answer
        happens on consumption (below) — the dream tick writes nothing
        into memories/*.json (append-only discipline, 不吞脏不静默).
      - fail-soft: main_runtime missing -> clean skip + tick_skip audit
        row (same contract as S3①); LLM says "not the same directive" or
        model_used=False -> no panel, no write; ANY exception leaves the
        pool untouched.
  * B. Consumption (in _tool_review_drafts, action="recurring"):
      - YES ("sediment"): one new L2 row is WRITTEN (type=decision,
        level="L2", source="recurring_sediment", metadata.cluster = the
        member ids, metadata.triage_decided_by="user"); every cluster
        member row gets metadata.superseded=True +
        superseded_by=<new row id> (soft flag, reversible, EP-2
        mechanism — no DELETE); audit row recurring_sediment_applied.
      - NO ("dismiss"): audit row recurring_sediment_dismissed + a
        cooldown stamp on the panel (re-asking for
        recurring_cooldown_days, default 7) — the dream face skips a
        cluster already dismissed inside the cooldown window.
      - either verdict consumes the pending panel entry (stops
        re-reporting, same _consume_pending_confirm path as triage).
      - cross-profile guard: a cluster member stamped by ANOTHER
        profile's source_profile is never sedimented from this session
        (same O1 rule as supersede — profile isolation is the point).
  * C. New knob recurring_max_per_tick (default 2) + recurring_cooldown_days
    (default 7) must exist on BOTH sides (DECAY_DEFAULTS + decay.json,
    S2 invariant: the only tolerated top-level drift is `search`).

Host-zero-change: this card only touches plugins/filemem/{__init__.py,
consolidation/__init__.py, decay.json, tests/};
/usr/local/lib/hermes-agent grep for _recurring_dedupe_consolidate = 0.
"""
import copy
import json
import pathlib
import sys

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))

# Reuse the S3 fixture skeleton (same source, no re-implementation).
from test_s3_dream_faces import (  # noqa: E402
    _row, _make_store, _make_provider, _decay_copy, _cons_log_rows,
)


def _fact_row(rid, content, **extra):
    row = _row(rid, content, mem_type="fact", **extra)
    # Stamp the SESSION profile (the S3 provider fixture's source_profile is
    # "s3-test") so the row is same-profile and eligible for the cluster
    # pool; a cross-profile stamp would be correctly dropped by O1.
    row.setdefault("metadata", {})["source_profile"] = "s3-test"
    return row


def _decay_with_directive_patterns() -> dict:
    """Test-specific decay: the DEFAULTS + a test-only directive pattern
    ('红线') so the fixture rows carry a directive signal WITHOUT changing
    the production table (改表不改码 — the face reads the table from
    decay, not from hardcoded strings in the code)."""
    import copy as _copy
    d = _decay_copy()
    inj = d.setdefault("injection", {})
    pats = inj.setdefault("directive_patterns", {"en": [], "zh": []})
    pats.setdefault("zh", []).append("红线")
    return d


def _pending_recurring(p, sid="s-s3"):
    pc = getattr(p, "_pending_confirm", None) or {}
    q = pc.get(sid, [])
    return [e for e in q if e.get("kind") == "recurring"]


def _directive_pairs():
    """Three near-synonym red-line rewordings — the user's exact 10-09
    symptom (7 fragment rows in production; 3 is enough for the spec)."""
    return [
        _fact_row("rl_1", "不可以修改任何 hermes 原生代码和文件，这条红线不可逾越",
                  created_at="2026-10-01T00:00:00"),
        _fact_row("rl_2", "任何 hermes 原生代码和文件（宿主树）零修改，只读",
                  created_at="2026-10-05T00:00:00"),
        _fact_row("rl_3", "不能碰不能修改任何hermes原生代码和文件",
                  created_at="2026-10-08T00:00:00"),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# A1: directive cluster -> LLM confirms -> exactly ONE 2-option panel, no
#     prefill, dream-time zero write into memories/*.json
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a1_directive_cluster_pushes_single_two_option_panel(tmp_path, monkeypatch):
    """近义红线三行 + LLM 判 same_directive=True -> 恰好 1 条 kind='recurring'
    面板, options 严格 {'sediment','dismiss'}, 无任何 prefill 字段;
    dream 阶段 零写 memories/*.json (行字节不变), 只留审计行."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True,
         "authoritative": "rl_3",
         "reason": "three rewordings of the same host-code red line"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    before = {t: (store / "memories" / f"{t}.json").read_text(encoding="utf-8")
              for t in MEMORY_TYPES
              if (store / "memories" / f"{t}.json").exists()}

    p._recurring_dedupe_consolidate()

    panels = _pending_recurring(p)
    assert len(panels) == 1, f"一个 cluster 推 1 条面板, 实得 {len(panels)}"
    e = panels[0]
    assert set(e.get("options", [])) == {"sediment", "dismiss"}, \
        f"二选项必须是 要/不要, 实得 {set(e.get('options', []))}"
    for pref_key in ("default", "recommended", "suggested", "prefilled", "hint"):
        assert pref_key not in e, f"面板预填了 {pref_key!r} — 违反 不预填 规格"
    cluster = set(str(x) for x in (e.get("cluster") or []))
    assert {"rl_1", "rl_2", "rl_3"} <= cluster, f"面板必须带 cluster 成员, 实得 {cluster}"
    # dream 阶段零写: 三份 fact.json 字节不变 (面板只入 _pending_confirm + 审计):
    after = {t: (store / "memories" / f"{t}.json").read_text(encoding="utf-8")
             for t in before}
    assert before == after, "recurring 面 dream 阶段不得写 memories/*.json (沉淀归消费侧)"
    aud = [a for a in _cons_log_rows(store) if a.get("action") == "recurring_dedupe"]
    assert aud, "recurring 面必须落审计行 (不许静默 no-op)"
    assert state["calls"] >= 1, "LLM 面必须真调宿主 (recurring 判定)"


# ═══════════════════════════════════════════════════════════════════════════
# A2: plain-fact cluster (no directive signal) -> NOT asked, left to S3①
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a2_plain_fact_cluster_not_asked(tmp_path, monkeypatch):
    """两条普通事实近义 (非指令/红线): 不推 clarify 面板 — 该归 S3① 语义合并面,
    recurring 面只问 指令类 聚集 (scope 钉死, 避免三张面互相弹)."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": False, "reason": "facts, not standing directives"},
    ])
    rows = [
        _fact_row("f_a", "hermes default venv path is installs/<hash>/environments"),
        _fact_row("f_b", "hermes venv lives under installs/<hash>/environments dir",
                  created_at="2026-10-02T00:00:00"),
    ]
    store = _make_store(tmp_path, {"fact": rows})
    p = _make_provider(tmp_path, store, _decay_copy())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert _pending_recurring(p) == [], "普通事实聚集不得进 recurring clarify 面"


# ═══════════════════════════════════════════════════════════════════════════
# A3: cross-profile cluster is refused at the coarse filter (profile isolation)
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a3_cross_profile_cluster_excluded(tmp_path, monkeypatch):
    """cluster 里混入别的 profile 戳的行 -> 确定性粗筛阶段就剔除 (O1 同规则):
    本 profile 的 recurring 面永不把跨 profile 行拉进面板/沉淀链."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_3", "reason": "same rule"},
    ])
    rows = _directive_pairs() + [
        _row("rl_x", "不可以修改任何 hermes 原生代码和文件（再重申）",
             mem_type="fact", created_at="2026-10-09T00:00:00"),
    ]
    rows[-1].setdefault("metadata", {})["source_profile"] = "orchestrator"
    store = _make_store(tmp_path, {"fact": rows})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    panels = _pending_recurring(p)
    if panels:
        cluster = {str(x) for x in (panels[0].get("cluster") or [])}
        assert "rl_x" not in cluster, "跨 profile 行不得进入本 profile 的 recurring 面板"


# ═══════════════════════════════════════════════════════════════════════════
# A4: independent N-cap + main_runtime missing -> clean skip + audit
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a4_n_cap_and_main_runtime_skip(tmp_path, monkeypatch):
    """N-cap: 4 个 真正独立 簇 (8 行, 簇内 2 行共享 >=2 专属 token, 簇间 0
    共享 token) + recurring_max_per_tick=1 -> 本 tick 恰好推 1 条面板;
    二次调用补推下一簇 (overflow 滚到下一 tick), 已面板化簇不重报;
    main_runtime 缺失 -> 整面 clean skip + tick_skip 审计行, 不炸, 不写."""
    # 4 独立簇: 簇 c 的 2 行共享 2 个专属拉丁 token (如 c0 共享 must+alpha),
    # 不同簇之间 0 共享 token (min_shared_tokens=2 门控保证簇间不连线).
    # 用拉丁词避免 CJK 2-gram 跨簇串线 (P2-24: 旧版 "红线{c}:" 前缀使 8 行
    # 合成 1 个 mega-cluster, N-cap 语义未被真正验证).
    rows = [
        _fact_row("c0_1", "must alpha zero",    created_at="2026-09-10T00:00:00"),
        _fact_row("c0_2", "must alpha one",     created_at="2026-09-11T00:00:00"),
        _fact_row("c1_1", "should beta two",    created_at="2026-09-10T01:00:00"),
        _fact_row("c1_2", "should beta three",  created_at="2026-09-11T01:00:00"),
        _fact_row("c2_1", "never gamma four",   created_at="2026-09-10T02:00:00"),
        _fact_row("c2_2", "never gamma five",   created_at="2026-09-11T02:00:00"),
        _fact_row("c3_1", "always delta six",   created_at="2026-09-10T03:00:00"),
        _fact_row("c3_2", "always delta seven", created_at="2026-09-11T03:00:00"),
    ]
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()
    decay["consolidation"]["recurring_max_per_tick"] = 1
    decay["injection"]["directive_patterns"]["en"].extend(
        ["must", "should", "never", "always"])
    p = _make_provider(tmp_path, store, decay)
    # dict 形式 replies: 按 簇内 专属词 substring 匹配 (位置无关, P2-8
    # 幂等前置优化前后 均稳定 — 不依赖 LLM 调用的全局序号):
    fake, state = install(monkeypatch, replies={
        "must alpha":   {"same_directive": True, "authoritative": "c0_1",
                         "reason": "same"},
        "should beta":  {"same_directive": True, "authoritative": "c1_1",
                         "reason": "same"},
        "never gamma":  {"same_directive": True, "authoritative": "c2_1",
                         "reason": "same"},
        "always delta": {"same_directive": True, "authoritative": "c3_1",
                         "reason": "same"},
    })
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1, \
        "N-cap=1 时本 tick 恰好推 1 条面板 (4 独立簇 -> 取最旧簇 c0)"

    # P2-26: overflow 滚到下一 tick — 二次调用补推下一簇, 已面板化簇不重报:
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 2, \
        "N-cap=1 第二次调用必须补推 1 条 (c1), 已面板化 c0 不得重报"
    # 面板 id 互异 (c0_1 + c1_1), 证明是 2 个不同簇而非重弹:
    ids = {e.get("id") for e in _pending_recurring(p)}
    assert ids == {"c0_1", "c1_1"}, \
        f"二次调用必须推 c1 簇面板 (c1_1), 实得 {ids}"

    # main_runtime 缺失 -> clean skip + audit, no raise, no write:
    store2 = _make_store(tmp_path / "s2", {"fact": [dict(r) for r in rows]})
    p2 = _make_provider(tmp_path, store2, _decay_with_directive_patterns(),
                        main_runtime=None)
    # _make_provider swallows main_runtime=None (its sentinel means "use
    # default"); set the attr directly to exercise the real no-runtime path.
    p2._main_runtime = None
    p2._recurring_dedupe_consolidate()  # must not raise
    assert _pending_recurring(p2) == []
    skips = [r for r in _cons_log_rows(store2)
             if r.get("action") == "tick_skip" and "main_runtime" in str(r.get("reason"))]
    assert skips, "main_runtime 缺失必须留 tick_skip 审计行"


def test_recurring_out_of_cluster_authoritative_refused(tmp_path, monkeypatch):
    """P1-21 (reviewer): LLM 幻觉出 cluster 外的 authoritative id ->
    面侧 pair-boundary 守卫拒绝 (0 面板, 无 push 审计行, 零写盘).
    测试 fake 整体替换了 caller (llm_caller.recurring_directive), caller
    侧守卫在 fake 路径不跑 — 面侧检查是唯一有效守卫, 本 spec 钉住它."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "ghost_row_not_in_cluster",
         "reason": "hallucinated anchor"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    before = {t: (store / "memories" / f"{t}.json").read_text(encoding="utf-8")
              for t in MEMORY_TYPES if (store / "memories" / f"{t}.json").exists()}
    p._recurring_dedupe_consolidate()
    assert _pending_recurring(p) == [], \
        "out-of-cluster authoritative 不得推面板 (P1-21)"
    after = {t: (store / "memories" / f"{t}.json").read_text(encoding="utf-8")
             for t in MEMORY_TYPES if (store / "memories" / f"{t}.json").exists()}
    assert before == after, "拒绝路径必须零写 memories/*.json"
    pushes = [r for r in _cons_log_rows(store)
              if r.get("action") == "recurring_dedupe"]
    assert pushes == [], f"拒绝路径不得留 push 审计行, 实得 {pushes}"


def test_recurring_caller_out_of_cluster_guard(tmp_path, monkeypatch):
    """P1-21 caller 侧 (reviewer): 直接调 真 llm_caller.recurring_directive
    (不 fake caller 本身), 走 已导入模块 的 _HOST_CALL_LLM seam (install()
    已 patch 它 — 同 fake, 直调 caller 函数).
    LLM 判 same_directive=True + authoritative=cluster 外 id -> caller 的
    pair-boundary 守卫翻成 same_directive=False (拒绝幻觉锚点)."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "ghost_out_of_cluster",
         "reason": "hallucinated"},
    ])
    import consolidation.llm_caller as _lc
    rows = [{"id": "rl_1", "content": "a"}, {"id": "rl_2", "content": "b"}]
    res = _lc.recurring_directive(_MAIN_RUNTIME, rows)
    assert res.get("same_directive") is False, \
        f"caller 必须拒绝 out-of-cluster 锚点 (翻成 False), 实得 {res}"
    assert res.get("model_used") is True  # 模型跑通了, 拒绝是守卫行为
    assert "boundary_refused:auth_not_in_cluster:ghost_out_of_cluster" \
        in str(res.get("reason")), \
        f"拒绝 reason 必须落痕 (P2-19), 实得 {res.get('reason')!r}"


# ═══════════════════════════════════════════════════════════════════════════
# A5: P1-5 (reviewer) — high-overlap directive pairs stay OUT of S3①
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a5_high_overlap_directive_pair_not_folded_by_s3_1(tmp_path, monkeypatch):
    """P1-5 (reviewer): 两端 都命中 directive 表的 高重叠 (Jaccard >= 0.5)
    指令对 不进 S3① semantic_merge 候选 — 该归 S3⑥ 的用户裁决 lane
    (directive 类 '必须问人' 的设计意图). 普通事实对不受影响."""
    # 两条高重叠 (token 几乎全同) 的 指令 行 + 两条 普通事实 高重叠对:
    rows = [
        _fact_row("d_1", "红线: 写盘前必须征得同意并调 reviewer 复盘",
                  created_at="2026-09-01T00:00:00"),
        _fact_row("d_2", "红线: 写盘前必须征得同意并调 reviewer 复盘不可逾越",
                  created_at="2026-09-02T00:00:00"),
        _fact_row("f_1", "hermes venv lives under installs/<hash>/environments",
                  created_at="2026-09-01T01:00:00"),
        _fact_row("f_2", "hermes venv path is under installs/<hash>/environments dir",
                  created_at="2026-09-02T01:00:00"),
    ]
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()  # 红线 在 directive 表
    p = _make_provider(tmp_path, store, decay)
    # dict-form replies: 指令对 的 prompt (含 红线) 判 not-same (即便 漏挡
    # 也不 误 fold); 事实对 判 same -> fold:
    fake, state = install(monkeypatch, replies={
        "红线": {"same_fact": False, "reason": "directive — not S3①'s lane"},
        "*": {"same_fact": True, "keep": "f_2", "supersede": ["f_1"],
              "reason": "same venv fact"},
    })
    p._semantic_merge_consolidate()
    # 候选里只有 普通事实 对 (f_1, f_2) — 指令对 (d_1, d_2) 被 directive
    # 排除挡在 S3① 之外 (留 S3⑥ 的用户裁决 lane):
    rows_after = {r["id"]: r for r in
                  json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert rows_after["f_1"]["metadata"].get("superseded") is True, \
        "普通事实对仍应走 S3① fold (排除只针对 directive 类)"
    # 核心 规格: 指令对 的 两行 都 不得 被 S3① 静默 fold — 且 更 强 的
    # 断言: 指令对 根本 不 进 LLM (S3① 的 prompt 里 不 出现 红线) —
    # '必须 问人' 的 directive lane 归 S3⑥, S3① 连 判 都 不 判:
    for prompt in state.get("prompts", []):
        assert "红线" not in prompt, \
            f"指令对 不得 进 S3① LLM 候选 (P1-5, 留 S3⑥ 用户 裁决 lane), prompt={prompt[:80]}"
    assert "superseded" not in rows_after["d_1"].get("metadata", {}), \
        "高重叠 指令对 不得 被 S3① 静默 fold (P1-5)"
    assert "superseded" not in rows_after["d_2"].get("metadata", {}), \
        "高重叠 指令对 不得 被 S3① 静默 fold (P1-5)"


# ═══════════════════════════════════════════════════════════════════════════
# A5b: F-1b (reviewer) — the SAME directive exclusion applies to S3②
#      (shared coarse filter): directive-vs-directive high-overlap pairs stay
#      out of _contradiction_detect's LLM candidates too (S3②'s stale-mark
#      lane is a deliberate no-no for user standing rules — a mutual-exclusion
#      directive pair is left to S3⑥'s clarify panel / manual adjudication).
#      Pins the user's (a)-decision: shared filter stays shared, no drift.
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a5b_directive_pair_not_in_s3_2_contradiction_candidates(tmp_path, monkeypatch):
    """F-1b: 两条 高重叠 directive 行 (两端 都 命中 '红线' 表) -> S3②
    _contradiction_detect 的 LLM 候选 里 不 出现 '红线' (共享 粗筛 的
    P1-5 排除 同样 作用于 矛盾 面 — 有意 为之, 留 S3⑥/人工 裁决).
    防 未来 给 _s3_pair_candidates 加 形参 分流 时 S3② 行为 漂移 无 人 察觉."""
    rows = [
        _fact_row("c_1", "红线: 写盘前必须征得同意并调 reviewer 复盘",
                  created_at="2026-09-01T00:00:00"),
        _fact_row("c_2", "红线: 写盘前必须征得同意并调 reviewer 复盘不可逾越",
                  created_at="2026-09-02T00:00:00"),
        # 一组 普通事实 高重叠对 — 保证 S3② 仍有 非 directive 候选 进 LLM
        # (证明 排除 只 挡 directive 对, 不 是 粗筛 整体 失 效):
        _fact_row("g_1", "hermes venv lives under installs/<hash>/environments",
                  created_at="2026-09-01T01:00:00"),
        _fact_row("g_2", "hermes venv path is under installs/<hash>/environments dir",
                  created_at="2026-09-02T01:00:00"),
    ]
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()
    p = _make_provider(tmp_path, store, decay)
    fake, state = install(monkeypatch, replies={
        "*": {"contradiction": False, "reason": "complementary, not exclusive"},
    })
    p._pending_confirm = {}
    p._contradiction_detect()
    prompts = state.get("prompts", [])
    # 核心 断言 (F-1b): directive 对 (c_1, c_2) 的 '红线' 不 进 S3② LLM 候选:
    for prompt in prompts:
        assert "红线" not in prompt, \
            f"directive 对 不得 进 S3② LLM 候选 (F-1b, 共享 粗筛 排除), prompt={prompt[:80]}"
    # 且 普通事实 对 (g_1, g_2) 仍 进 候选 (排除 精确 命中 directive, 粗筛 未 失效):
    assert any("venv" in prompt for prompt in prompts), \
        f"普通事实 高重叠对 仍 应 进 S3② LLM 候选 (排除 只 针对 directive 对), prompts={len(prompts)}"
    # 且 零 stale-mark (LLM 全 判 非 矛盾): 所有 行 保持 活:
    rows_after = {r["id"]: r for r in
                  json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    for rid in ("c_1", "c_2", "g_1", "g_2"):
        assert "stale" not in str(rows_after[rid].get("metadata", {})), \
            f"{rid} 不得 被 stale-mark (LLM 判 非 矛盾)"


# ═══════════════════════════════════════════════════════════════════════════
# A6: P1-3 (reviewer) — face-side corrupt shard read heals under lock
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a6_corrupt_shard_heal_under_face_read(tmp_path, monkeypatch):
    """P1-3 (reviewer T-12): 面侧读循环必须 锁内 走 heal helper —
    corrupt fact.json 注入 -> face 不炸 + 自愈 (rename 壳 + 审计行) +
    空池 clean (零面板). 违反 S4-T2 'corrupt check runs UNDER the
    shard lock at every call site' 不变量时 (无锁裸读) 并发下 torn
    JSON 误判 corrupt -> 池分叉 (数据可恢复但审计失真)."""
    import json as _json
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    # 注入 corrupt fact.json (覆写活文件为非法 JSON):
    (store / "memories" / "fact.json").write_text(
        "{ this is NOT valid json ]]", encoding="utf-8")
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_3", "reason": "same"},
    ])
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()  # must not raise
    # 自愈: 活文件重建 + 壳保留 + 审计行:
    fact = store / "memories" / "fact.json"
    assert fact.exists()
    assert json.loads(fact.read_text(encoding="utf-8")) == [], \
        "corrupt 活文件 必须 重建 为 空 []"
    shells = sorted((store / "memories").glob("fact.json.corrupt-*"))
    assert len(shells) == 1, f"corrupt 壳必须保留 1 个, 实得 {shells}"
    healed = [r for r in _cons_log_rows(store)
              if r.get("action") == "corrupt_memories_healed"]
    assert healed, "corrupt 自愈必须落 审计行 (S4-T2 不静默)"
    # 空池 -> 零面板 (行全在 壳里, 活文件 是 []):
    assert _pending_recurring(p) == [], \
        "corrupt 自愈后 活池 为空 -> 本 tick 不得推面板"


# ═══════════════════════════════════════════════════════════════════════════
# A7: P2-8 (reviewer) — idempotency check BEFORE the LLM call (no burn)
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a7_panelized_cluster_not_rellm_called(tmp_path, monkeypatch):
    """P2-8 (reviewer): 本 session 已推过 面板 的 cluster 再跑 face 不得
    重烧 LLM — 幂等检查 (pending 队列里 同 cluster 已有面板) 必须前置到
    LLM 调用 之前. 旧实现在 LLM 之后 才查 pending -> 白烧 一次 LLM."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_3", "reason": "same"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1
    calls_after_first = state["calls"]
    assert calls_after_first == 1, "首推 必须 烧 1 次 LLM"
    # 再跑 face (cluster 已 面板化, pending 里有): 不得 重烧 LLM:
    p._recurring_dedupe_consolidate()
    assert state["calls"] == calls_after_first, \
        f"已面板化 cluster 再跑 face 不得重烧 LLM (P2-8), 实得 calls={state['calls']}"
    # 面板 数 保持 1 (幂等 不重弹):
    assert len(_pending_recurring(p)) == 1


# ═══════════════════════════════════════════════════════════════════════════
# A8: P2-11 (reviewer) — per-cluster LLM input cap (mega-cluster guard)
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a8_mega_cluster_capped_at_recurring_max_rows(tmp_path, monkeypatch):
    """P2-11 (reviewer): mega-cluster (长期重述同一红线, 几十行) 不得 整簇
    喂 LLM (O(n²) 两两交集 + N 行×600 字符 塞进 200 token 输出 -> 判定
    劣化 成本失控). LLM 输入 只喂 最新 recurring_max_rows_per_cluster (默认 8)
    行; 沉淀时 全 cluster 挂链 (消费侧 用 panel.cluster 的 全量 成员 列表)."""
    # 12 行 同簇 (全含 '红线' + '写盘' 共享 >=2 token, 高重叠 合成 1 个
    # mega-cluster); recurring_max_rows_per_cluster=4 (旋钮 钉死 上限):
    rows = []
    for i in range(12):
        rows.append(_fact_row(
            f"m_{i}", f"红线 写盘 v{i}: 必须征得同意并复盘",
            created_at=f"2026-09-{15 + i:02d}T00:00:00"))
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()
    decay["consolidation"]["recurring_max_rows_per_cluster"] = 4
    p = _make_provider(tmp_path, store, decay)
    # 记录 LLM 实际 收到的 行数 (replies 用 动态 callable 捕获 prompt):
    fake, state = install(monkeypatch, replies={
        "*": {"same_directive": True, "authoritative": "m_11", "reason": "same"},
    })
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1
    # LLM 输入 prompt 里 的 行标记 ([i] id=...) 必须 被 砍到 <= 4 行:
    import re as _re
    prompt = state["prompts"][0] if state.get("prompts") else ""
    row_heads = _re.findall(r"\[\d+\] id=", prompt)
    assert len(row_heads) <= 4, \
        f"LLM 输入 必须 砍到 <= recurring_max_rows_per_cluster=4 行, 实得 {len(row_heads)}"
    # 但 panel.cluster 必须 是 全量 12 成员 (沉淀时 全 cluster 挂链):
    panel = _pending_recurring(p)[0]
    assert len(panel.get("cluster") or []) == 12, \
        f"panel.cluster 必须 携带 全量 成员 (沉淀 全 cluster 挂链), 实得 {len(panel.get('cluster') or [])}"


# ═══════════════════════════════════════════════════════════════════════════
# A9: F-4 (reviewer re-sign) — LLM names a TRUNCATED (pre-cap) row as
#     authoritative -> caller's pair-boundary guard (valid_ids = the rows
#     ACTUALLY FED, i.e. post-cap) refuses it. P2-11 截断语义 的 关键 行为.
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a9_truncated_row_as_authoritative_refused(tmp_path, monkeypatch):
    """F-4: 12 行 mega-cluster + 旋钮 4 -> LLM 只 收到 最新 4 行 (m_8..m_11).
    若 LLM 仍 报 出 被 截断 的 旧行 m_0 做 authoritative (它 没 被 展示,
    正常 不应 报) -> caller 的 valid_ids = 实际 喂出 的 4 行, 拒绝
    (boundary_refused:auth_not_in_cluster:m_0) -> 0 面板."""
    rows = []
    for i in range(12):
        rows.append(_fact_row(
            f"m_{i}", f"红线 写盘 v{i}: 必须征得同意并复盘",
            created_at=f"2026-09-{15 + i:02d}T00:00:00"))
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()
    decay["consolidation"]["recurring_max_rows_per_cluster"] = 4
    p = _make_provider(tmp_path, store, decay)
    fake, state = install(monkeypatch, replies={
        "*": {"same_directive": True, "authoritative": "m_0", "reason": "same"},
    })
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert _pending_recurring(p) == [], \
        "被截断旧行 做 authoritative 必须 被 拒绝 (0 面板, F-4)"
    # 拒绝 reason 落痕 (P2-19 + F-5: caller 守卫 翻 判 + 审计 链):
    import consolidation.llm_caller as _lc
    # 直调 caller 级守卫 (宿主层 fake, caller 的 解析 + 边界守卫 是 真码 —
    # face 级 L4598 与 caller 级 是 两级 独立 检查, 这里 钉 的 是 caller 级;
    # 宿主 应答 authoritative=m_0 而 实喂 仅 m_8/m_11 -> 守卫 拒绝):
    res = _lc.recurring_directive(_MAIN_RUNTIME,
                                  [{"id": "m_8", "content": "x"},
                                   {"id": "m_11", "content": "y"}])
    # 上面 的 直调 只 喂了 2 行 — m_0 不在 valid_ids -> 拒绝:
    assert res.get("same_directive") is False
    assert "boundary_refused:auth_not_in_cluster:m_0" in str(res.get("reason"))


# ═══════════════════════════════════════════════════════════════════════════
# A10: F-6 (reviewer re-sign) — pinned regression surface: high-overlap
#      plain-fact pair must stay in S3①'s LLM candidates (exclusion
#      condition over-wide / overlap threshold raised / S3① LLM broken
#      would drop 'venv' from the prompt). The and->or mis-edit needs a
#      high-overlap ONE-sided directive+fact fixture to be detectable —
#      out of this spec's scope (see docstring range note below).
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_a10_one_sided_directive_pair_still_in_s3_1(tmp_path, monkeypatch):
    """F-6: 钉 回归面: 高重叠 纯事实 pair (f_1, f_2) 必须 进 S3① LLM 候选
    — 排除 条件 过宽 / 重叠 阈值 抬高 / S3① 面 断 LLM 时 venv 从 prompt
    消失 -> 断言 1 变 红. 本 fixture 的 d_1 与 事实 行 词表 低 重叠
    (需 Jaccard >= 0.5 重叠 才 进 粗筛), 单向 directive+fact pair 的
    正向 进入 候选 行为 与 and->or 误改 均 不 在 本 spec 范围."""
    rows = [
        _fact_row("d_1", "红线: 写盘前必须征得同意并调 reviewer 复盘",
                  created_at="2026-09-01T00:00:00"),
        _fact_row("f_1", "hermes venv lives under installs/<hash>/environments",
                  created_at="2026-09-01T01:00:00"),
        # f_2 与 f_1 高重叠 (事实侧 pair, 保证 有 非 directive 对 进 候选):
        _fact_row("f_2", "hermes venv path is under installs/<hash>/environments dir",
                  created_at="2026-09-02T01:00:00"),
    ]
    store = _make_store(tmp_path, {"fact": rows})
    decay = _decay_with_directive_patterns()
    p = _make_provider(tmp_path, store, decay)
    fake, state = install(monkeypatch, replies={
        "*": {"same_fact": False, "reason": "distinct"},
    })
    p._semantic_merge_consolidate()
    # 核心 断言: S3① 的 LLM prompt 里 必须 出现 f_1 (事实行 进 候选),
    # 即 单端 directive 的 pair 不 被 排除 (and 语义 保持):
    prompts = state.get("prompts", [])
    assert prompts, "S3① 必须 有 LLM 候选 (事实对 f_1/f_2 高重叠)"
    assert any("venv" in prompt for prompt in prompts), \
        f"单端 directive pair 仍 应 进 S3① 候选 (F-6, and 语义), prompts={prompts[:2]}"
    # 且 零 fold (LLM 判 not-same): 所有 行 保持 活:
    rows_after = {r["id"]: r for r in
                  json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    for rid in ("d_1", "f_1", "f_2"):
        assert "superseded" not in rows_after[rid].get("metadata", {}), \
            f"{rid} 不得 被 fold (LLM 判 非 事实 等价)"


# ═══════════════════════════════════════════════════════════════════════════
# B1: consumption YES -> one new L2 decision row + members supersede-chained
#     + panel consumed + audit row
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_b1_sediment_writes_l2_and_supersede_chain(tmp_path, monkeypatch):
    """action='recurring' + verdict='sediment': 写 1 条 L2 decision 行
    (source='recurring_sediment', metadata.cluster=成员, triage_decided_by='user'),
    成员全部 metadata.superseded=True + superseded_by=新行 (软链, 不删),
    面板被消费 (不再重报), 审计行 recurring_sediment_applied 落盘."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_3", "reason": "same rule"},
    ])
    store = _make_store(tmp_path, {"fact": _directive_pairs()})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1

    res = json.loads(p._tool_review_drafts({
        "action": "recurring", "memory_id": "rl_3", "verdict": "sediment"}))
    assert res.get("success") is True, f"sediment 消费应成功, 实得 {res}"

    # 新 L2 行已写 (decision 分片 — recurring 沉淀是 决策 类):
    dec = json.loads((store / "memories" / "decision.json").read_text(encoding="utf-8"))
    new = [r for r in dec if str(r.get("metadata", {}).get("cluster")) != ""
           or str(r.get("source", "")) == "recurring_sediment"]
    assert new, "sediment 必须写出一条新 L2 行 (decision 分片)"
    new_id = new[0]["id"]
    assert str(new[0].get("level")) == "L2"
    assert str(new[0].get("source")) == "recurring_sediment"
    assert new[0].get("metadata", {}).get("triage_decided_by") == "user"

    # 成员 supersede 链 (软链):
    facts = {r["id"]: r for r in
             json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    for mid in ("rl_1", "rl_2", "rl_3"):
        m = facts[mid].get("metadata", {})
        assert m.get("superseded") is True, f"{mid} 必须被标 superseded"
        assert str(m.get("superseded_by")) == new_id, f"{mid} 的 superseded_by 必须指向新 L2"

    # 面板已消费:
    assert _pending_recurring(p) == [], "消费后面板不得重报"
    aud = [a for a in _cons_log_rows(store)
           if a.get("action") == "recurring_sediment_applied"]
    assert aud


# ═══════════════════════════════════════════════════════════════════════════
# B2: consumption NO -> no write, cooldown stamp, re-ask deferred
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_b2_dismiss_stamps_cooldown_and_writes_nothing(tmp_path, monkeypatch):
    """verdict='dismiss': 零写 memories/*.json (行字节不变), 面板被消费,
    冷却戳 (recurring_cooldown_days) 留下 — 冷却窗内同 cluster 不再重弹;
    审计行 recurring_sediment_dismissed 落盘."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_3", "reason": "same rule"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()

    res = json.loads(p._tool_review_drafts({
        "action": "recurring", "memory_id": "rl_3", "verdict": "dismiss"}))
    assert res.get("success") is True
    # 零写: fact.json 行集合 + 内容不变:
    after = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))
    assert {r["id"] for r in after} == {"rl_1", "rl_2", "rl_3"}
    for r in after:
        assert "superseded" not in r.get("metadata", {})
    assert _pending_recurring(p) == []
    aud = [a for a in _cons_log_rows(store)
           if a.get("action") == "recurring_sediment_dismissed"]
    assert aud, "dismiss 必须留审计行 (不许静默)"
    # 第二跳 (T-2 回归护栏): 冷却窗内重跑 face -> 不重弹 (本 spec 的
    # authoritative = rl_3 = 最新成员, 新旧键都能命中 — 防回潮的护栏):
    p._recurring_dedupe_consolidate()
    assert _pending_recurring(p) == [], \
        "冷却窗内同 cluster 不得重弹 (reviewer T-2)"


# ═══════════════════════════════════════════════════════════════════════════
# B3: P1-1 — non-newest LLM anchor must still be cooldown-keyed
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_b3_cooldown_key_survives_non_newest_anchor(tmp_path, monkeypatch):
    """P1-1 (reviewer): LLM authoritative 常指向 非最新 成员 (最完整措辞
    往往是更早的行). 冷却键必须按 cluster 恒等 (成员 id 排序元组) 命中,
    不能只按面侧 skip-key (=最新成员 id) 与审计 anchor (=LLM authoritative)
    比对 — 两者不等时冷却窗失效, 10-09 反复重问的症状回潮."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_1",
         "reason": "oldest member carries the fullest wording"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    p = _make_provider(tmp_path, store, _decay_with_directive_patterns())
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1

    res = json.loads(p._tool_review_drafts({
        "action": "recurring", "memory_id": "rl_1", "verdict": "dismiss"}))
    assert res.get("success") is True
    # 冷却窗内重跑 face: cluster 恒等键命中 -> 不重弹:
    p._recurring_dedupe_consolidate()
    assert _pending_recurring(p) == [], \
        "非最新锚点 dismiss 后, 冷却窗内同 cluster 不得重弹 (P1-1)"


# ═══════════════════════════════════════════════════════════════════════════
# B4: P2-25 — cooldown_days=0 disables the gate (re-ask every tick)
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_b4_zero_cooldown_reasks_every_tick(tmp_path, monkeypatch):
    """P2-25 (reviewer): recurring_cooldown_days=0 -> 冷却门控关闭,
    dismiss 后重跑 face 面板重弹 (门控开关语义, 与 B2/B3 的 7 天冷却互补)."""
    fake, state = install(monkeypatch, replies=[
        {"same_directive": True, "authoritative": "rl_1", "reason": "same"},
    ])
    rows = _directive_pairs()
    store = _make_store(tmp_path, {"fact": [dict(r) for r in rows]})
    decay = _decay_with_directive_patterns()
    decay["consolidation"]["recurring_cooldown_days"] = 0
    p = _make_provider(tmp_path, store, decay)
    p._pending_confirm = {}
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1
    res = json.loads(p._tool_review_drafts({
        "action": "recurring", "memory_id": "rl_1", "verdict": "dismiss"}))
    assert res.get("success") is True
    p._recurring_dedupe_consolidate()
    assert len(_pending_recurring(p)) == 1, \
        "cooldown=0 时冷却门控关闭, 重跑 face 必须重弹"


# ═══════════════════════════════════════════════════════════════════════════
# C: knob parity (recurring_max_per_tick + recurring_cooldown_days both sides)
# ═══════════════════════════════════════════════════════════════════════════

def test_recurring_c_knob_parity():
    """新旋钮两侧 parity (S2 invariant): recurring_max_per_tick /
    recurring_min_shared_tokens / recurring_cooldown_days 若一侧有,
    另一侧必须同值; 都有且相等 -> PASS. (P2-23: 第三个旋钮补入 loop.)"""
    dj = DECAY_JSON.get("consolidation", {})
    dd = DECAY_DEFAULTS.get("consolidation", {})
    for knob in ("recurring_max_per_tick", "recurring_min_shared_tokens",
                 "recurring_max_rows_per_cluster", "recurring_cooldown_days"):
        in_dj, in_dd = knob in dj, knob in dd
        if in_dj or in_dd:
            assert in_dj and in_dd, \
                f"{knob} 只在 {'decay.json' if in_dj else 'DECAY_DEFAULTS'} 有 (parity 破)"
            assert dj.get(knob) == dd.get(knob), f"{knob} 两侧值不等"
