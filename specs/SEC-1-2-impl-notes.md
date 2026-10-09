# SEC-1-2 实现蓝图（orchestrator 裁决，2026-10-01）

> 前两轮子代理两次假完成（烧 40min×2、零落盘）的根因 = 设计雷未拆清 + 任务过大。本蓝图把语义、插桩点、回归门全部钉死到行级，子代理照做不留规划空间。**分两个 task 串行做：T1 纯函数+配置+单测（无插桩无 I/O），T2 才接线。** 每个 task 收尾必 grep 验收（不认 status=completed）。

## 设计雷（已探针坐实，勿再当成开放问题）
- 227 条 pool：memories 72 条带 `metadata.origin`（55 medium + 17 high）；**episodic 分片全部分片的 pool dict 不带 `metadata` 键（155 条 origin=None）；knowledge 池也不带（active 0 条）**。
- 若 gate 把「缺 metadata.origin」当 `unknown/low → drop`（照搬 SEC-1-1 存储面规则），**155 条 episodic 会被误 drop 出自动注入 = 活数据 prefetch 大面积回归**。
- 活数据 pool 用 spec 默认 directive_patterns 实扫 = **0 条命中**。

## 裁决 1：注入面「未标记」≠「不可信」（核心消歧）
- **存储面（SEC-1-1，保持不动）**：缺省/坏 origin = `unknown/low` = 不信任（写侧防污染，越保守越好）。
- **注入面（本 story 新增）**：条目**没有** `metadata.origin`（池装配没带出来）= 「未标记」，**默认放行**（不误伤 episodic），**不** drop。只有**明确 stamped 成 `low`** 的条目才被 drop 出自动注入。
- 用一个配置开关锁死这个语义，防未来误改：`injection.drop_unknown_trust`（默认 `false`）。

## 裁决 2：池装配补齐 metadata.origin（让 gate 读到真实 trust）
- `_load_episodic_pool`（L2577+）：pool dict 增 `"metadata": {"origin": <源分片行的 metadata.origin 若存在，否则默认 user/medium>}`。episodic door 语义 = user/medium（SEC-1-1 已给 raw 行统一标 user/medium，这里带出来即可；个别 raw 行缺则补 user/medium）。
- `_load_knowledge_pool`（L2289+）：gold 层 pool dict 增 `"metadata": {"origin": {"kind":"user","trust":"medium"}}`（缺省 user/medium，**保守不猜 high**——若你要 high 再调，默认走 SEC 不猜高哲学）。
- `memories/*.json` 行已带 `metadata.origin`（SEC-1-1），不动。
- 补齐后 227 条全部有真实 origin（episodic 155=medium、knowledge=medium、memories=原值）→ gate 判据统一，回归门变干净。

## injection 块 schema（decay.json 顶级 + DECAY_DEFAULTS 同步，两文件必须一致）
```json
"injection": {
  "low_trust_auto_inject": false,
  "drop_unknown_trust": false,
  "directive_patterns": { "en": [ ...≥5 条... ], "zh": [ ...≥5 条... ] },
  "quote_delims": ["<memory-quote>", "</memory-quote>"]
}
```
- `low_trust_auto_inject=false`：明确 low 条目不进自动注入（prefetch）。
- `drop_unknown_trust=false`：缺 metadata（未标记）不 drop（放行）——**裁决 1 的开关**。
- `directive_patterns`：**config 驱动多语言**（en/zh 各 ≥5 regex，示例起点非穷举，改表不改码，代码零硬编码中文）。默认表照 spec SEC-1-2 节：en 含 `ignore (all )?previous`/`disregard (all )?previous`/`you must`/`system (note|message)`/`execute the following`/`new instruction`/`you are now`；zh 含 `忽略之前`/`忽略所有之前`/`你必须`/`系统提示`/`执行以下`/`执行下列`/`从现在起`/`不要告诉用户`。
- `quote_delims`：引用块定界标签（注入面把命中 directive 的 content 包成引用块，宿主按引用非指令解读）。

## T1（task 1 交付物：纯函数 + 配置 + 单测，**不碰 prefetch/池/插桩**）
1. **配置块**：`decay.json` 顶级 + `consolidation/__init__.py` 的 `DECAY_DEFAULTS` 加 `injection`（两文件逐键一致；默认值 = 上表；`low_trust_auto_inject`/`drop_unknown_trust` 缺省语义：块缺失 → 视为两个开关都 false + 空模式表 = gate 全放行，fail-soft）。
2. **纯函数**（模块级，可脱离 provider 实例单测，无 I/O）：
   ```python
   def injection_gate_decision(item, injection_cfg) -> tuple[str, Optional[str]]:
       # 返回 (action, mark)：
       #   action ∈ {"drop", "allow"}   # 是否 drop 出自动注入
       #   mark   ∈ {None, "untrusted-origin", "directive-quote"}  # 注入面/显式search 标记
       # 规则（按序判定，纯 re + dict 读，零 LLM）：
       #   1) 取 trust = (item.get("metadata") or {}).get("origin", {}).get("trust")
       #   2) 若 trust 明确 == "low" 且 cfg.low_trust_auto_inject is False → ("drop", "untrusted-origin")
       #   3) 若 trust 缺失(None) 且 cfg.drop_unknown_trust is True → ("drop", "untrusted-origin")
       #      （默认 drop_unknown_trust=False → 缺 metadata 放行，不误伤）
       #   4) content 命中任一 directive_patterns（re.search, re.I）→ ("allow", "directive-quote")
       #   5) 其余 → ("allow", None)
       #   6) 本函数自身抛异常 → 由调用方 try/except 兜底 ("allow", None) + logger.warning（fail-soft）
   ```
