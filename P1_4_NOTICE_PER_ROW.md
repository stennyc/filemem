# P1-4 通知面 per-row owner 加固（跨 profile 行不报数 + 身份 fail-open 收窄）

状态：完成（2026-10-06，源卡 t_b9c3eea7）
宿主源：零改动。插件文件：`__init__.py`（读侧 L2155-2205 收集循环 + L2220+ 新 helper +
L18 typing）+ `decay.json`（新增 `notice` 块 1 key）+ `consolidation/__init__.py`
（DECAY_DEFAULTS 镜像 `notice` 块，S2 键对偶）+ 测试 `tests/test_p1_3_notice_owner.py`
（8 条基线 → 10 条）+ 本文档 + `P1_3_NOTICE_OWNER.md` §三 留口标注。

## 一、背景

P1-3 的 [未决草稿] 通知面 owner 过滤是 **per-file**：文件 ≥1 行合格即整文件列入，
count = 文件总行数 —— 混合文件里其他 profile 的跨 profile 行仍随文件被报数；且
provider 身份解析失败（`self.source_profile` None/""）时 **fail-open 全列**（P0-2
通知面规则）。用户在 default profile 仍感到「收到其他 profile 的草稿鉴权」。
用户拍板解除 spec 第 3 条「行文本格式不动」禁令 → 本卡收口这两个留口。

## 二、改动面（只动读侧通知面，写侧 no_popup/sweep 不碰）

1. **helper 重构 per-row**：`_draft_file_owner_eligible(rows)->bool` 删除（grep 零
   残留），新增纯函数 **`_draft_eligible_rows(rows) -> (eligible, skipped)`**
   （无 I/O，返回该文件合格行数 + 被跳过的跨 profile 有戳行数；skipped 数喂给
   可选 hint）：
   - 行谓词复用 P0-2 `_row_source_profile` 词典取行戳（结构不漂移）：
     行戳 == my_sp → 合格；行无戳 / 无 metadata / 形状畸形（中立行）→ 合格；
     行戳 != my_sp（有戳且非本 profile）→ **不合格**（计入 skipped）。
   - **身份收窄腿（规则变更）**：my_sp 为 None/"" → **只中立（无戳）行合格，
     有戳行一律不列**（不知道自己是谁就不认领他人归属行）+ 发一行可 grep 的
     审计：`logger.warning("filemem: pending-draft notice: provider profile
     identity unresolved, neutral-rows-only mode")`。原 P0-2「无身份 fail-open
     全列」在**通知面**被明确弱化（读面 `_auto_recall_filter` 的 fail-open 不变）。
   - 载荷可解析但非 list → 中立：`(0,0)`，由调用侧按 P1-3 5b fail-soft 列 count=1
     （原行为保留）。
2. **收集循环 per-row count**（`_pending_draft_notice`）：逐文件
   `eligible, skipped = self._draft_eligible_rows(data)`；eligible==0 → 整文件不列；
   否则 `pending.append((df.name, eligible, skipped))` —— **count 从「文件总行数」
   变「合格行数」**（用户批准的行格式例外点）。json.loads 异常腿 fail-soft 不变
   （不可解析文件按中立列 count=1，原样）。
3. **行文本 + 可选 hint**：基础行 `  - {name}: {count} 条`（hint 关时字节与旧格式
   一致）；decay.json 新增 **`notice.cross_profile_skip_hint`（默认 false）**，
   true 时行追加「（另有 N 行跨 profile 不列）」，N = 该文件被跳过行数。
4. 一次性 gate（`_pending_drafts_notified` 按 caller sid 键控）、turn_line 增量行、
   其余 fail-soft 全部不动。

## 三、语义表（P1-4 终态）

