# filemem

> ## 功能介绍（中文）
>
> **filemem 是 Hermes Agent 的文件化记忆插件**：零外部依赖（无 Ollama / Qdrant / 向量库），本地优先，全部记忆以 JSON + Markdown 落盘在 `~/.hermes/plugins/filemem/storage/`，可手改、可 diff。
>
> **核心能力：**
> - **L0–L2 分层记忆**：L0 环境事实（自动存）→ L1 操作经验（会话结束确认）→ L2 架构决策（显式确认才升级）
> - **四类记忆**：偏好（preference）/ 事实（fact）/ 决策（decision）/ 任务（task），各存各的 JSON 文件
> - **双模检索**：零依赖关键词重叠评分 + 可选模型辅助（走 Hermes 辅助 LLM 任务，无 LLM 时自动降级为纯关键词）
> - **会话自动抽取（post-LLM hook）**：每轮自动从最近消息里捞出值得长期记住的事实/偏好，去重后进草稿箱——绝不裸写
> - **草稿确认闭环**：新会话首句自动检索时，会注入 `[未决草稿]` 提示（列出待确认的历史草稿文件），由 LLM 调 `filemem_review_drafts(action="confirm", target="<文件名>")` 处置；不指定 target 则处置当前会话草稿
> - **自动回忆（pre-LLM hook）**：每次调用 LLM 前自动注入相关记忆，跨会话"记得住"
> - **群聊杂讯守卫**：识别并拒绝把群聊骨架（`[System note`、`Group chat:`、JSON dump、`(pass)` 块等）存成持久记忆，入口 + 离线整理双保险
> - **离线 consolidation**：辅助 LLM 任务做重要性打分 + 情景摘要，衰减 TTL 按层级可配
>
> **一行安装：**
> ```bash
> bash install.sh   # 幂等；自动备份旧版、保留你的活数据 storage/、自动启用配置并自检
> ```
>
> **使用示例：**
> ```text
> filemem_write(type="preference", content="偏好简洁回复")        # 写一条记忆
> filemem_search(query="部署 117", min_score=0.4)                # 检索相关记忆
> filemem_read(type="fact", limit=10)                            # 按类型读取
> filemem_review_drafts(action="confirm")                        # 确认会话草稿转正
> ```
>
> ---

File-based **memory provider plugin** for [Hermes Agent](https://hermes-agent.nousresearch.com/docs) with a tiered memory system, keyword-overlap search, automatic conversation extraction, and offline consolidation.

Zero external dependencies (no Ollama, no Qdrant, no vector DB). Everything is plain JSON/Markdown on disk, local-first, with graceful degradation.

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

### Tools exposed

- `filemem_read(type, limit)` — read memories by type or all
- `filemem_write(type, content, category, level)` — write a new memory
- `filemem_search(query, min_score)` — search with similarity threshold
- `filemem_delete(id)` — delete a memory
- `filemem_review_drafts(action, memory_id?, target?)` — confirm/discard session drafts; `target` disposes of a historical session's draft file (e.g. `target="drafts_abc123.json"`)
- `filemem_check_promotion` — tier promotion checks (L0→L1→L2)

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
~/.hermes/plugins/filemem/
├── __init__.py            # provider, hooks, registration (~2300 lines)
├── config_schema.py       # decay/consolidation config validation
├── decay.json             # per-tier TTL defaults
├── consolidation/         # offline consolidation + LLM caller
│   ├── __init__.py
│   └── llm_caller.py
├── extractor/             # extraction docs/notes
├── plugin.yaml            # manifest (name, version, hooks, tools)
├── tests/                 # pytest suite (failure detector, gold knowledge, reinforce-recall, v2 self-test)
├── tools/                 # self-test helpers
└── storage/               # LIVE DATA — never shipped in the repo
    ├── MEMORY.md / USER.md
    ├── memories/*.json
    ├── consolidated/
    ├── drafts_*.json
    └── local_semantic/
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

## Design notes & evidence

- `IMPLEMENTATION.md` — module map and key invariants
- `COMPAT_MANIFEST.md` — API compat commitments across versions
- `PREFETCH_EVIDENCE.md` / `RUNTIME_EVIDENCE.md` — captured runtime traces of auto-retrieve/auto-catch
- `VERIFICATION.md` — acceptance checklist
- `BUGFIX_REPORT.md` — history of shipped fixes

## License

MIT — see `LICENSE`.