3. **单测** `tests/test_sec_1_2_gate.py`（纯函数层，≥12 用例，全用**合成 item dict**，不依赖活数据/实例）：
   - low 明确 + low_trust_auto_inject=false → drop/untrusted-origin
   - low 但 low_trust_auto_inject=true → allow（开关放开）
   - 缺 metadata + drop_unknown_trust=false（默认）→ allow/None（不误伤，核心）
   - 缺 metadata + drop_unknown_trust=true → drop（开关收紧）
   - medium/high → allow/None
   - content 命中 en 一条 directive（re 用例）→ allow/directive-quote
   - content 命中 zh 一条 directive → allow/directive-quote
   - content 普通（无指令）+ medium → allow/None
   - 模式表改 config 不改码：喂两套 cfg（en 命中 / zh 命中 / 空表）验证结果随表变
   - 坏 shape（metadata=非 dict / origin=非 dict / trust=非 str）→ 归为缺省放行或 drop，不炸
   - gate 自身抛异常（喂一个会在 re 阶段炸的坏 pattern 或 item 是奇怪类型）→ 调用方兜底 allow（单测 mock 让内部抛，断言外层 catch）
   - 零 LLM（import 审计：纯 re+dict，无 _load_llm_caller）
   - **收尾 grep 验收**：`grep -c injection decay.json consolidation/__init__.py` 均 >0；`python -m pytest tests/test_sec_1_2_gate.py -q` 全绿；`grep -c injection_gate_decision __init__.py` >0（函数落盘）。**不碰 prefetch 方法体。**

## T2（task 2 交付物：接线，T1 全绿后才做）
1. **池补齐**（裁决 2）：`_load_episodic_pool` + `_load_knowledge_pool` 按上表带 `metadata.origin`（episodic 带源行 origin 缺省 user/medium；knowledge 缺省 user/medium）。改完活数据 pool 227 条全有 origin（跑只读装配断言 origin=None 计数=0）。
2. **prefetch 插桩**（L1333+，精确定位）：
   - **drop 点**：在 `_egress_cut` 过滤之后、`_reinforce_recall`（L1398）**之前**：
     ```python
     _cfg = self._decay.get("injection", {}) or {}
     kept, quoted = [], []
     for r in results:
         act, mk = injection_gate_decision(r, _cfg)   # try/except → ("allow", None)
         if act == "drop": dropped += 1; continue
         if mk == "directive-quote": quoted_ids.add(r.get("id"))
         kept.append(r)
     ```
     （drop 的条目**绝不进** `_reinforce_recall` → 不 bump reference_count、不写盘。）
   - **quote 渲染点**：`for item in ...: parts.append(f"[{item['type']}] {item['content']}")` 处，若该 item id ∈ quoted_ids → 用 `quote_delims` 包 content：`f"[{t}] {q0} {content} {q1}"`（q0/q1 取 cfg.quote_delims；缺省两标签）；普通条目原样。
   - 门控自身异常 → 放行原样 + `logger.warning`（fail-soft 不锁宿主）。
3. **search_memories origin_mark**（`search_memories` 返回 dict，**下游标记键，不碰 EP-1 的池装配出池区 L2853/3005/3013**）：结果 dict 里对 `injection_gate_decision(action="drop")` 的条目加 `origin_mark="untrusted-origin"`；对 quote 的加 `origin_mark="directive-quote"`；普通无。放函数体下游（return 前），只改标记键，不改池/不改出池。
4. **get_tool_schemas** 的 filemem_search 描述补一句 origin_mark 语义（零误导标签）。
5. **只读回归探针** `tools/verify_injection_gate.py`（**绝不调 prefetch / 不触发 _reinforce_recall 写盘**——那只读装配 pool + 调 `injection_gate_decision`）：
   - 对活数据断言：episodic 补齐后 origin=None 计数=0；`action=="drop"` 计数=0（活数据无明确 low，unknown_trust 默认不 drop）；`mark=="directive-quote"` 计数=0（活数据 0 directive 命中，已探针确认）→ **gate 在活数据 = 恒等（0 drop 0 quote 0 mark）**。
   - 改代码前后各跑一次，两次输出必须**逐字节一致**（都是恒等）——这就是 SEC-1-2 的回归门（比原计划的"prefetch 全输出逐字节"稳：只读、不碰写盘、不漂移）。
   - 活数据冒烟：`search_memories` 若干 golden 查询返回的 id 集与改前一致（origin_mark 键是全新增字段，不影响既有 id 召回）。
6. **收尾 grep + 全量**：`python -m pytest tests/ -q`（EP-1 的 153 应保持绿，只可能新增你 test_sec_1_2_gate 的用例数）；`grep -n injection_gate_decision __init__.py` 见 prefetch 与 search_memories 两处消费；`python3 tools/verify_injection_gate.py` 打印「活数据恒等：0 drop / 0 quote / 0 mark，前后字节一致」。

## 明确不碰（YAGNI / 边界）
- SEC-1-3 审计留痕链（本轮只在 gate 异常/ drop 处 `logger.warning`/`logger.info`，不做 consolidation_log 结构化事件）。
- 宿主 memory defense 联动。
- EP-1 的出池区（L2853/3005/3013）+ supersede 配置键（search 块内）——本 story 只加顶级 injection 块，不交叠。
