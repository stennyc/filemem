# PLAN 类型 PRD（filemem plan memory type + filemem_plan tool）

> owner 钉：2026-10-14
> 状态：设计定稿（含多 profile / anchor / reconcile 修正），待 TDD 实现
> 改动面：仅 plugin 源码 + tests（红线：host tree 只读）
> 无 CRON/timer（红线：记忆机制禁止定时触发）

---

## 0. 背景与目标

agent 在 session 内经常产出**结构化多步骤计划**（PRD、修复步骤序列、实施 roadmap），
这些计划落地（用户确认最终版）后，执行过程中 agent 会偏离：做一半忘了、
跨 session 丢失、撞墙后换方案没记录、被新路线取代没留痕、
**召回的进度与落盘代码失同步**。

现有 4 个记忆类型没有一个匹配"有序步骤 + 完成判据 + 进度状态 + 方案演进谱系 +
可核实锚点"这个信息类。本 PRD 新增：

1. 第 5 个记忆类型 `plan`（独立分片 `plan.json`）
2. 专用工具 `filemem_plan`（list / create / advance 三动词，驱动 plan 全生命周期）
3. 写门 = probe + clarify 确认链（slice-4，owner 2026-10-17：LLM 探测到 agent 自己写的多步计划 → [记忆报告] 列 pending 条目 → 用户点头 → `filemem_plan(create)` 落地；分类面/regex 面永不直接写 plan 分片，见 §2/§1.4）
4. 状态机（6 状态）+ 状态相关 level 迁移（活任务 L2 无 TTL，终态降 L0/L1）
5. 读时两级 clarify（用户问"有哪些未完成 plan"时触发，b 面）
6. **auto-inject 面恒折出 plan**（任务 ≠ 上下文，plan 永不自动注入）
7. **多 profile 全开放 + 跨 profile 打戳**（不设管理者、不设创建者路线锁）
8. **step anchor 强制（跨 profile 可机验）** + **reconcile 核实-同步 op**

**目标（可验证）**：
- 一份落地 plan 存入后，跨 session 不丢失（L2 无 TTL）
- plan 执行中任何方案变更/卡住/换路线都留谱系（step 内候选 + plan 级 superseded）
- 用户问"未完成 plan"时，agent 不翻原始 json，走 `filemem_plan(action="list")`
- 用户对该份 plan 的每个决定（继续/换方案/完结/撤销/重开/核实）都走
  `filemem_plan(action="advance")`
- **召回的进度可核实**：每个 done 步骤带可机验锚点，续做前先 reconcile 核实
- **多 profile 协作**：任何启用 filemem 的 profile 可推进/改路线任何 plan，
  跨 profile 操作打戳可追溯，不锁创建者、不设管理者

---

## 1. 新增类型 `plan`

### 1.1 类型注册

```python
# __init__.py L115
MEMORY_TYPES = ["preference", "fact", "decision", "task", "plan"]
```

### 1.2 存储分片

- 新增 `memories/plan.json`，与 fact/task/preference/decision 分片平级
- `_SETTLED_SCAN_SHARDS` **不**加 `plan`（kanban settle 机制不适用 plan；plan 的
  done 走自己的状态机 + `complete_all`，不走 kanban done-signal 折出）
- TTL：plan 行 level 是**动态的**（§5 状态相关迁移），分片本身不按 level 批量清；
  清理走 L0/L1 的**行级** TTL（L0 30 天 + 14 天 warning，L1 90 天）

### 1.3 regex 检测（`_MEMORY_TYPE_REGEX` 加条目）

```python
_MEMORY_TYPE_REGEX["plan"] = [
    # 英文强信号
    r"\bstep\s*\d\b|\bphase\s*\d\b|roadmap|implementation\s+plan"
    r"|fix\s+sequence|repair\s+plan|action\s+plan|execution\s+plan|multi-step"
    r"|\bprds?\b",
    # 中文强信号
    r"步骤\s*\d|第\s*\d+\s*(步|阶段|点|条)|实施计划|修复步骤|执行计划"
    r"|多步|落地方案|PRD|路线图|分\s*\d+\s*(步|阶段)",
]
```

**优先级**：`preference > decision > task > plan > fact`
（plan 排在 task 之后：plan 内容常含"需要/待办"词，放后面避免抢 task 的行；
纯"有序步骤 + 完成判据"的强信号才命中 plan。）

### 1.4 LLM 面（`consolidation/llm_caller.py`）

**SLICE-4 反转（owner 裁决 2026-10-17）**：LLM 分类面（type-classifier）**不得**
是 plan 行进入 plan.json 的门。分类器缺乏"识别 agent 自己生成的 PRD / plan /
有序多步序列"的能力，把普通内容（用户纠正、设计选择、水位方案）贴了 plan 标签
污染 plan shard。故 slice-4 把 plan 从分类面的**返回词表**里移除：

- `TYPE_CLASSIFY_SCHEMA` enum **不含** `"plan"`（只剩
  `["preference","fact","decision","task"]`）；
- `TYPE_CLASSIFY_INSTRUCTIONS` 加显式 **never-return-"plan"** 条款（旧"plan =
  有序多步序列"定义行删除）；plan-shape 内容在此面归 `task`（有序待完成）或
  `decision`（已定案）；
- `classify_type` helper 的 verdict 有效集排除 `"plan"`（LLM 回 plan 一律判
  invalid -> None fail-soft）；
- `_reclassify_type_llm` 调用侧白名单只剩
  `("preference","decision","task")`（load-bearing 层——它才决定行落哪个
  shard；LLM 回 plan -> None，regex fact verdict stands）。

