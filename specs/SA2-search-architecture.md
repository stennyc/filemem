# SA2 — 检索架构 2.0（Search Architecture 2.0）

**状态**: DRAFT（@product-manager 拆分，2026-09-30）
**约束（硬）**: 零外部依赖（无 Postgres / 向量库 / 嵌入服务 / cross-encoder）；LLM 仅宿主主模型路径（`ctx.llm` 无 task，同 compressor 对齐）——**本 Epic 全部 story 零 LLM 调用**（纯数据模型 + stdlib），LLM 轴留给 EP Epic。

## Epic 目标

把 Hindsight TEMPR 四检索轴中 **keyword / temporal / graph 三轴** 本地化（semantic 轴因约束排除，由 EP-3 别名挖掘做约束内补偿）。当前检索面只有单轴关键词重叠（`search_memories`，min_score 0.3，`salience·e^(-λ·age)` 衰减），paraphrase/专名/时间/间接关联四类查询全部弱。

**基线事实（2026-09-30 实码取证）**:
- 语料规模：memories 72 条 + episodic 16 分片 155 条 + knowledge raw_pending 64 条 → 全量 <430 节点，图/索引全放内存 + JSON 文件
- 检索入口：`search_memories(query, limit, min_score, ranking="decay"|"recency")`（行号随版本漂移，grep 定位）；池 = memories/*.json + episodic 分片 + knowledge pool
- 打分轴：关键词重叠 × 衰减；CJK 分词注意已在（"Chinese has CJK unified ideographs" 注释处）
- 存储根：`_resolve_storage_root()` auto → `<base>/filemem`（本机 `/root/.hermes/filemem`）
- 测试基线：62 passed；测试一律 `parents[1]` 相对路径（见 skill filemem-debugging Portability Conventions）

## Stories

### SA2-1 双时间戳数据模型（occurred_at vs created_at）— S
**做什么**: `MemoryItem` 加 `occurred_at: Optional[str]`（naive-UTC ISO，同 `created_at` 约定）；保存路径纯正则抽取事件日期（支持 `2026-09-30` / `09-30` / `2026年9月30日` / `9月30日`，CJK 感知），抽不到 = None（回落 created_at）。新增 `tools/backfill_occurred_at.py`（动态路径，零硬编码——工具脚本 portability 规则）对存量 72+155 条回填，幂等（二跑零变更）。
**Acceptance**:
1. 新保存条目含日期文本 → 自动带 `occurred_at`；无 → None，不报错
2. 回填幂等 + 留 diff 日志；存量数据零丢失（前后行数/内容 hash 比对）
3. 单测：en/zh 日期抽取用例各 ≥5；零外部 import（stdlib only）；pytest 全绿
4. 零 LLM 调用（测试内计数器断言）

### SA2-2 时间窗口检索谓词 — S（依赖 SA2-1）
**做什么**: `filemem_search` 工具 schema 加可选 `since`/`until`（ISO 日期，确定性解析）；`search_memories` 加 `window` 参数（默认 None = 行为不变）。窗口过滤 = 有效事件时间（occurred_at 优先，缺失回落 created_at）落在 [since, until] 内才入池。
**Acceptance**:
1. 窗口查询只回 in-window 条目（fixture：跨窗口 item 必不出）
2. 无窗口查询结果与改动前**字节级一致**（回归门）
3. en/zh fixture 查询集（改动前基线捕获，入 tests/fixtures/）无回归

### SA2-3 BM25 关键词轴 + RRF 多轴融合 — M
**做什么**: 新模块 `search_index.py`（纯 stdlib，~150 行内）：BM25（k1=1.2, b=0.75，参数入 `decay.json` `search` 块可调）；分词 = latin 词 + CJK char-bigram（保持现有中文行为不回退）。内存索引随 provider 初始化/重载构建。RRF 融合轴：bm25 + recency（现有 decay/recency 排名降级为一轴）+ graph（SA2-4 前的 stub 位）。min_score 门保留：RRF 名次 → [0,1] 归一映射（公式写进 docstring，单测钉死）。
**Acceptance**:
1. 多词查询 recall@5 在 fixture 集上 ≥ 改前基线（不降），专名/技术词命中提升（BM25 idf 权重生效用例）

   > **2026-09-30 集成裁决（orchestrator）**：基线 fixture 首次抓取时 RRF 已激活（伪基线），已用真·改前树（git 5f3709c）对活数据实算重生。"不降"门的正确语义 = **retrievability**（legacy top5 每个条目仍在 post 合格池内——legacy 子串资格门逐字保留，可召回集不变）；**不是** top5 集合不变（与同条验收"idf 命中提升"互斥，RRF 池内重排 = 设计内）。ranking 提升由合成池 idf 用例（`test_bm25_idf_rare_term_outranks_common`）单独承接。测试门 = `test_retrievability_gate_en_zh`。
2. 中文 fixture 零回归（CJL bigram 分词对现有 golden 查询行为一致）
3. 加载性能：227 条语料索引构建实测（松断言 <500ms，紧目标 <50ms）
4. 零 LLM；零外部依赖（import 审计：stdlib + 宿主 agent 面 only）

### SA2-4 实体/关系图骨架（JSON 本地版）— M（依赖 SA2-1/SA2-3）
**做什么**: 存储根新增 `graph.json`（nodes = 各层条目 id；edges = {src,dst,kind,w}）。**构建规则全确定性、零 LLM**（LLM causal 边明确留 EP/后续）：
- temporal 边：occurrence 时间距 ≤7d 且同 category（规则可调）
- semantic 边：SA2-3 索引 top-3 共相关对（阈值入 decay.json）
- entity 边：共享字面实体 token（大小写敏感 alphanumeric 串 + CJK 术语启发式——Hindsight 第一档 entity resolution 即 string matching + co-occurrence，照搬第一档）
增量更新（保存时）+ 巩固 tick 全量重建；**稳定排序保证同数据 → 字节相同文件**（可 diff）。护栏：每节点 ≤5 边、总量 cap 2000；跨进程锁复用现有 `<path>.lock` 模式。检索面：RRF top-k 的 1-hop 邻居展开 → graph 轴入融合。
**Acceptance**:
1. fixture 间接关联用例（A 与 B 零共享词、经共享实体 C 相连）仅靠 1-hop 召回
2. 确定性：两次全量重建 → 文件 hash 相同
3. 零 LLM；图构建 + 1-hop 展开在检索延迟预算内（实测报告）
4. 无图（文件缺失）时检索降级 = SA2-3 两轴行为（fail-soft 门）

## Epic 级验收

| 门 | 判据 |
|---|---|
| 回归 | pytest 全绿（62 + 新增）；golden 查询 fixture（改前捕获）en/zh 无降 |
| 约束 | import 审计零外部；全 Epic LLM 调用计数 = 0（测试断言） |
| 卫生 | 测试 `parents[1]` 相对路径；工具脚本零硬编码（grep 门）；`__pycache__` 清理 |
| 交付 | 清 pycache + 重启 gateway + 检索 smoke；push `stennyc/filemem` |

## 排期与分工

| Story | 复杂度 | 主责 | 依赖 |
|---|---|---|---|
| SA2-1 | S | @data-engineer | — |
| SA2-2 | S | @coder | SA2-1 |
| SA2-3 | M | @coder | —（与 SA2-1 并行） |
| SA2-4 | M | @data-engineer | SA2-1 + SA2-3 |

每个 story 完成 → @reviewer 独立复查（真命令取证）+ 复盘表 → 才推进下一个（standing rule）。

## 明确不在本 Epic（YAGNI）

- semantic 嵌入轴（约束排除；EP-3 别名挖掘补偿）
- LLM causal 边 / 实体规范化（EP 层）
- cross-encoder 重排（B3 `relevance_score` 宿主模型路径已覆盖）
- 多 bank/租户、监控、集成面（filemem 单用户宿主插件，`source_profile` 已等价 bank 隔离）
