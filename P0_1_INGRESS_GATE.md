# P0-1 写侧 ingress 门（共享 store 清染 / 防再染）

**日期**: 2026-10-05
**状态**: ✅ 已实现并验证（15 新增测试全绿；全夹具零新增失败；宿主源零改动）
**代码面**: 仅 `__init__.py`（模块谓词 + 1 个 provider 方法 + 3 处写门接线）；宿主源 off-limits 未碰

## 背景

共享中立 store `~/.hermes/filemem`（全 profile + cron 汇聚）已被实证污染的三类活体行：
- 宿主 compact 指令块：`[CONTEXT COMPACTION — REFERENCE ONLY]…Respond ONLY to the latest user message`
- 整段 cron 任务 prompt：`[IMPORTANT: You are running as a scheduled cron job …]`
- 原始工具 JSON dump：`<untrusted_tool_result …>` / `{"output":…}` 裸 JSON

污染后果：compact 后 pre_llm_call 自动召回把过期任务内容注入 → 任务漂移。

## 实现（零 LLM，纯规则 + stdlib re/len）

### 模块谓词 `ingress_reject_class(content) -> str | None`
按序判定三拒存类（命中标签，未命中 None 放行）：
- **(a) `host_instruction_block`**：`lstrip()` 开头 200 字内以宿主/cron 注入前缀
  开头（`_HOST_INSTRUCTION_PREFIXES` 表：`[CONTEXT COMPACTION`、
  `[IMPORTANT: You are running as a scheduled cron job`）。只匹配块开头，
  行内引用不误伤。
- **(b) `raw_tool_output`**：content 以 `<untrusted_tool_result` 开头，
  或命中 `_looks_like_tool_dump`（`{"output"/{"content"/{"error` 裸 JSON，
  复用 S2 egress 的单一真源，零新谓词）。
- **(c) `whole_task_prompt`**：len ≥ 1000 且 `_TASK_STEP_RE` 步骤编号/列表标记
  （`Step N`、`第N步/条/点/阶段`、行首 `1.` `2)` `3、`）命中 ≥ 3。
  M1 的 `{/[` 开头类兜不住“纯散文 + 步骤列表”，本类补漏。

### Provider 方法 `_ingress_rejection(content, *, topic, origin) -> str | None`
复用现有 quarantine 模式（`_quarantine_raw_pending` 同族）：
1. **fail-open**：分类器异常 → 记 1 条 warning + 返回 None（行照常通过，绝不阻塞宿主 turn）。
2. 命中 → 判定先于副作用固化（reject 粘滞）：
   - `_quarantine_raw_pending(topic, content, [], origin, reason="P0-1 ingress reject-class: <cls>")`
     → 原文落 `knowledge/raw_pending.json`（可回滚），带 `rejection_reason` 语义。
   - `_append_consolidation_event({action: "ingress_reject", reject_class,
     content_sha1, char_len, origin, session_id, source_profile})` → 可 grep 审计行。
   - 每条 quarantine/audit 各自 try/except：hiccup 记 warning，**不改变判定**。
   - 1 条 INFO 观测行（拒存类不得成静默 no-op）。

### 三写门接线（拒存 = 不写记忆池）
| 门 | 位置 | 范围 |
|----|------|------|
| `_save_memory` | auto 写单一 choke point | `source == "auto"` 才 gate（保留 G-pass “manual/confirmed 不受影响”不变式；contamination 向量都在 auto 路径） |
| `_add_draft` | L1/L2 draft 入池前 | 全 gate（draft 皆 auto-origin） |
| `_extract_memories` | 批量抽取循环（M1 之后） | M1 保持第一门（字节不变）；P0-1 兜 M1 漏掉的 c 类，quarantine + `continue` |

## 验收门（全过）

- 三类拒存行落 quarantine 且 **0 条进记忆池**；正常记忆行字节级不变（现有夹具全绿）。
- py3.8 门禁 recipe（钉死）：
  `cd $HERMES_HOME/plugins/filemem && PYTHONPATH=$HERMES_AGENT_SITE /usr/bin/python3 -m pytest tests -q -p no:cacheprovider`
  判定 = 同组假红、零新增：全夹具 **456 passed / 13 failed**，13 条全为
  环境假红（`git diff` 在非 git 目录、`sys.stdlib_module_names` py3.8 缺失、
  1 条 perf 阈值），与改动前基线逐条一致（`comm` 双向 0）。
- 宿主源文件零改动：`find $HERMES_AGENT_SITE -name '*.py' -mmin -120` = 0。
- 仅动 2 个插件文件：`__init__.py`、`tests/test_p0_1_ingress_gate.py`（15 测试）。

## 未覆盖 / 后续（非本卡范围）

- 现有 `memories/*.json` 里已被污染的存量行需一次性清染脚本处理（本卡只做写侧防再染，
  读侧 egress S2 / injection SEC-1-2 已在既有夹具覆盖）。
- 拒存类标签/前缀表若需扩展，改 `_HOST_INSTRUCTION_PREFIXES` + 步骤正则即可，代码零硬编码中文。
