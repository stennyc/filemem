# P2 · clarify footer 引用了宿主未注册的 clarify 工具

- **card**: P2-clarify-footer-host-missing-2026-10-11
- **profile**: coder
- **line**: O1（footer 文案层，source-only，不推 remote）
- **状态**: OPEN（本地建卡，等 owner 签核后再推 stennyc/filemem）
- **红线**: 只动 filemem 插件 source + 测试，不碰 hermes 原生代码/文件

## 现象
filemem 注入的 `[记忆报告]` / `[未决草稿]` footer 文案固定写着：
> 请调用 clarify（确认保存/丢弃/取消），确认后调 filemem_review_drafts(action=...) 批量处置

但当前宿主（Hermes TUI/桌面）**没有注册 clarify 工具**（全 63 个可用工具目录中无 clarify 实现；
workspace 里 test_clarify_tool.py 只是移动端构建测试，不是活体工具）。结果：
- 草稿裁决提示永远弹不出来（clarify 不存在，agent 无工具可调）
- owner 只能口头裁决 → agent 直接调 filemem_review_drafts

## 根因
footer 文案（`_bringback_clarify_footer` / notice content face，见 __init__.py:3944 / 4052）
硬编码假设宿主有 clarify 面板，未做"clarify 工具是否存在"的探测。

## 修复范围（source-only）
1. 在 footer 生成处（`_bringback_clarify_footer` + notice content face）加宿主能力探测：
   - 宿主有 clarify 工具 → 保留现文案（"请调用 clarify…"）
   - 宿主无 clarify 工具 → 改文案为"请口头裁决（确认保存/丢弃/取消），
     agent 将直接调 filemem_review_drafts(action=confirm/discard) 处置"
2. 探测方式：读宿主工具注册表（host tool registry）判断 'clarify' 是否在 LLM tools-array；
   探测失败/拿不到 → 默认走"口头裁决"分支（fail-soft，宁可少弹窗不多假设）。
3. 测试：tests/ 加一条 footer 文案分支单测（有 clarify / 无 clarify 两态）。

## 验收
- 无 clarify 的宿主上，footer 显示口头裁决文案，不再误导 agent 调不存在的 clarify
- 有 clarify 的宿主上，文案不变（回归通过）
- 宿主树零改动（红线）

## 签核
- [ ] owner 签核后推 stennyc/filemem
