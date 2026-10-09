# EP — 证据与推断分层（Epistemic Layer，宿主主模型 @ 巩固边界）

**状态**: DRAFT（@product-manager 拆分，2026-09-30）
**约束（硬）**: 零外部依赖；LLM 只用宿主主模型路径（`ctx.llm` 无 task）；全部在巩固 tick 内执行，复用现有 `max_llm_per_tick` 有界预算，宿主 turn 零阻塞，全 fail-soft。

## Epic 目标

把 Hindsight 的「证据 vs 推断分离」本地化：filemem 现状 memories 数据**无任何谱系字段**（2026-09-30 实码取证：keys = category/content/created_at/id/last_referenced/level/metadata/reference_count/salience/source/ttl_days/type/updated_at），gold 知识层是**半成品**（`knowledge/` 只有 `raw_pending.json` 64 条 + archive + export，`entries.json` 不存在）。本 Epic 补完 observation 层 + 谱系 + 约束内语义补偿。

## Stories

### EP-1 谱系字段（supersedes / related_to / origin）— M
**做什么**: `MemoryItem.metadata` 增 `supersedes: [id]` / `related_to: [id]` / `origin: {kind, trust}`（字段契约写进数据契约 docstring）。保存/巩固路径写谱系（确定性规则：同类目 + 高重叠 + 新时间戳 → candidate supersedes，落 metadata 待 EP-2 确认）。检索面：`superseded` 条目降权（不进 RRF 主池，仅作谱系展开）。
**Acceptance**:
1. 谱系字段写入/读取 round-trip 单测；向后兼容（存量条目无字段 = 正常检索）
2. 跨进程锁沿用 `<path>.lock`；确定排序可 diff
3. 回归门：无谱系数据时检索行为与 EP 前一致
4. 零 LLM（确定性规则建候选，确认交 EP-2）

### EP-2 Observation 层补完（补 gold 半成品）— L
**做什么**: 新建 `knowledge/entries.json`（= Hindsight observation 层本地版）。巩固 tick 用宿主主模型（`distill` 同款入口，新增 `observe` 作业类型）把若干 raw facts 合成为 1 条 **preference-neutral observation**：落盘含 `supported_by: [{id, quote}]` + `proof_count` + `value`。Hindsight 的 stale 追踪照搬：新事实进来先**确定性文本匹配**标记 `stale`，巩固 tick 再重综合。
**Acceptance**:
1. 巩固 tick 预算内：`max_llm_per_tick` 语义覆盖 observe 作业（不超限）；宿主 turn 零阻塞（异步 distill 同款有界 queue）
2. observation 条目 100% 可溯源（`supported_by` 指向存在的 raw fact id，坏引用 = fail-soft 拒写 + 留痕）
3. stale 标记 + 重综合单测；LLM 失败 → 条目保持 unconfirmed，**不吞脏**（同 distill 哲学）
4. 检索面：knowledge pool 置顶语义保留；observation 进检索池（带 tier boost，复用现有 `knowledge_tier_boost` 标记位）

### EP-3 别名挖掘（语义缺口的约束内补偿）— M
**做什么**（已拍板接受其覆盖局限）: 巩固 tick 让宿主主模型给每个高频 topic 生成**关键词别名表**（含 CJK 术语 + 英文专名 + 同义技术词），落盘 `aliases.json`。检索时：查询词先经别名表展开 → 进 SA2-3 的 BM25 轴。**这是零嵌入模型约束下 paraphrase 命中的最大本地解。**
**已知局限（写进 doc + 验收）**: 只覆盖「LLM 见过的词」；没见过的 paraphrase 仍漏。
**Acceptance**:
1. fixture 用例：同义技术词（"build system" ↔ "toolchain"）经别名表后命中改前必漏的条目
2. 别名表确定性 + 增量（新 topic 才补，不每次全量重挖）；坏别名（指向空）惰性清理
3. LLM 失败 → 别名表缺失 = 检索回退 SA2-3 原生行为（fail-soft 门）
4. 巩固 tick 预算内；宿主 turn 零阻塞

## Epic 级验收

| 门 | 判据 |
|---|---|
| 溯源 | observation 100% 有 `supported_by`（单测 + 数据扫描） |
| 预算 | 巩固 tick `max_llm_per_tick` 超限 = 丢弃留痕（非静默）；宿主 turn 零阻塞（异步 queue 同款） |
| 哲学 | 全 fail-soft（LLM 失败永不阻断宿主、不吞脏） |
| 回归 | pytest 全绿；别名缺失/图谱缺失时逐级降级不炸 |

## 排期与分工

| Story | 复杂度 | 主责 | 依赖 |
|---|---|---|---|
| EP-1 | M | @data-engineer | — |
| EP-2 | L | @coder + @data-engineer | EP-1（observation 读谱系） |
| EP-3 | M | @coder | SA2-3（别名展开进 BM25 轴） |

**前置**：EP-2/EP-3 的检索面依赖 SA2-3（BM25 轴 + RRF）与 SA2-4（图轴），故 EP 排在 SA2 之后。EP-1 可与 SA2 并行。

## 明确不在本 Epic（YAGNI）

- belief 置信度连续演化（#7 数值 0–1）：当前 `value: high/medium/low` + salience 衰减已够用，上 0–1 连续值是 over-engineering，等 observation 层跑起来有真实漂移案例再升级
- 10 轮 agentic reflect（#8 完整版）：成本特征冲突；如需，单独出「有界离线 reflect」微 spec
- 多 bank / disposition / directives（Hindsight bank 配置面）：filemem 单用户宿主，`source_profile` 已覆盖隔离