plan 行进入 plan.json 的唯一门是 §2 的 **probe + clarify 确认链**（下方），
不是分类面。

### 1.5 plan 行 schema（metadata，含强制 anchor）

```json
{
  "id": "plan_20261014_abc123",
  "type": "plan",
  "content": "filemem plan 类型扩展",
  "level": "L2",
  "source": "manual",
  "created_at": "...", "updated_at": "...",
  "metadata": {
    "plan_state": "in_progress",
    "plan_id": "plan_20261014_abc123",
    "plan_revision": 1,
    "plan_steps": [
      {"id": 1, "text": "...", "status": "done",
       "anchor": {"kind": "test_pass", "value": "tests/test_plan_type.py::test_list",
                   "verified_at": "..."}},
      {"id": 2, "text": "...", "status": "done",
       "anchor": {"kind": "git_hash", "value": "d87aa17",
                   "verified_at": "..."}},
      {"id": 3, "text": "...", "status": "in_progress", "anchor": null},
      {"id": 4, "text": "...", "status": "pending", "anchor": null}
    ],
    "done_when": "4 个步骤全部 done（且每步有 anchor）",
    "source_session": "20261014_...",
    "last_step_updated_at": "...",
    "topic": "filemem",
    "source_profile": "coder",
    "last_touched_by": "coder",
    "last_completed_by": null,
    "decision_log": []
  }
}
```

**anchor 强制（owner 钉 2026-10-14）**：
- 每个 step 落 `done` 时，**必须**带 `anchor`（`kind` 三选一 + `value` +
  `verified_at`）或显式 `no_anchor`，**无模糊态**——不允许"没 anchor 又没标
  no_anchor"
- **anchor kind 白名单 = 跨 profile 可机验的三类**：
  - `test_pass`：某测试文件/用例真实跑过（terminal 可复核）
  - `git_hash`：落进某 commit（`git cat-file -e <hash>` 可复核）
  - `file_path`：某文件存在且含某 marker（read_file 可复核）
  - `no_anchor`：纯设计/讨论类步骤（无可机验凭据）→ **明确标记**，召回时
    reconcile 降级为"待人工核实"
- 白名单天然是"跨 profile 可机验"（磁盘/仓库/测试共享，任何 profile 都能
  复核）；**禁止**把"某 profile 的 session 内部临时变量"当 anchor（不可跨
  profile 机验）

**upsert 语义**：同一 `plan_id` 的后续 update 是 upsert（刷新 `updated_at` +
合并 metadata，不新开行）——plan 行是**可重复写的活记录**，不是"一事一行"
的不可变日志。这是 plan 与其余类型的本质区别。

**`decision_log[]`**（路线变更审计，§7 权限模型）：跨 profile 的路线变更
（select_option / reopen / abandon）追加一条 `{ts, by, op, reason}`，
可追溯"谁在什么时候改的路线"。

---

## 2. 写门 clarify（slice-4 改为 probe + clarify 确认链）

**SLICE-4（owner 裁决 2026-10-17）**：§2 原设计（`_detect_type` 命中 plan +
commit 信号 → 在 `_save_memory` 落盘前 clarify）被 probe + clarify 面取代——
原因是 §1.4 把 plan 从分类/regex 面移除后，`_detect_type` 不再返 plan，
`mem_type == "plan"` 的写门分支不复存在。新门如下：

**触发**（两个独立挂点，各自独立锚，不混）：

**挂点 A（slice-4，auto 行出口）**：`_save_memory` 的 auto 来源行落到
`task` / `decision` 分片 → post-lock、out-of-lock 跑 `_plan_probe`（与
distill 同姿态，host LLM 不坐锁内）。
**挂点 B（slice-5 F4，owner 2026-10-17 "补活路"）**：agent 写 spec/PRD
文件是**直接落盘**（write_file/patch），从不经过记忆存储，所以挂点 A
结构性地看不见它（supercompress PRD 轮验证：`probe_plan` 调用 0 次、
`plan_probe` 审计行 0 条）。活路是**本轮 assistant 文本**报落盘那一刻
（"PRD 落地了。文件：/…/specs/水位递进带PRD.md … 8 条 TDD RED 锚"）——
`sync_turn` 轮末（turn-close B4 fallback 之后）对 `assistant_content`
跑同一个 budget-gated `_plan_probe`，outside any write lock（与 distill
LLM pass 同姿态）。命中推 PENDING marker 走 clarify 通道，下一 turn 的
[记忆报告] 列出，用户定存/不存。零自动落盘（owner 裁决，slice-4 继承）。

**判定**：`_plan_probe` 是唯一的 plan 候选 LLM 面。预算门（per-session
cap，超 cap 不发 LLM 调用）+ 双层 pre-screen（slice-5 F4，**任一层命中
即过闸**——两层是 OR 不是 AND：AND 门会挂掉 slice-4 既有行为，plan-signal
词单独就够触发；pre-screen 只是成本闸，最终判定归 LLM）+ fail-soft
（host-down / miss / over-budget -> None，task/decision 行照常落盘，不阻塞
宿主）：
- **layer A（强）**：`_PLAN_LAND_SIGNALS`（模块级词表，spec 落盘结构词：
  "PRD 落地"、"specs/"、"独立失败面"、"逃生门"、"TDD RED 锚" …）。单条
  命中 = agent 在报 spec/PRD 落盘，是最强 plan 信号，单独足够过闸。
- **layer B（弱）**：`_MEMORY_TYPE_REGEX["plan"]`（plan-signal 词："步骤N"、
  "落地方案"、"实施计划" …）。单独命中也过闸（slice-4 行为不变）。
