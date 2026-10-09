# filemem "长期记忆" 完整修复 Epic — 蓝图（orchestrator 裁决，2026-10-02）

> 来源：@researcher A（记忆质量面 deleg_40c43e87）+ @researcher B（生产 liveness/故障面 deleg_dfad66f3）双轨实证。
> 用户裁决：**不完全处置——全部修到"配得上长期记忆称号"（S5 复探门为准）**。
> 硬约束不变：零外部依赖 / 零第三方 SDK / 零硬编码模型名（LLM 全 ctx.llm.complete_structured 宿主主模型无 task key）/ 宿主异步 turn 零阻塞 / 多语言 config 驱动。

## 实证基线（两位 researcher 的落盘证据，修复前快照）
- 写面活着：fact.json 10-02 14:20 仍在 auto-save；读面 76 memories + 155 episodic + 64 raw_pending。
- 巩固面纸面：中央 consolidation_log 末事件 09-29 17:31 后 3 天 0 动静；llm_rescore_events 全机不存在；entries.json 不存在（0 observation）；knowledge/archive/ 空（C 段 30 条封顶从未触发）；salience 76/76=0.5、reference_count 76/76=0（复强回路从未生产成功）；supersedes_cands/superseded 全 0；aliases.json 不存在（EP-3 no-op）。
- 注入面：task_1_1790657309 = 46961 字符浏览器 dump 占全库 29%、注入字符量 94%，nginx/web query 必霸榜；fact_91 = 用户投诉原话 + 悬空 @image 路径以 fact 身份注入；4 条 "File unchanged" 工具回声；同主题矛盾对（"C1 修复没命中路径" vs task_6 "三 bug 已修复验证"）同池无降权。
- 故障面：宿主不炸（引擎 _each_provider 吞异常 + prefetch 超时墙 + hook try/except + tick 级隔离，实跑验证）；小坑 = (a) memories JSON corrupt → 读/写两面静默死机无自愈 [中] (b) consolidation_log 无轮转无封顶（63 条 drafts_review 快照占 290KB）+ sweep_memories_expired 死代码 0 调用点 [中] (c) 不可写盘 save 门 raise 靠外层吞 [中低]。
- 召回面：字面词 query 召得回（RRF 有召回增益 vs legacy）；纯中文 paraphrase "浏览器刷新后白屏" 0 命中（legacy substring gate，BM25 只喂 RRF 轴不放宽门）；负样本 0 命中干净；window 谓词实生效。
- 注入毒面：low-trust 漏入 = 0（门有效，合成毒行被 drop + untrusted 标记）；64 行 raw_pending 到不了自动注入面（I3 provenance 门 + 不在检索池）。

## North Star 验收门（S5 复探，全部可实跑量化）
1. **活体**：生产 consolidation_log 出现 10-02 之后新事件（run 或 skip-with-reason，能自证）；llm_rescore_events.jsonl 存在且非空；knowledge/archive/ 非空（C 段真触发过）；entries.json ≥1 条 observation（宿主主模型 E2E 已证可行，需生产触发）。
2. **卫生**：注入 top5 噪声（tool dump/回声类）字符占比 < 10%；task_1 47KB dump 与 4 条回声出池（治理留审计）；fact_91 降 salience 或改写为结论句。
3. **梦境回路**：salience 分布出现梯度（非 76/76=0.5）；reference_count>0 行占比 > 50%（复强回路跑起来）；同主题矛盾对（C1 vs 三 bug）产生 supersedes/supersede_state 戳且旧结论在检索面降权/出池；aliases.json 被挖掘（EP-3 不再是 no-op）。
4. **召回**：中文同义改写 query（"浏览器刷新后白屏"类）top5 命中 0 → ≥1（别名轴生效 + 门放宽，见 S6-note）；RRF vs legacy 召回增益保持不回归（golden 检索门仍绿）。
5. **容错**：corrupt memories JSON 后自愈（读面恢复、旧文件 rename 留壳可回滚、不吞脏）；consolidation_log/episodic/sessions 有封顶或轮转（decay 键可调）；sweep_memories_expired 有 ≥1 个真实调用点；宿主 turn 零阻塞（fail-soft 全段保持）。
6. **硬约束回归**：零第三方 SDK / 零硬编码模型名 / LLM 无 task key / 宿主异步零阻塞 / config 驱动（grep 面全绿）；活数据变更全部可审计（治理/自愈留 trace）。

## Slices（派工顺序）
- **S1 根因（@researcher 诊断 → @coder 修）**：
  1a. 诊断（只读）：grep gateway/agent 日志 `FileMem: registered` 次数 vs 实际 session 数；实跑 provider initialize/register 路径看 ctx.llm 何时绑/何时丢；确认 7 段 tick 每段 skip 的静默点；确认 C 段（TTL/max_rows）为何 0 触发（是 tick 没跑还是配置没进）；确认 _reinforce_recall 从未生产成功的根因（与 LLM 段同链？）。证据驱动，产出"断链点 + 最小修法"。
  1b. 修（@coder）：register/initialize 时绑定 ctx.llm 的断链修复（宿主重启不丢）；7 段每段 skip 落审计事件（skip + 原因，自证纸面能力）；C 段配置链路核对（decay 进宿主实例）。
- **S2 入口卫生（@coder，与 S1 并行，不冲突面）**：
  2a. `_looks_like_tool_dump` 已有单源谓词接进 egress 出口（接线不写新谓词）；"File unchanged" 回声类指纹一并进拒写/出口类。
  2b. 一次性数据治理脚本 `tools/govern_memories.py`（tmp 验活数据）：清 task_1 47KB dump + 4 条回声（进 knowledge/archive/governance_*.jsonl 留审计）；fact_91 降 salience 或改写结论句（脚本内 --dry-run 默认，人工确认落盘）。
- **S4 寿命与自愈（@coder，S1b 后串行）**：sweep_memories_expired 接真实调用点（巩固 tick 或 on_session_end 段）；consolidation_log 封顶/轮转（抄 _auto_prune 先例，decay 键）；memories 读面 corrupt 自愈（rename .corrupt-<ts> + 空表重建 + 留壳）；episodic/sessions 封顶键。
- **S6-note 召回门（并入 S4 或独立小 slice）**：中文同义改写 0 召回——在别名轴生效（S3 前提）后，评估把 legacy substring gate 放宽为"BM25/别名轴候选可过门"（config 键开关，默认保守）。若别名挖掘后仍 0 命中才动门（YAGNI 顺序）。
- **S5 全量复探（@researcher A+B 原清单 + @reviewer 全量裁决）**：North Star 1-6 逐项实跑判定，出终判"配得上/勉强/名不副实"。

## 推仓与活数据纪律
- 代码走 scratch 推仓树（fm-legacy，当前 main=22d5146）+ @reviewer 串行验 + 推仓。
- 活数据（/root/.hermes/filemem）变更仅限：S2 治理脚本（审计留痕可回滚）+ S1b/S4 代码生效后宿主自然写入。**一切实验在 tmp 副本。**
- 宿主 gateway 重启时机：S1b 落地后需确认宿主进程加载新插件代码（reload/restart），这是"巩固面从纸面变真实"的物理前提——@reviewer 验完推仓后执行，重启后立即看 consolidation_log 新事件（North Star #1 首验）。
