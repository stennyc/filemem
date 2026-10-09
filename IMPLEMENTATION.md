# File-Memory Plugin 增强实现

> ⚠️ **DEPRECATED (2026-09-23)**: 本文档记录 2026-09-18 Ollama-embedding 时代的实现。
> 当时的 "1024 维 embedding 检索" 已随 Ollama 代码整体移除（见 `COMPAT_MANIFEST.md` `ollama_url removed` 段）——
> 当前检索面是**关键词重叠 × 衰减**，无任何 embedding/向量。以下为历史参考，非当前行为。

**日期**: 2026-09-18
**状态**: ✅ 已完成并验证（历史记录）

## 实现功能

### 1. 三层记忆分级系统

| 级别 | 类型 | 触发策略 | TTL | 示例 |
|------|------|----------|-----|------|
| L0 | 环境事实 | 自动保存 | 30天 | IP、端口、路径配置 |
| L1 | 操作经验 | 会话结束确认 | 90天 | 修复方法、调试技巧 |
| L2 | 架构决策 | 用户显式确认 | 永久 | 技术选型、设计模式 |

### 2. 多语言触发器

支持语言：English, Chinese, Japanese, Spanish

```python
TRIGGER_PATTERNS = {
    "L0": {"environment": {"en": [...], "zh": [...], "ja": [...], "es": [...]}}
}
```

### 3. TCM 沉淀混合策略

- **高引用**：被引用 ≥ 3 次
- **L1 老化**：7 天后自动评估
- **L0 老化**：25 天未引用
- **用户显式**：用户请求立即沉淀

### 4. 新增工具

| 工具 | 功能 |
|------|------|
| `filemem_write` | 写入记忆（支持 level 参数） |
| `filemem_read` | 读取记忆（自动过滤过期） |
| `filemem_search` | 语义搜索 |
| `filemem_delete` | 删除记忆 |
| `filemem_review_drafts` | 会话结束时确认草稿 |
| `filemem_check_promotion` | 检查是否可沉淀到 TCM |

## 验证结果

```
✓ L0 自动保存：IP 地址、端口等配置自动写入
✓ 多语言触发：en/zh/ja 均正确识别并路由
✓ L1 草稿：修复经验暂存，会话结束前确认
✓ 搜索：1024 维 embedding 检索正常  ← 历史（Ollama 时代），当前已无 embedding，检索为关键词重叠×衰减
✓ TCM 沉淀检查：符合规则的标记可沉淀
```

## 文件位置

- 插件：`~/.hermes/plugins/filemem/__init__.py`
- 存储：`~/.hermes/plugins/filemem/storage/memories/*.json`
- 草稿：`~/.hermes/plugins/filemem/storage/drafts_{session_id}.json`

## 后续优化建议

1. 集成 langdetect 提升语言检测准确率
2. 添加后台 cron 任务自动清理过期记忆
3. 添加记忆引用计数追踪
