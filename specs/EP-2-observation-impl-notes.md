# EP-2 Observation 层补完实现蓝图 — orchestrator 裁决 2026-10-01

> spec EP-2（L 级，"补 gold 半成品"）：`knowledge/entries.json` 里加 observation 条目
> （Hindsight observation 层本地版）：宿主主模型把若干 raw facts 合成为 1 条
> preference-neutral observation（`supported_by` + `proof_count` + `value`），
> 新事实进来确定性标 `stale`，巩固 tick 重综合。
>
> 拆两刀（防 SEC-1-2 式黑洞）：
>   T1 = 纯确定性数据面（observation schema + kind 判别 + 落盘 + 检索进池 + stale 标记，零 LLM，可合成 fixture 独立验）
>   T2 = 宿主模型 observe 面（llm_caller.observe + 巩固 tick 挂载 + 预算 cap + stale 重综合）
> T1 全绿才派 T2。

## 现码事实（@coder 读时核实行号，非参考）
- `knowledge/entries.json`：文件在（0 字节/不存在 → `_read_knowledge_entries` fail-soft `[]`），
  gold 层无数据。schema 由 `upsert_knowledge`（`__init__.py` L2870+）定义：
  `{id: k_<sha1(8)>, topic, content, content_hash, status: active|superseded, origin,
   provenance:{memory_ids, source_profile, session, commit_anchor}, superseded_by, created_ts}`
- `_read_knowledge_entries` L2818 / `_write_knowledge_entries` L2829 / `_quarantine_raw_pending` L2838 /
  `_load_knowledge_pool` L2944（active 条目 → pool dict，salience 0.9，SEC-1-2 已补 metadata.origin user/medium）
- `_memory_id_set()`：provenance gate 用的"memories 真实 id 全集"（L2882 附近 gold I3 引用）
- `knowledge/raw_pending.json`：513 行（宿主写的 raw facts 降级池；每条有 topic + content + 关联 memory ids，
  @coder 读实际字段名，可能按 `memory_ids` / `topics` 组织）
- 巩固 tick 链（`_spawn_llm_consolidation._run` L4128+）：`_llm_consolidate`(rescore) →
  `_supersede_confirm_candidates`(EP-2-confirm) → `_mine_alias_candidates`(EP-3)，各段独立 try/except
- 配置块 `consolidation` = `{max_llm_per_tick:10, supersede_max_confirms:5}`（decay.json 顶级 + DECAY_DEFAULTS）

## 数据面裁决（T1）
1. **`kind` 判别**：entries.json 每条加 `kind` 字段。存量无 kind 条目 → 视为 `kind=gold`
   （向后兼容，_load/_read 缺省补 gold）。observation 条目 `kind=observation`。
2. **observation schema = gold schema + 4 新键**：
   - `supported_by: [{id, quote}]`（溯源；每个 id 必须 ∈ `_memory_id_set()`，坏引用 = 整条拒写 +
     原文降级 raw_pending，同 gold I3 gate 哲学，不吞脏不静默）
   - `proof_count: int`（= len(supported_by)）
   - `value: high|medium|low`（preference-neutral 价值判定；T1 默认 medium，T2 observe 作业产真值）
   - `stale: bool`（默认 False；新事实冲突时翻 True）
   - `observed_topic: str`（合成自哪个 topic 的 raw facts）
3. **stale 标记面（T1，确定性零 LLM）**：`_mark_stale_observations(topic, new_fact_content)` ——
   保存门 / gold 写入口有新 fact 进来时：若该 topic 有 active observation，
   对 observation.content vs 新 fact content 做确定性 token 重叠 + 冲突信号判定
   （token 重叠高 且 新 fact 是 update/correct 语义 → stale；纯无关 → 不 stale），
   命中则 `stale=True`（不删、不降 status，仅标记；等 T2 巩固 tick 重综合）。
   确定性 = 用 `search_index.tokenize` 同源 token + 词频比，零 LLM、可 diff。
4. **检索面（T1）**：`_load_knowledge_pool`（L2944）对 `kind=observation`：
   - `status=active 且 stale=False` → 进池，salience 0.9 + tier boost（复用现 gold 置顶语义）
   - `stale=True` → 进池但降权（salience 0.5，不置顶）——"stale 降权直到重综合"
   - `status=superseded` → 不进池（同 gold I1）
   - 活数据无 observation（现 0 条）→ pool 行为与 T1 前字节一致（回归门）

## observe 作业面（T2，宿主模型，零硬编码模型名 / 零自定义 api / 全程共享宿主模型）
5. **`consolidation/llm_caller.py` 加 `observe(ctx, topic, facts)`**（镜像 distill/confirm_supersede/suggest_aliases，
   全走 `ctx.llm.complete_structured` **无 task key** → 宿主主模型，temperature=0.0）：
   - 输入：`facts = [{id, content}]`（该 topic 的 raw facts，限 N 条防 prompt 超限）
   - 输出 JSON schema：`{observation: str, value: high|medium|low, supported: [{id, quote}]}`
   - preference-neutral：指令要求中立综合（"summarize what the facts establish; do not inject
     preference or recommendation; cite each supporting fact"）
   - fail-soft：任何异常 → 返回空 observation（不落盘，raw facts 保持 raw_pending，下 tick 重试，不吞脏）
   - **零硬编码模型名 / 零第三方 SDK / 零 task 键**（同 EP-2-confirm / EP-3，验收 grep 审计）
