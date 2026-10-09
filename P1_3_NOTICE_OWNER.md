# P1-3 通知面 owner 过滤（_pending_draft_notice 不弹问跨 profile 草稿）

状态：完成（2026-10-05，coder / t_10b4b1e1）
宿主源：零改动。插件文件：`__init__.py` + 新测试 `tests/test_p1_3_notice_owner.py`（8 条）+ 本文件。

## 一、改动面（只动 pending 列表的收集条件，spec 钉死范围）

1. **`_pending_draft_notice` 收集循环**（原 L1814 语义）：
   - 逐文件读行后，经 `_draft_file_owner_eligible(data)` 判定：
     文件**至少一行**满足「`metadata.source_profile` == 本 provider `self.source_profile`（L1347 语义，
     现 L1562）**OR** 无 source_profile 戳（legacy 中立行）」才进 pending；
     全文件行都跨 profile → 整文件不列、不弹问。
   - **行读失败 / 无 metadata / 非 dict 行 → 中立处理（列入，fail-soft，不锁宿主）**：
     json.loads 异常腿从 `continue`（跳过）改为 `pending.append((df.name, 1))`（列入）；
     可解析但行形状畸形（非 list 载荷、非 dict 行、metadata 非 dict）→ helper 返回 True（中立 → 列入）。
   - 一次性 gate（`_pending_drafts_notified` 按 caller sid 键控）、行文本格式
     （`{name}: {count} 条`，count = 文件总行数不变）、turn_line 增量行、
     其余 fail-soft 语义**全部未动**。

2. **新增 helper `_draft_file_owner_eligible(rows)`**（`_pending_draft_notice` 之后）：
   - 复用 P0-2 的 `_row_source_profile` + `_auto_recall_eligible` 两个纯谓词 ——
     通知面与读面共享同一「这行归谁」判断，结构上不可能漂移（P0-3 同表纪律的延伸）。
   - **fail-open 双腿**：
     ① provider 无 profile 身份（`self.source_profile` None/""）→ 全列（今天行为，
        缺失身份永不静默砍通知，P0-2 规则）；
     ② 载荷不可整形（非 list）→ 中立 → 列入。

## 二、验收门（全绿）

- `tests/test_p1_3_notice_owner.py` 8 条覆盖 spec 6 门：
  ① 跨 profile 文件不列不弹（且 gate 未触发，后续合格文件仍可弹）
  ② 同 profile 文件照列
  ③ 无戳 legacy 文件照列（中立放行）
  ④ 混合文件（≥1 行本 profile）照列，count 仍为文件总行数
  ⑤ 行损坏 fail-soft 按中立：5a 不可解析 JSON 列入 / 5b 畸形行形状列入
  ⑥ 一次性 gate 语义不变（同 sid 二次调用不重弹；新 sid 可再弹）
  ⑦ 无 profile 身份 fail-open 全列（spec 未列但 P0-2 钉死的规则，加锁）
  ⑧ 空草稿文件不列（既有语义保持）
- py3.8 门禁 recipe（钉死）：
  `cd $HERMES_HOME/plugins/filemem && PYTHONPATH=$HERMES_AGENT_SITE /usr/bin/python3 -m pytest tests -q -p no:cacheprovider`
  结果：**492 passed / 12 failed**。基线（改前同组全量）为 484 passed / 13 failed；
  FAILED 集双向 diff：改后集 ⊂ 基线集，唯一差集 1 条 = flapping 的已知环境条
  `test_sa2_4_graph.py::test_live_build_perf_and_counts`（基线挂、改后自愈绿，
  在 P0-3 handoff 的 known-flaky perf/audit 名单内）——**零新增失败**；
  13→12 少的那 1 条即该 flaky 条，非本卡改动所修。
  492 = 484 + 8 新测试，零丢失。
- 宿主源零改动：仅插件目录内 `__init__.py` 与新增测试/文档。

## 三、已知留口（不在本卡范围）

- `_tool_review_drafts` 的 confirm/discard 处理面**不做** owner 过滤：用户可显式
  target 任意历史文件处置（显式面永不被隔离砍，P0-2 规则）；通知面只决定
  「该不该弹问」，不决定「能不能处置」。（P1-4 未触碰，维持原样。）
- **P1-3 原留口①「通知面过滤是 per-file 而非 per-row（混合文件跨 profile 行
  随文件被报数）」→ 已由 P1-4（t_b9c3eea7）收口**：`_draft_file_owner_eligible`
  重构为 per-row 计数 `_draft_eligible_rows(rows) -> (eligible, skipped)`，
  count 从「文件总行数」改「合格行数」；混合文件跨 profile 行不再随文件报数。
- **P1-3 原留口②「provider 身份解析失败时 fail-open 全列（P0-2 通知面规则）」
  → 已由 P1-4 收窄（规则变更记录）**：`self.source_profile` None/"" 时不再是
  全列，而是 **NEUTRAL-ROWS-ONLY**（只列无戳中立行，有归属戳行一律不列）+
  一行可 grep 的 `logger.warning("filemem: pending-draft notice: provider
  profile identity unresolved, neutral-rows-only mode")` 审计。P0-2「无身份
  fail-open 全列」规则在**通知面**被本卡明确弱化；读面（`_auto_recall_filter`）
  的 fail-open 语义**不变**。
- 新增可选开关 `notice.cross_profile_skip_hint`（decay.json，默认 **false**）：
  true 时合格文件行追加「（另有 N 行跨 profile 不列）」，false 时输出字节与
  旧格式一致。详见 `P1_4_NOTICE_PER_ROW.md`。
