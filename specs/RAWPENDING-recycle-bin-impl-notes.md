# raw_pending 回收站人工面（Path B）— 实现蓝图（orchestrator 裁决，2026-10-02）

> 只扩 `tools/cleanup_raw_pending.py`，**不碰 `__init__.py`**、零 LLM、纯 stdlib。
> 任务小（~40 行工具 + 测试）。蓝图已钉死，@coder 照做不留规划空间。

## 背景（三流模型，orchestrator 已裁决）
- ① 可恢复流（自家 clean 水）→ 走 Hindsight 式 auto-consolidate（已落盘的 observe，读 memories/episodic 干净源，**不是 raw_pending**）
- ② 不可信流（tool dump / 群聊灌水 / 外部引用）→ **永不自动蒸馏**（prompt-injection 载体），只进回收站，**人工审**
- ③ 产物 observation
- 活数据 64 行 = 55 queue_overflow + 9 catch_filter，memory_ids 全空。取证结论：49/55 是垃圾/半成品、全过 30 天时效、SEC-1 门 + `low_trust_auto_inject=false` 下自动蒸馏会 drop（空转）→ **不建自动蒸馏通道，Path A deferred**。
- C slice `_auto_archive_raw_pending` 已把池封顶（>30 天/>30 条 → `knowledge/archive/` append-only JSONL，保留 origin/reason）。**本工具补齐"回收站人工"那条腿**：让 ② 能被人工列出 + 捞回池。

## 两个归档源（--list / --promote 都要覆盖）
1. 池：`knowledge/raw_pending.json`（活，当前 64 行；C tick 未跑）
2. 手动 clean 归档：`quarantine/raw_pending_queue_overflow_*.jsonl`（既有 clean() 产物，活数据已有 1 个文件）
3. 自动 C 归档：`knowledge/archive/raw_pending_auto_*.jsonl`（C slice 产物，尚未生成）

**只认这三类 raw_pending 相关路径。`quarantine/` 下还有 decision/dropped/fact/preference/task 等其它目录——一律不碰、不 glob。**

## 改动面（全在 tools/cleanup_raw_pending.py）

### 1. `--list`（人工审面，无状态、无写）
- 枚举上表 3 个源，输出一张表，每行：
  `#<全局行号>  [pool|quarantine|auto-archive]  origin=<origin>  reason=<reason[:40]>  created_ts  <content 前 80 字>`
- 全局行号：三源合起来从 1 连续编号（用于 --promote 定位）。源内按 created_ts 升序；源间固定顺序 pool → quarantine → auto-archive。
- 纯读，不写任何文件。JSON 损坏/缺失 → 跳过该源并告警到 stderr，不算 fatal。
- 无 `--list` 且无 `--promote` → 维持现有默认（跑 clean 或 restore，按现有行为）。

### 2. `--promote <row>`（人工捞回，最小）
- `<row>` = `--list` 的全局行号。
- 目标源 = 该号对应的源（pool/quarantine/auto-archive）。
- **语义**：把该行**捞回池 `knowledge/raw_pending.json`**，并把该行 `origin` 改为 `manual_promote`（人工关注标记），**写一条审计行**到 `knowledge/archive/manual_promote_<ts>.jsonl`（append，记录 `row, source, origin, created_ts, promoted_ts`）。
  - 若源本就是 pool（行已在池里）→ 只做 origin 标记 + 审计，不重复插入。
  - 若源是 quarantine/auto-archive → 取出行、写回池（池内已有同 content+topic 的幂等去重：已存在则不重复插，仅标记）。
- **不改其它行、不写 memories**（写 memories 是宿主 `_save_memory(source='manual')` 的职责，本工具不越界）。
- 行号不存在 / 越界 → 退出码 2 + stderr 明确错误（含有效行号范围），**不动任何文件**。
- 锁协议：与既有 clean/restore 完全一致 —— 开 `raw_pending.json.lock`（`a+`）+ `fcntl.flock(LOCK_EX)`，RMW 池在该锁内（innermost 方向，与插件 C slice 的 `_shard_lock` 同文件同向，天然不反转）。flock 仅 Linux 有；非 POSIX 降级为无锁（与现有工具一致，文档注明）。
- **--dry-run 生效于 --promote**：预览"将捞回哪条 + 落哪个池"，不落盘。

### 3. origin 标记
- 新 origin 值 `manual_promote`。它只进**池**，不进 clean 面；后续若要真写 memories，走宿主 manual 路径（user/high 信任戳，SEC-1-1 映射已支持 `manual`）。工具本身不产信任戳。

### 4. prune 层（归档保留 90d → manifest 永久；用户"回收站不能一直蓄"）
**这是插件面（`__init__.py` + 配置），不是工具面。** 用户裁决：脏水归档不能无界蓄积，要按时间 FIFO 清最旧的。C slice 已有"池 30d/30 条出池"（已落盘），本层补"归档保留 90d 后 prune，删前留 manifest 骨架"。

