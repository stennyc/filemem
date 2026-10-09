# S4 寿命与自愈 实现蓝图（orchestrator 裁决，2026-10-03）

> 权威依据：`specs/LTM-fix-epic.md` S4 + S6-note 段 + North Star #5/#6。
> 硬约束（不变）：零第三方 SDK / 零硬编码模型名（LLM 全走 host main-model，无 task key）/ 宿主异步 turn 零阻塞 / 多语言 config 驱动（不硬编码中文）/ 活数据全程只读（一切实验在 tmp 副本）。
> **不重开 S1b 五修（F1-F5）/ 不碰 S2 egress / 不开新 feature（YAGNI）。**
> 本蓝图所有落点 @orchestrator 已实读钉到函数名 + 行号锚（行号随编辑漂移，以函数名为准，行号仅供定位）。

## 拆派顺序（两条串行 coding task，逐条收绿再下一条）
- **S4-T1（寿命/封顶面）**：S4-① sweep 接真实调用点 + S4-② consolidation_log 封顶/轮转 + S4-④ episodic/sessions 封顶键。
- **S4-T2（自愈 + 召回面，T1 收绿后）**：S4-③ memories 读面 corrupt 自愈 + S6-note 召回门 config 开关。

---

## S4-T1：寿命与封顶

### S4-① sweep_memories_expired 接真实调用点（North Star #5）
- 现状实锤：`sweep_memories_expired` 定义 @ L3754，**全仓 0 调用点**（死代码）。它已自带 bounded 语义：仅当 active 文件 > `memories_rotate_mb`（L3750 读，默认 1MB）才动手，只移 `auto_cleanup=True`（TTL_CONFIG）且 `is_expired()` 的行到 `memories/<type>_YYYY-MM.jsonl`（append-only 留壳），低于阈值 no-op 幂等。
- 修法（最小）：在 `_run` 线程体的确定性 C 段（现在 S1b F1 起真跑了）挂 `self.sweep_memories_expired()`，**紧跟在 `_auto_prune_raw_pending_archive()` 之后、`.start()` spawn 之前**，包 `try/except` fail-soft（任何异常不破坏前段已完成的 rescore/confirm/mine/observe/archive/prune，与既有 C 段段隔离同构）。零 LLM、纯文件、读面不变量（被 sweep 的行本就对所有读点不可见）。
- 语义：把 docstring 里设想的"4h sweeper"落到"每次 consolidation tick 幂等跑一次"（阈值门控保证不重复扫）。宿主 turn 仍零阻塞（跑在 daemon 线程）。

### S4-② consolidation_log.jsonl 封顶/轮转（North Star #5）
- 现状：`_append_consolidation_event` @ L4262 纯 append 无封顶无轮转（实证：63 条 drafts_review 快照占 290KB 且不回收）。
- 修法（抄 C 段既有模式，不新造谓词）：新增确定性方法 `_rotate_consolidation_log()`，挂 `_run` C 段（sweep 之后、`_auto_prune` 之前，或紧随，顺序自洽即可）。触发键 `consolidation.log_rotate_lines`（默认如 5000；0/缺失 = 禁用 fail-soft）：行数超阈值时把当前 `consolidation_log.jsonl` 的**前 N-k 行**移到 `consolidation_log.<UTC-ts>.jsonl`（rename/move，非删除——审计链不断），active 文件留最近 k 行继续 append。k 取 `log_keep_tail_lines`（默认如 500）。
- 锁：整段 read-modify 包 `with self._shard_lock(log_path)`（同 `_append_consolidation_event` 的 flock 协议，跨进程安全）。fail-soft：轮转 hiccup 不阻塞宿主、不丢行（move 失败则保留 active 不动，留 warning）。
- 配置面（多语言中性，改表不改码）：在 `consolidation/__init__.py` 的 `DECAY_DEFAULTS["consolidation"]` 与 `decay.json` **两处同步**加 `log_rotate_lines` + `log_keep_tail_lines`（默认值一致）。key 缺失/0 = 不轮转（fail-soft，绝不默认删数据）。

