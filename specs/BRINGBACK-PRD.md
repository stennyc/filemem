# PRD · filemem 跨 session「带回账本」（方案 2 + clarify user choose）

Owner: 2026-10-10 拍板方向；本 PRD 为实现阶段契约。
范围：**全部落在 `~/.hermes/plugins/filemem/`，零 Hermes 原生改动（红线）**。
前置依赖：SESSION_ISOLATION_V2（A-E 已落地）——本 PRD 叠在其 confirmed 门之上，不改其骨架。

## Subject

新 session 开局，用户可**主动选择**"要不要把上一个关闭 session 的 confirmed 摘要带回本次 session 的自动注入面"。连续性由 user per-session 当场拍板，不做全局定档；"模型需要更多"永远走检索面（M4 全捞），不受本开关影响。

## Introduction

V2 现状（A-E）下，跨 session 延续只有两条路：
- **检索面**（`filemem_search`）：M4 全池跨 session 捞，**按需、显式**——模型要搜才拿。
- **confirmed 晋升**：旧 session 的摘要被确认成 confirmed 全局行后才进 auto 面。

缺口：**"自动带入"** 这层不存在。新 session 的 auto 面只有 confirmed 全局骨架，
"上一个 session 刚确认过的事"不会自动带过来（除非已晋升全局）。用户每次接续长任务
要么复述、要么让 agent 手动 search——连续性靠"主动"，体验是金鱼的。

本 PRD 补上"带回"这层：新 session 开局，clarify 问一次 user **per-session 三选一**
（`带回 / 干净开局 / 全新`），选项决定本 session 的 auto 面行为；**带回 ≠ 拿不到——
检索面 M4 永远全捞，clarify 只决定"要不要自动灌 / 灌多少"，不决定"能不能搜到"。**

### 2026-10-10 实现定论（reviewer B2-A + owner 拍板后，取代下文旧"第三腿收行"方案）

最初设计是"带回腿 = 第三条收行谓词（`session_id==上个session AND confirmed`）"。
第 1 轮 reviewer 实证指出该腿 **0 边际死代码**：上一个 session 的 confirmed 行
**本来就被 V2 的 confirmed-global 腿无条件下收**（L526 的 confirmed 腿不看 session_id），
所以第三腿对它再收一次是冗余；对它未确认行，第三腿又叠了 confirmed（刻意不收）——
全场景 0 新增行。owner 拍板 **B2-A 路线**：

- **删带回腿**（第三收行谓词不存在了）。
- **"带回"重新定义为纯排序**：选中 `bring_back` 时，把"上一个 session 的 confirmed
  行"在 auto 面结果里**置顶**（`_bringback_reorder_key`，0/1 排序键）——纯重排、
  **不改集合**（confirmed 腿已收的行只被排前，不新增不删除）。
- **新增 `fresh`（全新）选项**：本 session auto 面**全空**（连 confirmed 全局骨架都
  折出），检索面不动——"全新 ≠ 失联"。
- **删冗余选项**：旧"只带已确认全局"与"干净开局"语义重复，合并成 3 选项。

**B1 修复（实现 bug）**：P3 clarify footer 原本被冻进 `last_injected.txt` 快照，冷启动
回放会把"要不要带回"问句反复重放。修复 = 快照写在 footer 追加**之前**（footer 只走
返回面，不进冻结块）。`test_b1_footer_not_frozen_into_p3_snapshot` 哨兵钉死。


## Problem Statement

1. **连续性不足**：V2 把"旧 session 未确认行"折出 auto 面（治串），副作用是"旧 session
   已确认的行"也不自动进新 session——接续长任务断档。
2. **定档 vs 选择权**：把"带几个 session / 带不带"做成全局定档（我最初给的"三档"），
   等于把 user 的判断前置固化；owner 拍板改成 **per-session 的 clarify 选择**。
3. **带回 ≠ 失联**：必须向用户厘清——选"不带回"后模型仍可 `filemem_search` 拿任意旧
   session（M4 全捞），clarify 开关只控制"自动灌"这一下。

## Goals and Objectives

1. **G1** 新 session 开局注入一个 clarify footer："要不要把上一个 session 带回本次
   auto 面？"选项 = `带回 / 干净开局 / 全新`（3 值，冗余的"只带已确认全局"已合并进
   干净开局）。
2. **G2** 选"带回"（`bring_back`）：auto 面结果里把**上一个关闭 session 的 confirmed
   行置顶**（纯排序，`_bringback_reorder_key`；不改集合——confirmed 腿本就收了它们，
   带回只让它们排前 = 连续性上下文优先）。
3. **G3** 选"干净开局"（`clean_start`）：维持 V2 现状（confirmed 全局骨架 + 当前
   session 行；不置顶、不折出；检索面不受影响）。