完整生命周期（三层）：
```
池 raw_pending.json (30d OR 30条, 已做 C slice)
  → 归档 knowledge/archive/raw_pending_auto_*.jsonl (保留 90d, 新增 prune)
    → manifest knowledge/archive/pruned_index.jsonl (永久, ~100B/行, 新增)
```

【新方法】`_auto_prune_raw_pending_archive(self, decay=None) -> int`（纯确定性、零 LLM，镜像 `_auto_archive_raw_pending` 风格），返回本次 prune 的归档文件数（0 = 无可 prune / 配置缺 / 关闭）。

【配置键】`knowledge.archive_prune_days = 90`（`0` 或 knowledge 块缺 = 关闭，fail-soft 不删）。加进**两处并逐键一致**（同 C slice 规矩）：
- `decay.json` 顶级 `knowledge` 块（活数据 source of truth）
- `consolidation/__init__.py` `DECAY_DEFAULTS["knowledge"]` 块
（两处的 knowledge 块都从 {stale_min_overlap, observe_max_per_tick, raw_pending_ttl_days, raw_pending_max_rows} 加 `archive_prune_days: 90`。）

【FIFO 语义（钉死）】文件级 prune：`glob("knowledge/archive/raw_pending_auto_*.jsonl")`，按**文件名时间戳**升序（`raw_pending_auto_%Y%m%dT%H%M%SZ.jsonl` 解析出 ts）。对每个 ts < `now - archive_prune_days` 的文件：
1. 先逐行读它，对每行 append 一条 manifest 到 `knowledge/archive/pruned_index.jsonl`（open mode="a" append-only，镜像 C slice），字段固定：
   `{origin, topic, created_ts, content_sha1, char_len, pruned_ts, source_file}`（content_sha1=sha1(content) hex；char_len=len(content)；pruned_ts=now iso；source_file=被删文件名）。
2. manifest 全部写完 → `os.unlink` 删该归档文件。
3. 文件名 ts 解析失败 → **跳过该文件不删**（fail-soft，不碰）。

【关键边界裁决（消歧，@coder 不得另议）】prune **只扫 `knowledge/archive/raw_pending_auto_*`（C slice 自家产物）**：
- **不碰 `quarantine/raw_pending_queue_overflow_*`**（那是既有 cleanup `clean()` 的 manual 产物，有 `--restore` 生命周期，自动删它会破坏既有工具语义）；
- **不碰 `pruned_index.jsonl` 自身**（glob 前缀天然不匹配，永久留存）；
- **不碰 `memories/`、`quarantine/` 下非 raw_pending 目录（decision/dropped/fact/preference/task）**；
- **不碰非 `raw_pending_auto_*` 的 archive 文件**（glob 前缀限定）。

【挂载】`_spawn_llm_consolidation._run` 在 C archive 段之后追加：`rescore→confirm→mine→observe→archive→prune`。prune 段独立 `try/except Exception` fail-soft（宿主 turn 零阻塞不变，与 archive 段同结构）；`_llm_ctx` None 不影响 prune（prune 零 LLM，纯文件操作，与 LLM 无关，照常跑）。

【纯 stdlib】新方法只 import `os`（或复用已 import 的）+ `json` + `hashlib` + `datetime`/`timezone`，**零第三方、零 LLM**。

【--promote 语义随之收紧（工具 doc 注明即可，不改逻辑）】人工 `--promote` 窗口 = 90d 保留期内（prune 前）可捞回；过 90d 后正文已清，只能查 manifest 审计（origin/topic/content_sha1/char_len），不可还原正文——这就是"回收站"本义：留过什么、为什么拦，不留正文。

## 硬约束（@coder 全程遵守）
- 纯 stdlib（argparse/json/pathlib/fcntl/datetime/os），**零第三方 SDK、零硬编码模型名、零 LLM 调用**。
- 不 import 插件 `__init__.py`、不碰 `memories/*.json`、不碰 `knowledge/archive/raw_pending_auto_*` 以外的 C slice 逻辑（只读它做 list/promote 源）。
- 不碰 `quarantine/` 下非 raw_pending 目录。
- 活数据 `/root/.hermes/filemem` **全程只读**（工具在合成 tmp store 上跑测试，绝不写活数据）。

