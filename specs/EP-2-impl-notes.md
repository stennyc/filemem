# EP-2 实现蓝图（supersede 候选确认，宿主主模型面）— orchestrator 裁决 2026-10-01

> 用户需求（原话约束）：把 `supersede_cands`（EP-1 确定性候选）→ **人工/LLM 确认为 `supersedes`**；
> **零硬编码模型名 + 零自定义 API/provider 配置块 + 全程共享宿主主模型**。

## 硬约束（拍板，验收逐条查）
- LLM 面 = `consolidation/llm_caller.py` 现成的 `ctx.llm.complete_structured(...)` **无 task key**（宿主主模型，同 distill/salience/relevance）。**不得**新增 model/provider 配置键、不得硬编码任何模型名字符串、不得 import 任何第三方 SDK。
- 新增 LLM 函数体 **纯宿主面**：import 审计仅 stdlib + `ctx.llm`，零 `openai`/`anthropic`/`requests`。
- 用户 "人工" 路径 = 一个**确定性 CLI 工具**（零 LLM，人拍板翻 state）；"LLM" 路径 = 宿主模型批量确认。两条都落 `supersede_state: candidate → confirmed`。

## 数据事实（EP-1 已落盘，EP-2 只翻 state 不重判候选）
- 新行：`metadata.supersedes=[old_ids]`, `metadata.supersede_state="candidate"`, `metadata.superseded=False`
- 被封顶旧行：`metadata.superseded=True`, `metadata.superseded_by=<new_id>`（EP-1 已让 superseded 行出 RRF 主池，候选/确认态都出池——EP-2 确认是"信任升级"，非池成员变化）
- 确认的可观测效果（EP-2 交付）：`supersede_state` 翻 `confirmed` + 旧行打 `superseded_confirmed=True`（可选标记）+ `consolidation_log.jsonl` append-only 审计 + 检索面 origin_mark 可识别（低信任候选保留可见）。

## 实现步骤（@coder 照做，顶向下，每步先跑 pytest 再进下一步）

### 1. llm_caller.py 加 confirm 作业（镜像 distill，文件尾 L133+）
```python
CONFIRM_SCHEMA = {"type":"object","properties":{
    "confirmed":{"type":"boolean"},
    "reason":{"type":"string"}}, "required":["confirmed"]}
CONFIRM_INSTRUCTIONS = """You are a memory curator. Two memory items are in the same
category. The NEWER item is a candidate to supersede (replace) the OLDER item only if
the newer genuinely obsoletes the older (same fact updated / corrected / refined).
If they merely overlap or are unrelated, do NOT confirm.
Return JSON: {"confirmed": boolean, "reason": short str}"""

def confirm_supersede(ctx, new_text: str, old_text: str) -> Dict[str, Any]:
    """EP-2 LLM confirmation: does NEWER genuinely supersede OLDER? Host main-model
    path (ctx.llm, no task key), temperature 0.0, fail-soft: ANY failure ->
    confirmed=False (never auto-trust on failure; candidate stays unconfirmed)."""
    try:
        result = ctx.llm.complete_structured(
            instructions=CONFIRM_INSTRUCTIONS,
            input=[{"type":"text","text":f"OLDER:\n{old_text}\n\nNEWER:\n{new_text}"}],
            json_schema=CONFIRM_SCHEMA, temperature=0.0, max_tokens=160)
        parsed = result.parsed or {}
        return {"confirmed": bool(parsed.get("confirmed", False)),
                "reason": (parsed.get("reason") or "")[:200], "model_used": True}
    except Exception as exc:
        return {"confirmed": False, "reason": f"model_unavailable_fallback={exc}",
                "model_used": False}
```

### 2. __init__.py 加确认作业 + 候选扫描
- 新方法 `_supersede_confirm_candidates(self)`（放在 `_llm_consolidate` 附近，~L4000）：
  1. `if self._llm_ctx is None: return`（standalone 无宿主 LLM → skip 非 error）
  2. 幂等/并发 guard：`_supersede_confirming` 标志
  3. 扫候选：遍历 memories 各 shard，收集 `metadata.supersede_state=="candidate"` 的行；对每行取 `metadata.supersedes[0]` 对应旧行文本（同 shard 内按 id 查，查不到旧行 = 坏引用 fail-soft 跳过留痕不炸）
  4. 预算 N-cap：`consolidation.supersede_max_confirms`（默认 5，与 `max_llm_per_tick` 并列；overflow 滚下一 tick）
  5. `llm = _load_llm_caller()`；逐对 `llm.confirm_supersede(self._llm_ctx, new_text, old_text)`
  6. `confirmed==True` → 该新行 `metadata.supersede_state="confirmed"`；其每个 supersedes 目标旧行 `metadata.superseded_confirmed=True`（都在 `self._shard_lock` 下写同一 shard 文件）；`confirmed==False` 保持 candidate 不动
  7. 每条写 `consolidation_log.jsonl`：`{"action":"supersede_confirm","new_id":...,"target_id":...,"confirmed":bool,"model_used":bool,"reason":...,"ts":...}`（append-only）
  8. fail-soft：单条异常不影响批次；整 tick 异常 → 候选保持 candidate，下边界重试（同 rescore）
