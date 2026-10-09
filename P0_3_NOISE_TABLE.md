# P0-3: 噪声指纹表扩 3 确定性类 + 写侧 ingress 切同表（P0-1 保守零误杀）

## 一、改动总览（4 文件，宿主源码零改动）

| 文件 | 改动 |
|---|---|
| `decay.json` | `egress_noise` 块扩表（改表不改码）：`tool_dump_openers` en/zh += `["{\"total_count\":", "{\"success\":"]`（首批同值）；新键 `fragment_prefixes`（多语表，startswith 语义）首批 `["搞清楚了。我把情况说明清楚"]` |
| `consolidation/__init__.py` | `DECAY_DEFAULTS["egress_noise"]` 同步扩表（s2 键一致契约：decay.json ↔ DECAY_DEFAULTS 逐键一致，单侧扩表会击穿 `test_s2_egress_governance.py::test_decay_json_defaults_egress_noise_key_parity`） |
| `__init__.py` | ① 新模块谓词 `_noise_table_hit`（共享开表 helper：`tool_dump_openers` + `fragment_prefixes` 双表、纯 stdlib startswith、缺块/畸块 fail-soft 放行该腿）；② `_egress_noise_cut` 加 ⑤ 腿（fragment_prefixes startswith，与 ③ 同形表驱动、共走 `_noise_table_hit`）+ ② 腿对 `egress_noise` 非 dict 畸形补 fail-soft（pre-P0-3 畸形块会在 ② 腿 `en.get` 上抛，谓词本应 raise-proof）；③ `ingress_reject_class` 加参 `decay_cfg: Optional[Dict] = None`，raw_tool_output 腿改「3 键恒活（代码默认，零回归）**OR** 共享开表命中」；④ 调用点 `_ingress_rejection`（原 L2878）传 `getattr(self, "_decay", None)` |
| `tests/test_p0_3_noise_table.py` | 新测试 9 条，覆盖验收门 ①-⑦ + decay.json ↔ DECAY_DEFAULTS P0-3 键一致 |

**不变量（保守零误杀）**
- `_looks_like_tool_dump` 3 键单源**一字未动**（宿主源零改动的同时 ① 键头恒活）。
- superset 门：原 3 键拒存行（`{"output"`/`{"content"`/`{"error"` + `<untrusted_tool_result` 前缀）在表存在时**全部仍拒**。
- fragment 前缀仅 startswith（全句开头锚点），**绝不做子串匹配**——正文中间含锚点子串的正常行零误杀（验收门 ⑥ 锁死）。
- 零 LLM、纯 stdlib（startswith/len）；宿主包（`$HERMES_AGENT_SITE`）零 `.py` 改动。

## 二、写侧读侧同表（不再不对称）

P0-1 后读侧 egress 有配置表但写侧仍硬编码 3 键，缺口 3 类共 63 行（`{"total_count"` ×24 / `{"success"` ×38 / 过程碎片 ×1）漏进 `memories/*.json`。P0-3 后：

```
                    egress_noise (decay.json ↔ DECAY_DEFAULTS 双源逐键一致)
                              │  tool_dump_openers (en/zh, startswith)
                              │  fragment_prefixes (en/zh, startswith)
              ┌───────────────┴────────────────┐
      读侧 _egress_noise_cut            写侧 ingress_reject_class
      (③ 开表 + ⑤ 碎片表,             (raw_tool_output 腿:
       经 _noise_table_hit)           3 键恒活 OR _noise_table_hit)
              └───────────────┬────────────────┘
              同一张表、同一个 helper —— 两面永不漂移
```

写侧调用链：`sync_turn → _save_memory/_add_draft/_extract_memories → _ingress_rejection → ingress_reject_class(content, self._decay)`。`_decay` 缺失/无 egress_noise 块（如 P0-1 测试 fixture `{"lambda_days": 0.05}`）→ `decay_cfg=None` 等效 P0-1 纯代码行为（向后兼容门 ⑦ 锁死）。

## 三、fail-soft 矩阵（门 ⑤ 验证的畸形形状）

| 畸形 | ingress 写侧 | egress 读侧 |
|---|---|---|
| `egress_noise` 非 dict（如字符串） | 表腿放行（3 键仍活） | ②-⑤ 全腿放行（① 3 键恒活；pre-P0-3 会抛 AttributeError → 已修） |
| `tool_dump_openers`/`fragment_prefixes` 非 dict / 语言行非 list / 条目非 str | 该表 fail-soft 无命中 | 同上 |
| 块整体缺失（`None` / `{}`） | 无表命中 = P0-1 行为 | 无表命中 |

## 四、测试证据（py3.8 门禁 recipe 钉死）

```
cd $HERMES_HOME/plugins/filemem && \
PYTHONPATH=$HERMES_AGENT_SITE /usr/bin/python3 -m pytest tests -q -p no:cacheprovider
```

- **全套**：`484 passed / 13 failed`（P0-1 基线 460+15=475 条基线通过集 + 9 条 P0-3 新增 = 484，零丢失）。
- **13 条失败 = 同组 8 条环境假红全子集 + 5 条**：
  `test_config_tier_flock` ×5（flock 多进程竞态，环境性）+ `test_sa2_1_occurred_at` ×2（import audit）+ `test_sec_1_1_origin` ×1（import audit）+ `test_s1b_tick_wiring`/`test_s4_*`/`test_s6_gate_relax` ×3（no_host_package_changes_in_diff 环境子进程）+ `test_sa2_4_graph` ×1（perf 计数）——**与 P0-1/P0-2 基线 FAILED 集逐字节一致，零新增**（两次全量跑 diff 为空，flaky perf 条在 12↔13 间漂移属已知环境抖动，非本改动引入——该条失败集在 8 假红基线文档内）。
- **P0-3 单测 9/9 通过**：门 ① 三类缺口 ingress 拒存（quarantine + 0 池行 + ingress_reject 审计行）；② egress 同表 drop；③ 3 键旧行仍拒（superset）；④ 正常事实行字节级不变（零误杀）；⑤ 表畸形 fail-open（含 pre-P0-3 ② 腿 AttributeError 回归）；⑥ fragment 锚点子串在正文中间不误杀（startswith 语义）；⑦ decay_cfg=None / 无表块 = P0-1 纯代码行为。
- **宿主源零改动**：`find $HERMES_AGENT_SITE -newermt …` 期间零命中。

## 五、已知留口（非本卡范围）

- `tools/govern_memories.py` 的离线治理 `classify_row` 只读 `tool_dump_openers` 开表，未接 `fragment_prefixes`——离线行级治理对过程碎片类 63 行中的 1 行不覆盖（读侧 `_egress_noise_cut` 已覆盖，注入面安全）。若要把 63 行存量行清出池，需 P1-3 或治理工具补腿。
- 首批表值 `en == zh`（同值同表）是按 spec 钉死的；后续按语言分列扩展时只改 decay.json（+ DECAY_DEFAULTS 同步），谓词零改动。
