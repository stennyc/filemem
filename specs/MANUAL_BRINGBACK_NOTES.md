## 手动 bring-back (owner 2026-11-04)

**动机**：owner 拍板把 filemem 新会话的 bring-back 从「自动弹 clarify 卡」改成
「用户手动口令触发」。旧机制：session 首 turn prefetch 尾部自动拼「🔁 带回账本」
footer（指令模型弹 4 选 clarify，动态插值 prev_sid / handoff_note / topic）。
新机制：footer 变 1 行中性提示（『如需带回旧 session 自动注入面，说"带回"口令』），
用户说口令时模型（读 filemem_set_bringback 工具描述）弹 clarify 卡，4 选项 =
[最近 session A 标题, 最近 session B 标题, 取消 bringback, 输入指定 session id
（Other 自由文本行）]。选项 1/2 的标题来自新增的 `_recent_sessions_for_clarify(n=2)`
（合并 last_closed ledger + active trace jsonl，top-2 by ts，`_session_title_hint`
取标题，fail-soft 退裸 id），模型把用户点选映射回 sid 调
`filemem_set_bringback(choice='bring_back', session_id_override=<sid>)`；选项 4
用户手填 sid 同路径；选项 3（取消）= 不调工具（默认 clean_start 保持，零副作用）。

**实现面**（全在 `~/.hermes/plugins/filemem/__init__.py`，零宿主改动）：

1. `_bringback_clarify_footer` 改中性 1 行提示：
   - 不再插值 prev_sid / handoff_note / topic（字节稳定，prefix-cache 不漂）
   - 不再硬指令模型弹 clarify、不再列 4 选项
   - **30min 新鲜度窗口（`_BRINGBACK_FRESH_WINDOW_S`）不再 gate footer 面**：
     手动触发 = 用户可指定任意 closed session（含超过 30min 的旧 session），
     窗口只继续 gate「auto last-closed pin 解析」（`_resolve_bringback_session_id`
     行为不变）
   - C-gate（user 消息点名已知 closed sid → 记 pin 意图 + 抑制 footer）保留
   - P1-3 子进程闸、one-shot 闸、A-fix 持久化 ledger 保留
   - 无 last_closed 戳时 footer 也照弹（中性提示不依赖 target；手动指定是出口）

2. 新增 `_recent_sessions_for_clarify(n=2)` helper：
   - closed 腿 = `last_closed_sessions.jsonl`（per-profile 过滤，归一化 source_profile）
   - active 腿 = `sessions_dir/*.jsonl` 有 trace 但不在 closed ledger 的 sid
     （trace 首行 source_profile 跨 profile 裁剪；无戳 trace = 中性保留）
   - 按 ts（closed_ts / active mtime）DESC 排序取 top-N，标题 =
     `_session_title_hint`（fail-soft 退裸 id）
   - 全 fail-soft：任何 hiccup → 返回 []，clarify 卡 1/2 选项退裸 id 占位

3. `filemem_set_bringback` 工具面：
   - 工具描述改「MANUAL TRIGGER」语义（用户说带回/接着上次时模型弹 clarify 卡，
     4 选项 + 映射规则写明）
   - 新增可选参数 `session_id_override`（用户在 clarify 卡 1/2 点选或 4 手填的
     sid）：choice='bring_back' 时落盘为 pin；**优先级高于** `pin_session_id`
     （legacy 参数保留）；其他 choice 下忽略（零副作用）
   - 4 值 choice 枚举不变（bring_back / clean_start / fresh / topic_mismatch）

**不变式（保留）**：
- 全新 ≠ 失联：任何 choice（含 fresh）不动检索面（filemem_search / M4）
- B1：footer 不进 P3 冷启动快照（快照写在前，footer 只走返回面）
- per-session：选择文件 keyed by session_id，新 session 重新问
- 收行仍 V2 两腿：bring_back 只是排序（`_bringback_reorder_key`），不新增行

**测试**：
- `tests/test_bringback_clarify.py`：footer 断言全翻（中性提示 + 不列选项 +
  不硬指令）；新增 `_recent_sessions_for_clarify` 5 测（合并排序 / 无 trace
  退裸 id / per-profile 隔离 / fail-soft / n<=0）；新增 `session_id_override`
  3 测（落 pin / clean_start 下忽略 / 优先于 pin_session_id）
- `tests/test_p1_2_topic_guard.py` T9 翻转（footer 不再带 prev topic +
  不列 topic_mismatch 选项）
- `tests/test_pin_session_id.py` F1 翻转（footer 不再宣布 handoff 工件；
  pin 落盘与工件 ride-pin 行为不变）
- 存量回归：失败集与改动前 base 完全一致（pre-existing 12 个：
  test_fresh_end_to_end_prefetch_empties_auto_face_keeps_search、drain 3 个、
  handoff_pin t1、p1_2 t5-t8、pin p2、sa2_3/sa2_4 各 1）——零新增失败

**plugin 侧限制（不变）**：footer + 工具描述只 instruct 模型，模型是否真弹
clarify 卡 / 是否把标题映射回 sid 是 LLM 行为（A-limitation 同源，新触发点）。