## 测试面（新建 tests/test_cleanup_raw_pending.py，全用 tmp_path 合成 store，镜像 test_ep_2_observe.py fixture 样板，不碰活数据）
- `test_list_enumerates_all_three_sources`：pool/quarantine/auto-archive 各放 1 行 → --list 输出 3 行 + 全局行号 1..3，源标签正确。
- `test_list_corrupt_source_skipped_not_fatal`：某源 JSON 损坏 → 跳过并 stderr 告警，其余源仍列出，退出码 0。
- `test_promote_from_quarantine`：quarantine 行 --promote → 池内出现该 content+topic、origin=manual_promote、审计行写入 manual_promote_*.jsonl。
- `test_promote_from_auto_archive`：auto-archive 行 --promote → 同上。
- `test_promote_pool_row_marks_not_dup`：池内行 --promote → origin 改 manual_promote、不产生重复行、审计行写入。
- `test_promote_idempotent_content_topic`：池已有同 content+topic → 不重复插、仅标记。
- `test_promote_missing_row_exit2`：越界行号 → 退出码 2 + stderr 含有效范围、所有文件字节不变。
- `test_promote_dry_run_no_mutation`：--promote --dry-run → 无文件落盘（池/审计均不变）。
- `test_promote_respects_origin_preserved_reason_created_ts`：捞回行保留原 reason/created_ts，仅改 origin。
- `test_lock_serializes_rmw`（若环境支持 flock）：两进程并发 promote 不同行 → 池最终一致、无 lost update。
- **prune 层（测试新建 tests/test_rawpending_prune.py 或并入 test_cleanup_raw_pending.py，全 tmp_path 合成 store）**：
  - `test_prune_deletes_stale_writes_manifest`：造 2 个 `knowledge/archive/raw_pending_auto_<ts>.jsonl`（1 个 ts 老于 now-90d、1 个新）→ 调 `_auto_prune_raw_pending_archive()` → 老文件被删、新文件保留；`pruned_index.jsonl` 追加了老文件每行 manifest（origin/topic/created_ts/content_sha1/char_len/pruned_ts/source_file 字段全齐）；返回值=删的文件数（1）。
  - `test_prune_disabled_when_zero_or_missing`：`knowledge.archive_prune_days=0` 或 knowledge 块缺 → 返回 0、零文件被删、manifest 不变。
  - `test_prune_skips_unparseable_filename_ts`：造一个文件名 ts 解析不了的 `raw_pending_auto_foo.jsonl`（无时间戳）→ 跳过不删、fail-soft 不炸。
  - `test_prune_does_not_touch_quarantine_or_manifest`：造 `quarantine/raw_pending_queue_overflow_*.jsonl` 老文件 + `pruned_index.jsonl` → 调 prune → 二者字节不变（glob 前缀天然不匹配）。
  - `test_prune_mounted_after_archive_failsoft`：prune 段挂 observe→archive 之后，archive 段异常不影响 prune、prune 异常不影响既有 5 段（镜像 test_consolidation_boundary_mount_order_and_failsoft_isolation）。
  - `test_config_key_consistent`：`decay.json["knowledge"]` 与 `consolidation.DECAY_DEFAULTS["knowledge"]` 逐键一致（含新键 `archive_prune_days`）。

## 硬约束补充（prune 层，@coder 遵守）
- prune 只 import stdlib（os/json/hashlib/datetime/timezone），**零第三方、零 LLM**；`_llm_ctx` None 不影响 prune（纯文件操作照常跑）。
- prune **只删 `knowledge/archive/raw_pending_auto_*`**，不碰 quarantine 族、manifest、memories、其它 archive 文件（glob 前缀限定）；删前必写 manifest（append-only）。
- 配置键 `knowledge.archive_prune_days` 两处逐键一致（decay.json + DECAY_DEFAULTS）；`0`/缺 = 关闭 fail-soft 不删。

## 验证（@coder 收尾必做）
- 新增测试全绿（工具面 + prune 面）：`PYTHONPATH=/root/.hermes/installs/76f6e7e5145b5f39/environments/564a2ba842ca476b8fb9a41eb0dba1b4/workspace /usr/bin/python3 -m pytest tests/test_cleanup_raw_pending.py tests/test_rawpending_prune.py -q -p no:cacheprovider`（prune 测试若并入 test_cleanup_raw_pending.py 则单文件；全 tmp_path 合成 store 不碰活数据）。
- 全量回归零 D+C/EP-2/SEC-1 影响：同 venv 跑 tests/（--ignore test_config_tier_flock），除 3 py3.8 stdlib 假红外全绿，报数与 base 交叉核对。
- grep 验收（不认 status）：
  - 工具面：`grep -n "def list_rows\|def promote\|manual_promote\|--list\|--promote" tools/cleanup_raw_pending.py` 全命中；`grep -nE "openai|anthropic|requests|urllib|httpx|import __init__|from __init__" tools/cleanup_raw_pending.py` 空=好。
  - prune 面：`grep -n "def _auto_prune_raw_pending_archive\|pruned_index\|content_sha1\|archive_prune_days" __init__.py` 全命中；`grep -n "archive_prune_days" decay.json consolidation/__init__.py` 两处命中（逐键一致）；prune 段 grep 第三方 SDK（openai/anthropic/requests/urllib/httpx）空=好（hashlib/json/os 是 stdlib 可）。
- 活数据只读确认：`/root/.hermes/filemem` 全程 mtime 不变。
