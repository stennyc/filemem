# P3-2b — filemem no_popup 门补第 ④ 腿：同会话 draft 内容去重 + skip_id 自杀陷阱防护

宿主零改动（hermes-agent 安装根目录 零 filemem diff）。本卡只动插件目录
`<plugins>/filemem`，只动写侧 no_popup 门（L2254/L2326/L3279 区域）
+ decay.json + DECAY_DEFAULTS，与 P1-4 读侧（L2146-2209 通知面 /
`_draft_eligible_rows`）互斥，零重叠。

## 背景（用户主诉 10-05 实查钉死）

活体 filemem 数据目录 某 draft 文件（`drafts_20261005_165735_41f71d.json`）含 **10 行
完全同内容的 draft**（同一段"A 方案落点分叉"L2 分析被 retry-loop 连写 10 遍），
漏过 P3-1 no_popup 三腿门弹鉴权给用户。逐腿核过：

- leg① egress_noise 开表不中（正文非工具 dump）；
- leg② min_chars 不中（~3000 字 > 40）；
- leg③ salience 边界擦过（0.5 == floor 0.5，门判 `<` 严格小于）。

真根因 = **门里没有"同会话重复内容"这条腿**。本卡补 leg④，用户已拍板"1 也要"。

## 改动面

宿主零改动。本卡只动插件目录 `<plugins>/filemem`，全部为
**内容锚定 patch**（禁止 write_file 整文件覆写 `__init__.py`，与并行的 P1-4
单写者护栏一致；本卡 4 处 diff 逐处 patch，绝不整文件重写）：

| 文件 | 改动 | 说明 |
|---|---|---|
| `__init__.py` | 新增 `_draft_content_dup_in_session(self, content, skip_id=None)` | 纯内存读，扫 `self._session_drafts.get(self.session_id, [])`，判 `item.content.strip() == content.strip()` 且 `item.id != skip_id`；`session_id is None → False`（不炸）。只查本会话 in-memory 池；跨会话去重不属本卡（YAGNI，归 P1-4/P3 sweep）。 |
| `__init__.py` | 新增 `_no_popup_dedupe_enabled(self) -> bool` | 读 `decay["no_popup"]["dedupe_in_session"]`，非 False 即开（缺失/malformed 块 → 默认开，fail-soft，配置驱动不硬编码）。 |
| `__init__.py` | `_no_popup_gate` 加第 ④ 腿 + 改签名 | `def _no_popup_gate(self, content, kind, salience=None, skip_id=None)`。仅当 `kind == "draft"` 且 `_no_popup_dedupe_enabled()` 时跑 `dup = _draft_content_dup_in_session(content, skip_id)`，`dedupe_hit` 命中则并入 `hit`。去重命中复用现有 audit 原语：`action="no_popup_discard"` 行**追加字段 `"dedupe": true`**（3 旧腿 audit schema 字节不动），logger 改 `FileMem: auto-discarded duplicate draft content: {s[:24]!r}`（含 "duplicate" 字样供 grep 区分）；非去重命中沿用旧 logger 行。dup 判定异常 → 按未命中（照旧入队），audit 写失败不重入队（sticky verdict，P0-1 姿态）。 |
| `__init__.py` | 两调用点改传参 | `_add_draft` 处（`_no_popup_gate(content, kind="draft", salience=None)`）**不变**（append 前，无 skip）；`_push_pending_confirm` 处改为 `skip_id=item_id` —— **关键自杀陷阱防护**：`_add_draft` 在 append 后才走第二次 gate（`_push_pending_confirm` → 去重腿），若不去重腿 skip 掉自己刚 append 的 `item_id`，合法首条草稿会被自己第二道 gate 误杀（审计行 0 内容，面板丢行）。 |
| `decay.json` | `no_popup.dedupe_in_session: true` | 默认开；配置驱动，用户可关，不硬编码。 |
| `consolidation/__init__.py` (DECAY_DEFAULTS) | `no_popup` 块加 `dedupe_in_session: True` | **S2 键-parity 不变式要求**（P1-4 的注释明文：decay.json 与 DECAY_DEFAULTS 之间唯一容忍的顶层漂移是 `search`；每个 decay.json 键必须镜像进 DECAY_DEFAULTS）。不加会破 `test_s2_egress_governance.py::test_decay_json_defaults_egress_noise_key_parity`。P1-4 对 `notice.cross_profile_skip_hint` 也照做了同一镜像。 |

l0 kind（auto-save 面）**不走**去重腿（scope 钉死：l0 池有自己的去重面，本卡
不扩 scope）。

## 4 腿语义表

| 腿 | 触发条件 | 命中动作 |
|---|---|---|
| ① egress_noise 开表 | `_noise_table_hit` 命中（P0-3 共享表） | 丢弃 + `no_popup_discard` 审计 |
| ② too short | `len(normalized) < no_popup.min_chars`（40） | 丢弃 + 审计 |
| ③ low salience | 显式 `salience < no_popup.salience_floor`（0.5） | 丢弃 + 审计 |
| ④ **draft 同会话重复**（本卡新增） | `kind == "draft"` 且 `dedupe_in_session` 非 False 且本会话内存池已有 `content.strip()` 相等、`id != skip_id` 的行 | 丢弃 + `no_popup_discard` 审计行**追加 `"dedupe": true`** + logger "auto-discarded **duplicate** draft content" |

