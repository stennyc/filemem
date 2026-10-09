# SEC-1 — 记忆注入面硬化（Memory Defense 本地版，#9 单独安全 spec）

**状态**: DRAFT（@product-manager 拆分，2026-09-30，用户拍板：与 SA2/EP 分开）
**威胁模型**: filemem 的注入回路 = `外部内容（web/tool-result/群聊引用）→ 被 capture 进 memories → 后续会话经 pre_llm_call prefetch 重新注入宿主 prompt`。Hindsight 有专门 Memory Defense 模块防的正是这类 "prompt-injection-through-memory" 攻击；filemem 现只有 room-skeleton 入口谓词（拦群聊骨架，不拦带指令的外部内容）。

## 现状取证（2026-09-30 实码）

| 面 | 现状 | 缺口 |
|---|---|---|
| `<untrusted_tool_result>` 剥框 | 已存在（`__init__.py` L405 正则 + `_untrusted_inner` 两处消费） | 只处理**入口捕获时**的 tool-result 剥框；若捕获漏网 / 后续版本漂移，条目 content 里已带指令文本就会原样再注入 |
| origin 标记 | 只有 `raw_pending.json` 的 `origin`（catch_filter/llm_failed_raw/queue_overflow）+ 写入侧 `source` 字段 | **memories/*.json 条目本身无信任标记**，prefetch 注入面不区分来源 |
| 注入面（prefetch → prompt） | 格式化后直接拼 `<memory-context>` 块注入 | 无指令语法降权、无 origin 门控 |
| 群聊骨架 | room-skeleton 开首谓词（ingress+egress 共用） | 覆盖 `[Group chat` 类骨架，不覆盖自由文本指令注入 |

## Stories

### SEC-1-1 条目信任标记（origin trust stamp）— S
**做什么**: `metadata` 增 `origin: {kind, trust}`，kind ∈ {user, model, tool_output, external, unknown}，trust ∈ {high, medium, low}。stamp 规则**确定性、零 LLM**：
- 用户确认/手写（`filemem_write` 人工路径、confirmed draft 审阅）→ `user/high`
- auto-catch 自 user 回合 → `user/medium`
- auto-catch 自 tool-output（剥框后仍含指令语法特征）→ `tool_output/low`
- 外部抓取/unknown → `external/low`
回填工具 `tools/backfill_origin.py`（动态路径，portability 规则）对存量 72+155 条按 `source` 字段映射回填，幂等。
**Acceptance**:
1. 所有写入路径 stamp 覆盖率 100%（单测：每个 save 出口 assert 有 stamp）
2. 存量回填幂等 + diff 留痕；坏映射 → `unknown/low`（fail-safe 取最低信任，不猜高）
3. 零 LLM

### SEC-1-2 注入面门控（prefetch egress 按 origin + 指令语法）— M
**做什么**: `prefetch`/`search_memories` 注入面：
- `low` trust 条目：默认**不进自动注入**（只在显式 `filemem_search` 结果里带 `[untrusted-origin]` 标记可见）；`high`/`medium` 正常注入
- 指令语法降权：条目 content 命中指令性语法特征（"ignore previous"/"system note"/"you must"/"执行以下" 等，**模式表外置到 `decay.json` 可配 + 多语言**——遵守用户多语言维护性规则，不硬编码中文）→ 注入为「引用块」形式（`<memory-quote>…</memory-quote>` 定界，宿主模型按引用而非指令解读）
- 剥框回归门：`_untrusted_tool_result` 路径单测固化（防未来漂移）
**Acceptance**:
1. 用例：`low` trust 条目含指令文本 → prefetch 输出必不含该条目；显式 search 可见且带标记
2. 引用块定界单测：同一 content 经门控后以 quote 形式出现
3. 模式表多语言：en/zh fixture 各 ≥5 条命中/不命中用例；改动模式表无需改代码（config 驱动）
4. 回归：无指令特征 + `high` trust 的 golden 查询注入行为与改前一致

### SEC-1-3 审计留痕 — S
**做什么**: 注入门控决策（drop/quote/allow）写 `consolidation_log.jsonl` 事件（action=`injection_gate`），含 entry id + 决策原因。静默丢弃 = 违规（团队 invariant：reject-class 必须留痕，room-skeleton guard 同款模式）。
**Acceptance**:
1. 每次门控决策可审计（grep 事件用例）
2. 日志有界（沿用现有 consolidation_log 轮转约定）

## Epic 级验收

| 门 | 判据 |
|---|---|
| 威胁闭合 | 「外部指令文本进 memories → 再注入」回路全分支覆盖（low 直接 drop / 指令语法 quote / 剥框回归门） |
| 保守性 | fail-safe 一律取**最低信任**；门控只收紧注入面，不改变存储面（条目仍落盘，可审计可手改） |
| 回归 | pytest 全绿；golden 注入 fixture 无降 |
| 卫生 | 零硬编码路径/语言模式（多语言模式表 config 驱动）；LLM 调用计数 = 0 |

## 排期与分工

| Story | 复杂度 | 主责 | 依赖 |
|---|---|---|---|
| SEC-1-1 | S | @data-engineer | —（可与 SA2 并行） |
| SEC-1-2 | M | @coder | SEC-1-1 |
| SEC-1-3 | S | @coder | SEC-1-2 |

## 明确不做（YAGNI）

- 加密/端到端隔离（单用户本地宿主，bank 级隔离已由 `source_profile` 覆盖）
- LLM 判定信任（成本特征冲突；确定性规则 + 最低信任兜底已闭合威胁）
- 与宿主 memory defense（若有）联动（宿主面另立接口，不在本 spec）