- **两层都 miss** -> `prescreen_skip` -> 不发 LLM 调用（成本红线，"宁漏
  勿炸"只适用于双 miss 情形）。判定记在 `self._plan_probe_last_decision`
  （"prescreen_pass"/"prescreen_skip"），`_plan_probe_last_seen` 记被探测
  的原文，供活路审计。命中 LLM 回 `probe=true` + 结构化的 `{title, steps, done_when}` 时，
产一个 PENDING PROBE marker（**不写行**），经 clarify 通道
（`_push_pending_confirm(kind="plan_probe")`）在下一 turn 的 [记忆报告] 列出，
问用户"是否存为持久 plan 记忆"。

**clarify 内容**：

```
检测到多步计划「<title>」（<N> 步，done-when: <done_when>），
是否存为持久 plan 记忆？
  - 存（进 plan 分片，L2 无 TTL，跨 session 不丢）
  - 不存（丢弃，task/decision 行已落盘，不动）
```

**消费**：`filemem_review_drafts(action="plan_probe", verdict=...)`：

- 用户答"存"（verdict="save"）→ 走 `filemem_plan(action="create")` 写
  `plan.json`，`plan_state=in_progress`，`level=L2`，`source=manual`，
  `source_profile=<当前 profile>`（`_plan_create` 构造 PRD 形状行 + 三戳 +
  auto-id'd pending steps + 强制 anchor）。这是 plan shard 的**唯一写入门**。
- 用户答"不存"（verdict="dismiss"）→ 不写 `plan.json`，消耗 panel 条目（停
  止重新上报）。task/decision 行已在盘上，不动。

**关键不变量**：
- 无任何自动落盘路径（probe 只产 marker，clarify 是用户确认门）。
- 分类面（§1.4）永不写 plan shard（owner 红线）——probe + 用户点头是唯一门。
- 预算记账姿态："charge the attempt, not the success"——host-call 失败也扣
  session 预算，持续 host-down 不重试 LLM 调用（避免 LLM 成本爆炸红线），
  镜像 `_reclassify_type_llm` 的"call happened -> charge it"。

**位置**：`_plan_probe` + `_plan_probe_push`（`__init__.py`，save-door
post-lock LLM pass，与 distill 同段）+ `filemem_review_drafts` 的
`action="plan_probe"` 消费分支。clarify 走既有 `_push_pending_confirm`
pending-confirm 队列（[记忆报告] 面板，零新宿主代码）。

---

## 3. 读时两级 clarify（b 面）

**触发时机 = b（用户主动问才触发）**，owner 钉。

- **不做** session 开局自动扫 plan 分片。bring-back 4 选项面**保持纯净**，
  专管"自动注入面"，不掺 plan。
- 用户问"有哪些未完成的 plan / 还有哪份 plan 没做完" → agent 调
  `filemem_plan(action="list")`（缺省跨 profile，见 §6.3）→ 列表非空 →
  进两级 clarify。列表为空 → 直接答"没有未完成 plan"，不弹 clarify。
- **因为不自动触发，不需要"最多弹 3 次"的防吵机制**（那是给自动面设计的）。
  代价（如实说）：用户不问，plan 就一直挂着——这是 b 的定义行为，不是缺陷。

### 第一级：选哪份

```
有 N 份未完成 plan：
  1. <title> — 进行到第 X/Y 步（<topic>，source: <profile>）
  2. <title> — 卡在第 X 步：<blocked_reason>（source: <profile>）
  ...
要继续哪一份？
  - 继续 plan 1
  - 继续 plan 2
  - 都不处理，直接开新 session
```

（blocked 的 plan 在 list 里**单独标注卡住原因 + 当时摆的选项 + 未选项的
rejected 理由**，agent 看到"卡在第 3 步，摆了 b1/b2/b3，b2 已选 / 未决"
才知道怎么续接，不重新撞墙。）

### 第二级：对选中那份做什么（按 plan_state 分支）

**in_progress：**
```
<plan.title>，当前进度：
  1. [x] ...（anchor: test_pass tests/test_x.py::t）
  2. [x] ...（anchor: git d87aa17）
  3. [>] ...（无 anchor，pending）
  4. [ ] ...
下一步？
  - 继续推进（先 reconcile 核实进度再干）
  - 撤销整个 plan（abandon）
  - 标记已完成（done，全部步骤强制置 done）
  - 暂不处理（保持 in_progress）
```

**blocked：**
```
<plan.title> 卡在第 X 步：<blocked_reason>
当时摆的选项：
  - b1: <text>（已弃，理由：<reason>）
  - b2: <text>（已选 / 未决）
  - b3: <text>（已弃，理由：<reason>）
下一步？
  - 从摆的选项里选一个换方案（select_option）
  - 按原路继续（resume，不换方案）
  - 撤销整个 plan（abandon）
  - 标记已完成（done）
  - 暂不处理（保持 blocked）
```

### 两级 clarify 的落点

- 第一级每个选项 → 锁定该 plan_id
- 第二级每个选项 → 调 `filemem_plan(action="advance", plan_id=..., op=...)`
  （op 映射见 §6.2）
- "暂不处理" → 不改 plan 行（`plan_state` 不变，`updated_at` 不变），
  下次再问时 plan 还在（L2 无 TTL）

### 续做类 op 的隐含前置（owner 钉）

第二级选"继续推进 / 按原路继续 / 换方案"（continue / resume / select_option）
时，**agent 续做前先调 `advance(op="reconcile")` 核实进度**，再实际推进。
这把"先核实后延续"从"靠 agent 自觉"变成"工具动词"——agent 续做 plan 就调
reconcile，忘不掉。