4. **G4** 选"全新"（`fresh`）：本 session auto 面**全空**（连 confirmed 全局骨架都折
   出），检索面 M4 不动——"全新 ≠ 失联"。
5. **G5** 检索面 M4 **全捞不变**——带回/fresh 只控制 auto 面，不改变检索面语义。
6. **G6** 带回**只置顶 1 个 session（上一个关闭的）**，不做"带 N 个"——要更多走 G5
   检索面。

## User Stories

- 作为 **owner**，我开新 session 接续昨天的 filemem 任务，系统问我"要不要带回刚才确认
  过的结论"，我点"带回"，agent 直接接着干，不用复述。
- 作为 **owner**，我开一个全新话题的 session，系统问"带回上一个吗"，我点"干净开局"，
  agent 不带旧账本，上下文干净。
- 作为 **owner**，我选了"干净开局"但中途发现 agent 需要上一个 session 的某条结论，
  我让 agent `filemem_search` 那条结论——**能搜到**（M4 全捞），带回腿没影响它。
- 作为 **agent**，我在 session 开局收到 clarify footer，按 user 选择决定是否把上个
  session 的 confirmed 摘要纳入本次自动注入块。

## Technical Requirements

### TR1 · "上一个关闭 session" 戳（新增）
- `on_session_end`（L2365）落账本时，记录 `last_closed_session`（per-store json，含
  `session_id` + `closed_ts` + `source_profile`）。**零 cron/timer**（红线），纯 session
  事件驱动。
- 带回腿读这个戳拿到"上一个 session id"。无该戳（首次运行）→ 带回腿无腿可带，
  fail-soft 退回 G3（纯 B）。

### TR2 · 带回排序键 + fresh 门（取代旧"第三腿收行谓词"，B2-A 定论）
`_auto_inject_eligible`（L497）**保持 V2 两腿不变**（`sid == cur_sid OR confirmed`），
**不加第三腿**（实证 0 边际死代码，见上文实现定论）。auto 面装配（L3236-3266）在
两腿之后叠两层：
```
# fresh 门（本 session 选 fresh）：auto 面全空，含 confirmed 骨架折出；检索面不动
if _get_bringback_choice(sid) == "fresh" and results:
    results = []
# ... confirmed 腿（_auto_inject_eligible 两腿）照旧 ...
# 带回置顶（本 session 选 bring_back + 有 pin）：纯排序，0/1 键，集合不变
if _get_bringback_choice(sid) == "bring_back" and pin_sid and len(results) > 1:
    results = sorted(results, key=lambda r: _bringback_reorder_key(r, pin_sid))
```
`_bringback_reorder_key(row, pin_sid)`：`row.session_id==pin_sid AND confirmed` 返 0
（置顶），否则返 1——**纯重排，不新增不删除行**。profile 门（P0-2）+ O1 折出 +
confirmed 门（V2）不变。置顶只对 **episodic 池行**生效（memories 池行无 session_id，
天然只走 confirmed 全局腿，不参与置顶）。

### TR3 · clarify footer（复用 `_pending_draft_notice` 注入机制）
- 挂在新 session **第一个非 trivial 的 `pre_llm_call`**（L3083+ 的 P3/普通 prefetch 路径
  之前）。provider 不能同步调 clarify（engine drop 返回）——footer 注入文本 + 提示模型用
  `clarify` 工具解，复用现有 `o1_scope_clarify` / `filemem_review_drafts` 的 clarify 套路。
- 一次性门控：per `(session_id, "bringback")` 只问一次（仿 `_pending_drafts_notified`
  的 session-keyed one-shot，L3738）。
- footer 三选项措辞（owner 拍板用词）：
  1. **带回**（接着刚才干，带回上一个 session 确认过的摘要）
  2. **干净开局**（不接着刚才，只留全局确认骨架）
  3. **只带已确认全局**（= 干净开局，明示不回带旧 episodic）
  （选项 2/3 语义同，分开是给用户一个"明白不回带旧 session"的确认项；实现上二者都
  落到 G3。）

### TR4 · "带回"状态的消费
- 模型 clarify 选"带回"后，状态落 `bringback_{session_id}.json`（`chosen=true`，
  仿 `_pending_confirm` 内存 + 落盘双轨）。**当次 session** 的后续 prefetch 装配
  auto 面时，带回腿激活（TR2 第三腿打开）。
- 选"干净开局/只带已确认"→ 不带回腿，行为 = 纯 B。
- **只影响当次 session**；下个 session 重新问（per-session 选择，不跨 session 记忆选择）。

### TR5 · 检索面不动
`search_memories` 的 M4 全捞（L6932）一行不改。带回腿**只在 auto 面**；`filemem_search`
（工具面）的"全捞任意旧 session"语义与本 PRD 完全解耦。

