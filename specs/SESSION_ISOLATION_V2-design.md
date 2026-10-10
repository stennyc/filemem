# Session Isolation V2 — 两面分治 + confirmed 全局骨架（设计定稿）

Owner: 2026-10-10 定稿。全部改动落在 filemem 插件侧（`/root/.hermes/plugins/filemem/`），
零 Hermes 原生代码改动（原生改动会被 `hermes update` 覆盖失效，红线）。

## 背景（实证结论，非猜测）

1. **单一全局池**：所有 profile + 所有 session 的记忆堆在 `~/.hermes/filemem/`。
   memories 池 1793 行，`source_profile` 分布：orchestrator 842 / default 539 / reviewer 102 /
   4d 83 / coder 64 / devops 59 / researcher 40 / data-engineer 28 / product-manager 22 /
   security-engineer 14。
2. **memories 池行没有 session_id 字段**（只有 `source_profile`）。session_id 只在
   **episodic 池**（`episodic/yyyy/mm/*.jsonl`，session-end 写）行上有。
3. **P0-2 profile 隔离本身在工作**：`_auto_recall_filter` 只切 cross-profile 行。
   本轮注入的 4 行全部 `source_profile='default'`（当前 profile），所以没被切——
   问题不是"跨 profile"，是"同 profile 跨 session 旧任务/脏行回喂"。
4. **P3 快照回放放大**：`prefetch` 的 P3 冷启动分支把上一轮冻结的注入块
   （`last_injected.txt`）原样回放，`_filter_replayed_snapshot` 只过 P0-2 profile 门、
   不过 session/confirmed 门 → 旧块每 turn 重放。
5. **脏行已落库**：4960 字测试思维流（preference）、9504 字 traceback（task）、
   1865 字 `<memory-quote>` 验证记录（decision），origin 全 `user/medium`，
   被 ingress 门漏进池子。
6. **`confirmed=True` 是可靠的"用户已确认"谓词**：活数据 396 条 confirmed 行全
   `origin.kind=user`（1 条 factory 种子）。写入点：distill LLM 判 `value=high`
   （`_pending_confirm` 消费 → `meta["confirmed"]=True`）、factory 种子、EP-2 supersede
   确认链。
7. **自动清理只有 L0**：`TTL_CONFIG` L0=30d `auto_cleanup=True`，O7
   `sweep_memories_expired` 超 1MB 物理搬移；L1/L2 `auto_cleanup=False` 永久。
   当前 0 行到期（池子才 ~3 天数据）。未确认行会赖在活跃池直到 30 天——这就是
   "越堆越脏"的结构性入口。

## 设计（Owner 拍板四点）

| # | 决策 | 含义 |
|---|------|------|
| ① | 原设计缺陷 = 没加 session_id 和 TTL | 承认；修复不靠补戳，靠 ②③ 谓词 + episodic 池已有 session_id |
| ② | per-profile 全局记忆 = 用户已 confirmed 的 | auto 面骨架谓词复用 `metadata.confirmed=True`，零新字段 |
| ③ | 工具面对齐 auto 面（隐私理由） | 工具面缺省 = 本 profile 池，**删掉全局级联**；`ALL` 显式才全量 |
| ④ | 现有数据不重要，可清理删除 | 存量物理删未确认行（E），不走"折出不删" |

## 渠道分治（核心）

```
自动注入面 (prefetch / pre_llm_call)
  骨架 = 当前 profile + confirmed=True 全局行
       + 当前 session 的全部行（经 episodic 池 session_id 判定）
  排除 = 其他 session 的未确认行 / 旧任务 / 脏思维流 / traceback

工具面 (filemem_search / _tool_search)
  缺省 = 当前 profile 全部行（含未确认临时行），删全局级联
  "ALL" = 显式全 profile 召回，结果 SEC-1-2 标 reference-only（只参考不作指令）
```

## 谓词（新，模块级，纯 dict 读、fail-soft）