---

## 4. 状态机（6 状态）

```
                     create（probe+clarify 确认链，§2 slice-4）
                        │
                        ▼
                  ┌─────────────┐
                  │ in_progress │◄────────────────────────┐
                  └─────────────┘                        │
                    │   │   │                             │
      revise        │   │   │   block                     │ resume
      (原地改)      │   │   │   (卡住+摆选项)              │ (按原路)
                    │   │   │   │                         │
                    │   │   │   ▼                         │
                    │   │   │ ┌─────────┐                 │
                    │   │   │ │ blocked │──────────────────┘
                    │   │   │ └─────────┘
                    │   │   │        │ select_option
                    │   │   │        │ (选 b2，弃 b1/b3，
                    │   │   │        │  step 内 audit)
                    │   │   │        ▼
                    │   │   │  回 in_progress（revision+1）
                    │   │   │
                    │   │   │  complete_all（强制全步骤 done）
                    │   │   │        ▼
                    │   │   │   ┌────────┐
                    │   │   │   │  done  │  settled=True, L2
                    │   │   │   └────────┘
                    │   │   │
                    │   │   │  abandon（用户明确不要，无新plan）
                    │   │   │        ▼
                    │   │   │   ┌───────────┐
                    │   │   │   │ abandoned │  superseded=True, L0
                    │   │   │   └───────────┘
                    │   │   │
                    │   │   │  abandon + create 新plan（方向变更）
                    │   │   │        ▼
                    │   │   │   ┌───────────┐
                    │   │   │   │superseded │  谱系→新plan, L1
                    │   │   │   └───────────┘
                    │   │   │
                    │   │   │  reopen（终态重开）──► 回 in_progress（L2）
```

**6 个状态**：`in_progress` / `done` / `abandoned` / `superseded` / `blocked` /
（blocked 内部"摆选项"数据面是 `step.options` 结构，不算独立状态）。

### 各状态语义

| 状态 | 语义 | 触发 op | level | 折出 auto-inject? |
|---|---|---|---|---|
| `in_progress` | 活任务，执行中 | create / continue / resume / select_option / reopen | **L2 无 TTL** | 恒折（§3） |
| `blocked` | 卡住等用户决策（中间态，非终态） | block | **L2 无 TTL** | 恒折 |
| `done` | 全部完成（含跨 profile 手动做完漏更新，强制全步骤 done） | complete_all | **L2**（经验价值） | 恒折 + settled |
| `abandoned` | 用户明确不要了（无接续新 plan） | abandon | **L0**（30天清） | 恒折 + superseded |
| `superseded` | 方向变更，被新 plan 路线取代（不是"错了"，是"换路线了"） | abandon + create 新plan（谱系） | **L1**（90天） | 恒折 + superseded_by |

**所有 plan 状态恒折出 auto-inject**（§3 的"plan 任务≠上下文"）——plan 行
**永不**被自动灌进上下文，唯一召回入口是 b 面 `list`。

### done 的语义钉死（owner 钉）

"标记已完成" = **强制把所有 pending/in_progress 步骤全部置 done**。
理由：用户可能手动改过或在其他 profile 完成了但漏更新——选"已完成"就是把
脏状态一次清掉，不必先去同步其他 profile。

### superseded 与 abandoned 的区别（owner 钉）

- `abandoned`：整份作废，**没有**接续新 plan（"不要了"）→ L0
- `superseded`：整份**路线**被新 plan 取代（"别走 a 路线走 b"）→ 旧行
  `superseded_by=<新 plan_id>`，**保留 L1**（"为何没走 a"是决策记录，
  参考价值比 abandoned 高、比 done 低）

---

## 5. level 迁移表（状态相关，非固定值）

| plan_state | level | TTL | 说明 |
|---|---|---|---|
| `in_progress` | L2 | 无 | 活任务永远活着 |
| `blocked` | L2 | 无 | 中间态，比 in_progress 更需要被记住（主动停等人） |
| `done` | L2 | 无（settled 折出 auto-inject，盘上留） | 经验价值 |
| `abandoned` | L0 | 30 天自动清 + 14 天 warning | 无参考价值 |
| `superseded` | L1 | 90 天 | 决策记录价值 |

**迁移规则**（advance 各 op 内原子完成，同一把 plan.json 分片锁）：
- `create` / `resume` / `select_option` / `reopen` / `continue` / `revise` /
  `reconcile`：保持或升 L2
- `complete_all`：→ L2（done 保留 L2）
- `abandon`（无 supersede_with）：→ L0
- `abandon` + `supersede_with`（方向变更）：→ L1（superseded）
- **永不清理 in_progress / blocked 行**（L2 无 TTL + 不进
  `_SETTLED_SCAN_SHARDS`，kanban 信号不误折 plan）

---

## 6. `filemem_plan` 工具（3 action × 9 op）

### 6.1 工具 schema（注册进 host tool registry）

```
filemem_plan(
  action: "list" | "create" | "advance",
  # ── list 参数 ──
  state?: "in_progress" | "blocked" | "done" | "abandoned" | "superseded" | "all",
          # 缺省 "in_progress" → 实返 in_progress + blocked（"未完成"的语义，§6.4）
  topic?: str,
  profile?: str,        # 缺省 = 跨 profile 全量（§6.3，启用 filemem 即见）
                        #   "source:X"    = 只看 X 落的（source_profile 过滤）
                        #   "touched:X"   = 只看 X 推进过的（last_touched_by 过滤）
  # ── create 参数 ──
  title: str,
  steps: [str],         # 有序步骤（自动编号 id）
  done_when?: str,
  topic?: str,
  # ── advance 参数 ──
  plan_id: str,
  op: "continue" | "revise" | "block" | "select_option" | "resume"
     | "complete_all" | "abandon" | "reopen" | "reconcile",
  step?: int,
  step_done?: int,      # continue 时标记某步 done
  anchor?: {kind, value},  # continue/complete_all 落 done 时必带（§1.5 强制）
  reason?: str,
  options?: [{id, text}],
  choice?: str,
  new_steps?: [str],
  supersede_with?: str,
)
```