### S4-④ episodic / sessions 封顶键（North Star #5）
- 现状：`episodic/yyyy/mm/*.jsonl`（`_write_episodic_entry` @ L5278 分片写）与 `sessions/<sid>.jsonl`（raw trace append @ L2076）无封顶/回收键，随 session 数无限增长。
- 修法（同 S4-② 的 retention 语义，copy `_auto_prune_raw_pending_archive` 的 filename-timestamp 解析 + 保留期模式）：新增确定性方法 `_cap_episodic_sessions()`，挂 `_run` C 段。键（`DECAY_DEFAULTS` + `decay.json` 双同步）：
  - `episodic.max_age_days`（默认 90）：`episodic/` 下分片 sharding 月戳早于 now-N 的 `.jsonl` → 移到 `knowledge/archive/`（append-only，留壳，与 raw_pending_auto_* 同区不同前缀，人工可捞回）。
  - `sessions.max_age_days`（默认 30）：`sessions/<sid>.jsonl` 旧于 now-N 的 → 同样入 `knowledge/archive/`。
  - 0/缺失 = 该方向禁用（fail-soft）。**只 glob 明确前缀，绝不触碰 `knowledge/archive/`、`memories/`、`consolidated/` 他类**（bounded-glob，同 prune 层纪律）。
- 留壳 + 可审计：移前的目标文件在 manifest（`knowledge/archive/pruned_index.jsonl` 或同类）记一行 `{source_file, moved_ts, char_len}`，"忘掉≠抹掉"。

### S4-T1 测试（tests/test_s4_lifespan.py，全 tmp 合成 store，不碰活数据）
- sweep：合成 `memories/fact.json` > 阈值 + 混入 expired auto_cleanup 行 + 非 expired 行 + L2 永久行 → 跑 sweep 后：expired 行进 `fact_YYYY-MM.jsonl`、非 expired/L2 留在 active、低于阈值 no-op 幂等、sweep 被 `_run` C 段真调（用 spy 断言调用发生，不依赖真实 4h）。
- log 轮转：造 > log_rotate_lines 行的 consolidation_log → 跑 `_rotate_consolidation_log` 后 active 只剩 k 行、溢出行进 `consolidation_log.<ts>.jsonl`（rename 非删）、shard lock 生效（双进程不 tear）、key 缺失 no-op。
- episodic/sessions cap：合成旧分片 → 跑 `_cap_episodic_sessions` 后旧的入 archive、新的留、manifest 有行、bounded-glob 不碰他类。
- 配置一致性：`decay.json` ↔ `DECAY_DEFAULTS` 新键（log_rotate_lines/log_keep_tail_lines/episodic.max_age_days/sessions.max_age_days）逐键值相等断言。
- 硬约束 grep：新方法零第三方 import、零硬编码模型名、零 LLM；`_run` 挂载处宿主 turn 零阻塞（都在 daemon 线程体内）。

---

## S4-T2：读面自愈 + 召回门（T1 收绿后串行）

### S4-③ memories 读面 corrupt 自愈（North Star #5，"corrupt 后自愈、旧文件 rename 留壳可回滚、不吞脏"）
- 现状实锤：`search_memories` 读面 choke @ L3548-3556 对 `memories/*.json` 裸 `json.load`，**无 corrupt 自愈**——一条坏 JSON 静默杀死召回面（读面死机）。写面 `_save_memory` RMW 读步（~L2358-2370）与 dedup 面 `_find_duplicate_entry`（L2218 起）同样裸读。
- 修法（单源 helper，不逐处散写）：新增 `_read_memories_file(self, mem_file) -> List[dict]`：正常读返回 parsed list；`JSONDecodeError/OSError` 时 → ① rename 坏文件为 `memories/<type>.json.corrupt-<UTC-ts>`（留壳，人工可回滚，不删）② 重建空 list `[]`（active 文件重建空壳，写面可继续）③ append 一条 `_append_consolidation_event` 审计行（`action="corrupt_memories_healed"`, `type`, `renamed_to`, `ts`，自证"自愈发生+去哪了"，不静默吞脏）。RMW 读步在 `self._shard_lock(mem_file)` 内调用（corrupt 判定在锁内做，避免并发写夹缝）。
- 接线（bounded 3 处，不铺开）：把 `search_memories`（读面 choke L3548-3556）、`_find_duplicate_entry`（dedup 读）、`_save_memory` RMW 读步（写面）改走 `_read_memories_file`。episodic/knowledge 读面各有自身 fail-soft，不动（YAGNI）。
- 语义边界：只 heal `memories/<type>.json` 这一个读/写 choke 类文件；rename 留壳保证可回滚；审计行让"自愈"在北一可自证（North Star #1 同源——巩固/自愈面从纸面变真实）。