```python
def _auto_inject_eligible(row, session_sp, cur_session_id):
    """auto-inject 面骨架谓词（prefetch / P3 回放 / egress 三面共用）：
      (1) profile 隔离   _auto_recall_eligible(row_sp, session_sp)      # P0-2 不变
      (2) O1 折出        not _o1_fold_out_of_auto(row)                   # 不变
      (3) confirmed 门:
           row 带 session_id == cur_session_id  →  收（当前 session 全收）
           row.metadata.confirmed is True        →  收（per-profile 全局）
           其他（无 session_id 旧行、未确认行）   →  折出
      session_sp=None → (1) fail-open 全收；(3) 不收紧（同 P0-2 不变式）。
    """
```

落地选择 **C-b**（Owner 拍）：memories 池行**不补 session_id**；"当前 session 全收"
只对 **episodic 池**行生效（它们自带 session_id）。memories 池行一律靠 confirmed 进
auto。谓词最终形：

```
_auto_inject_eligible(row, session_sp, cur_sid) =
    P0-2 profile 隔离
    AND not O1 折出
    AND ( row.get("session_id") == cur_sid            # episodic 当前 session 行
          or (row.get("metadata") or {}).get("confirmed")   # confirmed 全局行
          or row.get("type") == "episodic" and 缺 session_id 匹配  →  仅当 type 非 episodic 的
                                                                       memories 旧行才要求 confirmed )
```

> 简化表述：memories 池行（无 session_id）→ 只认 confirmed；episodic 行 → 认
> session_id==当前 session。两腿 OR，profile/O1 门不变。

## patch 清单（全在插件侧）

| # | 面 | 位置 | 改动 |
|---|----|------|------|
| A | 读·auto | `prefetch`（L3152）→ `search_memories(profile_scope=_session_sp, auto_only=True)` | 池装配后过 `_auto_inject_eligible`；未确认旧行折出 |
| B | 读·工具 | `_tool_search`（L7690-7701） | 缺省 `profile=None` 改 `cascade=False`（删全局级联），scope 钉当前 profile；`"ALL"` 保留全量 + reference-only |
| C | P3 快照 | `_filter_replayed_snapshot`（L3583） | 逐行谓词升级 `_auto_inject_eligible`（含 confirmed 维）；旧块未确认行折出、re-freeze |
| D | 写侧晋升 | `_save_memory` / L0→L1 晋升面 | auto-catch 只落 L0；L1/L2 晋升只收 confirmed 行（未确认临时行不长进 L1/L2） |
| E | 存量清理 | 新工具 `tools/sweep_unconfirmed.py` | 物理删 `confirmed≠True` 行（~1398 条，含脏思维流/traceback）；留 396 confirmed + 1 factory。仿 `sweep_low_value.py`：dry-run 默认 + 留痕 jsonl + consolidation_log 审计行 |

**落地顺序**：E（独立可先跑，清存量）→ A（治本轮问题，核心）→ C（打断重放）→
B（工具面对齐）→ D（防再涨）。

## 测试

- 跑现有回归：`tests/test_session_isolation_fix.py`、`test_p0_2_read_isolation.py`、
  `test_p0_1_ingress_gate.py`、`test_cull_profile_scope.py`、`test_no_popup_gate.py`
- 补新：`tests/test_auto_inject_confirmed_gate.py`
  - `_auto_inject_eligible` 三面：旧行（无 session_id、无 confirmed）折出；
    episodic 当前 session 行全收；confirmed 全局行跨 session 收；
    session_sp=None fail-open
  - `_filter_replayed_snapshot` 升级后未确认旧行折出、confirmed 行保留
  - `_tool_search` 缺省不再级联全局（0 命中不回退 profile_scope=None）

## C 的影响（已评估，无回归）

| 行类型 | 现在 P3 回放 | C 之后 | 差别 |
|--------|--------------|--------|------|
| 当前 session 未确认行 | 收 | 收 | 不变 |
| confirmed 全局行（跨 session） | 收 | 收 | 不变 |
| 旧 session 未确认行（脏思维流/traceback/旧任务） | 收（bug） | 折出 | **修这一类** |

副作用只有：P3 回放块变小 → token 占用降（现在注入 17-21KB，太重）。无功能回归，
因为"合法但未确认"的旧行本就不该进 auto 面。

## 红线遵守