### TR6 · 测试
新增 `tests/test_bringback_clarify.py`：
- 谓词三面：带回腿 on（选中 + last_closed 存在）→ 上 session confirmed 行收、上 session
  未确认行折出（叠 confirmed 守脏）；带回腿 off → 全折出（= 纯 B）。
- `last_closed_session` 戳：on_session_end 落、带回腿读、无戳 fail-soft 退纯 B。
- clarify footer：首个 pre_llm_call 注入一次、session-keyed 不重问；三选项落到
  G2/G3/G3。
- **回归隔离**：带回腿必须不污染现有 `_auto_inject_eligible` 两腿语义——
  `test_auto_inject_confirmed_gate.py` 全绿（带回腿默认关）。
- 检索面不动证明：带回腿 on/off 时 `search_memories` M4 全捞结果不变（哨兵测试）。

## Benefits

1. **连续性可挑**：user 每次 session 开局自主选"接着干 / 干净开 / 只全局"，不是被定档。
2. **不串脏**：带回腿叠 confirmed，带的是"确认过的精华"，未确认脏行照旧折出。
3. **cost 可见**：带回 = 多带 1 个 session 的 confirmed 摘要（有限、可控、user 知情）。
4. **失联兜底**：带回 ≠ 拿不到，检索面 M4 永远全捞，user 选"干净开局"也不锁死旧记忆。
5. **零回归**：V2 两腿 + 检索面语义不动，带回腿是纯叠加、默认关。

## KPIs

- 功能：3 个 clarify 选项各自落到 G2/G3/G3 的注入块差异（单测断言带回腿 on/off 的行集）。
- 串行率：带回腿 on 时，注入块里**未确认行 = 0**（带回腿叠 confirmed 的回归哨兵）。
- 失联率：带回腿 off 时，`filemem_search` 仍能命中旧 session 行（M4 哨兵测试）。
- token delta：带回 vs 不带的注入块字符数差（日志可观测，预期 = 1 个 session 的
  confirmed 摘要量，有界）。
- 回归：`test_auto_inject_confirmed_gate.py` + `test_session_isolation_fix.py` 全绿
  （带回腿默认关，零存量回归）。

## Development Risks

1. **带回腿与 confirmed 门交叉**：带回腿叠 confirmed 后，"上个 session 没确认的行"
   仍被折出——这是**有意**（不串脏），但需单测钉死，避免有人误以为"带回=带全部"。
2. **clarify 走注入 footer，非同步**：模型若不在当 turn 解 clarify，带回腿保持默认关
   （fail-soft 退纯 B）——这是安全侧，但 user 可能"忘了点"导致没带回。**可接受**：
   忘点 = 选"干净开局"，检索面仍兜底。
3. **`last_closed_session` 戳的并发/陈旧**：多 profile 共享 store，戳要 per-profile
   （带 `source_profile`）防跨 profile 带回；closed_ts 防"很久前的 session 被带回"
   ——**暂不加时间窗**（owner 拍"带回=上一个"），若后续要"最近 N 天内"再叠 ts 谓词。
4. **scope 蔓延**：本 PRD 只动 auto 面带回腿 + clarify footer + 戳；**不动**检索面、
   不动 confirmed 晋升链、不动 sweep。实现时严守"叠加不替换"。
5. **原生红线**：clarify footer 走 provider 注入（plugin 侧），绝不改 Hermes 原生
   clarify/gateway 行为。

## Conclusion

把"带账本上岗"从**架构定档**降级为 **user 的 per-session 选择**（方案 2 + clarify），
auto 面叠一条**叠 confirmed 的带回腿**（带回 = 上一个 session 的精华，不串脏），
检索面 M4 永远全捞兜底（带回 ≠ 失联）。改动全在 plugin 侧，V2 主线不破，默认关零回归。

---

## 待 owner 点头的开工项（未点头我不动 `__init__.py`）

| 阶段 | 内容 | 红线 |
|---|---|---|
| P1 | `last_closed_session` 戳（on_session_end 落，per-profile） | 零 cron/timer |
| P2 | `_auto_inject_eligible` 第三腿（带回腿，叠 confirmed，默认关） | 不动现有两腿 |
| P3 | clarify footer（session-keyed one-shot，复用 `_pending_draft_notice` 套路） | 引擎 drop 返回，注入不 raise |
| P4 | 选态落盘（`bringback_{sid}.json`，仅当次 session） | 不跨 session 记忆选择 |
| P5 | `tests/test_bringback_clarify.py`（含回归哨兵 + 检索面不动证明） | 存量测试零回归 |
| P6 | 本地跑全绿 + reviewer 子进程复盘 | 推远端(stennyc/filemem)等你批量点头 |

**开工信号**：你说"开工 P1-P5"或指定阶段，我才动代码；P6 的推远端始终等你批量点头。
当前 PRD 已落 `specs/BRINGBACK-PRD.md`，**未动任何实现**。