### 6.2 各 op 的原子动作（advance 内，同一把 plan.json 分片锁）

| op | 原子动作 | 状态 | level |
|---|---|---|---|
| `continue` | 某步 status→done（`step_done`），**必带 anchor**（§1.5），刷新 updated_at + last_touched_by | in_progress 内 | L2 |
| `revise` | 整份步骤替换（`new_steps`，同目标微调），revision+1 | in_progress 内 | L2 |
| `block` | state→blocked，存 blocked_at_step/reason/options[]（outcome=pending） | in_progress→blocked | L2 |
| `select_option` | 指定 step：原方案→chosen（`choice`），未选 option 标 rejected（带 reason）；step text 更新；revision+1；state blocked→in_progress；**decision_log 追加**（路线变更，§7） | blocked→in_progress | L2 |
| `resume` | state blocked→in_progress（不换方案，纯状态复位；原 options 标未采用） | blocked→in_progress | L2 |
| `complete_all` | 全 pending/in_progress 步骤强制 done（每步补 anchor 或 no_anchor），state→done，settled=True，打 last_completed_by | →done | L2 |
| `abandon` | state→abandoned，superseded=True；若有 `supersede_with` → state=superseded + superseded_by=它；**decision_log 追加** | →abandoned/superseded | L0/L1 |
| `reopen` | 终态（done/abandoned/superseded）→in_progress，清 settled/superseded 标记，revision+1；**decision_log 追加** | →in_progress | L2 |
| `reconcile` | **核实-同步**（§8），逐 step 核 anchor，自动同步 ahead/behind，diverged/unverifiable 抛 clarify；刷新 last_touched_by | 状态可迁移 | 不变 |

### 6.3 `list` 的读面过滤 + 多 profile（owner 简化钉）

**核心简化（owner 钉 2026-10-14）**：砍掉"创建者 profile 专属可见性"，改成
**"启用 filemem 的 profile 即见所有 profile 的未完成 plan"**——磁盘/仓库/测试
是跨 profile 共享现实侧，谁能核实就能推进，不必锁创建者。

- **缺省跨 profile**：不传 `profile` 参数 = 列所有启用 filemem 的 profile 的
  plan 行，每行带 `source_profile` + `last_touched_by` + `last_completed_by`
  三戳，agent/用户一眼看出"coder 落的，data-engineer 接的话推进到第 3 步"
- **`profile` 参数是可选收口**（不是隔离门，是过滤器）：
  - 缺省 = 全量（启用即见）
  - `profile="source:X"` = 只看 source_profile=X 的
  - `profile="touched:X"` = 只看 last_touched_by=X 的
- **settled/superseded 折出**：done（settled）/abandoned/superseded 默认
  `state="in_progress"` 不显示，显式 `state=...` 才查（审计面）

### 6.4 `list` 默认语义

owner 问"有哪些**未完成** plan"，未完成 = `in_progress` **或** `blocked`
（卡住也是一种未完成）：

- `state` 缺省 / `"in_progress"` → 返回 **in_progress + blocked**
  （blocked 行展开 `blocked_reason` + `open_options` 及各自 outcome/reason）
- `state="blocked"` → 只返 blocked
- `state="done"` / `"abandoned"` / `"superseded"` → 显式查终态（审计）
- `state="all"` → 全部（含折出行）

排序：`last_step_updated_at` / `updated_at` 降序（最活跃在前）。

### 6.5 list 输出结构（agent 消费）

```json
{
  "plan_id": "plan_20261014_abc",
  "title": "filemem plan 类型扩展",
  "plan_state": "blocked",
  "progress": "2/4 done, blocked at step 3",
  "blocked_reason": "step 3 依赖的接口无法访问",
  "open_options": [
    {"id": "b1", "text": "改用备用接口", "outcome": "rejected", "reason": "外部服务不可控"},
    {"id": "b2", "text": "本地 mock",     "outcome": "chosen",   "reason": "依赖最少"},
    {"id": "b3", "text": "跳过此步",      "outcome": "pending"}
  ],
  "steps": [
    {"id": 1, "status": "done",  "anchor": {"kind": "test_pass", "value": "tests/test_plan_type.py::test_list"}},
    {"id": 2, "status": "done",  "anchor": {"kind": "git_hash",  "value": "d87aa17"}},
    {"id": 3, "status": "in_progress", "anchor": null},
    {"id": 4, "status": "pending", "anchor": null}
  ],
  "topic": "filemem",
  "source_profile": "coder",
  "last_touched_by": "data-engineer",
  "last_completed_by": null,
  "last_updated": "2026-10-14T22:30:00Z"
}
```

---

## 7. 权限模型（多 profile 全开 + 戳，owner 简化钉）

> **一句话（owner 钉 2026-10-14）**：plan 对"启用 filemem 的所有 profile"
> 开放，读写（含路线变更）全开，唯一约束是跨 profile 操作打戳
> （last_touched_by / last_completed_by + 路线变更 decision_log）；
> **不设"当前管理者"字段，不设"创建者路线锁"**——兜底靠戳的可追溯
> + 现有多 agent 协作模型可随时唤起创建者复核。