- 零原生改动：全部改动在 `~/.hermes/plugins/filemem/`。
- E 物理删是 Owner 显式拍板（④），不留备份；但留**留痕 jsonl**（append-only，
  删的是活跃池、留痕可审计，非物理抹掉——"忘掉≠抹掉"的 trace 半边保留）。
- factory 种子行（origin=factory, confirmed=True）不删。

## 跨 session 延续（Session Switch）—— 定论补充

**核心问题**：S1 关闭、S2 开（session_id 不同），"agent 带旧账本摘要上岗"如何成立？
核查后厘清：自动注入面在新 session 开局对旧 session 的 episodic 是封闭的；跨 session
延续只走两条显式路。下面逐条判"跨不跨 session"。

### 三条路判定

| 路径 | 跨 session 带旧账本？ | 依据（代码点） |
|------|----------------------|----------------|
| **P3 冷启动回放** | ❌ 不带 | `sessions/{sid}/last_injected.txt` 按 session 分目录（L3503）；S2 冷启动读 `sessions/S2/...` 不存在。P3 只重放**同 session 跨进程**的冻结块，不是跨 session。 |
| **prefetch auto 面（confirmed 门）** | ❌ 不带未晋升的旧 episodic | `row.session_id == cur_session_id`（L523-524）对新 session S2 是空的（S2 还没写 episodic）；S1 的 episodic 行未 `confirmed=True` 被折出。 |
| **检索面（search_memories M4）** | ✅ 带（按需） | `_load_episodic_pool` 全池跨 session 捞，**不分 session_id**；S2 主动 `filemem_search` 能搜到 S1 摘要。 |

### 定论
1. **session_id 不一致 ≠ bug，是 V2 刻意防串的那一刀**：旧 session 的"刚发生"默认不进
   新 session 的自动脑内。要延续记忆只能走"显式检索"或"confirmed 晋升"。
2. **跨 session 延续 = 检索面（按需全捞）+ confirmed 晋升（进全局骨架）**。P3 快照
   不算跨 session（它只跨进程）。
3. **三层账本结构**：
   - `sessions/{sid}.jsonl` = 原始流水（当前 session，审计/回放，不进 LLM 召回）
   - `episodic/.../{sid}.jsonl` = 结账产物（session_end 落盘，跨 session 留存，进检索）
   - `memories/*.json` = 提纯总账（confirmed 全局骨架，长期带 TTL/salience）
   新 session "带账本上岗" = 从"结账产物/总账"取，而非"原始流水"。

### 验证
- 单测：`tests/test_auto_inject_confirmed_gate.py`（"当前 session 全收腿 + confirmed 腿"
  的三面判定，已覆盖跨 session 场景：旧 session 未确认行折出、confirmed 行跨 session 收）。
- 检索面跨 session 全捞由现有 `test_o1_supersede` / M4 recall 路径守护。

### 带回账本（bring-back）—— 延续的第三条路（owner 2026-10-10 拍板方向）
上面"跨 session 延续 = 检索面 + confirmed 晋升"是 **V2 现状**。owner 进一步拍板补第三条
**带回路**（详见 `specs/BRINGBACK-PRD.md`，尚未实现）：

- **带回 = user per-session 选择**（clarify footer 在 session 开局问一次：带回 / 干净开局 /
  只带已确认全局），**不是全局定档**。选"带回"时 auto 面叠一条"带回腿"：
  `row.session_id == 上一个关闭 session AND confirmed`（带回腿**叠 confirmed**，不串脏行；
  只带 1 个 session = 上一个，不预设"带 N 个"——要更多走检索面）。
- **关键不变式**：**带回 ≠ 失联**。检索面 M4 永远全捞（clarify 选择不影响它）；clarify 只决定
  "要不要自动灌上一个 session 的 confirmed 摘要"。选"干净开局"模型仍可 `filemem_search` 拿任意旧
  session。
- **带回腿默认关**，是 V2 confirmed 门之上的纯叠加，不破坏现有两腿（`sid==cur` / `confirmed`）；
  未实现前，"跨 session 延续"仍只走上面两条。

一句话：V2 现状 = 检索 + 晋升（封闭 auto 面对旧 episodic）；BRINGBACK = 在此之上给 user
一个**主动带回**的开关，带回也只带"上一个 session 的 confirmed 精华"，检索面常开兜底。
