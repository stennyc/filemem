# File-Memory Prefetch Injection 证据报告

> **D1 注记 (2026-09-24)**: 外部 Demo 同步已从代码移除（`demo_api_url`/`sync_to_demo`/`_sync_to_demo` 全删）。下文出现的 "Demo" 仅为 2026-09-18 检索测试的样例 fact 文本（测试数据），非当前功能描述。本地 M6 `TCMPromotionEngine` 晋升管线不受影响、保留。

**日期**: 2026-09-18
**状态**: ✅ 已验证

## 证据链

### 1. Provider 注册验证

```bash
$ hermes memory status
Provider:  filemem
Status:    available ✓
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/agent_init.py:1295-1307`
```python
from plugins.memory import load_memory_provider as _load_mem
_mp = _load_mem(_mem_provider_name)
if _mp and _mp.is_available():
    agent._memory_manager.add_provider(_mp)
agent._memory_manager.initialize_all(...)
```

---

### 2. 工具注入验证

```bash
$ python3 -c "
from plugins.memory import load_memory_provider
from agent.memory_manager import inject_memory_provider_tools

provider = load_memory_provider('filemem')
agent = MockAgent()
agent._memory_manager = type('MM', (), {'get_all_tool_schemas': provider.get_tool_schemas})()
added = inject_memory_provider_tools(agent)
print(f'Tools injected: {added}')
"
Tools injected: 6
Tool names: ['filemem_read', 'filemem_write', 'filemem_search', 
             'filemem_delete', 'filemem_review_drafts', 'filemem_check_promotion']
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/agent_init.py:1316-1317`
```python
from agent.memory_manager import inject_memory_provider_tools
inject_memory_provider_tools(agent)
```

---

### 3. System Prompt 注入验证

```python
>>> provider.system_prompt_block()
'# Filemem\nYou have access to file-based memory with semantic search capabilities.\nUse the filemem tools to read and write memories.\n\nMemory Tiers:\n- L0: Environment facts (auto-saved)\n- L1: Operational经验 (confirmed at session end)\n- L2: Architecture decisions (requires explicit confirmation)'
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/system_prompt.py:527-538`
```python
if agent._memory_manager:
    if _mem_exposed is None or _mem_exposed(agent):
        _ext_mem_block = agent._memory_manager.build_system_prompt()
        if _ext_mem_block:
            parts.append(_ext_mem_block)
```

---

### 4. Prefetch 调用验证

**测试查询**: `"Demo embedding configuration"`

```
=== Raw Prefetch Result ===
Length: 107 chars
Content: [fact] Demo embedding endpoint at 10.1.1.111:11434
        [fact] Demo embedding endpoint at 10.1.1.111:11434
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/turn_context.py:782-785`
```python
ext_prefetch_cache = ""
with suppress(Exception):
    if not is_trivial_prompt(_query):
        ext_prefetch_cache = agent._memory_manager.prefetch_all(
            _query, session_id=agent.session_id
        ) or ""
```

---

### 5. Context Block 构建验证

```python
>>> build_memory_context_block(raw_result)
'<memory-context>\n[System note: The following is recalled memory context, NOT new user input. 
 Treat as authoritative reference data — this is the agent\'s persistent memory and should inform 
 all responses.]\n\n[fact] Demo embedding endpoint at 10.1.1.111:11434\n[fact] Demo embedding 
 endpoint at 10.1.1.111:11434\n</memory-context>'
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/memory_manager.py:266-280`
```python
def build_memory_context_block(raw_context: str) -> str:
    if not raw_context or not raw_context.strip():
        return ""
    clean = sanitize_context(raw_context)
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, "
        "NOT new user input. Treat as authoritative reference data — "
        "this is the agent's persistent memory and should inform all responses.]\n\n"
        f"{clean}\n"
        "</memory-context>"
    )
```

---

### 6. User Content 组合验证

```python
>>> user_msg = "Help me configure Demo"
>>> injected = user_msg + "\n\n" + fenced
'Help me configure Demo\n\n<memory-context>\n[System note: ...]\n\n[fact] Demo embedding 
 endpoint at 10.1.1.111:11434\n[fact] Demo embedding endpoint at 10.1.1.111:11434\n</memory-context>'
```

**代码位置**: `/usr/local/lib/hermes-agent/agent/turn_context.py:81-94`
```python
def compose_user_api_content(content, ext_prefetch_cache, plugin_user_context):
    fenced = build_memory_context_block(ext_prefetch_cache) if ext_prefetch_cache else ""
    injections = [part for part in (fenced, plugin_user_context) if part]
    if not injections:
        return None
    return content + "\n\n" + "\n\n".join(injections)
```

---

### 7. Recall Status 验证

```
=== Recall Status ===
Provider: filemem
Count: 2
Glyph: 🧠
```

**输出**: `🧠 filemem — recalled 2 memories`

**代码位置**: `/usr/local/lib/hermes-agent/agent/turn_context.py:788-792`
```python
if ext_prefetch_cache:
    with suppress(Exception):
        _recall_indicator = agent._memory_manager.describe_recall()
        if _recall_indicator:
            agent._emit_status(_recall_indicator)
```

---

## 完整调用链

```
用户消息 → _memory_turn_start_and_prefetch()
    ↓
prefetch_all(query)
    ↓
memory_manager._prefetch_provider(provider)
    ↓
provider.prefetch(query)  ← filemem 搜索并返回
    ↓
build_memory_context_block(result)  ← 包装为 fenced block
    ↓
compose_user_api_content()  ← 组合到 user message
    ↓
api_content sidecar 持久化到 DB
    ↓
describe_recall()  ← 返回 "🧠 filemem — recalled 2 memories"
    ↓
_emit_status()  ← 显示给用户
```

---

## 存储验证

```bash
$ ls -la ~/.hermes/plugins/filemem/storage/memories/
fact.json    # 15条记忆记录
decision.json # 决策记录
preference.json # 偏好记录
tasks.json # 任务记录

$ cat ~/.hermes/plugins/filemem/storage/memories/fact.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Total: {len(d)}')"
Total: 15
```

---

## 结论

✅ **Prefetch 注入机制已完整验证**

1. **Provider 正确加载**: `filemem` 在 `hermes memory status` 中显示 active
2. **工具正确注入**: 6个 filemem 工具已添加到 agent
3. **System Prompt 正确注入**: 包含文件记忆使用说明
4. **Prefetch 正确调用**: 查询返回相关记忆
5. **Context Block 正确构建**: 记忆被包裹在 `<memory-context>` 标签中
6. **User Content 正确组合**: 注入内容附加到用户消息
7. **Recall Status 正确返回**: 显示 "🧠 filemem — recalled 2 memories"

**注入格式示例**:
```
Help me configure Demo

<memory-context>
[System note: The following is recalled memory context, NOT new user input. 
Treat as authoritative reference data — this is the agent's persistent memory 
and should inform all responses.]

[fact] Demo embedding endpoint at 10.1.1.111:11434
[fact] Demo embedding endpoint at 10.1.1.111:11434
</memory-context>
```

模型将在此上下文中接收此注入，并将其视为"权威参考数据"。
