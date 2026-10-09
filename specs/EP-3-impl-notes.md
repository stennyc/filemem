# EP-3 别名挖掘（alias mining）实现蓝图 — orchestrator 裁决 2026-10-01

> 用户需求：查询词先经别名表展开 → 进 SA2-3 BM25 轴，零嵌入约束下的 paraphrase 最大本地解。
> 硬约束（同 EP-2）：**零硬编码模型名、零自定义 api/provider 配置块、全程共享宿主主模型**（`ctx.llm.complete_structured` 无 task key，同 distill/salience/confirm_supersede）。

## 数据面
- 落盘：`{storage_root}/aliases.json` — 形如 `{"topics": {<canonical_topic>: {"aliases": [...], "source": "llm|human", "ts": ...}}, "ts": ...}`
  - 每条 topic：canonical topic 词 + 生成出的别名列表（含 CJK 术语、英文专名、同义技术词）
  - **增量**：只对新 topic 补挖，不每次全量重挖（用 topic 字符串集合做 diff）
  - 坏别名（展开后命中 0）惰性清理（搜索命中 0 次 N 轮后降权/剔除）

## 实现步骤

### 1. llm_caller.py 加 `suggest_aliases` 作业（镜像 confirm_supersede，纯宿主面）
```python
ALIAS_SCHEMA = {"type":"object","properties":{
    "aliases": {"type":"array","items":{"type":"string"},"description":"5-15 synonyms / related terms including CJK where appropriate"}},
    "required":["aliases"]}
ALIAS_INSTRUCTIONS = """You are a vocabulary expandor for a search system.
Given a topic, produce a flat list of synonyms, related terms, abbreviations,
and CJK equivalents (where the topic is English). 5-15 items, no repetition
of the topic itself, no full phrases — short high-signal tokens only."""

def suggest_aliases(ctx, topic: str) -> Dict[str, Any]:
    """EP-3: host main-model face (no task key). Fail-soft: any exception →
    aliases=[] + model_used=False (caller treats as no-alias, native
    keyword-search degradation — 不吞脏 不炸宿主)."""
    try:
        result = ctx.llm.complete_structured(
            instructions=ALIAS_INSTRUCTIONS,
            input=[{"type":"text","text":f"Topic: {topic}"}],
            json_schema=ALIAS_SCHEMA, temperature=0.0, max_tokens=256)
        parsed = result.parsed or {}
        raw = parsed.get("aliases") or []
        aliases = sorted({str(a).strip().lower() for a in raw if str(a).strip()})
        return {"aliases": aliases, "model_used": True}
    except Exception as exc:
        return {"aliases": [], "model_used": False, "reason": f"model_unavailable_fallback={exc}"}
```

### 2. __init__.py 加 alias 管理 + 巩固 tick 挂载

**加载面（lazy）**
- 新方法 `_load_aliases(self) -> Dict`：读 `self.storage_dir / "aliases.json"`（JSON 文件，缺 → `{}`，坏 → 留痕 + 缺省 `{}` fail-soft 不炸宿主）；返回 `{"topics": {..}, "ts": ...}`
- 新方法 `_expand_query_with_aliases(self, query: str) -> str`：
  1. 读 `_load_aliases()` → 对每个 topic + 该 topic 的 alias 列表，若 topic 字符串或其任一 alias 是 `query.lower()` 的子串（或 query tokens 任一命中 topic tokens）→ 命中该 topic
  2. 把命中的 topic canonical + 全部未出现在 query 的 aliases 拼到 `query` 末尾（空格分隔）
  3. 返回展开后的 query（未命中 → 原 query 一字不变）
  4. 纯字符串 + JSON 读，零 LLM，零 I/O 写

**巩固 tick 挖掘面**
- 新方法 `_mine_alias_candidates(self)`（放在 `_supersede_confirm_candidates` 附近）：
  1. `if self._llm_ctx is None: return`
  2. 幂等 guard `_alias_mining_in_flight`
  3. 扫活数据 memories + episodic 高频关键词：从 memories 各 shard 的 content 里提取 top-N 关键词（用 `search_index.tokenize` 同源 tokenize 统计词频，阈值 `aliases.min_term_freq`（默认 3 次出现才进 topic 候选）），取 top `aliases.max_topics_per_tick`（默认 5）个词做 canonical topic 候选
  4. 对照 `aliases.json` 里已有 topic，只对新 topic（增量）调 `llm.suggest_aliases(self._llm_ctx, topic)`
  5. 结果写回 `aliases.json`（`self._shard_lock` 保护，同文件追加式写；每次全量覆盖该 topic 的 aliases，旧 topic 保留）
  6. append-only 审计 `consolidation_log.jsonl`：`{"action":"alias_mine","topic":...,"aliases_count":N,"model_used":bool,"ts":...}`
  7. 单条异常不影响批次；整 tick 异常 → 已有 aliases 不变，新 topic 滚下边界
  8. 预算 N-cap 读 `aliases.max_topics_per_tick`（与 EP-2 的 `supersede_max_confirms` 并列不交叠）