- 挂到巩固边界：`_spawn_llm_consolidation`（或 on_session_end 处）在 rescore 之后追加调用 `self._supersede_confirm_candidates()`（同异步线程，宿主 turn 零阻塞）

### 3. 配置默认键（decay.json 顶级 consolidation + DECAY_DEFAULTS 同步）
- `consolidation.supersede_max_confirms: 5`（两文件逐键一致；与现有 `max_llm_per_tick` 并列，不交叠）

### 4. 人工确认 CLI（零 LLM）`tools/confirm_supersede.py`
- 零硬编码路径（`os.environ.get("HERMES_HOME", Path.home()/".hermes")` → 活数据 filemem，同 sibling 工具）
- `--new <id> --confirm` / `--new <id> --reject`：确定性翻 `supersede_state`（confirm→confirmed + 旧行 superseded_confirmed=True；reject→supersede_state="rejected"），`_shard_lock` 下写，append-only 审计 `{"action":"supersede_confirm_human","new_id":...,"verdict":"confirmed|rejected","ts":...}`
- `--list`：列出所有 `supersede_state=="candidate"` 的待确认项（只读）
- 全程零 LLM import

### 5. 测试 `tests/test_ep_2_confirm.py`（合成 fixture，不依赖活数据/provider 实例，≥10 用例）
1. 合成：新行 candidate + 旧行 → `confirm_supersede` mock 返回 confirmed=True → 新行 state=confirmed + 旧行 superseded_confirmed=True
2. mock confirmed=False → 新行保持 candidate（不吞脏，候选不变）
3. mock 抛异常（模型不可用）→ confirmed=False 且 model_used=False → 候选保持 candidate（fail-soft 不自动信任）
4. 坏引用（supersedes 指向不存在的旧 id）→ 跳过留痕不炸，批继续
5. 预算 cap：`supersede_max_confirms=2` + 3 条候选 → 只确认 2 条，剩 1 条滚下一 tick
6. 幂等：已是 confirmed 的行不再进候选集（不重复确认）
7. `--list` 只读不写盘（合成 fixture 跑前后文件 hash 一致）
8. `--new X --confirm` / `--reject` 确定性翻 state + 审计行追加
9. 锁：确认写入走 `_shard_lock`（并发两进程不撕裂）
10. 零硬编码模型审计：`llm_caller.py` confirm 段 grep 模型名/provider/openai/anthropic/requests = 0；仅 `ctx.llm.complete_structured`
11. schema round-trip：confirmed/rejected/candidate 三态 to_dict/from_dict 无损

### 6. 收尾 grep 验收（不认 status）
- `python -m pytest tests/ -q` 全绿（175 baseline + 新增）
- `python -m pytest tests/test_ep_2_confirm.py -q` 全绿
- `grep -n confirm_supersede consolidation/llm_caller.py` 有 def
- `grep -n _supersede_confirm_candidates __init__.py` 有 def + 被巩固边界调用
- `grep -c supersede_max_confirms decay.json` 与 `DECAY_DEFAULTS` 均 >0 且一致
- `python3 tools/confirm_supersede.py --list` 只读可跑
- 硬编码审计：`grep -nE 'model\s*[:=]\s*["a-z]|openai|anthropic|httpx|requests' llm_caller.py` 新增段 = 0

## 边界（不碰）
- EP-1 候选 stamp（`_stamp_lineage_candidates`）不动——EP-2 只消费其产物翻 state
- SEC-1-2 注入门 / SA2 检索 / distill/salience 现有作业不动
- 不做 EP-2 spec 里另一个"observation 层 knowledge/entries.json"（那是独立后续，本次只做用户点名的 supersede 候选确认，YAGNI）
