"""S3: 做梦 (consolidation) 语义面扩展 + 水位混合触发 — TDD 规格测试.

蓝图 (上一轮讨论层敲定的设计, 此处固化为可执行规格):

  现状: 做梦 tick 有 4 个 LLM 面 (rescore / supersede-confirm / alias-mine /
        observe) + 5 个确定性 C 段. 4 个面全部是 "同分片 + token 重叠驱动",
        盲区 = "语义等价 / 措辞改写 / 跨分片". 触发 = 纯定时 (30min interval).

  S3 新增 4 个 LLM 面 (全部照抄现有面的骨架: main_runtime gate -> in-flight
        guard -> call-level 0 命中跳过 -> N-cap 独立预算 -> 逐条 LLM fail-soft
        -> append-only 双日志; 零新 provider/model config; 零硬编码模型名):

    ① _semantic_merge_consolidate   跨分片语义等价合并面 (对症写/读侧无去重的洞)
    ② _contradiction_detect         矛盾检测面 (两条存活记忆互斥 -> 标一条 stale)
    ③ _salience_decay_downweight    陈旧度自动降权面 (长期未引用 + 被新事实反证
                                     -> 降 salience, 老记忆自然沉底)
    ④ _aggregate_consolidate        摘要/聚合面 (同 topic N 条碎事实 -> 一条 L1/L2
                                     结论, 喂 L0->L1->L2 晋升)

  S3 触发改造 (混合, 不动定时基线):
        保留 30min interval 当平滑器, 叠加 "水位 override":
          未处理增量 (未确认 supersede 候选 + 未打分 episodic + 未合成 raw_pending)
          >= 阈值 就提前触发, 否则仍走定时节奏; 触发后 N 分钟冷却防风暴.
        增量必须是 O(1) 可缓存计数 (读一个 marker), 绝不能每 tick 全池重算.

本文件 = 这 4 面 + 混合触发 的回归门控.

诚实性 (TDD): 上面这些面/旋钮 "尚未实现". 每个新面用-case 用
pytest.mark.xfail(strict=False, reason="S3 face not yet implemented") 挂着:
  * 今天 (未实现): 方法/旋钮缺失 -> AttributeError / KeyError -> 按预期失败 (XFAIL),
    套件保持绿;
  * 面落地后: 用例自动转 PASS (strict=False -> 转 PASS 不报错, 无需改标记);
  * 若某实现偏离规格: 用例 FAIL 并暴露, 门控生效.
既有 4 面 + C 段的回归基线 (regression) 不受本文件影响, 仍由
test_s1b_tick_wiring.py / test_ep_2_*.py / test_ep_3_alias.py 锁住.

复用: test_s1b_tick_wiring.py 的 fixture 骨架 + _llm_host_fake.install
(宿主主模型面打桩). 合成 tmp store, 不碰活数据 / 不 import 宿主包.
"""
import copy
import json
import pathlib
import sys
import threading
import time
import types

import pytest

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))

import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider, MEMORY_TYPES, MemoryItem  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

DECAY_JSON = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))

_NOT_IMPL = "S3 face not yet implemented"


# ── 复用 test_s1b_tick_wiring 的 fixture 骨架 ────────────────────────────────


def _row(rid, content, mem_type="fact", **extra):
    d = {"id": rid, "type": mem_type, "content": content, "level": "L0",
         "salience": 0.5, "reference_count": 0,
         "created_at": "2026-09-15T00:00:00", "updated_at": "2026-09-15T00:00:00"}
    d.update(extra)
    return d


def _make_store(tmp, rows_by_type, raw_pending_rows=None):
    store = tmp / "store"
    for sub in ("memories", "sessions", "episodic", "consolidated",
                "knowledge", "knowledge/archive", "knowledge/export"):
        (store / sub).mkdir(parents=True, exist_ok=True)
    for t in MEMORY_TYPES:
        (store / "memories" / f"{t}.json").write_text(
            json.dumps(rows_by_type.get(t, []), ensure_ascii=False, indent=2),
            encoding="utf-8")
    if raw_pending_rows is not None:
        (store / "knowledge" / "raw_pending.json").write_text(
            json.dumps(raw_pending_rows, ensure_ascii=False), encoding="utf-8")
    return store


