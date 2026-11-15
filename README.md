# filemem

> ## 功能介绍（中文）
>
> **filemem 是 Hermes Agent 的文件化记忆插件**：零外部依赖（无 Ollama / Qdrant / 向量库），本地优先，全部记忆以 JSON + Markdown 落盘在**中立共享根 `<base>/filemem`**（`<base>` = HERMES_HOME 剥掉尾部 `profiles/<name>` 段，如 `/root/.hermes/filemem`；**不用 per-profile 存储**——所有 profile 共享同一棵树），可手改、可 diff。
>
> **核心能力：**
> - **L0–L2 分层记忆**：L0 环境事实（自动存）→ L1 操作经验（会话结束确认）→ L2 架构决策（显式确认才升级）
> - **六类记忆**：偏好（preference）/ 事实（fact）/ 决策（decision）/ 任务（task）/ 计划（plan）/ 定时（scheduled），各存各的 JSON 文件
> - **多轴检索（RRF）**：BM25 关键词（拉丁词 + 中文 n-gram，idf 加权）× 衰减/新鲜度 × 图谱 1-hop 扩展，三路倒排 rank fusion；`origin_mark` 标记低可信 / 指令引用行
> - **会话自动抽取（post-LLM hook）**：每轮自动从最近消息里捞出值得长期记住的事实/偏好，去重后进草稿箱——绝不裸写
> - **草稿确认闭环**：新会话首句自动检索时，会注入 `[未决草稿]` 提示，由 LLM 调 `filemem_review_drafts(action="confirm")` 处置
> - **自动回忆（pre-LLM hook）**：每次调用 LLM 前自动注入相关记忆，跨会话「记得住」
> - **跨 session 延续**：`filemem_set_bringback` 把上个 session 的已确认摘要钉进当前 session 自动注入面；`filemem_handoff_extract` 从死锁 session 抽交接摘要
> - **结构化计划**：`filemem_plan` 管理多步 PRD 计划（list/create/advance 状态机）
> - **用户思维底座**：`gate5_worldview_upsert` 落用户世界观/方法论行（L2，无 TTL，仅用户拍板可写）
> - **顶替/更正**：`filemem_supersede` 让过时行退出自动召回池（留盘审计），写更正行带血缘链
> - **跨设备共享池**：可选 PostgreSQL 中央池（`shared_memory_search/handoff/take`），failure 行设备特定不推中央
> - **failure 行语义**：失败工具调用/报错轮被打上 `failure_type` 标，豁免 LLM 重判、不推共享池、不进自动确认面板——坏轮次不会污染持久记忆
> - **群聊杂讯守卫**：识别并拒绝把群聊骨架（`[System note`、`Group chat:`、JSON dump、`(pass)` 块等）存成持久记忆，入口 + 离线整理双保险
> - **离线 consolidation**：宿主主模型 LLM 面做重要性打分 + 情景摘要，衰减 TTL 按层级可配
>
> **14 个工具面**（完整清单见下方英文版 Tools exposed 表）：filemem_read / write / search / delete / review_drafts / check_promotion / supersede / plan / gate5_worldview_upsert / set_bringback / handoff_extract / scheduled / shared_memory_search / shared_memory_handoff / shared_memory_take。
>
> **一行安装：**
> ```bash
> bash install.sh                          # 幂等；自动备份、保留活数据、启用配置
> FM_EXCLUDE=4d,amoi,culinary,default bash install.sh --all-profiles   # multiplex 宿主：全 profile 启用（可排除）
> ```
>
> **使用示例：**
> ```text
> filemem_write(type="preference", content="偏好简洁回复")        # 写一条记忆
> filemem_search(query="部署 117", min_score=0.4)                # 多轴检索
> filemem_read(type="fact", limit=10)                            # 按类型读取
> filemem_review_drafts(action="confirm")                        # 确认会话草稿转正
> filemem_supersede(old_id, new_content, reason)                 # 顶替过时行
> ```
>
> **许可证：GPL-3.0-or-later**——非商用自由使用/修改/分发（copyleft，衍生作品同许可）；**商用需另购授权**。详见 `LICENSE`。
>
> ---

File-based **memory provider plugin** for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) with a tiered memory system, keyword-overlap search, automatic conversation extraction, and offline consolidation.