**为什么砍掉"创建者专属路线锁"（owner 判断）**：在普通会话 / bot / group
协作下，任何人都能拉起创建者本人更新 plan。既然创建者随时可被唤起，
"只有创建者能改路线"这条预设（创建者缺席才需要锁）就不成立，故砍。

**戳设计（可追溯，非权限门）**：

| 戳 | 记什么 | 谁打 |
|---|---|---|
| `source_profile` | 谁**落**了这份 plan（创建者，不可变） | create 时戳 |
| `last_touched_by` | 谁最近**推进/改**了（continue/revise/select_option 刷新） | 每次推进 |
| `last_completed_by` | 谁**完成**了（complete_all 打） | complete_all |
| `decision_log[]` | 路线变更全留痕（select_option/reopen/abandon，`{ts,by,op,reason}`） | 路线变更 |

- **接活 ≠ 接手**（owner 原话）：`last_touched_by` 只记"step N 被 profile X
  动了"，**不**记"谁拥有这份 plan"。不设"当前管理者"字段，避免"接活"
  语义漂移成"接手"
- **跨 profile 的路线变更**（select_option/reopen/abandon）：任何启用 profile
  都可调（不设创建者锁），但**必打 decision_log**；对路线有疑问 → 直接拉起
  `source_profile` 那个 profile 复核（协作模型自带能力，filemem 不建锁）

**delegate 子 agent（层 1）边界**：
- 子 agent 继承父 model，工具受子 profile 白名单约束，**无写 filemem 权限**
  （`DELEGATE_BLOCKED_TOOLS` 红线）→ 子**不写 plan 行**
- 子对 plan 的唯一合法动作 = **读**（父在 delegate `context` 里传 plan_id，
  子 `filemem_plan(action="list")` 看进度）
- 进度更新**回父**：子干完某步把"可验证句柄"（测试 pass / 文件路径 / commit）
  报父，**父**调 `advance(op="continue", step_done=N, anchor=...)`——
  与 coder profile 既有红线"子结果=自报，父验收后落"一致，anchor 走同一套

---

## 8. reconcile：核实-同步（处理"召回进度与落盘代码失同步"）

**问题**：召回的 plan 行是"记忆里的进度"，落盘代码是"现实进度"，可能失同步：
- **ahead**（plan 落后于代码）：现实里 step 4/5 已做完，plan 行还是 pending
- **behind**（plan 超前于代码）：plan 说 step 3 done，代码里 step 3 没落地
  （上次 agent 虚标 / 代码被 revert / 改在别分支没合）
- **diverged**（路线分叉）：plan 记 step 3 用 b2，代码现实是 a / b4
- **unverifiable**（无锚点）：step 标 no_anchor，不可机验

**召回一份错的进度，比没带更糟**（agent 基于错前提接着做，漂移加倍）。
`reconcile` 是"先核实后延续"的落点。

### 8.1 `advance(op="reconcile")` 原子动作

1. **逐 step 核 anchor**（按 kind）：
   - `test_pass` → 跑该测试，看真绿不绿
   - `git_hash` → `git cat-file -e <hash>`（含在不在当前分支）
   - `file_path` → 读文件看 marker 在不在
   - `no_anchor` → 不可机验，标 `verify=unverifiable`
2. **逐 step 判定**：`synced` / `ahead` / `behind` / `diverged` /
   `unverifiable`
3. **按判定同步**：
   - `ahead` → 补标 done + 补记 anchor + revision+1（自动，确定性）
   - `behind` → 虚标 done 回退 pending + progress_log 记"reconcile 回退" +
     revision+1（自动，确定性）
   - `diverged` → 标该 step 路线失配，**抛 clarify**（"step 3 plan 记 b2 但
     现实是 X，按哪个？"），**不擅自改**
   - `unverifiable` → 抛 clarify（"step N 无锚点不可机验，确认做完了吗？"）
4. **输出 reconcile 报告**：

```json
{
  "plan_id": "...",
  "synced": [1], "ahead": [4, 5], "behind": [3],
  "diverged": [], "unverifiable": [],
  "actions_taken": [
    "step4→done(anchor=test_pass:tests/test_x.py::t)",
    "step5→done(anchor=git:d87aa17)",
    "step3→pending(虚标回退, revision+1)"
  ],
  "needs_user": []
}
```

**只自动改 ahead/behind（机验过、确定性），diverged/unverifiable 一律抛回
clarify 让用户定**——agent 不擅自改路线、不擅自确认无锚点步骤（与 owner
"没有我点头不可以动手"红线一致）。

### 8.2 anchor 失效 ≠ step 没做（关键边界，owner 钉）

anchor 本身会过期（测试重命名 / commit 被 rebase 掉 / 文件移动）：

- `reconcile` 核对 anchor **失败** ≠ "step 没做"，而是"**anchor 失效，需重新
  定位现实**"
- agent 用 git log / 搜索现实现**重新找凭据**：找到 → 更新 anchor（保持
  synced）；找不到 → 升 `unverifiable` 抛人工
- **禁止**"anchor 查不到 → 直接判 step 没做 → 白做一遍"

### 8.3 触发时机（跟 b 面一致，不自动跑）

- **不自动跑**（不污染 bring-back、不烧 LLM、不偷改 plan）
- 触发 = 续做类 op（continue/resume/select_option）的**隐含前置**（§3）：
  第二级 clarify 选"继续"后，agent 续做前先 reconcile
- 用户显式问"这份 plan 进度准不准" → 同样走 reconcile

