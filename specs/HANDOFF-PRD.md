# PRD · filemem 交接面（handoff_extract + 命名 pin + footer 接入）

Owner: 2026-10-17 拍板方向；本 PRD 为实现阶段契约。
范围：**全部落在 `~/.hermes/plugins/filemem/`，零 Hermes 原生改动（红线）**。
前置依赖：BRINGBACK-PRD（P1-P6 已落地）+ supercompress compact-ledger（素材源）。

## Subject

**救命通道**：超长 session 卡死在 host 侧 474k "Compression refused" 死锁
（`_salvage_or_refuse_grown_transcript`，pure code、无 config 面），compact 不动、
对话续不上。本 PRD 给一条**不碰 host 侧**的出路：

1. **face 1 · `filemem_handoff_extract(session_id, focus="")`**（新工具）
   命名一个（已死的/卡死的）session → 从它的 **compact ledger + filemem 行**
   提取素材 → LLM 生成交接摘要 → 落 `handoffs/{source_sid}.json` + 一条
   `decision` 行（session_id 戳 = 生成方 session）→ 该行的 confirmed 态
   会被 V2 confirmed-global 腿自动收进下个 session 的 auto 面。
2. **face 2 · `filemem_set_bringback` 加 `pin_session_id`**
   显式命名一个 closed session，把它的 confirmed 行**置顶**本次 auto 面
   （纯排序，B2-A 不变式：不新增/不删除行）。**绕过 30min freshness 窗口**
   （窗口只对 auto 腿保留）；无效 id → 诚实回退 auto 腿。
3. **face 3 · bring-back footer 轻量接入**（owner 裁决 = A，本轮做）
   若 last-closed session 留有 handoff 摘要，footer 多一行
   "留有交接摘要（#…）"，选"带回"时置顶。不加新选项、不加确认生命周期
   （确认生命周期 = face B，后续轮次再议）。

## 素材面验证（2026-10-17 实证，session 20261015_142319_4bb2e8 = 474k 死锁实例）

| 素材 | 路径 | 大小 | 状态 |
|---|---|---|---|
| compact ledger | `supercompress_store/…4bb2e8.compact.json` | 204KB | **活着**（10-17 14:18 仍在写；死锁的是 host 侧 guard，引擎没卡死）|
| raw transcript | `supercompress_store/…4bb2e8.jsonl` | 6.9MB | 只 `focus` 显式要时才进（防 token 爆炸 —— 474k 教训：绝不"全量原文再生成"）|
| model 侧 | `…4bb2e8.model.jsonl` | 416KB | 同上 |
| filemem 行 | `filemem/sessions/…4bb2e8.jsonl` | 106 行 | confirmed/草稿记忆 |

**结论：交接摘要的素材本地全可读，零 host 改动，零新 hook。**
LLM 面复用现成 `consolidation/llm_caller._structured_call`（main_runtime
snapshot，fail-soft 到 confirmed=False，与 distill 同姿势）。

## 数据模型

- `handoffs/{source_session_id}.json`（新，store 内 `handoffs/` 子目录）：
  ```json
  {
    "source_session_id": "20261015_142319_4bb2e8",
    "generated_by_session": "<当前 session>",
    "generated_ts": "...",
    "materials": ["compact.json", "filemem-rows"],
    "summary": "<LLM 交接摘要全文>",
    "llm_used": true
  }
  ```
  LLM 不可用时 `summary` 退化为 compact 段拼接 + 明示 `llm_used=false`
  （audit 标记，宁可降级不吞脏；行数据不丢）。
- 一条 `memories/decision.json` 行：`content`=摘要全文，`session_id`=
  **生成方 session**（不是 source session —— 生成是本次 session 的决策，
  戳落在决策发生时；source sid 在 content 头部明示），`confirmed` 走
  正常 confirm/draft 链路。source session 的 filemem 行（`sessions/{sid}.jsonl`）
  是**只读素材源**，不是写入目标。
- `bringback_{sid}.json` 加可选字段 `pin_session_id`（缺省/absent = auto
  last-closed，与今日行为字节级一致）。

## 红线

1. 零 host 改动：不碰 `/usr/local/lib/hermes-agent/`（含 474k guard 本身 ——
   那面的修复是 host 侧纯代码改动，需 owner 另行点头，不在本 PRD 范围）。
2. 不动检索面 M4（`filemem_search` 全捞语义不变）。
3. 不动 V2 两腿 + confirmed-global 腿的收行谓词（face 2 是纯排序）。
4. freshness 30min 窗口**只对 auto 腿保留**；显式 pin 绕过（owner 拍板）。
5. 零新 hook / 零 timer；LLM 面挂在 tool call 同步路径（宿主 turn 内）。
6. 本地改 + 测试；commit/push 等 owner 批量点头（同 supercompress 规矩）。

## 实施顺序（owner 拍板）

1. **face 1 handoff_extract 先**（救命通道优先）
2. face 2 pin
3. face 3 footer 接入

## TDD 测试矩阵（`tests/test_handoff_extract.py` + `tests/test_pin_session_id.py`）

### H 系列（face 1）
- H1: compact.json + filemem 行齐全 → `handoffs/{sid}.json` 写出，materials
  列全，`llm_used=true`（fixture 假 LLM 返回固定摘要）。
- H2: compact.json 缺失 → 只用 filemem 行素材，不 crash，materials 如实降级。
- H3: LLM 不可用（main_runtime=None）→ `llm_used=false`，summary = compact
  段拼接（audit 降级），handoff 行仍落。
- H4: `focus` 非空 → raw transcript（`supercompress_store/{sid}.jsonl`）的
  focus 相关行进入素材；`focus` 空 → raw 不进（哨兵：474k 教训）。
- H5: 每次提取落**一条** decision 行，session_id 戳 = 生成方 session，
  source sid 在 content 头部；同一 source 二次提取 → 替换不追加（一行一会话
  语义，同 bringback 选态文件）。
- H6: 无效 session id（无 compact 也无 filemem 行）→ 工具面诚实报错
  `{"success": false, "reason": "no materials"}`，不写文件。
- H7: profile 隔离（source session 行带别的 profile 戳）→ 不收（不串 profile）。

### P 系列（face 2）
- P1: pin 有效 + 该 session 有 confirmed 行 → auto 面置顶（排序哨兵），
  检索面 M4 不变。
- P2: pin 有效但 closed_ts 超 30min 窗口 → 仍置顶（窗口绕过哨兵）。
- P3: pin 无效（无 last_closed 行）/ 别 profile → 回退 auto 腿，
  `pin_target=null` + 原因如实。
- P4: choice≠bring_back + pin 参数 → pin 忽略（哨兵）。
- P5: 无 pin 参数 → 与今日行为字节级一致（`test_bringback_clarify.py` 全绿）。

### F 系列（face 3）
- F1: last-closed 有 handoff 摘要 → footer 含"留有交接摘要（#source_sid）"行；
  选 bring_back 后摘要行置顶（= face 2 的 pin 到该 source sid）。
- F2: 无 handoff → footer 与今日字节级一致（回归哨兵）。

## 验收

- 7 个 H + 5 个 P + 2 个 F 全绿 + `test_bringback_clarify.py` /
  `test_auto_inject_confirmed_gate.py` / `test_session_isolation_fix.py` 存量零回归。
- reviewer 子进程四面复盘（同 supercompress 规矩）。
- 本地全绿后等 owner 批量点头才 commit/push（stennyc/filemem 仓）。
