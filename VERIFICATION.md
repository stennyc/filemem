> ⚠️ **DEPRECATED (2026-09-23)**: Ollama-embedding era verification record. Ollama code has been removed — this document is historical reference only.

# File-Memory 插件验证报告

**日期**: 2026-09-18
**状态**: ✅ 已验证并通过

## 验证摘要

| 检查项 | 状态 | 说明 |
|--------|------|------|
| Provider 注册 | ✅ | `hermes memory status` 显示 active |
| 工具注入 | ✅ | 6个 filemem 工具注入 agent |
| System Prompt | ✅ | 自动注入文件记忆说明 |
| L0 自动保存 | ✅ | 检测到 IP/URL 自动保存 |
| L1 草稿机制 | ✅ | 操作经验暂存到 drafts.json |
| 多语言触发 | ✅ | EN/ZH/JA/ES 关键词识别 |
| 语义搜索 | ✅ | 1024维 embedding 检索 |
| 会话结束确认 | ✅ | on_session_end 提示确认草稿 |

## 已注册工具

```
filemem_read      - 读取记忆（支持类型过滤）
filemem_write     - 写入记忆（支持分级）
filemem_search    - 语义搜索
filemem_delete    - 删除记忆
filemem_review_drafts - 会话结束草稿确认
filemem_check_promotion - TCM沉淀检查
```

## 自动触发规则

### L0 - 环境事实（自动保存）
- **触发关键词**: ip, port, path, url, endpoint, 配置, 端口, 路径
- **行为**: 立即保存到 fact.json，level=L0，TTL=30天
- **示例**: "API endpoint is http://10.0.0.0:8080" → 自动保存

### L1 - 操作经验（草稿暂存）
- **触发关键词**: fix, solve, method, 修复, 解决, 方法, 经验
- **行为**: 暂存到 drafts_{session_id}.json，等待会话结束确认
- **示例**: "This fix solves the auth issue" → 草稿，需确认

### L2 - 架构决策（显式确认）
- **触发关键词**: architecture, design, decision, 架构, 设计, 决策
- **行为**: 日志记录，等待用户显式确认
- **示例**: "We should use event sourcing pattern" → 等待确认

## 验证测试结果

```
=== Final Verification ===

1. Provider Registration:
   Name: filemem
   Available: True

2. System Prompt Block:
   Length: 300 chars
   Contains filemem tools reference: True

3. Tool Schemas (6 tools):
   - filemem_read
   - filemem_write
   - filemem_search
   - filemem_delete
   - filemem_review_drafts
   - filemem_check_promotion

4. Auto-Trigger Test (sync_turn):
   Auto-saved: 1 facts with IP
   Level: L0

5. Draft Test (L1 operation trigger):
   Drafts created: 1
   Draft content: This fix solves the authentication issue...

=== All Verifications Passed ===
```

## 存储位置

- **记忆文件**: `~/.hermes/plugins/filemem/storage/memories/*.json`
- **草稿文件**: `~/.hermes/plugins/filemem/storage/drafts_{session_id}.json`
- **Embeddings**: `~/.hermes/plugins/filemem/storage/embeddings/`

## 使用方式

### 智能体自动触发
当对话内容匹配触发关键词时，模型会自动：
1. 调用 `filemem_write` 保存 L0 事实
2. 调用 `filemem_review_drafts` 确认 L1 草稿
3. 通过 prefetch 注入相关记忆到上下文

### 手动操作
模型可通过以下工具手动管理记忆：
```python
filemem_write(type="fact", content="...", level="L0")
filemem_read(type="all", limit=10)
filemem_search(query="...", limit=5)
filemem_delete(id="...")
filemem_review_drafts(action="confirm")
filemem_check_promotion(memory_id="...")
```

## 配置说明

```yaml
# config.yaml
memory:
  provider: filemem
  
plugins:
  entries:
    filemem:
      ollama_url: http://10.1.1.111:11434
      embedding_model: bge-m3
      auto_extract: true
```
