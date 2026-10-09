# SEC-1-3 审计留痕实现蓝图（injection_gate 事件）— orchestrator 裁决 2026-10-01

> spec SEC-1-3（S 级）：注入门控决策写 `consolidation_log.jsonl` 事件（action=`injection_gate`），
> 含 entry id + 决策原因。静默丢弃 = 违规（reject-class 必须留痕）。

## 设计裁决（orchestrator，消歧不留规划空间）
1. **审计面 = 非平凡决策**：prefetch 注入门（`__init__.py` L1474-1497 的 gate 循环）里，
   `action=="drop"` 与 `mark=="directive-quote"` 两类决策 **逐事件** 写一行
   `{action:"injection_gate", entry_id, decision, reason, origin, session_id, source_profile, ts}`
   到 `consolidation_log.jsonl`（走现成 `_audit` 助手 L3795——已含 `_shard_lock` 跨进程锁
   + mkdir + append-only + fail-soft，不重造）。
2. **allow 不逐行留痕**：allow = 热路径默认态，逐行写 = 每次 recall 几十行噪声。
   invariant 是 "reject-class 必须留痕"；allow 的可审计性 = 条目本身在存储面存在
   （可 grep 条目本身）。活数据 gate 恒等（0 drop/0 quote）→ **0 审计行**，
   天然有界；攻击场景事件量 = 被拒条目数，天然有界。
3. **日志有界** = 沿用现有 consolidation_log 约定（append-only + fail-soft + 跨进程锁）。
   确认过现无轮转机制 → **不新增**轮转 scope（YAGNI，spec 原文"沿用现有约定"）。
4. **fail-soft**：审计写失败（磁盘 hiccup）绝不炸 prefetch 宿主路径——`_audit` 助手
   本身 fail-soft（异常留痕 logger.warning 不 raise）。
5. **search 面不写审计**：search_memories 的 origin_mark 是返回给调用方的可见标记
   （本身即审计面），不写盘；只有 prefetch 注入面（写进宿主 prompt 的路径）留痕。

## 数据事实
- gate 循环：`_kept` 收集、`_dropped_inj` 计数、`_quoted_ids` 集（L1476-1497）；
  每行决策点 = `injection_gate_decision(r, _inj_cfg)` 返回处（L1482）
- `_audit(event)` 助手 L3795（`_audit_line` 或同名——读实际名），EP-2 的
  `supersede_confirm` 行已在用同一 log 文件（action=supersede_confirm），
  `injection_gate` 并列 action 不交叠
- 活数据：0 drop/0 quote → 0 审计行（verify_injection_gate.py 探针恒等不变）

## 实现步骤（@coder 照做，顶向下，每步先跑 pytest）

### 1. prefetch 注入门审计插桩（__init__.py L1474-1497 循环内）
- gate 循环里 `decision` 点：
  - `_act == "drop"` → `self._audit({"action":"injection_gate","entry_id":str(r.get("id","")),"decision":"drop","reason":"low-trust origin auto-inject disabled","origin":( (r.get("metadata") or {}).get("origin") or {} ),"session_id":session_id,"source_profile":sp,"ts":_utcnow().isoformat()})`
  - `_mk == "directive-quote"` → 同 shape，`decision="directive-quote","reason":"instruction-grammar hit; rendered as quote block"`
- 单条审计失败不影响该行的 gate 语义（drop 的仍 drop、quote 的仍 quote）；
  审计走 `_audit` fail-soft，不加额外 try 层（助手已兜底）
- session_id / source_profile 变量在 prefetch 作用域已存在（读实际行确认；
  source_profile 缺 → "default"）

### 2. 测试 `tests/test_sec_1_3_audit.py`（合成 fixture，不依赖活数据，≥8 用例）
1. 合成 store（1 low-trust 行 + 1 普通行）+ prefetch（stub 必要 provider 面）→
   consolidation_log.jsonl 恰 1 行 `injection_gate`（decision=drop, entry_id=low 行 id）
2. 合成 directive-hit 行 → 1 行 `injection_gate`（decision=directive-quote, entry_id 对）
3. 全 allow（活数据形状：user/medium + user/high，0 directive）→ **0 行** injection_gate
   （恒等零噪声门）
4. 审计行 shape：必备键 {action,entry_id,decision,reason,ts} 断言 + JSON 可解析
5. 审计写 fail-soft：log 路径不可写（指向目录）→ prefetch 不炸、gate 语义不变
   （drop 的仍不 inject）
6. 同 tick 多 drop（2 low 行）→ 2 行审计（逐事件非汇总）
7. 审计与 EP-2 supersede_confirm 行同文件不交叠（action 判别可读回各自事件）
8. 锁：审计写走 `_shard_lock`（读代码确认 + 2 进程并发 prefetch 不撕裂 log 行）

### 3. 收尾 grep 验收（不认 status）
- `python -m pytest tests/ -q` 全绿（218 baseline + 新增）
- `grep -n 'injection_gate' __init__.py` 见 prefetch 插桩点
- `grep -c "action.*injection_gate\|injection_gate.*action" __init__.py` ≥2（drop+quote 两点）
- 活数据回归：`python3 tools/verify_injection_gate.py` 仍打印「活数据恒等：0 drop / 0 quote / 0 mark」
  （审计改动不碰 gate 判定 → 探针恒等不变）
- 活数据 consolidation_log.jsonl 若存在：无 injection_gate 行（活数据 0 事件 = 零噪声坐实）

## 边界（不碰）
- gate 判定逻辑（injection_gate_decision）一字不动——本 story 只在决策点加审计写
- EP-2 confirm 审计 / EP-3 alias 审计 / SEC-1-2 quote 渲染 / SA2 检索 不动
- 不新增 consolidation_log 轮转（YAGNI，spec"沿用现有约定"）
