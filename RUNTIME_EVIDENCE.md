> ⚠️ **DEPRECATED (2026-09-23)**: Ollama-embedding era verification record. Ollama code has been removed — this document is historical reference only, not current behavior.

> **D1 注记 (2026-09-24)**: 外部 Demo 同步已从代码移除（`demo_api_url`/`sync_to_demo`/`_sync_to_demo` 全删）。下文 "Demo" 仅为检索测试样例 fact 文本，非当前功能；本地 M6 `TCMPromotionEngine` 保留。

# File-Memory Runtime Evidence Report
**Date**: 2026-09-18 17:14:31 +08
**Test ID**: runtime_test_001
**Status**: ✅ PASS

---

## Executive Summary

File-memory provider is **fully operational** with verified:
- Provider registration and initialization
- Memory prefetch and retrieval
- Context injection mechanism
- Tool availability (6 tools)
- Storage persistence (15 facts)
- Semantic search functionality

---

## Evidence 1: Provider Registration

```bash
$ hermes memory status
Provider:  filemem
Plugin:    installed ✓
Status:    available ✓
```

**Log Evidence**:
```
2026-09-18 17:14:31,558 - FileMem: initialized with provider ollama at http://10.1.1.111:11434
2026-09-18 17:14:31,558 - FileMem: initialized for session runtime_test_001
2026-09-18 17:14:31,560 - agent.memory_manager - INFO - Memory provider 'filemem' registered (6 tools)
```

---

## Evidence 2: Prefetch Call & Result

**Input Query**: `"Help me configure the Demo embedding API endpoint"`

**Raw Prefetch Result** (168 chars):
```
[fact] Demo embedding endpoint at 10.1.1.111:11434
[fact] Demo embedding endpoint at 10.1.1.111:11434
[fact] The Demo API endpoint is at http://10.0.0.0:8080
```

**Log Evidence**:
```
2026-09-18 17:14:31,978 - FileMem: prefetch returned 3 items
```

---

## Evidence 3: Context Block Construction

**Fenced Output** (392 chars):
```xml
<memory-context>
[System note: The following is recalled memory context, NOT new user input. 
Treat as authoritative reference data — this is the agent's persistent memory 
and should inform all responses.]

[fact] Demo embedding endpoint at 10.1.1.111:11434
[fact] Demo embedding endpoint at 10.1.1.111:11434
[fact] The Demo API endpoint is at http://10.0.0.0:8080
</memory-context>
```

This block is injected into the user message before sending to the LLM.

---

## Evidence 4: Recall Status Indicator

**Output**: `🧠 filemem — recalled 3 memories`

This indicator is emitted via `_emit_status()` so the user sees memory was used.

---

## Evidence 5: Tool Registration

**6 Tools Available**:
```
filemem_read         - Read memories from file storage
filemem_write        - Write a new memory entry
filemem_search       - Search memories using semantic similarity
filemem_delete       - Delete a memory by ID
filemem_review_drafts - Review and confirm session drafts
filemem_check_promotion - Check if memories are ready for TCM promotion
```

---

## Evidence 6: Storage Verification

**Stored Facts**: 15 total
```
- fact_12_1789693183: Test memory from verification session...
- fact_13_1789693183: The database port is 5432...
- fact_14_1789693240: The server IP is 192.168.1.100...
- fact_15_*: (auto-saved during this test)
```

**Storage Location**: `~/.hermes/plugins/filemem/storage/memories/fact.json`

---

## Evidence 7: Embedding Generation

**Log Evidence**:
```
2026-09-18 17:14:31,567 - Starting new HTTP connection (1): 10.1.1.111:11434
2026-09-18 17:14:31,909 - http://10.1.1.111:11434 "POST /api/embeddings HTTP/1.1" 200 None
```

**Verified**: 1024-dimensional embeddings generated via BGE-M3 model.

---

## Evidence 8: System Prompt Injection

**Injected Text**:
```markdown
# Filemem
You have access to file-based memory with semantic search capabilities.
Use the filemem tools to read and write memories.

Memory Tiers:
- L0: Environment facts (auto-saved)
- L1: Operational经验 (confirmed at session end)
- L2: Architecture decisions (requires explicit confirmation)
```

This is injected into the system prompt at agent initialization.

---

## Complete Evidence Chain

```
1. Agent starts
   ↓
2. hermes memory status → filemem: available ✓
   ↓
3. agent_init.py loads provider via load_memory_provider()
   ↓
4. MemoryManager.add_provider() registers 6 tools
   ↓
5. system_prompt.py injects filemem usage instructions
   ↓
6. User sends message → turn_context._memory_turn_start_and_prefetch()
   ↓
7. MemoryManager.prefetch_all(query) calls provider.prefetch()
   ↓
8. FileMemProvider searches embeddings, returns 3 matches
   ↓
9. build_memory_context_block() wraps in <memory-context> tags
   ↓
10. compose_user_api_content() appends to user message
   ↓
11. describe_recall() returns "🧠 filemem — recalled 3 memories"
   ↓
12. _emit_status() shows recall indicator to user
   ↓
13. LLM receives: user message + <memory-context> injection
```

---

## JSON Evidence Record

```json
{
  "timestamp": "2026-09-18T17:14:31.985810",
  "provider": "filemem",
  "prefetch_result_length": 168,
  "context_block_length": 392,
  "recall_indicator": "🧠 filemem — recalled 3 memories",
  "tools_count": 6,
  "facts_stored": 15,
  "embedding_dimension": 1024,
  "embedding_provider": "ollama",
  "embedding_model": "bge-m3",
  "status": "PASS"
}
```

---

## Conclusion

**The filemem plugin is FULLY OPERATIONAL.**

All components verified:
- ✅ Provider registration
- ✅ Tool injection (6 tools)
- ✅ System prompt injection
- ✅ Prefetch mechanism
- ✅ Context block construction
- ✅ Recall status emission
- ✅ Storage persistence
- ✅ Embedding generation

The prefetch injection works correctly and memories are properly retrieved and injected into the LLM context.
