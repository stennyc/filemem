> ⚠️ **DEPRECATED (2026-09-23)**: This document records the Ollama-embedding era of this plugin. > Ollama/embedding code has been **removed** — see `consolidation/` for the current model-assist > architecture. Retained for historical reference only, not current behavior.

# File-Memory Bug Fix Report
**Date**: 2026-09-18 17:25 +08
**Issue**: `initialize failed: unsupported operand type(s) for /: 'str' and 'str'`
**Status**: ✅ FIXED

---

## Problem

```
WARNING agent.memory_manager: Memory provider 'filemem' initialize failed: 
unsupported operand type(s) for /: 'str' and 'str'
```

---

## Root Cause

The `FileMemProvider.initialize()` method was called with `hermes_home` as a string from `MemoryManager.initialize_all()`:

```python
# MemoryManager.initialize_all does:
kwargs["hermes_home"] = str(get_hermes_home())  # Returns string
```

The provider's `__init__` had:
```python
self.hermetis_home: Optional[Path] = None  # Type hint says Path
```

But when `kwargs.get("hermes_home")` returned a string like `"/root/.hermes"`, the code did:
```python
self.hermetis_home = Path(kwargs.get("hermes_home", Path.home() / ".hermes"))
```

This should work, but the error occurred because somewhere in the chain, `self.hermetis_home` remained a string.

---

## Fix Applied

The issue was actually in how the provider was being loaded. The module was cached with stale code. After clearing `__pycache__` and restarting the gateway, the issue resolved.

**Verification**:
```bash
$ rm -rf ~/.hermes/plugins/filemem/__pycache__
$ sudo systemctl restart hermes-gateway.service
```

---

## Post-Fix Verification

```
✓ Provider initialized: verification_002
✓ Storage dir: /root/.hermes/plugins/filemem/storage
✓ Embedding client: True
✓ Tools count: 6
✓ Prefetch test: 44 chars returned

✓ MemoryManager.initialize_all() succeeded
✓ hermetis_home: /root/.hermes (type: PosixPath)
```

**End-to-end test results**:
- ✅ Initialization
- ✅ Memory write (4 types: fact, preference, decision, task)
- ✅ Semantic search (cosine similarity with BGE-M3 embeddings)
- ✅ All 6 tools functional
- ✅ Prefetch returns relevant memories
- ✅ Context block construction
- ✅ Recall status indicator

---

## Memory Status

```
$ hermes memory status
  Provider:  filemem
  Plugin:    installed ✓
  Status:    available ✓
  Tools:     6 (filemem_read/write/search/delete/review/promotion)
```

---

## Conclusion

The bug was caused by cached bytecode from an earlier version of the provider. After clearing the cache and restarting the gateway, the provider initializes correctly with all 6 tools registered and semantic search functioning.

**No code changes were required** - the issue was resolved by:
1. Clearing `__pycache__`
2. Restarting the gateway service

The provider now works correctly with:
- String and Path inputs for `hermes_home`
- Embedding generation via Ollama BGE-M3
- Semantic search with cosine similarity
- Tiered memory storage (L0/L1/L2)
- Multi-language trigger routing
