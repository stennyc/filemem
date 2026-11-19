# P3 复盘门审计 — 宿主面 auto-inject 滤过期 + 背景自动捞

- 审计对象: c621ccf (P3 宿主面, `__init__.py` +61/-4, 只动 `_prefetch_body` + `_reclaim_from_archive` 加 reason 参数)
- 审计基准: owner 终裁 2026-11-19 (REACTIVATION_DESIGN §1/§3) + S2/S3 数据面复盘门 PASS 结论
- 审计方式: 源码逐行 (不靠 test 自报) + 盘上独立 smoke (17 checks, 走真实 `prefetch` / `search_memories` 调用路径, 非复制 P3 块逻辑)
- 日期: 2026-11-19

## 审计点 1 — P3 auto 面滤过期语义: **PASS**

`__init__.py` L5054-5071 (P3 块, 在 O1 fold L5022-5029 之后、SESSION_ISOLATION_V2 confirmed-gate L5078+ 之前):

- L5062-5064: `reclaimed=True` 行 → 保留, 永不进入过期比较 (owner §3: 暂存浮出层是已过期源唯一入口)。
- L5066-5069: 非 reclaimed 行取顶层 `expires_at`, `_exp and _exp < _now_iso` → 滤出 (严格小于, 等于不过滤, 语义正确)。
- L5066: `(r.get("expires_at") or "").strip()` — 无 `expires_at` 老行 → `_exp` 为空 → 保留 (fail-open, 与 S1 `clean_reclaims` 的 undatable-keep 红线镜像, L11763 docstring)。
- 比较基准 L5060: `_utcnow()` naive-UTC, 与 L11654 存储格式 (naive ISO, S3 fold / cascade 同源) 一致。

盘上 smoke A1/A2/A3/A4 (scratch 脚本 `/root/.hermes/cache/scratch/p3_smoke_disk.py`):
过期 live 行不出 auto ctx; 未过期 live 行出; 无 expires_at 老行出; reclaimed 归档行出。全绿。

## 审计点 2 — 背景自动捞 reason 正确: **PASS**

- P3 调用点 L5055-5058: `_arch_hit = [r for r in results if r.get("reclaimed")]` →
  `self._reclaim_from_archive(_arch_hit, reason="auto_retrieve")`。
- `_reclaim_from_archive` L11938-11966: `reason` 参数透传给 `put_reclaim(reason=...)`;
  数据面调用点 L11998 (`_archive_cascade_fold` 内 S2 hook) 不传 reason → 走默认
  `reason: str = "explicit_search"` (L11939), 字节稳定 (与 c621ccf 前完全一致)。
- `put_reclaim` L11684-11758: 新行 `reclaim_reason=reason` (L11735/11749);
  RENEW 路径同 src_id 未过期时 L11722 翻 `reclaim_reason=reason` (续命同时翻来源, 后到面赢)。
- 盘上 smoke B2: 归档命中 → `reclaimed.json` 中 `reclaim_reason == "auto_retrieve"`;
  C2: 显式面路径 (数据面 S2 hook 形状) 默认 `explicit_search`; D6: 同 src_id 续命后
  行数不增、reason 翻 auto_retrieve。全绿。

## 审计点 3 — 显式搜面不受 P3 影响: **PASS**

- P3 块物理上在 `_prefetch_body` (L5029-5077); 显式面 `filemem_search` →
  `_tool_search` (L14049) → `_recall_and_format` (L13976, `obs_face="explicit"`) →
  `search_memories` (L10914)。`search_memories` 内无任何 `expires_at` 滤过期逻辑
  (全文件 grep: `expires_at` 只出现在 L5044/5066 P3 块与 L11460+ reclaim 区, 无显式面路径)。
- 显式面唯一与暂存相关的写: S3 fold 内 L11998 S2 hook (数据面, P3 前已存在,
  默认 `explicit_search`)。
- 盘上 smoke C1: 过期 live 行在显式面 `search_memories` 仍命中 (全量池, owner 终裁);
  C2 同面归档命中走 `explicit_search` 默认。全绿。

## 审计点 4 — reclaimed 行永不过滤: **PASS**

L5062-5065 `continue` 分支先于任何过期比较; 与 P3 test
`test_auto_face_filters_expired_live_rows_keeps_live_and_reclaimed` (test_p3_auto_face.py L255-277)
同语义。盘上 smoke A4: 归档行 (S3 fold 打 `reclaimed=True`) 出 auto ctx。绿。

## 审计点 5 — 源零 diff 不变量 (P3 也守): **PASS**