6. **`_observe_consolidate()`（`__init__.py`，放 `_mine_alias_candidates` 之后）**：
   - `self._llm_ctx is None → skip`（standalone 无宿主 LLM，非 error）
   - 幂等 guard `_observing_in_flight`
   - 扫 `raw_pending.json` 按 topic 分组（读实际字段）；已有 active 非 stale observation 的 topic 跳过（增量）
   - 对**新 topic** 或 **stale topic**：取该 topic raw facts → `llm.observe(self._llm_ctx, topic, facts[:N])`
   - 结果 `supported_by` 每个 id 必须 ∈ `_memory_id_set()`（坏引用 → 整条拒写 + 原文降级 raw_pending，留痕）
   - 落盘 observation 条目（kind=observation, supported_by, proof_count, value, stale=False）
     走 `_shard_lock(entries.json)`；同 topic 旧 active observation 翻 superseded（I1 语义，不 DELETE）
   - stale observation 被重综合后 → 新 observation stale=False，旧条 superseded
   - 审计：`consolidation_log.jsonl {action:observe, topic, observation_id, proof_count, model_used}`
   - N-cap `consolidation.observe_max_per_tick`（新键，与 max_llm_per_tick/supersede_max_confirms 并列不交叠）
   - 单条异常不炸批次 / 整 tick 异常旧 observation 原样保留
7. **巩固边界挂载**：`_spawn_llm_consolidation._run` 在 `_mine_alias_candidates` 之后追加
   `try: self._observe_consolidate() except: logger.warning(...)`（同异步线程宿主 turn 零阻塞，
   rescore→confirm→mine→observe 顺序）

## 配置键
8. `consolidation.observe_max_per_tick: 3`（decay.json 顶级 + DECAY_DEFAULTS 逐键一致，
   与 max_llm_per_tick/supersede_max_confirms 并列不交叠）

## 测试
- T1 `tests/test_ep_2_observation_data.py`（纯确定性，合成 fixture，≥10 用例）：
  1. observation 条目 to_dict/from_dict round-trip（kind/supported_by/proof_count/value/stale 无损）
  2. 存量无 kind 条目 load 视为 gold（向后兼容不炸）
  3. supported_by 坏引用（id 不在 memory_id_set）→ 拒写 + 原文降级 raw_pending（不静默不吞脏）
  4. _mark_stale_observations 命中（同 topic 新 fact 冲突）→ stale=True；无关 → stale=False
  5. _mark_stale 确定性可 diff（两次同输入字节一致）
  6. 检索进池：active 非 stale observation 进 pool（salience 0.9 + boost）；stale → 降权 0.5；superseded → 不进
  7. 活数据回归门：无 observation 时 _load_knowledge_pool 与 T1 前字节一致（用现 0 条 entries 验）
  8. kind 判别 gold/observation 互不干扰（同文件并存读回正确）
  9. 锁：observation 写走 _shard_lock（2 进程不撕裂）
  10. 零 LLM：T1 全段 grep llm/supported 写入面 import = 0（stale 标记 + 检索面纯确定性）
- T2 `tests/test_ep_2_observe.py`（合成 fixture + mock，≥10 用例）：
  1. observe mock 返回 observation+supported → 落盘 kind=observation 条目正确
  2. observe mock 返回空（LLM 失败）→ 不落盘，raw facts 保持 raw_pending，下 tick 可重试
  3. observe 抛异常 → fail-soft 不炸 tick，已有 observation 不动
  4. supported_by 坏引用 → 拒写 + 降级 raw_pending
  5. 增量：已有 active 非 stale observation 的 topic 不重挖（mock 计数验）
  6. stale topic 重综合：stale observation 的 topic 进 observe 面（重建 stale=False）
  7. N-cap observe_max_per_tick 生效（超的滚下 tick）
  8. 审计行 {action:observe,...} append-only
  9. 零硬编码模型审计：observe 段 grep model名/provider/openai/anthropic/requests/task= = 0，唯一入口 ctx.llm.complete_structured
  10. 巩固边界挂载顺序 rescore→confirm→mine→observe（读 _run 实际行序 + fail-soft 隔离）

## 收尾 grep 验收（T2 末，不认 status）
- `python -m pytest tests/ -q` 全绿（227 baseline + T1 + T2 新增）
- `grep -n "kind" __init__.py`（_load_knowledge_pool / 写盘 kind 判别点）
- `grep -n "def observe" consolidation/llm_caller.py`（宿主面）
- `grep -n _observe_consolidate __init__.py`（def + 巩固边界调用）
- `grep -n _mark_stale_observations __init__.py`（def + 保存门调用）
- `grep -c observe_max_per_tick decay.json` 与 DECAY_DEFAULTS 均 >0 逐键一致
- `grep -nE 'model\s*[:=]|openai|anthropic|requests|task=' consolidation/llm_caller.py` observe 段 = 0
- 活数据回归：无 observation 时活数据检索面行为不变（0 observation 字节一致门）

## 边界（不碰）
- EP-1 谱系 stamp / EP-2-confirm supersede 确认 / EP-3 别名 / SEC-1 注入门 / SA2 RRF 不动
- gold 层 `upsert_knowledge` 现有逻辑不破坏（只在 entries.json 加 kind 判别，存量 gold 条目向后兼容）
- observation 只读 memories（溯源），不写 memories/*.json；raw_pending.json 只读（除拒写降级 append）