### S6-note 召回门（North Star #4，YAGNI 顺序：别名轴生效后才评估放宽）
- 现状实锤：legacy substring gate @ L3660-3673 = "任一 query word 是 content 子串即 eligible"；alias 读面 `_expand_query_with_aliases`（L3450，L3623 在 gate 前展开）只能**放宽**不能收紧——但它依赖 `aliases.json`，而活数据 **aliases.json 不存在**（EP-3 no-op，直到 S1b 解锁的 tick 真跑起来才产出）。所以中文 paraphrase（"浏览器刷新后白屏"类）当前 0 命中 = 别名表还没被挖掘 + legacy gate 不吃别名轴候选。
- 修法（保守 config 开关，默认不改行为，YAGNI）：新增键 `search.gate_relax_alias`（默认 **false**；`DECAY_DEFAULTS["search"]` + `decay.json["search"]` 双同步，值逐键一致，开关关闭时**逐字节不变**——回归门仍绿）。这是"别名挖掘后仍 0 命中才动门"的落点——开关默认关，等 S5 复探看别名轴真产出后再开。

**S6 gate 精确机制（@orchestrator 实读钉死，coder 零设计空间）：**
- gate 落点 = `search_memories` 的 **RRF 分支**（`fused_scores is not None`，蓝图 L3723-3733）。现状：`legacy_match = any(w for w in query_words if w in content_lower)`，`query_words = set(query_for_axes.lower().split())`（L3694，**空格切分**的 alias 展开后 query）。对无空格中文 query（"浏览器刷新后白屏"）整个串是一个"word"，几乎非 content 子串 → `legacy_match=False` → L3732 `continue`，**真正被 BM25/RRF 轴排了名（fused score 非零）的候选在 L3791 `min_score` 过滤之前就被切掉**——这就是中文 paraphrase 0 命中根因（别名表 absent 时 `_expand_query_with_aliases` 原样返回，无空间 token 可 substring）。
- 开关语义（只动 RRF 分支，`else` legacy-keyword 分支保持逐字节）：开关 **true** 时，一个 item 若 `legacy_match` 为 False 但 `fused_scores[pos] >= min_score`（即 RRF/BM25 轴真给它排了名、过了 min_score 门）则**放行**（不过 `continue`，落进 L3791 统一 min_score 过滤）；开关 **false / key 缺失** 时 `legacy_match` False 一律 `continue`（现状逐字节不变）。实现就一行门改写（RRF 分支的 `if not legacy_match: continue` → `if not legacy_match and not (_gate_relax and fused_scores[pos] >= min_score): continue`），`_gate_relax = bool((self._decay.get("search") or {}).get("gate_relax_alias", False))` 在 gate 循环外取一次。`min_score` 是 `search_memories` 入参（L3548 签名，作用域内可直接用，别另造默认值）。
- 关键边界：本开关**只在 alias 表真产出（aliases.json 存在、EP-3 已挖出中文 canonical topic + alias）时才有实际放宽效果**；活数据 aliases.json 目前不存在 → 开关开也 0 命中（因为 fused 轴本就 0，没有可放行的候选）。所以合成测试必须**在 tmp 造 aliases.json**（中文 canonical topic + alias）才能验"开关开则中文 paraphrase top5 0→≥1"。
- 边界：本 slice 只加开关 + 一条"开关开则中文 paraphrase 在别名数据存在时 top5 0→≥1"的合成测试（tmp 造 aliases.json + 中文 paraphrase query），**不默认改变生产召回行为**。

### S4-T2 测试（tests/test_s4_corrupt_heal.py + test_s6_gate_relax.py，全 tmp）
- 自愈：坏 `memories/fact.json`（写非法 JSON）→ `search_memories` 不再静默死机，坏文件被 rename 成 `.corrupt-<ts>`、active 重建空、审计行 `corrupt_memories_healed` 落 consolidation_log、`_save_memory` 写面可继续落新行（dedup 读同走 helper）。
- S6 开关：默认 false → 与 legacy 逐字节一致（旧 golden 门不回归）；true + tmp 造 aliases.json（中文 canonical topic + alias）→ 中文 paraphrase query top5 从 0 变 ≥1（别名轴候选过门）。
- 硬约束 grep：新键逐键 decay.json↔DECAY_DEFAULTS 相等；零第三方；别名门多语言中性（读 decay 键，不写死中文串）。

---

## 收口与推仓纪律（@coder 报告须含）
1. 全量 pytest（base a5fdb87/0c3e0d2 之上）：8 个既有失败（5 flock host-path + 2 sa2_1 + 1 sec_1_1 import-audit 假红）**保持不变**，S4 新增全绿；给精确 passed/failed 数 + FAILED 清单。
2. commit 到 fm-legacy（0c3e0d2 之上），msg 分别标 S4-T1（寿命封顶）/ S4-T2（自愈+召回门）；报告 sha。
3. 宿主包 0 改动（diff 面全在插件 scratch 树）+ 活数据 `/root/.hermes/filemem` stat 前后一致（全程只读）。
4. 任何超出"上面明确列出的键/方法/挂载点"的生产面改动 → 单列清单 + 理由（orchestrator 收口裁决）。
