# P0-2: filemem 读侧硬隔离 + 召回降噪（auto-retrieve 面）

对应 kanban 卡 t_9438e856。代码域与 P0-1 同文件（`__init__.py`），串行在
P0-1 ingress 门之后。宿主源文件零改动。

## 根因
compact 后任务漂移的主因 = filemem 读侧 auto-retrieve 把**跨 profile**（4d cron
爬虫）的过期任务记忆注入 default 会话。P0-1 修了写侧（ingress 门拒存）；本卡
修读侧（auto-inject 面硬隔离 + 降噪三件）。

## 改动清单（全在 plugins/filemem 内，宿主 agent 包（$HERMES_AGENT_SITE）零改动）

### 1. `__init__.py`（读侧硬隔离 + 降噪，纯规则+stdlib，零 LLM）
- `_resolve_session_profile(fallback_profile)`：当前会话 profile 取值。
  **实例 `source_profile`（最具体的 per-session 身份）优先，进程级
  `HERMES_PROFILE` env 兜底**；都取不到 → None → 调用方 fail-open（不隔离，
  全量召回，保现状）+ 一条 warning。实例优先于 env 是刻意的：per-session 身份
  严格比进程级 env 提示更具体，且让合成 provider 的召回在「进程 env 挂着别的
  profile」时仍确定性自洽。
- `_row_source_profile(item)`：读行上生效的 source_profile 戳（`metadata.
  source_profile` 优先，再顶层 `source_profile`；缺失/空 = 中立 None）。
  **非 dict 行 / 非 dict metadata 一律降级 None（中立保留），绝不 raise**
  （此前 `(item.get("metadata") or {})` 对非 dict item 会 AttributeError，
  违背 docstring 的 never-raise 承诺 —— 本卡修掉）。
- `_auto_recall_eligible(row_sp, session_sp)` / `_auto_recall_filter(pool,
  session_sp)`：硬隔离谓词。session_sp=None → 全保留（fail-open）；row_sp=None
  → 中立保留；row_sp==session_sp → 保留；跨 profile → 切。
- `search_memories(..., profile_scope=None)`：`profile_scope` 非 None 时，在
  **组池阶段**（lineage 切分 / 窗口 / episodic + knowledge 扩展之前）对
  memories 池 + episodic 池各跑一次 `_auto_recall_filter`。None（显式
  `filemem_search` 面）保留全池 —— 用户显式查询永不被隔离砍掉。
- `prefetch()`（auto-retrieve 面）：解析会话 profile（`_resolve_session_profile`）
  作为 `profile_scope` 传进 `search_memories`；并对 auto 面搜索文本做
  `_normalize_search_text`（剥宿主路径 token：`$HERMES_HOME`、`~/.hermes`、
  `./hermes`、裸词 `hermes`），让 "hermes" 不再与所有路径行共现（4d 爬虫误
  召回向量）；归一化 fail-soft（异常退回原 query + 单条 warning）。
  - **降噪三件并入**：① auto-inject 行数封顶 `search.auto_recall_max`
    （默认 3，0=不封顶；在注入门之后、reinforce/audit/render 之前施加，
    封顶行绝不悄悄 bump reference_count；显式面不受此限）；② 搜索文本归一化
    如上；③ 复用现成 `min_search_score`/egress 语义（不造新旋钮）。
- `_load_episodic_pool` 的 pool dict 新增 `source_profile` 键（透传行级戳；
  无戳 legacy 行 → None=中立；字节稳定 —— 各轴只读 content/topic/salience/ts/id）。

### 2. `decay.json` + `consolidation/__init__.py` DECAY_DEFAULTS
- `search.auto_recall_max: 3`（S2 键平价：两文件同值，本卡补齐 decay.json 侧）。

### 3. 既有测试桩修 6 处（P0-2 新增 `profile_scope` kwarg 打破了定参 lambda 桩）
- `tests/test_s2_egress_governance.py`（2 处）+ `tests/test_pending_draft_notice.py`
  （4 处）：`p.search_memories = lambda query, limit=5, min_score=0.3: ...` →
  `lambda *a, **k: ...`（吃透 P0-2 kwarg；与 sec_1_3 既有约定一致）。

### 4. 新增 `tests/test_p0_2_read_isolation.py`（15 条，验收门全覆盖）
① 跨 profile 行不在 auto-retrieve、在显式 search；② 中立无戳行（无键 / 空串
legacy）在 auto-retrieve；③ 行数封顶生效（>3 命中只出 top-3；0=不封顶）；
④ 路径 token 归一化后 "hermes" 不再误召回 4d 路径行。外加：fail-open（无 profile
不隔离）、实例优先于 env、行谓词双位置读取 + fail-soft 形状、prefetch 面把
session profile 作为 profile_scope 接线、decay.json/DECAY_DEFAULTS 键平价。
全部 fixture 用 L2（ttl_days=None 永不过期），查询词刻意 substring 命中目标行，
真实 search_memories 均传 min_score=0.0，避免 RRF 门槛误切。

## 验收门（recipe 钉死，py3.8）
```
cd $HERMES_HOME/plugins/filemem && PYTHONPATH=$HERMES_AGENT_SITE \
  /usr/bin/python3 -m pytest tests -q -p no:cacheprovider
```
判定 = **同组 13 条环境假红、零新增**（13 = 4 flock 之外的环境项：
config_tier_flock ×5、no_host_package_changes_in_diff ×4（目录非 git repo →
`git diff` 退 129）、sa2_1/sec_1_1 stdlib/import audit ×3（py3.8 无
`sys.stdlib_module_names`）、sa2_4 live perf ×1）。
本卡实跑：`13 failed, 475 passed`（475 = P0-1 基线 460 + P0-2 新增 15，0 丢失）。
洁净 env 与 ambient `HERMES_PROFILE=coder` 下失败集合**逐字节一致**（实例优先
于 env 的设计让 ambient profile 不再误砍合成 provider 的召回）。

## 不变量
- 宿主源零改动（off-limits）；仅 2 代码/配置文件 + 1 新测试 + 2 既有测试桩修正。
- 零 LLM、纯规则+stdlib；所有 P0-2 路径 fail-soft（异常/坏形状/无 profile →
  全量召回 + 单条 warning，绝不锁宿主）。
- 显式 `filemem_search` 面跨 profile 可见（用户查询不被隔离）；只有 auto-inject
  面被门控。

## Reviewer 提示
P0 级共享存储修复。四条验收门客观判据均已绿；环境 13 假红与 P0-1 基线逐字节
一致（comm 双向 0 新增 0 移除），非回归。