**检索面调用点**
- `search_memories` 的 `build_search_axes` 调用（`__init__.py` L3082 附近）：
  `query_for_axes = self._expand_query_with_aliases(query)` → 传入 `build_search_axes(pool, query_for_axes, ...)`
  同时 **legacy 关键词 gate 的 `query_words` 也改用展开后的 query 拆词**（`query_words = [w.lower() for w in re.findall(r'\w+', query_for_axes)]`），让 alias 命中也通过 gate
  注：alias 只**放宽** gate（展开后新 token 命中 → eligible），不收紧（原始 query token 命中仍是合法路径）

**巩固边界挂载**
- `_spawn_llm_consolidation` 的 `_run()` 里在 `_supersede_confirm_candidates()` 之后追加：
```python
try:
    self._mine_alias_candidates()
except Exception as e:
    logger.warning(f"FileMem: alias mining spawn failed (existing aliases intact): {e}")
```

### 3. 配置默认键（decay.json 顶级 + DECAY_DEFAULTS 同步，两文件逐键一致）
- `aliases: {min_term_freq: 3, max_topics_per_tick: 5}`（两文件逐键一致；与 consolidation/injection/search 块并列不交叠）

### 4. 人工 CLI（零 LLM）`tools/manage_aliases.py`
- 零硬编码路径（`os.environ.get("HERMES_HOME", Path.home()/".hermes")` → `filemem`，同 sibling 工具）
- `--list`：只读列出当前 aliases.json 全部 topic（`[topic] → [a1, a2, a3, ...]`）
- `--add <topic> <alias1> <alias2> ...`：人工补别名（确定性写入，不删旧别名，`_shard_lock` + 审计 `{"action":"alias_add_human","topic":...,"added":[...]}`）
- `--remove <topic>`：删某 topic 的别名（确定性删除，审计 `{"action":"alias_remove_human","topic":...}`）
- `--prune`：惰性清理 — 把连续 3 次搜索中命中 0 条的 alias 剔除（从 `aliases.json` 里删，审计 `{"action":"alias_prune"}`），纯确定性无 LLM

### 5. 测试 `tests/test_ep_3_alias.py`（合成 fixture，不依赖活数据/provider 实例，≥12 用例）
1. `suggest_aliases` mock 返回 5 aliases → 写回 aliases.json 该 topic 正确（合成 store）
2. `suggest_aliases` mock 返回空（LLM 失败）→ aliases.json 该 topic = []，不写脏
3. `suggest_aliases` 抛异常 → 同 2，候选 topic 保留未挖，不炸 tick
4. 增量：aliases.json 已有 topic X，巩固 tick 只对新 topic Y 调 LLM，X 不动
5. `min_term_freq=3`：高频词 ≥3 才进候选（合成 fixture 3 条含 "toolchain" → 进候选；2 条含 "build" → 不进）
6. `max_topics_per_tick=2`：3 个新 topic → 只挖 2，第 3 个滚下 tick
7. `_expand_query_with_aliases`：query "build system" + aliases {"build system":["toolchain"]} → 展开 query = "build system toolchain"
8. `_expand_query_with_aliases` 未命中：query "python" + 无 "python" 在 aliases → 展开 query = "python" 原样
9. **fixture 用例（spec 要求）**：search pool 有一条 content 含 "toolchain" 不含 "build system"；查询 "build system" 改前 0 条命中（native keyword），展开后 "build system toolchain" 命中 → 条目进 eligible 池（改前必漏的条目改后命中）
10. aliases.json 损坏（JSON 解析失败）→ `_load_aliases` 返回 `{}` fail-soft，搜索不炸
11. 三态 round-trip + 向后兼容：无 aliases.json → `_expand_query_with_aliases` = 原 query 一字不变（byte-identical 门）
12. 零 LLM 审计：`_expand_query_with_aliases` / `_mine_alias_candidates` / CLI 全段 grep openai/anthropic/requests/task=/model名 = 0；suggest_aliases 唯一 LLM 入口 = ctx.llm.complete_structured
13. 并发：两个 CLI 进程同时 `--add` 同 topic 不撕裂（_shard_lock fcntl 锁）

### 6. 收尾 grep 验收（不认 status）
- `python -m pytest tests/ -q` 全绿（196 baseline + 新增）
- `python -m pytest tests/test_ep_3_alias.py -q` 全绿
- `grep -n suggest_aliases consolidation/llm_caller.py` 有 def
- `grep -n _expand_query_with_aliases __init__.py` 有 def + 被 search_memories 调用
- `grep -n _mine_alias_candidates __init__.py` 有 def + 被巩固边界调用
- `grep -c aliases decay.json` 与 DECAY_DEFAULTS 均 >0 且逐键一致
- `python3 tools/manage_aliases.py --list` 只读可跑
- 硬编码审计：`grep -nE 'model\s*[:=]\s*["a-z]|openai|anthropic|httpx|requests|task='` 在 llm_caller.py suggest_aliases 段 = 0

## 边界（不碰）
- EP-1 候选 stamp / EP-2 confirm / SEC-1-2 注入门 / SA2 检索 RRF / distill/salience 现有作业不动
- `aliases.json` 是纯新增文件，不写活数据 memories / episodic