---

## 9. 进度跟踪

### 9.1 更新触发（纯显式，无自动 regex 抓取）

owner 选 b（读时触发）后，进度更新走工具显式动作，**不做自动抓取**
（跨 session 哪份 plan 是"当前在做"无可靠锚点，误更新比漏更新糟；与 b 的
"用户驱动"哲学一致）。

两个自然时机：
1. **用户驱动**：b 面第二级 clarify 选动作时，agent 调对应 `advance` op
2. **任务驱动**：agent session 内实际干完某步（有可验证句柄：测试 pass /
   文件落盘 / commit）后，**自己**调 `advance(op="continue", step_done=N,
   anchor=...)` 记一笔——低风险写（只翻状态位 + 记 anchor），不需用户点头

### 9.2 进度日志（审计）

plan 行 metadata 内 `progress_log[]`（保留最近 10 条）：

```json
"progress_log": [
  {"ts": "...", "op": "continue", "step": 2, "note": "step2 done (anchor test_pass)"},
  {"ts": "...", "op": "block", "step": 3, "note": "撞墙，摆 b1/b2/b3"},
  {"ts": "...", "op": "select_option", "step": 3, "choice": "b2", "note": "依赖最少"},
  {"ts": "...", "op": "reconcile", "step": 3, "note": "虚标回退 pending"}
]
```

---

## 10. 与其他机制的关系（不动项）

- **`_detect_type` / `_reclassify_type_llm`**：slice-4 反转——`_detect_type`
  **不**再返 `"plan"`（plan regex 行仅存为 probe pre-screen 信号，plan-hit
  路由到 `task`）；`_reclassify_type_llm` verdict 接受集**不含** `"plan"`
  （LLM 回 plan -> None，regex fact verdict stands）。其余类型判定不变。
  详见 §1.4（SLICE-4 反转说明）。
- **EP-1/EP-2 supersede 谱系**：plan 的 `superseded`（方向变更）**复用**
  现有 `superseded_by` 字段语义，但**不走** EP-1 的 Jaccard 0.5 自动候选
  （plan 的 supersede 是显式 `advance(op="abandon", supersede_with=...)`）
- **settled 机制（kanban done-signal）**：plan 的 done **不**走
  `_SETTLED_DONE_SIGNALS` / `_SETTLED_SCAN_SHARDS`（plan 有自己的
  `complete_all`）
- **auto-inject 面**：`_auto_inject_eligible` / `_o1_fold_out_of_auto` 对
  **plan 类型恒返回 True（恒折出）**——plan 永不自动注入（§3）；
  搜索面（filemem_search type="plan"）仍可显式查
- **O1 1D**：auto-inject 面的 O1 1D 隔离**不继承**到 plan 面（plan 跨 profile
  全开，§7）
- **I1 无 DELETE**：所有 plan 行（含 abandoned/superseded/done）**永不物理删**，
  只做 level 迁移 + 折出标记；L0 行 30 天 TTL 是唯一"消失"路径，
  清理前 14 天 warning
- **bring-back 4 选项面**：完全不掺 plan（§3）
- **host tree**：零改动（红线）；plan 类型 + 工具全在 plugin 内
- **probe + clarify 面（§2 slice-4）**：plan shard 的**唯一写入门**是
  probe 命中 -> [记忆报告] clarify -> 用户点头 -> `filemem_plan(action="create")`。
  分类面（§1.4）与 regex 面（§1.3 slice-4 后）均**不**直接写 plan shard。
  原 `_PLAN_COMMIT_SIGNALS` 常量（§2 旧设计）已被 probe 面取代，不再存在。

---

## 11. 实现改动清单（本地源码 only）

| 文件 | 改动 |
|---|---|
| `__init__.py` L115 | `MEMORY_TYPES` 加 `"plan"` |
| `__init__.py` `_MEMORY_TYPE_REGEX` | 加 `"plan"` 条目（§1.3，slice-4 后仅作 probe pre-screen 信号） |
| `__init__.py` `_detect_type` | **slice-4**：不再 verdict `"plan"`（plan regex 行改路由到 `task`） |
| `__init__.py` `_reclassify_type_llm` | **slice-4**：verdict 接受集**排除** `"plan"`（load-bearing 层） |
| `__init__.py` 新 | `_PLAN_LAND_SIGNALS`（§2 slice-5 F4：spec 落盘结构词表，pre-screen layer A） |
| `__init__.py` 新 | `_plan_probe` + `_plan_probe_push`（§2 slice-4 的 probe + clarify 面；slice-5 F4 改双层 pre-screen） |
| `__init__.py` `_save_memory` | **slice-4**：auto task/decision 行出口挂 probe（挂点 A，post-lock，与 distill 同段） |
| `__init__.py` `sync_turn` | **slice-5 F4**：轮末 assistant 文本挂 probe（挂点 B，turn-close B4 fallback 之后，活路） |
| `__init__.py` `filemem_review_drafts` | **slice-4**：`action="plan_probe"` 消费分支（save -> `_plan_create`，dismiss -> 不写） |
| `__init__.py` 新 | `filemem_plan` 工具（list/create/advance + 9 op，§6） |
| `__init__.py` 新 | plan 行 upsert 语义（同 plan_id 合并）+ anchor 强制校验 |
| `__init__.py` 新 | level 迁移函数（advance 各 op 内原子完成，§5） |
| `__init__.py` 新 | reconcile 五态判定 + anchor 失效重定位（§8） |
| `__init__.py` 新 | auto-inject 收行谓词对 plan 类型恒折（§10） |
| `__init__.py` | `filemem_search`/`filemem_write` tool schema 的 type enum 加 `"plan"`（只读兼容） |
| `__init__.py` | 跨 profile 戳（last_touched_by / last_completed_by / decision_log，§7） |
| `consolidation/llm_caller.py` | `TYPE_CLASSIFY_SCHEMA` enum **排除** `"plan"`（slice-4）；`TYPE_CLASSIFY_INSTRUCTIONS` 加 never-return-plan 条款（slice-4）；`classify_type` helper 有效集排除 plan |
| `consolidation/llm_caller.py` 新 | **slice-4**：`PLAN_PROBE_SCHEMA` + `PLAN_PROBE_INSTRUCTIONS` + `probe_plan` helper（probe LLM 面） |
| 新测试 | `tests/test_plan_type.py`（TDD 先红后绿）+ `tests/test_plan_type_slice4.py`（slice-4 F1/F2/F3 面） |