- P3 块唯一写路径: L5057 `_reclaim_from_archive` → `put_reclaim` → 只写
  `memories/reclaimed.json` (L11620-11625 `_reclaim_path`)。活池 shard / 归档 jsonl
  零写。
- 盘上 smoke D1: 归档 jsonl 桶 md5 前后全同; D1b: 活池行 id 集合不变。
  备注: auto face 末端有 **pre-P3** 的 P0-v2 `_reinforce_recall` (L5488 → L6746,
  命中 live 行 bump `reference_count`/`last_referenced` 落盘) — 非 P3 写入,
  已在 smoke 中排除并单独标注 (审计面只锁 P3 写集合 = 暂存投影)。
- live 行 (含过期) 永不被 stage: L5055 只收 `reclaimed=True` 行; 盘上 D4b:
  无归档命中时 `reclaimed.json` 不生成。绿。

## 审计点 6 — fail-soft 全链: **PASS**

- 暂存写失败: `put_reclaim` L11756-11758 捕获所有异常 → warning + `None`,
  `_reclaim_from_archive` L11967-11968 只计数成功项, P3 块拿到 0 也继续。
  盘上 D2/D3: 模拟 `_write_reclaims` 抛 OSError → 0 条写入、不抛异常、检索照常返回。绿。
- 活池行无 expires_at → 不过滤 (fail-open): L5066; 盘上 A3 绿。
- 归档桶坏行: S3 fold 的 `_read_archive_bucket` L11830-11838 逐行 try/except,
  坏行 skip, 全坏 → 空 (c621ccf 前已存在, P3 未改; 数据面复盘门已验)。

## 审计点 7 — 数据面/宿主面边界清晰: **PASS**

- c621ccf diff 面: 仅 `__init__.py` L5030-5077 插入 (P3 块) + L11938-11966
  (`_reclaim_from_archive` 加 reason 参数, 默认值保数据面字节稳定) + 新 test。
  `search_memories` 主体、S2/S3 数据面折叠、`_tool_search` 显式面均未动。
- 语义分离: auto 面 (prefetch) 滤过期 + 背景自动捞; 显式面 (retrieve) 全量池不滤。
  两侧红区由 P3 test 9/9 + 盘上 smoke 双锁。绿。

## 盘上 smoke 结果 (独立脚本, 非 pytest 自报)

脚本: `/root/.hermes/cache/scratch/p3_smoke_disk.py` (17 checks, 全 PASS):

```
[PASS] A1 auto 面过期 live 行滤出
[PASS] A3 auto 面 legacy 无 expires_at fail-open 保留
[PASS] A4 auto 面 reclaimed 归档行保留(暂存浮出层)
[PASS] A2 auto 面 live 未过期行保留 (cap 之上)
[PASS] B1 背景自动捞写暂存投影 (arch1)
[PASS] B2 暂存投影 reclaim_reason=auto_retrieve
[PASS] B3 暂存 schema 字段齐全 (id=recl_arch1_20261119123125)
[PASS] D1 源零 diff (归档 jsonl)
[PASS] D1b 活池行集合不变 (仅 pre-P3 P0-v2 计数 bump)
[PASS] C1 显式面不过滤: 过期 live 行仍命中 ids=[live_leg, arch1, live_exp, arch2, live_ok]
[PASS] C2 数据面 (显式搜) 默认 reason=explicit_search
[PASS] D2 暂存写失败 -> 0 条写入且不抛异常
[PASS] D3 暂存故障下检索照常返回
[PASS] D4 全过期 auto 面零命中
[PASS] D4b live 行永暂存: 无归档命中则 reclaimed.json 不生成
[PASS] D5 全过期显式面仍命中
[PASS] D6 同 src_id 续命不新增行, reason 翻 auto_retrieve
SMOKE VERDICT: PASS (all checks green)
```

## 不一致点

无。两处 smoke 首轮 FAIL 均为脚本侧误判, 已修正并留痕:
1. auto 面 `auto_recall_max=3` cap 把 fresh live 行挤出注入 ctx (pre-P3 机制,
   非 P3 块行为) → 改 dedicated query 单独验证 P3 keep 路径。
2. P0-v2 `_reinforce_recall` 在 auto face 末端 bump 活池行 `reference_count`
   (pre-P3, 非 P3 写入) → 源零 diff 比对面收窄到 P3 写集合 (暂存投影),
   bump 字段单独标注。

## sign-off 建议

P3 宿主面 7 个审计点全 PASS, 盘上 smoke 17/17 绿, 源零 diff 不变量守住
(P3 写集合 = 仅 `reclaimed.json` 暂存投影)。可收口。

verdict: PASS