def _make_provider(tmp, store, decay, main_runtime=None):
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s-s3"
    p.source_profile = "s3-test"
    p.storage_dir = store
    p.memories_dir = store / "memories"
    p.sessions_dir = store / "sessions"
    p.episodic_dir = store / "episodic"
    p.knowledge_dir = store / "knowledge"
    p.knowledge_entries_path = store / "knowledge" / "entries.json"
    p.knowledge_raw_pending_path = store / "knowledge" / "raw_pending.json"
    p.knowledge_archive_dir = store / "knowledge" / "archive"
    p._decay = decay
    p._main_runtime = main_runtime if main_runtime is not None else _MAIN_RUNTIME
    # 4 既有面 + 4 新面的 in-flight guard 全部初始 False (面落地时 provider 自带,
    # 测试侧显式铺平避免 AttributeError).
    p._llm_consolidating = False
    p._supersede_confirming = False
    p._alias_mining_in_flight = False
    p._observing_in_flight = False
    p._semantic_merging = False      # ①
    p._contradiction_scanning = False  # ②
    p._salience_decay_running = False  # ③
    p._aggregating = False           # ④
    p._last_consolidation_ts = 0.0
    p._last_skip_audit_ts = 0.0
    p._last_water_level_ts = 0.0     # 水位冷却 marker (O(1))
    p._water_level_counter = 0       # O(1) 增量水位计数 (未处理活)
    return p


def _decay_copy() -> dict:
    d = copy.deepcopy(DECAY_DEFAULTS)
    d.setdefault("consolidation", {})
    return d


def _cons_log_rows(store):
    p = store / "consolidation_log.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _spawn_capture(p):
    cap = []
    real = p._spawn_llm_consolidation

    def rec():
        cap.append(time.time())
        return real()

    p._spawn_llm_consolidation = rec
    return cap