| 场景 | 列入? | count | 备注 |
|---|---|---|---|
| 纯跨 profile 文件（全行他戳） | 否 | — | 不弹问（P1-3 不变） |
| 同 profile 文件 | 是 | 全部行 | P1-3 不变 |
| 无戳 legacy 文件 | 是 | 全部行 | 中立放行，不变 |
| 混合文件（1 本 profile + 2 跨） | 是 | **1** | P1-4 变更：旧 count=3 |
| 行损坏（不可解析 JSON） | 是 | 1 | fail-soft 中立，不变 |
| 行形状畸形（可解析非 list / 非 dict 行） | 是 | 1 | fail-soft 中立，不变 |
| 空草稿文件 | 否 | — | 既有语义，不变 |
| 无身份（my_sp None/""）+ 无戳行 | 是 | 无戳行数 | **P1-4 收窄**（旧：全列） |
| 无身份 + 有戳行 | 否 | — | **P1-4 收窄** + warning 审计 |
| hint=true 且该文件有 skipped | 行尾追加「（另有 N 行跨 profile 不列）」 | 合格行数 | 默认 false |
| hint=false（默认） | 基础行字节同旧格式 | 合格行数 | 零输出膨胀 |

## 四、测试基线

`tests/test_p1_3_notice_owner.py` 8 条基线 → 10 条（本文件 10/10 全绿）：
- ① 纯跨 profile 文件不列不弹（不变）
- ② 同 profile 文件照列（不变）
- ③ 无戳 legacy 文件照列（不变）
- ④ 混合文件（1 自己 + 2 跨）→ **count=1**（`test_04_mixed_file_per_row_count`，
  旧断言 count=总行数 废除；同时断言 hint 默认 off 时行文本无「跨 profile」）
- ⑤ 行损坏 fail-soft 中立列入（5a 不可解析 / 5b 畸形行形状，不变）
- ⑥ 一次性 gate 语义不变（不变）
- ⑦ **身份收窄**（`test_07_identity_unresolved_neutral_rows_only`，旧「无身份全列」
  废除）：无戳行列入 + 有戳行不列 + caplog 观测到 warning 审计行
- ⑧ 空草稿文件不列（不变）
- ⑨ 新增：hint 开关 on（`test_09_hint_on_names_skipped_cross_profile_rows`）→
  行含「（另有 2 行跨 profile 不列）」
- ⑩ 新增：hint 默认 off（`test_10_hint_off_default_is_byte_identical_base_format`）
  → 行恰为 `  - drafts_s_mixed.json: 1 条`（字节级）

## 五、验收门结果

- 门禁 recipe：`cd <plugins>/filemem && PYTHONPATH=<hermes 宿主源码根目录> /usr/bin/python3 -m pytest tests -q -p no:cacheprovider`（插件仓库内自跑，宿主安装路径按环境解析，不硬编码）
- 本卡自身净态（P3-2b 落盘前）：**504 passed / 14 failed** —— 14 = 已知假红族
  13（flock×5 + host-diff×4 + import-audit×3 + flaky sa2_4 perf×1）+ sa2_4 当次
  环境 flapping 计 1；**零新增失败**（S2 key-parity 在本卡 `notice` 键镜像后转绿）。
- P3-2b（t_a8df587b）并发落盘 `no_popup.dedupe_in_session` 后的组合跑：
  508P/15F —— 新增红仅 1 条 = S2 key-parity 漂移集合变为 `['no_popup','search']`
  （decay.json 的 `no_popup` 块加了 `dedupe_in_session` 而 DECAY_DEFAULTS 未镜像）。
  该红**归 P3-2b 收口**（`no_popup.*` 为其独占区），本卡未触碰；本卡 `notice`
  键在两文件均已镜像，对 S2 无贡献漂移。
- grep 门：`_draft_file_owner_eligible` 代码零残留（仅 P1_3 文档历史提及）；
  `cross_profile_skip_hint` 在 decay.json + `__init__.py` + DECAY_DEFAULTS + 测试四处在位。
- 宿主零 diff：改动仅插件目录 4 文件 + 2 文档；宿主源码（hermes-agent 安装根目录）
  文件 mtime 均为 09-17/09-26（未动）。

## 六、宿主零改动声明

本卡全部改动位于插件目录 `<plugins>/filemem/`（`__init__.py`、`decay.json`、
`consolidation/__init__.py`、`tests/test_p1_3_notice_owner.py`、
`P1_3_NOTICE_OWNER.md`、本文档）。宿主包（hermes-agent 安装根目录）零 diff，
测试全 tmp_path 合成夹具，未触碰活 store（filemem 数据目录）数据，
未重启 gateway。local-only 交付，推仓待两卡（P1-4 + P3-2b）合并验证后一次性 fast-forward。