Zero external dependencies (no Ollama, no Qdrant, no vector DB). Everything is plain JSON/Markdown on disk, local-first, with graceful degradation.


## Installation

### Linux

```bash
# Clone to Hermes plugin directory
git clone https://github.com/stennyc/filemem.git ~/.hermes/plugins/filemem

# Auto-install to all profiles that have it enabled in config.yaml:
bash ~/.hermes/plugins/filemem/install.sh --all-profiles
```

### Windows (PowerShell)

```powershell
# Clone to both plugin directories (AppData + ~/.hermes)
git clone https://stennyc@github.com/stennyc/filemem.git "C:\Users\$env:USERNAME\AppData\Local\hermes\plugins\filemem"
& "C:\Users\$env:USERNAME\AppData\Local\hermes\plugins\filemem\install.ps1" -AllProfiles
```

### Enable in profile config

Add `filemem` to `plugins.enabled` in the target profile's `config.yaml`:

```yaml
plugins:
  enabled:
  - filemem
  - filemem
  # ... other plugins
```

Then restart the Hermes gateway.

### Dual-path note (Windows)

Hermes on Windows stores plugins in two locations:
- `C:\Users\<user>\AppData\Local\hermes\plugins\` (primary, loaded at runtime)
- `~/.hermes/plugins/` (sync copy, used by some tools)

The install scripts above copy to **both** locations automatically.

## Features

| Area | What it does |
|---|---|
| **File-based storage** | Memories persisted as JSON under `~/.hermes/plugins/filemem/storage/` (`MEMORY.md`, `USER.md`, `memories/*.json`). Inspectable and diffable in any editor. |
| **Memory tiers** | `L0` environment facts (auto-saved) → `L1` operational experience (confirmed at session end) → `L2` architecture decisions (explicit confirmation required). |
| **Memory types** | `preference`, `fact`, `decision`, `task` — each in its own JSON file. |
| **Keyword-overlap search** | Dependency-free keyword-overlap × decay scoring (no embeddings, no vector DB); optional **model-assist** routes search through Hermes' main-model LLM path (`ctx.llm`) with automatic fallback to keyword ranking when no LLM is available. |
| **Auto-extract (post-LLM hook)** | Every turn, the plugin catches durable facts/preferences/decisions from the last few messages, dedupes (exact + keyword-overlap duplicate detection at 0.85 overlap), and stores drafts in a review box — never auto-committed. |
| **Auto-retrieve (pre-LLM hook)** | Before each LLM call, relevant memories are prefetched and injected as context, so the model "remembers" across sessions without manual recall. |
| **Room-transcript guard** | Structural-opener + marker predicates reject group-chat scaffolding (`[System note`, `Group chat:`, `(pass)` blocks, JSON dumps) so never-storable meta content is filtered at ingress **and** purged at consolidation — one shared reject-list, no drift. |
| **Turn-failure detection** | Detects failed tool calls / error turns in recent history and reinforces or discards related memories accordingly. |
| **Offline consolidation** | A host main-model LLM path (via `ctx.llm`, no auxiliary task — aligned with the built-in compressor) performs salience scoring and episodic summarization; decay config (`decay.json`) controls TTL per tier. |
| **Draft review loop** | New memories land as drafts instead of auto-committing. At the start of a NEW session, auto-retrieve injects a `[未决草稿]` notice listing unconfirmed draft files from earlier sessions; the LLM then disposes of them via `filemem_review_drafts` — `target="<drafts_xxx.json>"` for historical files, or plain confirm/discard for the current session. (The session-end confirmation prompt itself is discarded by the engine, so the next session's opening turn is the actual user-visible surface.) |
| **Provider caching** | Per-session provider instances are cached (`threading.Lock`), so repeated hooks never re-instantiate the storage layer. |
| **Neutral shared storage (T5b)** | filemem **never uses per-profile storage**. Every profile converges on ONE neutral root — `<base>/filemem` where `base` is `HERMES_HOME` (or `~/.hermes`) with a trailing `profiles/<name>` segment stripped — so all profiles share a single central tree. The per-profile path (`<hermes_home>/plugins/filemem/storage`) is only a legacy fallback the host relocates away from at startup (`storage root relocated` log). |
| **Cross-device shared pool** | An optional central PostgreSQL pool (`shared_memory`) lets rows sync across devices/agents: `shared_memory_search` (read-only consensus), `shared_memory_handoff` (push a row to central), `shared_memory_take` (pull a central row into the local shard). Failure rows are device-specific and are NOT pushed to the shared pool. |
| **Failure-row semantics** | Failed tool-call / error turns are detected and tagged `failure_type`; such rows are exempt from supersede/consolidation LLM re-judgement, never pushed to the shared pool, and are excluded from the auto-confirm panel, so a bad turn can't poison durable memory. |

### Tools exposed (14)

| Tool | What it does |
|---|---|
| `filemem_read(type, limit)` | Read memories by type or all |
| `filemem_write(type, content, category, level)` | Write a new memory (genuinely NEW facts only) |
| `filemem_search(query, min_score)` | Multi-axis RRF retrieval: BM25 keyword (latin + CJK n-grams, idf-weighted) × decay/recency × graph 1-hop expansion, fused by reciprocal-rank fusion; `origin_mark` flags untrusted / directive-quote rows |
| `filemem_delete(id)` | Delete a memory |
| `filemem_review_drafts(action, memory_id?, target?, verdict?)` | Confirm/discard session drafts in bulk; `target` disposes a historical session's draft; `plan_probe`/`verdict` is the user's save/dismiss nod on a pending plan-probe panel entry |
| `filemem_check_promotion` | Tier promotion checks (L0→L1→L2) |
| `filemem_supersede(old_id, new_content, reason, validity_scope?)` | Invalidate an outdated row (marks it `superseded`, leaves it on disk for audit) and write a correcting row carrying the lineage link — so the stale row stops auto-recalling instead of coexisting with the newer fact |
| `filemem_plan(action, …)` | Structured multi-step plans (PLAN-type PRD): `list` recalls unfinished plans, `create` lands a new plan, `advance` drives its state machine (continue/revise/block/complete_all/abandon/…) |
| `gate5_worldview_upsert(content, source, …)` | Land a user world-view / methodology row (how they act, or the order they want you to act); L2, no TTL, replaced only by a later user-confirmed upsert |
| `filemem_set_bringback(choice, …)` | Cross-session continuity: pin a previous session's confirmed summaries to this session's auto-inject (bring_back / clean_start / fresh / topic_mismatch) |
| `filemem_handoff_extract(session_id, focus?)` | Extract a handoff summary from a dead / deadlocked session so a new one can continue its work |
| `filemem_scheduled(action, …)` | Scheduled wake-ups a user wants revisited at a set time (suggest / list / resolve) |
| `shared_memory_search(query, …)` | Read-only query against the central cross-device shared pool (PostgreSQL `memories` table) |
| `shared_memory_handoff(id)` / `shared_memory_take(id)` | Hand a row to the central pool / take a central row into this agent's local shard |

## Install guide

### 1. Requirements

- Hermes Agent (this plugin uses the standard `MemoryProvider` API + hooks)
- Python 3.10+ in Hermes' runtime (verified on 3.11 and 3.14)
- No third-party packages needed

### 2. Manual install

```bash
# clone / download this repo, then:
mkdir -p ~/.hermes/plugins/filemem
rsync -a --exclude storage/ --exclude __pycache__/ --exclude .pytest_cache/ \
    /path/to/filemem/ ~/.hermes/plugins/filemem/
```

> **Windows note:** Hermes Desktop reads plugins from `~/.hermes/plugins/`, while the CLI reads
> `%LOCALAPPDATA%\hermes\plugins\`. The install script below wires both up with a symlink so
> you maintain a single copy.

### 3. One-line install (recommended)

```bash
# Linux / macOS / WSL — installs into ~/.hermes/plugins, mirrors for CLI, enables config, verifies:
bash install.sh

# or point at a git checkout:
HERMES_HOME="$HOME/.hermes" bash install.sh
```

What the script does (idempotent, backs up before touching anything):

1. Creates `~/.hermes/plugins/filemem/` (or replaces an existing one **with a backup** `filemem.bak.<timestamp>`)
2. Copies code (excludes `storage/` — your live memory data survives upgrades)
3. Mirrors to `%LOCALAPPDATA%/hermes/plugins/` on Windows dual-home setups (symlink where possible, copy otherwise)
4. Enables `memory.provider: filemem` in `~/.hermes/config.yaml` (never overwrites an existing memory section — appends if absent, prints a diff if present)
5. Runs a smoke test: imports the provider module, asserts `register` is defined and the 6 tools are listed in `plugin.yaml`

### 4. Enable in config

If you skipped the script, add to `~/.hermes/config.yaml`:

```yaml
memory:
  provider: filemem

plugins:
  entries:
    filemem:
      auto_extract: true
```

Then restart Hermes (Desktop: kill & relaunch; CLI: new session). Restart is required — the backend caches config.

### 5. Verify

```bash
hermes config get memory.provider   # → filemem
# in a Hermes chat, ask it to remember a fact, start a new session, ask about it.
```

Live behavior also shows up in logs: `grep -i filemem ~/.hermes/logs/*.log`.

### Uninstall

```bash
rm -rf ~/.hermes/plugins/filemem.bak.* 2>/dev/null
mv ~/.hermes/plugins/filemem "$HOME/filemem.uninstalled"   # keeps storage/
# remove the memory.provider: filemem line from config.yaml
```

## Data layout

```
# NEUTRAL STORAGE ROOT (T5b) — every profile converges here; <base> =
# HERMES_HOME (or ~/.hermes) with a trailing profiles/<name> stripped,
# e.g. /root/.hermes/filemem. The per-profile path is a legacy fallback only.
<base>/filemem/
├── memories/*.json         # L0/L1/L2 rows by type (preference/fact/decision/task/plan/scheduled)
├── sessions/              # per-session draft + handoff artifacts
├── graph.json             # 1-hop retrieval expansion graph
├── decay.json             # per-tier TTL config
├── consolidation_log.jsonl  # append-only audit (supersede/confirm/consolidation)
└── …
# the plugin code itself still ships in ~/.hermes/plugins/filemem/ (the
# discovery dir); only the DATA lives in the neutral root above.
```

## Development

```bash
python3 -m pytest tests/ -q
python3 -m pytest tests/ -q -k failure_detector   # single file
```

The plugin must remain importable standalone: sub-package imports are resolved by
pushing the plugin directory onto `sys.path` at module load (see `_PLUGIN_DIR` in
`__init__.py`), so tests and the Hermes plugin loader both work.

## Operations invariant (117 incident 2026-09-27)

The Hermes engine **auto-discovers any directory under `plugins/` that carries a
`plugin.yaml`** and instantiates it as a memory provider. This means a leftover
backup dir (`.old` / `.backup-*` / `.pre-*`) sitting next to the live
`filemem/` gets registered as a *second, stale* provider and can steal the
memory slot, silently degrading the live code path.

**Rule: `plugins/` holds live code only. Backups and audit artifacts belong in
`~/.hermes/archive/` (or anywhere outside the discovery root), never in
`plugins/`.** When you upgrade, move the old install out instead of leaving it:

```bash
mkdir -p ~/.hermes/archive
mv ~/.hermes/plugins/filemem.old \
   ~/.hermes/plugins/filemem.backup-* \
   ~/.hermes/plugins/filemem.pre-* \
   ~/.hermes/archive/
```

The plugin also now **warns at startup** if it detects a sibling `plugin.yaml`
dir declaring `name: filemem` (`_warn_duplicate_plugin_dirs` in
`initialize`) — a stale path is meant to be loud, not a silent no-op. Clear the
WARNING by moving the flagged dir(s) to `archive/`, then restart the gateway.

## Version

`plugin.yaml` declares `version: 1.0.0`. The repo currently tags `filemem-v0.1.0`
(the tag predates the 1.0.0 tool-surface expansion — 8 new tools: `filemem_supersede`,
`filemem_plan`, `gate5_worldview_upsert`, `filemem_set_bringback`,
`filemem_handoff_extract`, `filemem_scheduled`, `shared_memory_*`). Bump a `v1.0.0`
tag when the next release is cut.

## Design notes & evidence

- `IMPLEMENTATION.md` — module map and key invariants
- `COMPAT_MANIFEST.md` — API compat commitments across versions
- `PREFETCH_EVIDENCE.md` / `RUNTIME_EVIDENCE.md` — captured runtime traces of auto-retrieve/auto-catch
- `VERIFICATION.md` — acceptance checklist
- `BUGFIX_REPORT.md` — history of shipped fixes

## License

GPL-3.0-or-later — see `LICENSE` (non-commercial; commercial use requires a separate license purchased from the author, stennyc).