fail-soft 姿势不变（宁多弹不可崩）：任何一腿判定异常 → 按未命中（照旧入队）；
audit 写失败不重入队（sticky verdict，P0-1 姿态）。

## 自杀陷阱说明（skip_id 防护）

`_add_draft` 的完整路径：
1. gate#1（L3279，`skip_id=None`）：append **前**判，池里若无同内容则放行。
2. append 后 → `_push_pending_confirm` → gate#2（`skip_id=item_id`）：此时池里
   **已有**这条刚 append 的 item，若 gate#2 不带 skip，去重腿会把自己刚写的行
   判成"重复" → 误杀合法首条草稿（审计行 0 内容，[记忆报告] 面板丢行）。
   `skip_id=item_id` 跳过自己那一行 → 首条合法草稿安然入 pending-confirm 队列。

只有 gate#2（push_pending_confirm）传 `skip_id`；gate#1（_add_draft）不传
（append 前无自己可 skip）。这就是防自杀的关键。

## 测试基线（tests/test_no_popup_gate.py）

现有 12 条零改断言全绿（新腿默认开对旧 3 腿行为零影响）+ 新增 5 条 ④a-e：

- **④a** `test_dedupe_a_second_same_content_draft_discarded_with_dedupe_audit`
  — 同内容第 2 次 `_add_draft` 丢弃 + 审计行带 `"dedupe": true` + 池/盘各 1 行
  （165735 场景复现）；
- **④b** `test_dedupe_b_different_content_both_land_zero_false_kill` — 不同内容
  两条都入（零误杀，无 dedupe 审计行）；
- **④c** `test_dedupe_c_knob_off_repeats_release` — `dedupe_in_session=false`
  → 重复放行（开关可关）；
- **④d** `test_dedupe_d_l0_face_does_not_run_dedupe_leg` — kind="l0" 重复内容
  不走去重腿，照旧两条都入队（scope 钉死）；
- **④e** `test_dedupe_e_skip_guard_first_draft_not_self_killed` — 合法首条完整
  路径（含 gate#2）不被自杀、面板 pending-confirm 收到该条、0 dedupe 审计行
  （防自杀回归门）。

全量 `tests/test_no_popup_gate.py`：17 passed（12 基线 + 5 新增，全绿）。

## 全量 pytest 结果（hermes-agent venv, python3.11）

`python -m pytest tests -q -p no:cacheprovider` → **513 passed / 10 failed**。
10 条失败全部为已知假红族（与本卡改动无关，P1-4 并行落盘前的既有基线）：

- flock tmp_path 族 ×4（`test_config_tier_flock.py`：
  auto_neutral_root_from_profile_home / ..._from_base_home_no_profiles /
  dual_root_defense_missing_base_falls_back_loud / unresolved_symlink_home_is_defended）；
- host-diff 族 ×4（`test_s1b_tick_wiring` / `test_s4_corrupt_heal` /
  `test_s4_lifespan` / `test_s6_gate_relax` 的 `test_no_host_package_changes_in_diff`，
  对本 sandbox 活宿主报 diff，非逻辑错）；
- flaky sa2_3 ×1（`test_sa2_3_bm25_rrf::test_retrievability_gate_en_zh` golden）；
- sa2_4 graph perf ×1（`test_sa2_4_graph::test_live_build_perf_and_counts`
  活库 build 超时，机器负载相关）。

**本卡零新增失败**：本卡 5 条新测试全绿，且本卡曾因 decay.json 加 `dedupe_in_session`
而一度打破的 `test_s2_egress_governance.py::test_decay_json_defaults_egress_noise_key_parity`
已通过在 DECAY_DEFAULTS 镜像同键修复，现 16/16 全绿。

## 宿主零改动声明

hermes-agent 安装根目录 内 grep `dedupe_in_session` /
`_draft_content_dup_in_session` / `_no_popup_dedupe_enabled` = **0 命中**。
本卡改动全部落在插件目录 4 个文件：

- `<plugins>/filemem/__init__.py`
- `<plugins>/filemem/decay.json`
- `<plugins>/filemem/consolidation/__init__.py`
- `<plugins>/filemem/tests/test_no_popup_gate.py`

与 P1-4 读侧（L2146-2209 `_draft_eligible_rows` 通知面）零重叠：本卡只动写侧
`_no_popup_gate` / `_push_pending_confirm` / `_add_draft` + decay.json no_popup
块 + DECAY_DEFAULTS no_popup 块；P1-4 只动读侧 + decay.json notice 块 +
DECAY_DEFAULTS notice 块。`__init__.py` 内 P1-4 的 `_draft_eligible_rows`
（L2179 调用点 / L2220 定义）未被本卡 patch 触碰（内容锚定 patch 全程只匹配
写侧锚点，未整文件覆写）。

## 不推仓（两卡 local-only）

本卡与 P1-4 均 local-only；推仓等两卡都 done 后由编排者合并验证
（disjoint 区域 grep + 组合 pytest）一次性 fast-forward，符合"验证后才推进"。