### TDD 测试面（先写红测试）

1. **regex 命中（slice-4 反转）**：plan 强信号 → `_detect_type` 路由到
   `task`（**不**再返 `"plan"`）；纯 task 信号（"记得跑测试"）仍 `"task"`；
   纯 fact 仍 `"fact"`（不回归）
2. **LLM 面（slice-4 反转）**：`classify_type` **不**能返 `"plan"`（enum +
   instructions + helper 有效集 + 调用侧白名单四处全排除）
3. **probe + clarify 写门（§2 slice-4）**：auto task/decision 行带 plan
   signal → post-lock 跑 `_plan_probe`；LLM 命中 → PENDING marker ->
   `_push_pending_confirm(kind="plan_probe")`；用户 verdict="save" ->
   `filemem_plan(action="create")` 落 plan.json；verdict="dismiss" -> 不写；
   over-budget / no-runtime / miss -> None（task/decision 行照常落盘，不阻塞）
4. **list 过滤 + 多 profile**：
   - 缺省跨 profile，返回 in_progress + blocked（不含 done/abandoned/superseded）
   - `state="blocked"` 只返 blocked；`state="done"` 显式查终态
   - `profile="source:X"` / `"touched:X"` 过滤
   - 每行带 source_profile + last_touched_by + last_completed_by 三戳
   - blocked 行带 blocked_reason + open_options（含各 option outcome/reason）
5. **advance 各 op 原子性**（同一把分片锁内）：
   - continue：step→done + **强制 anchor**（无 anchor 且未标 no_anchor 的
     done 被拒 / 降级 no_anchor）+ 刷新 last_touched_by
   - block：state→blocked + 存 options（outcome=pending）
   - select_option：step 方案替换 + 未选标 rejected + revision+1 +
     state→in_progress + **decision_log 追加**
   - complete_all：全步骤强制 done（补 anchor 或 no_anchor）+ settled +
     L2 + 打 last_completed_by
   - abandon（无 supersede_with）：superseded=True + L0 + decision_log
   - abandon（有 supersede_with）：state→superseded + superseded_by + L1 +
     decision_log
   - reopen：终态→in_progress + 清标记 + L2 + decision_log
   - **跨 profile 推进**：data-engineer profile 调 continue coder 的 plan →
     成功 + last_touched_by=data-engineer（不夺 source_profile）
6. **reconcile 五态**：
   - ahead（现实超前）→ 自动补 done + anchor
   - behind（虚标）→ 自动回退 pending
   - diverged（路线分叉）→ 抛 clarify（needs_user），不擅改
   - unverifiable（no_anchor）→ 抛 clarify（needs_user）
   - **anchor 失效**（git hash 被 rebase / 测试重命名）→ 重新定位现实，
     **不**直接判"没做"
7. **level 迁移表**（§5）：各 op 后行 level 正确
8. **upsert**：同 plan_id 二次 write 不新开行，合并 metadata
9. **progress_log**：保留最近 10 条
10. **I1 无 DELETE**：abandoned/superseded/done 行都在盘上（只折出，不删）
11. **auto-inject 恒折**：`_auto_inject_eligible(plan_row, ...)` 恒 True
    （plan 行永不进 auto-inject），但 `filemem_search(type="plan")` 仍查得到

---

## 12. 验收判据

1. 一份 4 步 plan 经 probe+clarify 确认链（§2 slice-4：LLM 探测 → [记忆报告] pending 条目 → 用户 verdict="save" → `filemem_plan(action="create")`）存入 `plan.json`（L2，每 done 步带 anchor）
2. 新 session，用户问"有哪些未完成 plan" → `filemem_plan(action="list")`
   列出（in_progress/blocked，带三戳 + options），agent 不翻原始 json
3. 用户选"继续" → 第二级 clarify → agent 先 `reconcile`（核实进度）→
   干完 step 3 → 调 `advance(op="continue", step_done=3, anchor=...)`
4. agent 做 step 4 撞墙 → `advance(op="block", step=4, options=[b1,b2,b3])`
   → plan 变 blocked
5. 用户选 b2 → `advance(op="select_option", step=4, choice="b2")`
   → step 4 方案 a→b2，b1/b3 标 rejected（step 内 audit），revision+1，
   state 回 in_progress，decision_log 记 "coder select_option b2"
6. 另一 profile（data-engineer）接手推进 → `continue` 成功 +
   last_touched_by=data-engineer（source_profile 仍 coder）
7. 用户选"标记已完成" → `advance(op="complete_all")`
   → 全步骤强制 done（补 anchor），settled，L2，折出 auto-inject（行留盘）
8. 全程零 host 改动、零 CRON、plan 行无物理删除、plan 永不进 auto-inject
