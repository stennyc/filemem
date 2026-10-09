# Epic ① LLM 显式化 — 收口备忘录（2026-10-04 22:00 +08）

> 本备忘录是 Epic ①（4d LLM 面显式化 + 宿主主模型路由）的书面闭环记录。
> 官方门卡 t_bc249735 在 20:40–21:13 的整板清板中丢失（无终态留档），
> 本文件以其 20:35 全 GREEN 判定 + 本次 21:16–21:47 活体复核 + 21:47 py3.8 全量重跑
> 为证据源，补上书面验收。全部数字为落笔时刻实测，非回忆值。

## 一、六项验收终判（21:47 复跑口径）

| 项 | 判定 | 实证（落笔时刻） |
|---|---|---|
| ① 新 tick llm_call 行带 model/provider（非 None） | ✅ | 全 log llm_call=167；带 model 行=176，model 全分布 = agnes-3.0-flash（custom） |
| ② 零 model_unavailable_fallback / 零 main_runtime skip | ✅ | fallback 行=0；main_runtime missing 重启后仅 1 条瞬态（13:34:38Z），其后 14 条 llm_call **全部**带 model（下一 tick per-tick rebind 捕获，自愈） |
| ③ face 级遥测可 grep | ✅ | distill / confirm_supersede / suggest_aliases 三类 face 行均在 consolidation_log |
| ④ 宿主 git diff=0 | ✅ | 三方 @8ce163f 零 diff（20:35 门判）；本备忘录后新增的 window 修复仅动插件面 2 文件 |
| ⑤ 宿主 py3.8 全量 | ✅* | 21:47 重跑：441 passed / 13 failed，**13 项逐条归因全为非缺陷**（下表），红卡唯一真缺陷已修并锁定 |
| ⑥ 推仓 + rsync 三面同步 | ✅ | 远端 = 本地 repo = 生产树 @8ce163f 零 diff（20:35 门判）；本备忘录提交后 rsync 增量 = window 修复 2 文件 |

\* ⑤ 的 13 项失败归因（py3.8 @ 生产树，env PYTHONPATH=/usr/local/lib/hermes-agent）：

| 类 | 数量 | 测试 | 根因 | 性质 |
|---|---|---|---|---|
| 环境 | 3 | sa2_1 ×2 + sec_1_1 ×1 `*_stdlib_only_import_audit` / `plugin_top_level_import_audit` | py3.8 缺 `sys.stdlib_module_names`（3.10+ API） | 已知假红（清单在册） |
| 环境 | 5 | config_tier_flock ×5 | tmp_path 含 `profiles` 段触发 storage root 解析误判 + flock 子进程 import 面 | 已知假红（清单在册） |
| 环境 | 4 | s1b / s4_corrupt / s4_life / s6 `test_no_host_package_changes_in_diff` | 生产树无 `.git`（`git diff` rc=129）；同 4 测试在 repo 面（有 .git）4/4 绿（21:55 实测） | 环境项（生产树纯 rsync 目标） |
| 数据漂移 | 1 | sa2_4 `test_live_build_perf_and_counts` | 活语料 227→311 行（memories 153 + episodic 158），1s 预算实测 1.8s | 预算需随语料重校，非回归 |
| 真缺陷→已修 | 1 | sa2_2 `test_live_week_window_smoke` | EP-1 谱系 backfill 绕过 SA2-2 window（见 §二） | **已修 + 锁定测试 + 全绿** |

## 二、EP-1 × SA2-2 window 绕过（本 Epic 收口发现的唯一真缺陷，已修）

**症状**：带 window 的 `search_memories` 返回出窗行 fact_15（09-23，窗口 09-27 起）/ fact_70 / fact_115。
**根因**（探针两步钉死）：
1. `filter_pool_by_window` 本身正常——出窗行在过滤后主池中被正确排除；
2. 但 EP-1 谱系 split（`__init__.py` L3680，在 window 过滤 L3705 之前）把
   `metadata.superseded=True` 行拆出后，backfill 循环（L3854）在 scoring 后
   追加且 bypass min_score gate —— **该路径完全不受 window 约束**。
   三条出窗行正是 `confirm_supersede` LLM face 盖过 superseded 戳的活行。