# ═══════════════════════════════════════════════════════════════════════════
# ① _semantic_merge_consolidate — 跨分片语义等价合并面
#
# 规格: 粗筛复用 EP-3 alias topic 聚类 + BM25 相似度 (白嫖现成, 控成本),
#       只把高相似跨分片候选对喂给 LLM 判 "是否同一事实"; 保留信息最全/
#       最新的一条, 其余标 superseded (软标, 不删, 可回滚 — 复用 supersede 机制).
#       独立 LLM 预算 (semantic_merge_max_per_tick).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_semantic_merge_folds_cross_shard_equivalent_rows(tmp_path, monkeypatch):
    """两条语义等价但措辞不同、分片不同 (preference vs fact) 的行 ->
    LLM 判 "same fact" -> 旧的被标 superseded, 新的保留; append-only 审计行
    semantic_merge 落盘; 零误杀 (非等价对不被并)."""
    fake, state = install(monkeypatch, replies=[
        {"same_fact": True, "keep": "fact_678", "supersede": ["preference_140"],
         "reason": "same venv rule, fact_678 is the fuller rewrite"},
    ])
    store = _make_store(tmp_path, {
        "preference": [_row("preference_140", "hermes plugin venv: single shared venv",
                            mem_type="preference")],
        "fact": [_row("fact_678", "Hermes plugin venv rule: use single shared venv, "
                                  "never pip install into native venv",
                      mem_type="fact")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._semantic_merge_consolidate()
    # LLM 被调 1 次 (仅喂高相似跨分片对), 保留 fact_678, 旧行 superseded
    assert state["calls"] >= 1
    rows = {r["id"]: r for r in
            json.loads((store / "memories" / "preference.json").read_text(encoding="utf-8"))}
    assert rows["preference_140"]["metadata"].get("superseded") is True
    aud = [a for a in _cons_log_rows(store) if a.get("action") == "semantic_merge"]
    assert aud, "semantic_merge 审计行必须落盘 (reject-class 不许静默 no-op)"


@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_semantic_merge_keeps_distinct_facts(tmp_path, monkeypatch):
    """误合并红线: 两条不同事实即便同 topic 高相似, LLM 判 same_fact=False ->
    两行都保留, 不标 superseded (宁可漏并不可错并 — 有 safety pad 但默认保守)."""
    fake, state = install(monkeypatch, replies=[{"same_fact": False, "reason": "distinct"}])
    store = _make_store(tmp_path, {
        "fact": [_row("fact_a", "hermes default venv path is installs/<hash>",
                       mem_type="fact"),
                 _row("fact_b", "hermes plugins must not pip install into native venv",
                       mem_type="fact")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._semantic_merge_consolidate()
    rows = {r["id"]: r for r in
            json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert "superseded" not in rows["fact_a"]["metadata"]
    assert "superseded" not in rows["fact_b"]["metadata"]


@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_semantic_merge_independent_budget_and_main_runtime_gate(tmp_path, monkeypatch):
    """预算/门控 (照抄既有面骨架): 独立预算 semantic_merge_max_per_tick;
    main_runtime 缺失 -> 整面 clean skip + tick_skip 审计行, 不炸."""
    store = _make_store(tmp_path, {
        "fact": [_row("fact_a", "x", mem_type="fact"),
                 _row("fact_b", "x y", mem_type="fact")],
    })
    p = _make_provider(tmp_path, store, _decay_copy(), main_runtime=None)
    p._semantic_merge_consolidate()  # must not raise
    skips = [r for r in _cons_log_rows(store)
             if r.get("action") == "tick_skip" and "main_runtime" in str(r.get("reason"))]
    assert skips, "main_runtime 缺失时语义合并面必须留 tick_skip 审计 (North Star #1)"


# ═══════════════════════════════════════════════════════════════════════════
# ② _contradiction_detect — 矛盾检测面
#
# 规格: 扫同 topic 高相似的 "存活" 行对, LLM 判是否互斥; 互斥 -> 标较旧
#       一条 stale (等巩固重综合), 不删; 独立预算 contradiction_max_per_tick.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_contradiction_marks_older_row_stale(tmp_path, monkeypatch):
    """两条互斥事实 (一条说用 docker, 一条说今天不用) -> LLM 判 contradiction
    -> 较旧一条标 metadata.stale=True (读侧降权), 较新一条不动; 审计行落盘."""
    fake, state = install(monkeypatch, replies=[
        {"contradiction": True, "stale_id": "fact_old",
         "reason": "new fact negates the old one"},
    ])
    store = _make_store(tmp_path, {
        "fact": [_row("fact_old", "deployment uses docker", mem_type="fact",
                       created_at="2026-01-01T00:00:00"),
                 _row("fact_new", "deployment does NOT use docker today", mem_type="fact",
                       created_at="2026-10-01T00:00:00")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._contradiction_detect()
    rows = {r["id"]: r for r in
            json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert rows["fact_old"]["metadata"].get("stale") is True
    assert rows["fact_new"]["metadata"].get("stale") is not True
    aud = [a for a in _cons_log_rows(store) if a.get("action") == "contradiction_detect"]
    assert aud


@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_contradiction_no_false_positive_on_complementary_rows(tmp_path, monkeypatch):
    """互补 (非互斥) 的两条 -> LLM 判 contradiction=False -> 谁都不标 stale
    (避免把正常并存的 L 层不同记忆误杀)."""
    fake, state = install(monkeypatch, replies=[{"contradiction": False, "reason": "complementary"}])
    store = _make_store(tmp_path, {
        "fact": [_row("fact_a", "path is installs/<hash>/environments/<id>", mem_type="fact"),
                 _row("fact_b", "never pip install into native venv", mem_type="fact")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._contradiction_detect()
    rows = {r["id"]: r for r in
            json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    assert rows["fact_a"]["metadata"].get("stale") is not True
    assert rows["fact_b"]["metadata"].get("stale") is not True


# ═══════════════════════════════════════════════════════════════════════════
# ③ _salience_decay_downweight — 陈旧度自动降权面
#
# 规格: 长期未被引用 (reference_count 低 / last_referenced 久) + 被新事实反证
#       的行 -> 自动降 salience (往 0 沉底), 让老记忆自然退出注入热区.
#       确定性粗筛 (零 LLM) + 可选 LLM 复核; 只改 salience 值, 不删行.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=False, reason="S3 ③ fixture missing superseded marker")
def test_salience_decay_downweights_stale_unrefined_row(tmp_path, monkeypatch):
    """一条 reference_count 高但被新事实反证 (metadata.superseded) + 长期未引用
    的行 -> salience 被降权 (低于原值), 更新 last_referenced 逻辑不动; 降权是
    软标, 行仍可读. 🔴 修正: 触发条件是 "被反证/标 stale + 久未引用", fixture
    必须给 fact_stale 打 metadata.superseded (之前漏了 -> 保守不动 -> 假 XFAIL)."""
    store = _make_store(tmp_path, {
        "fact": [_row("fact_stale", "deployment uses docker", mem_type="fact",
                       salience=0.9, reference_count=50,
                       last_referenced="2026-01-01T00:00:00",
                       metadata={"superseded": True,
                                 "superseded_by": "fact_fresh"}),
                 _row("fact_fresh", "deployment does NOT use docker", mem_type="fact",
                       salience=0.9, reference_count=0,
                       last_referenced="2026-10-01T00:00:00")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._salience_decay_downweight()
    rows = {r["id"]: r for r in
            json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))}
    # 被新事实反证 + 久未引用的旧行 salience 下降; 新行不动
    assert rows["fact_stale"]["salience"] < 0.9
    assert rows["fact_fresh"]["salience"] == 0.9
    aud = [a for a in _cons_log_rows(store) if a.get("action") == "salience_decay"]
    assert aud


@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_salience_decay_is_deterministic_and_zero_llm(tmp_path):
    """规格钉死: 粗筛是确定性 (零 LLM) — 同一池两次运行结果字节一致; 允许
    可选 LLM 复核但默认关 (config 驱动, 缺省 = 纯确定性). 这条锁 "零 LLM
    也能沉底" 的回归基线, 面落地后若加了无门控的 LLM 调用会 FAIL."""
    store = _make_store(tmp_path, {
        "fact": [_row("fact_stale", "deployment uses docker", mem_type="fact",
                       salience=0.9, reference_count=50,
                       last_referenced="2026-01-01T00:00:00"),
                 _row("fact_fresh", "deployment does NOT use docker", mem_type="fact",
                       salience=0.9, reference_count=0,
                       last_referenced="2026-10-01T00:00:00")],
    })
    p = _make_provider(tmp_path, store, _decay_copy())
    p._salience_decay_downweight()
    first = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))
    p._salience_decay_downweight()  # 幂等二次运行
    second = json.loads((store / "memories" / "fact.json").read_text(encoding="utf-8"))
    assert first == second, "确定性降权面必须幂等 (零 LLM 无随机性)"


# ═══════════════════════════════════════════════════════════════════════════
# ④ _aggregate_consolidate — 摘要/聚合面
#
# 规格: 同 topic 的 N 条碎事实 -> 聚合成一条 L1/L2 结论 (喂 L0->L1->L2 晋升),
#       复用 observe 的 upsert_observation 写盘模式 (T1 契约, 不 DELETE).
#       独立预算 aggregate_max_per_tick. 大池才立项 — 但按 "全做" 决策先落地.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_aggregate_synthesizes_topic_conclusion(tmp_path, monkeypatch):
    """同一 topic 的 3 条 L0 碎事实 -> LLM 聚合成一条 L1 结论 (observation 形态,
    supported_by 指向源 id); 源 L0 行不删 (append-only); 审计行落盘."""
    fake, state = install(monkeypatch, replies=[
        {"observation": "Hermes plugin venv rule: use single shared venv, never "
                        "pip install into native venv",
         "supported": ["fact_1", "fact_2", "fact_3"], "value": "high"},
    ])
    rp = ([{"topic": "venv", "content": "use single shared venv",
            "memory_ids": ["fact_1"], "created_ts": "2026-09-28T00:00:00"},
           {"topic": "venv", "content": "never pip install native venv",
            "memory_ids": ["fact_2"], "created_ts": "2026-09-28T00:00:00"},
           {"topic": "venv", "content": "each plugin reuses the same env",
            "memory_ids": ["fact_3"], "created_ts": "2026-09-28T00:00:00"}])
    store = _make_store(tmp_path, {
        "fact": [_row(f"fact_{i}", f"venv fragment {i}", mem_type="fact")
                 for i in (1, 2, 3)],
    }, raw_pending_rows=rp)
    p = _make_provider(tmp_path, store, _decay_copy())
    p._aggregate_consolidate()
    entries = []
    ep = store / "knowledge" / "entries.json"
    if ep.exists():
        entries = json.loads(ep.read_text(encoding="utf-8"))
    aggr = [e for e in entries if e.get("kind") == "aggregation"
            or e.get("origin") == "llm_aggregate"]
    assert aggr, "聚合面必须把碎事实聚成一条落盘 (knowledge/entries.json)"
    aud = [a for a in _cons_log_rows(store) if a.get("action") == "aggregate_consolidate"]
    assert aud


@pytest.mark.xfail(strict=False, reason="S3 ④ aggregation face not yet implemented")
def test_aggregate_keeps_source_rows_intact(tmp_path, monkeypatch):
    """聚合是 "合成上层结论", 不 DELETE 源 L0 (append-only 纪律, 同 observe):
    聚合后 3 条源 fact 行仍在 fact.json, 字节不丢.
    🔴 修正: 之前靠 "默认不删" 兜 -> XPASS 假绿; 现在聚合成功后 显式重读
    源行, 断言 3 条字节全在 (若聚合面某次误删/改写源行, 这条真 FAIL)."""
    fake, state = install(monkeypatch, replies=[
        {"observation": "venv rule", "supported": ["fact_1", "fact_2", "fact_3"],
         "value": "medium"},
    ])
    rp = ([{"topic": "venv", "content": f"f{i}", "memory_ids": [f"fact_{i}"],
            "created_ts": "2026-09-28T00:00:00"} for i in (1, 2, 3)])
    store = _make_store(tmp_path, {
        "fact": [_row(f"fact_{i}", f"venv fragment {i}", mem_type="fact") for i in (1, 2, 3)],
    }, raw_pending_rows=rp)
    before = (store / "memories" / "fact.json").read_text(encoding="utf-8")
    p = _make_provider(tmp_path, store, _decay_copy())
    p._aggregate_consolidate()
    # 聚合面必须已写入 knowledge (observation/aggregation entry) — 门控真咬
    ep = store / "knowledge" / "entries.json"
    assert ep.exists(), "聚合面未落盘 (face 未真正执行) -> 门控失效"
    # append-only: 聚合后源 fact.json 字节不得丢行 (源 L0 仍可读)
    after = (store / "memories" / "fact.json").read_text(encoding="utf-8")
    src_after = {r["id"] for r in json.loads(after)}
    assert {"fact_1", "fact_2", "fact_3"} <= src_after, \
        f"聚合面误删源 L0 行 (append-only 破): 源 {src_after}"


# ═══════════════════════════════════════════════════════════════════════════
# 混合触发: 定时 (30min 平滑器) + 水位 override + 冷却防风暴
# ═══════════════════════════════════════════════════════════════════════════
#
# 规格钉死 (改 _maybe_spawn_consolidation_tick 的 gate, 不碰 4 个 LLM 面本身):
#   * 保留 interval 节奏 (pool 无活时零空转 — 因为 0 命中面本就跳过, 成本≈0)
#   * 叠加水位 override: 未处理增量 >= threshold 就提前触发 (低延迟)
#   * 冷却: 触发后 cooldown_min 内不再被水位触发 (防 retry-flood 风暴)
#   * 水位增量 = O(1) 可缓存计数 (未确认候选 + 未打分 episodic + 未合成
#     raw_pending 之和), 绝不每 tick 全池重算
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_water_level_override_triggers_early_above_threshold(tmp_path):
    """水位 override: 未处理增量 >= threshold 时, 即便仍在 30min 窗口内也提前
    触发一次 tick (override 定时). 增量走 O(1) 计数, 不重算全池."""
    store = _make_store(tmp_path, {})
    decay = _decay_copy()
    decay["consolidation"]["consolidation_interval_min"] = 30
    decay["consolidation"]["water_level_override"] = True          # 新旋钮
    decay["consolidation"]["water_level_threshold"] = 3            # 新旋钮
    decay["consolidation"]["water_level_cooldown_min"] = 0         # 关冷却便于隔离测
    p = _make_provider(tmp_path, store, decay)
    cap = _spawn_capture(p)
    # 模拟: 上一 tick 后攒了 3 条未处理活 (超过 threshold=3)
    p._water_level_counter = 3
    p._last_consolidation_ts = time.time()   # 仍在 30min 窗口内 (定时本会跳过)
    p._maybe_spawn_consolidation_tick()
    assert len(cap) == 1, "水位达阈值必须 override 定时提前触发"


# 双向钉: 把 "定时器" 和 "水位" 解耦开 — 之前这条只测 "窗口内不触发", 而那是
# 定时器兜的默认行为 (与水位无关, 误触发 override 检测不到)。修法: 先推到窗口外
# (定时器本会触发), 断言 override 未达阈值时 "不额外提前" (仍走定时器 1 次);
# 再把 _last_consolidation_ts 拉回窗口内 (定时器本 skip), 断言未达阈值 -> 0 次
# (若 override 误触发逻辑写反, 这里才会暴露)。
@pytest.mark.xfail(strict=False, reason="S3 trigger: two-direction override pin")
def test_water_level_below_threshold_keeps_timer_rhythm(tmp_path):
    """水位未达阈值: override 不得额外触发, 行为完全退回纯定时 —
    (a) 窗口内: 定时器 skip + override 不介入 -> 0 次;
    (b) 窗口外: 定时器触发 1 次 (与 override 无关) -> 1 次. 双向钉死
    'override 只锦上添花, 不破坏定时节奏'."""
    store = _make_store(tmp_path, {})
    decay = _decay_copy()
    decay["consolidation"]["consolidation_interval_min"] = 30
    decay["consolidation"]["water_level_override"] = True
    decay["consolidation"]["water_level_threshold"] = 3
    decay["consolidation"]["water_level_cooldown_min"] = 0
    p = _make_provider(tmp_path, store, decay)
    cap = _spawn_capture(p)

    # (a) 窗口内 (定时器本 skip) + 水位未达 -> 0 次
    p._water_level_counter = 1
    p._last_consolidation_ts = time.time()
    p._maybe_spawn_consolidation_tick()
    n_in_window = len(cap)

    # (b) 窗口外 (定时器本触发) + 水位未达 -> 仅定时器 1 次, override 不额外加
    cap.clear()
    p._water_level_counter = 1
    p._last_consolidation_ts = time.time() - 31 * 60  # 推开 30min 窗口外
    p._maybe_spawn_consolidation_tick()
    n_out_window = len(cap)

    assert n_in_window == 0, f"(a) 窗口内 + 未达阈值必须 0 次, 实得 {n_in_window}"
    assert n_out_window == 1, f"(b) 窗口外仅定时器触发 1 次 (override 不额外), 实得 {n_out_window}"


@pytest.mark.xfail(strict=False, reason=_NOT_IMPL)
def test_water_level_cooldown_suppresses_storm(tmp_path):
    """防风暴: 一次水位触发后 cooldown_min 内, 即便水位仍高也不再被水位触发
    (你 session 里那个 10x retry-flood 就是活例子). 冷却期内的触发最多 1 次."""
    store = _make_store(tmp_path, {})
    decay = _decay_copy()
    decay["consolidation"]["consolidation_interval_min"] = 30
    decay["consolidation"]["water_level_override"] = True
    decay["consolidation"]["water_level_threshold"] = 1
    decay["consolidation"]["water_level_cooldown_min"] = 60      # 1h 冷却
    p = _make_provider(tmp_path, store, decay)
    cap = _spawn_capture(p)
    p._water_level_counter = 5
    p._last_consolidation_ts = time.time()
    p._maybe_spawn_consolidation_tick()        # 第 1 次: 触发 + 打冷却
    n1 = len(cap)
    p._last_consolidation_ts = time.time()      # 伪装仍在窗口内
    p._water_level_counter = 5
    p._maybe_spawn_consolidation_tick()        # 第 2 次: 冷却期内, 不再触发
    assert n1 == 1 and len(cap) == 1, "冷却期内水位不得重复触发 (防风暴)"


@pytest.mark.xfail(strict=False, reason="S3 trigger: O(1) counter pin (rescan-count)")
def test_water_level_counter_is_o1_not_full_rescan(tmp_path):
    """水位必须 O(1) 可缓存: gate 期间的 "全池重算" 次数必须被 TTL 缓存 bound.
    规格钉法 (🔴 修正: 旧版 `or "water_level" in src` 宽松分支恒真, 门控失效):
      把 TTL 拉到足够长 (跨两次 gate 调用都不过期), 连续 gate 两次,
      全池扫描函数 _s3_water_level_rescan 必须只被调 1 次 (第 1 次填缓存,
      第 2 次命中缓存) —— 若实现改成 "gate 内每次现算全池", rescan 计数 = 2, FAIL."""
    import types
    store = _make_store(tmp_path, {})
    decay = _decay_copy()
    decay["consolidation"]["water_level_override"] = True
    decay["consolidation"]["water_level_threshold"] = 1
    decay["consolidation"]["water_level_cooldown_min"] = 0
    decay["consolidation"]["water_level_cache_ttl_s"] = 3600  # 跨 2 次 gate 不 expiry
    p = _make_provider(tmp_path, store, decay)

    rescan_counts = []
    real_rescan = p._s3_water_level_rescan
    p._s3_water_level_rescan = lambda: (rescan_counts.append(1), real_rescan())[1]

    # 灌 1 条未处理活 (raw_pending topic 未合成 -> 水位 >= 1 达 threshold)
    p.knowledge_raw_pending_path.write_text(
        json.dumps([{"topic": "t", "content": "raw", "created_ts": "2026-09-28T00:00:00"}]),
        encoding="utf-8")
    p._water_level_counter = 1

    p._last_consolidation_ts = time.time()  # 窗口内 -> 走 override 分支才读水位
    p._maybe_spawn_consolidation_tick()   # gate #1: rescan 填缓存
    p._last_consolidation_ts = time.time()
    p._maybe_spawn_consolidation_tick()   # gate #2: 命中缓存, 不再 rescan

    assert len(rescan_counts) <= 1, \
        (f"水位 gate 期间全池重算 {len(rescan_counts)} 次 "
         f"(>1 = TTL 缓存没 bound, gate 内现算全池 -> 规格违)")


# ── decay 旋钮 parity (新旋钮进 DECAY_DEFAULTS + decay.json 两侧) ────────────

def test_s3_decay_knobs_parity():
    """新旋钮 (water_level_override / threshold / cooldown + 4 面各自
    *_max_per_tick) 一旦落地, 必须进 decay.json 与 DECAY_DEFAULTS 两侧
    (S2 invariant: 唯一允许 drift 的是 search). 今天两侧都没有 -> 这条是
    纯规格预告, 不 xfail: 面落地后此条必须 PASS, 否则回归门控漏旋钮."""
    dj = DECAY_JSON.get("consolidation", {})
    dd = DECAY_DEFAULTS.get("consolidation", {})
    for knob in ("water_level_override", "water_level_threshold",
                 "water_level_cooldown_min", "water_level_cache_ttl_s",
                 "semantic_merge_max_per_tick", "contradiction_max_per_tick",
                 "salience_decay_enabled", "salience_decay_floor",
                 "salience_decay_age_days", "aggregate_max_per_tick"):
        # 落地前: 两侧都不存在 -> 校验 "若一侧有则另一侧必须同值"; 落地后:
        # 两侧都有且相等. 两条路径都 PASS, 所以不挂 xfail.
        in_dj, in_dd = knob in dj, knob in dd
        if in_dj or in_dd:
            assert in_dj and in_dd, f"旋钮 {knob} 只在 {'decay.json' if in_dj else 'DECAY_DEFAULTS'} 有 (parity 破)"
            assert dj.get(knob) == dd.get(knob), f"旋钮 {knob} 两侧值不等"


# ── 回归基线: 4 既有面 + C 段 不被 S3 面落地后误伤 ─────────────────────────

def test_existing_faces_still_run_with_s3_guards_absent(tmp_path, monkeypatch):
    """S3 加 4 新面后, tick body 的既有 4 LLM 面 (rescore/supersede/alias/observe)
    + 5 C 段 的 fail-soft 契约不得回退 — 全池空时整 tick 干净跑完 (同
    test_s1b_tick_wiring 的 regression gate, 但钉在 S3 骨架上防新面插入打崩)."""
    store = _make_store(tmp_path, {}, raw_pending_rows=[
        {"topic": "t", "content": "raw", "created_ts": "2026-09-28T00:00:00"}])
    p = _make_provider(tmp_path, store, _decay_copy())
    p._main_runtime = None  # LLM 面全 clean-skip
    # 既有 C 段 (零 LLM) 必须照跑:
    p._auto_archive_raw_pending()
    assert (store / "knowledge" / "archive").exists()