**修复（方向 a，最小）**：`search_memories` window 块内对 backfill 源
`_lineage_superseded` 补跑同一谓词（+2 行，注释含回归说明）；
window=None 时该行为 no-op（legacy 无谱系语料 backfill 恒空，byte-identical 回归门不破）。
**锁定**：`tests/test_sa2_2_window.py::test_lineage_backfill_respects_window`
（合成种子，不依赖活数据）+ 原 `test_live_week_window_smoke` 复绿。
**验证**：py3.8 定向 26/26 绿（window 15 + EP-1 10 + 新测试 1）；全量 441/13（§一表）。

## 三、LLM 活体出账（修复面持续生效证明）

- 最新 llm_call 行 21:44:16Z（查询前 3 分钟），最后 5 条落在 21:38:48–54Z（suggest_aliases / distill）。
- `main_runtime missing` 最后一条 = 06:36:37Z（重启前旧故障面）；重启（12:23Z）后仅
  13:34:38Z 一条瞬态（face=None 的 tick 首跳），其后 14/14 带 model → per-tick rebind 自愈。
- 当前网关（pid 1081820）environ 仍带 `HERMES_BIN=/usr/local/lib/hermes-agent/venv/bin/hermes`
  drop-in，systemd user 单元 drop-in 在位，跨 recycle 持久。

## 四、整板清板调查（20:40–21:13，官方门卡 t_bc249735 丢失）

- `kanban.db` mtime = 21:06:44，落在清板窗口内；`task_events` 最后一条 = 18:29:19
  （t_d9dcf2fa archived，epoch 时间戳，非 ISO）。
- 幸存 5 张 archived 卡含 2 张 filemem LLM 面卡：t_d9dcf2fa
  （"LLM 面全 skip 根因诊断 + lazy 捕获修复（4d 活体闭环）"，18:29 归档）与
  t_805119bb（"Trigger gateway reload and await natural consolidation"）。
- 清板后 95 张 done 卡 + 其 events 全部消失；Epic 卡 id
  （t_bc249735 / t_4d1be710 / t_e6bd75fa / t_3b00b8c3 / t_805daf69）在
  kanban.db、kanban/kanban.db、tasks.db、tasks/tasks.db、state.db 全盘 0 痕迹。
- gateway.log / agent.log / cleanup.log / clean-tui-cron.log / curator/ 中
  **搜不到** bulk kanban archive/prune/wipe 操作行。
- 判定：清板由 kanban 内核外的操作执行（无事件留痕、无日志留痕），执行者无法从
  盘上证据归因；不影响 Epic ① 验收有效性——§一/§二/§三 全部证据在 disk 上可复现。
  幸存 2 张 fm 卡（t_d9dcf2fa 等）可作官方门卡判定原件参照。

## 五、行动清单（Epic ① 闭环后）

1. [x] window 缺陷修复 + 锁定测试（本备忘录 §二，commit 8c48610）
2. [x] py3.8 全量重跑 + 归因（§一表，commit 8c48610）
3. [x] sa2_4 graph build 预算随语料自适应：`max(1000ms, 20ms × node_count)`
   —— spec 锚点（≤88 节点 = 1s）原样保留，超 spec 尺寸后按节点数线性放开
   （co-occurrence 组合对超线性，固定提额几天后再红；20ms/节点对 189 节点
   实测中位 2.3s 留 ~6x 复杂度回归捕获余量）。commit bb6cf55。
4. [x] 备忘录 + 修复推送远端：8ce163f → bb6cf55（2 commits，22:0x +08）
5. [ ] 后续 Epic 门卡验收后，归档时同板写事件、避免再丢留痕（清板教训，§四）

### 收口终态（推送后复验，非落笔时刻值）
- 生产树 py3.8 全量：**442 passed / 12 failed**（12 = 5 flock tmp_path +
  4 生产树无 .git + 3 py3.8 缺 stdlib_module_names，全已知环境类，零真缺陷）
- repo 面 py3.8 全量：**446 passed / 8 failed**（8 = 5 flock tmp_path +
  3 stdlib_module_names；4 个 no-git 测试在 repo 面 4/4 绿）
- `git ls-remote` main = bb6cf55
