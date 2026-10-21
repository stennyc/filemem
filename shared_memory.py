"""S2: filemem 侧内联共享同步 (owner 定稿 2026-10-21, PRD /root/.hermes/docs/shared-memory-prd.md)

跨机共享 confirmed 记忆: 本机 <base>/filemem/memories 5 shard 与一台中央
Postgres (memories 表) 双向同步。设计原则 (owner 拍板, 不再讨论):

- 组件形态: 内联几十行, 无 daemon / 无独立插件 / 无中间层。本文件就是
  filemem 仓自带的 shared_memory.py, kb 仓会内联一份同构实现 (X 方案,
  门控逻辑两仓各一份, S5 双审专查行为一致性)。
- 检索 = B' 写前拉后推: 只在"写 confirmed 行"时刻 pull 增量 + 合入本地
  shard + push 本机 confirmed 行; 纯搜永不碰中央。
- 门控 = 真·B: 行 metadata.shared_version (本地单调) 大者赢 -> 同 version
  比 updated_at -> 再比 agent_id 字典序小者赢; 被覆盖旧版进 memory_history。
  与中央 upsert_memory_row PL/pgSQL 函数同语义 (S1 已 30/30 实测)。
- 配置 = 各 profile config.yaml plugins.entries.filemem.shared_memory
  (全 dashboard, 缺段 / enabled=false -> 整个模块 no-op, 字节级不变)。
- 版本存 metadata.shared_version: 不改 MemoryItem 既有字段 / 行结构,
  零侵入 filemem 行模型。

fail-soft: 任何 PG 不可达 / 配置缺失 / psycopg2 缺失 / 异常 -> 静默跳过,
绝不阻塞 filemem 本地写路径 (owner 原则: 本地数据源始终安全)。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 统一时间戳时区基准 (S5: 门控比较走 epoch, naive 时间戳按 UTC 解释,
# 与中央 TIMESTAMPTZ 的 UTC 存储同基准 — 跨机时钟纪律要求 NTP)。
try:
    from datetime import timezone as _tz_mod
    _UTC = _tz_mod.utc
except Exception:  # pragma: no cover
    _UTC = None

# 连接用参数 (fail-soft, 每次调用都读 —— 不缓存, 避免 config 改动后旧连接)
_MAX_CONNECT_ATTEMPTS = 1  # 不做重试风暴: 连不上就 no-op, 下次写再试


def _load_shared_memory_config() -> Optional[Dict[str, Any]]:
    """读 plugins.entries.filemem.shared_memory 段。

    返回:
      None  -> 缺段 / enabled != true / 读 config 失败 (no-op)
      dict   -> 含 agent_id + pg{host,port,db,user,password}
    配置缺失 = 字节级不变 (owner 验收硬指标)。
    """
    try:
        from hermes_cli.config import load_config, load_config_readonly
    except Exception:
        return None
    try:
        config = load_config_readonly()
    except Exception:
        try:
            config = load_config()
        except Exception:
            return None
    try:
        entries = config.get("plugins", {}).get("entries", {})
        fm = entries.get("filemem") or {}
        sm = fm.get("shared_memory")
        if not isinstance(sm, dict) or not sm.get("enabled"):
            return None
        pg = sm.get("pg") or {}
        agent_id = str(sm.get("agent_id") or "").strip()
        if not agent_id or not pg.get("host") or not pg.get("db"):
            return None
        return {
            "agent_id": agent_id,
            "token": str(sm.get("token") or ""),
            "pg": {
                "host": str(pg.get("host")),
                "port": int(pg.get("port") or 5432),
                "db": str(pg.get("db")),
                "user": str(pg.get("user")),
                "password": str(pg.get("password") or ""),
            },
        }
    except Exception as e:
        logger.warning(f"shared_memory: config read failed, no-op: {e}")
        return None


class _PgConn:
    """最小 PG 连接封装 (psycopg2 惰性 import, 缺失 -> no-op)。"""

    def __init__(self, pg: Dict[str, Any]):
        import psycopg2  # 惰性: 未装则整条路径 no-op, 不炸 filemem
        self._pg = pg
        self._conn = psycopg2.connect(
            host=pg["host"], port=int(pg["port"]), dbname=pg["db"],
            user=pg["user"], password=pg["password"],
            connect_timeout=5,
        )
        self._conn.autocommit = False

    def cur(self):
        return self._conn.cursor()

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


class _TokenRejected(Exception):
    """中央 agent_registry 拒 (unknown/revoked token)。

    S4 owner 拍板 (a): token 校验在中央 SQL 函数层 (require_agent),
    客户端 fail-soft 捕获后仍不阻塞本地写, 但语义上区分"拒"与"连不上"。"""
    pass


def _central_auth(conn, agent_id: str, token: str) -> None:
    """中央层 token 硬约束 (owner S4 拍 (a)): push/pull 前调
    require_agent(agent_id, sha256(token))。token_hash 不入行数据
    (memories/kb_docs 不存 token), 只做调用时校验; 中央 RAISE ->
    _TokenRejected (客户端语义"拒", 非"连不上"), 本地写仍 fail-soft。"""
    import hashlib
    token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    cur = conn.cur()
    try:
        cur.execute("SELECT require_agent(%s, %s)", (agent_id, token_hash))
    except Exception as e:
        # require_agent 的 RAISE EXCEPTION (unknown/revoked) 是 SQL 层硬约束,
        # psycopg2 报为 error 22000 raise_exception。这里把"中央拒"与
        # "连接/网络异常"区分: 只转中央业务拒 (SQL 执行到 require_agent),
        # 连接断 (connect 阶段) 已在外层 _PgConn 抛, 不在此捕获。
        raise _TokenRejected(str(e)) from e


# ── 行 <-> 中央列 映射 (version 走 metadata.shared_version) ─────────────────

def _row_to_rowdict(row: Dict[str, Any]) -> Dict[str, Any]:
    """shard 行 -> 中央列 dict (只取 confirmed 语义相关列)。"""
    meta = row.get("metadata") or {}
    return {
        "id": row.get("id"),
        "mem_type": row.get("type"),
        "content": row.get("content", ""),
        "level": row.get("level"),
        "confirmed": bool(meta.get("confirmed", False)),
        "source_profile": meta.get("source_profile"),
        "version": int(meta.get("shared_version") or 1),
        "updated_at": row.get("updated_at"),
        "created_at": row.get("created_at"),
        "metadata": meta,
    }


def _rowdict_to_local(d: Dict[str, Any]) -> Dict[str, Any]:
    """中央列 dict -> 本地 shard 行 (保持 filemem 行结构, version 塞回
    metadata.shared_version; 不引入任何 MemoryItem 之外的新顶层键)。
    PG 的 TIMESTAMPTZ 被 psycopg2 转成 datetime —— 必须转回 ISO 字符串,
    否则本地 json.dump 炸 (filemem 行里的时间戳全是 ISO str, 不是 datetime)。"""
    from datetime import datetime as _dt
    meta = d.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    meta = dict(meta)
    meta["shared_version"] = int(d.get("version") or 1)
    # metadata jsonb 里的时间戳也可能被 PG 还成 datetime —— 全量清一遍
    def _norm(v):
        if isinstance(v, _dt):
            return v.isoformat()
        return v
    meta = {k: _norm(v) for k, v in meta.items()}
    def _ts(v):
        if isinstance(v, _dt):
            return v.isoformat()
        return v
    return {
        "id": d.get("id"),
        "type": d.get("mem_type"),
        "content": d.get("content", ""),
        "level": d.get("level"),
        "created_at": _ts(d.get("created_at")),
        "updated_at": _ts(d.get("updated_at")),
        "metadata": meta,
    }


# ── 本地门控合入 (与中央 PL/pgSQL upsert_memory_row 同语义) ─────────────────

def _gate_accepts(
    cur_version: Optional[int], cur_updated, cur_agent: Optional[str],
    new_version: int, new_updated, new_agent: str,
) -> bool:
    """中央门控在本地侧的镜像: 新行是否胜出 (本地 shard 已有 cur 行时)。
    version > -> 同 version 比 updated_at -> 再比 agent_id 字典序小者赢。

    S5 双审 critical 修复: 时间戳比较统一走 epoch (datetime.fromisoformat),
    不再用字符串字典序 (ISO str 的时区/微秒位数差异会让"同一瞬时"判为不等,
    与中央 TIMESTAMPTZ 瞬时比较 diverge = split-brain, R6 同型)。
    无效/缺时间戳 -> None 视为 -infinity (与中央 COALESCE(x,'-infinity') 同语义)。"""
    def _epoch(v):
        """-> float epoch (UTC) 或 None (缺/无效)。datetime 对象也归一。"""
        if v is None:
            return None
        from datetime import datetime as _dt
        try:
            if isinstance(v, _dt):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=_UTC)
                return v.timestamp()
            s = str(v)
            if not s:
                return None
            d = _dt.fromisoformat(s.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=_UTC)
            return d.timestamp()
        except Exception:
            return None
    cur_epoch = _epoch(cur_updated)
    new_epoch = _epoch(new_updated)
    NEG_INF = float("-inf")
    c = cur_epoch if cur_epoch is not None else NEG_INF
    n = new_epoch if new_epoch is not None else NEG_INF
    if new_version > (cur_version or 0):
        return True
    if new_version == (cur_version or 0):
        if n > c:
            return True
        if n == c:
            return new_agent < (cur_agent or "")
    return False


def _merge_into_local(
    memories_dir: Path, incoming: List[Dict[str, Any]], agent_id: str,
) -> int:
    """把中央拉回的增量行按门控合入本地 5 shard。返回本地真被覆盖的行数。

    合入规则 (与 S1 G1-G4 同语义):
      - 本地无此 id -> 直接写本地
      - 本地有 -> 门控接受才覆盖 (中央行的 agent_id 记在 metadata.source_profile
        的同行, 门控比较用中央行的 agent_id)
      - 覆盖时本地旧行不另开审计 (中央 memory_history 已记); 本地只是数据面
    """
    if not incoming:
        return 0
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for d in incoming:
        by_type.setdefault(str(d.get("mem_type") or "fact"), []).append(d)
    clobbered = 0
    for mem_type, rows in by_type.items():
        mem_file = memories_dir / f"{mem_type}.json"
        if not mem_file.exists():
            continue
        with open(mem_file, "r", encoding="utf-8") as f:
            items = json.load(f)
        local_by_id = {it.get("id"): it for it in items}
        changed = False
        for d in rows:
            rid = d.get("id")
            cur = local_by_id.get(rid)
            if cur is None:
                items.append(_rowdict_to_local(d))
                local_by_id[rid] = _rowdict_to_local(d)
                changed = True
                continue
            cur_meta = cur.get("metadata") or {}
            new_meta = (d.get("metadata") or {})
            if _gate_accepts(
                int(cur_meta.get("shared_version") or 0),
                cur.get("updated_at"), cur_meta.get("source_profile"),
                int(new_meta.get("shared_version") or 1),
                d.get("updated_at"), agent_id,
            ):
                merged = _rowdict_to_local(d)
                merged["metadata"]["source_profile"] = cur_meta.get("source_profile") or agent_id
                local_by_id[rid] = merged
                for i, it in enumerate(items):
                    if it.get("id") == rid:
                        items[i] = merged
                        break
                clobbered += 1
                changed = True
        if changed:
            tmp = mem_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, mem_file)
    return clobbered


# ── 主入口: 写 confirmed 行后调用 (写前拉后推的"推"侧 + 读侧) ──────────────

_local_lock = threading.Lock()


def sync_on_write(
    provider,
    new_confirmed_ids: List[str],
) -> Dict[str, Any]:
    """provider = FileMemProvider 实例 (用其 hermetis_home/memories_dir/source_profile)。
    new_confirmed_ids = 本次写路径刚落盘的行 id (可为空: 纯拉兜底也走这里)。

    返回 {"status": "skipped"|"ok", "pulled": n, "pushed": n, "clobbered_local": n}
    """
    cfg = _load_shared_memory_config()
    if cfg is None:
        return {"status": "skipped", "reason": "config-absent-or-disabled"}
    with _local_lock:  # 跨 provider 实例共享一把锁: 同机多 profile 并发写不撕裂
        try:
            return _sync_locked(provider, cfg, new_confirmed_ids)
        except _TokenRejected as e:
            # S4: 中央 token 校验拒 (unknown/revoked) —— 语义上"拒", 不是
            # fail-soft 跳过: 记 WARNING 让 owner 看到 token 失效, 但仍不阻塞
            # 本地写路径 (owner 原则: 本地数据源始终安全)。
            logger.warning(f"shared_memory: central token rejected for agent "
                           f"{cfg.get('agent_id')}: {e}")
            return {"status": "rejected", "reason": "token-rejected"}
        except Exception as e:
            # fail-soft: PG 不可达 / 部分失败 -> 本地写已完成, 同步静默跳过
            logger.warning(f"shared_memory sync_on_write no-op (fail-soft): {e}")
            return {"status": "skipped", "reason": f"fail-soft: {e}"}


def _sync_locked(provider, cfg: Dict[str, Any], new_confirmed_ids: List[str]) -> Dict[str, Any]:
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        # S4 owner 拍 (a): 中央层 token 硬约束 — 拉/推前先过 require_agent,
        # 不通过中央 RAISE -> 客户端转 _TokenRejected (语义"拒", 非 fail-soft
        # 连不上)。token 空且中央没 agent_registry 行时也 RAISE (unknown)。
        _central_auth(conn, agent_id, cfg.get("token", ""))
        kind = "memory"
        cur = conn.cur()

        # 1) 水位线: 本机该 kind 的 last_pull (缺 -> 全量拉, 首次部署)
        cur.execute(
            "SELECT last_pull FROM agent_watermarks WHERE agent_id=%s AND kind=%s",
            (agent_id, kind),
        )
        row = cur.fetchone()
        since = row[0] if row else None

        # 2) pull 增量: updated_at > since (首次 since=None -> 全量, 带 LIMIT 防 50 agent 首推风暴)
        # S5: 只拉 confirmed=TRUE 行 (中央 CHECK 约束 + "只共享 confirmed" 原则).
        if since is None:
            cur.execute(
                "SELECT id, mem_type, content, level, confirmed, source_profile, agent_id, "
                "version, updated_at, created_at, metadata FROM memories "
                "WHERE updated_at IS NOT NULL AND confirmed = TRUE "
                "ORDER BY updated_at LIMIT 5000"
            )
        else:
            # psycopg2 把 since (last_pull, TIMESTAMPTZ) 读回的是 datetime;
            # 本地行的 updated_at 是 ISO str — 必须统一成 datetime 再比
            # (跨机时钟纪律: 本地/中央都是带时区 ISO, 可直接比)。
            from datetime import datetime as _dtc, timezone as _tzc
            if isinstance(since, str):
                since = _dtc.fromisoformat(since.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=_tzc.utc)
            cur.execute(
                "SELECT id, mem_type, content, level, confirmed, source_profile, agent_id, "
                "version, updated_at, created_at, metadata FROM memories "
                "WHERE updated_at > %s AND confirmed = TRUE "
                "ORDER BY updated_at LIMIT 5000",
                (since,),
            )
        incoming_raw = cur.fetchall()
        incoming = [
            dict(zip(
                ["id", "mem_type", "content", "level", "confirmed", "source_profile",
                 "agent_id", "version", "updated_at", "created_at", "metadata"], r
            )) for r in incoming_raw
        ]

        # 3) 门控合入本地 shard (本地旧行门控, 与中央 PL/pgSQL 同语义)
        clobbered = _merge_into_local(provider.memories_dir, incoming, agent_id)

        # 4) push 本机新 confirmed 行 (走中央 PL/pgSQL upsert_memory_row, 门控在 SQL 层)
        # S5: 客户端过滤 confirmed — 只推 metadata.confirmed==True 行 (未确认行
        # 留在本地 pending, 不进中央; 与 "只共享 confirmed 行" 原则对齐).
        pushed = 0
        for rid in new_confirmed_ids:
            rowmap = _read_local_row(provider.memories_dir, rid)
            if rowmap is None:
                continue
            d = _row_to_rowdict(rowmap)
            if not d.get("confirmed"):
                continue  # 未确认行: 不推中央 (本地保留, 待 confirm 后下次推)
            # 本地行的 updated_at/created_at 是 ISO 字符串 (filemem 约定);
            # metadata jsonb 推中央前做一次 json 序列化兜底 (datetime 已在
            # _rowdict_to_local 清过, 这里是 push 侧的本地行, 再保险一次)。
            def _js(v):
                from datetime import datetime as _dt2
                return _dt2(v).isoformat() if isinstance(v, _dt2) else v
            cur.execute(
                "SELECT upsert_memory_row(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (d["id"], d["mem_type"], d["content"], d["level"], d["confirmed"],
                 d["source_profile"] or provider.source_profile, agent_id,
                 d["version"], _js(d["updated_at"]), _js(d["created_at"]),
                 json.dumps(d["metadata"], ensure_ascii=False,
                            default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o))),
            )
            pushed += 1

        # 5) 推水位线 (本机该 agent+kind 的 last_pull/last_push)
        if incoming:
            last_ts = max((d["updated_at"] for d in incoming if d.get("updated_at")), default=None)
            if last_ts:
                cur.execute(
                    "INSERT INTO agent_watermarks (agent_id, kind, last_pull, last_push, last_version_seen) "
                    "VALUES (%s, %s, %s, now(), (SELECT max(version) FROM memories)) "
                    "ON CONFLICT (agent_id, kind) DO UPDATE SET "
                    "last_pull = EXCLUDED.last_pull, last_push = now(), "
                    "last_version_seen = GREATEST(agent_watermarks.last_version_seen, EXCLUDED.last_version_seen)",
                    (agent_id, kind, last_ts),
                )
        conn.commit()
        return {"status": "ok", "pulled": len(incoming), "pushed": pushed,
                "clobbered_local": clobbered}
    finally:
        conn.close()


def _read_local_row(memories_dir: Path, rid: str) -> Optional[Dict[str, Any]]:
    """从 5 shard 按 id 找到本地行 (写路径刚落盘, 必在)。"""
    for mem_type in ("fact", "preference", "decision", "task", "plan"):
        mem_file = memories_dir / f"{mem_type}.json"
        if not mem_file.exists():
            continue
        with open(mem_file, "r", encoding="utf-8") as f:
            items = json.load(f)
        for it in items:
            if it.get("id") == rid:
                it.setdefault("type", mem_type)
                return it
    return None


def backfill_versions(memories_dir: Path) -> int:
    """存量 955+ 行回填 metadata.shared_version=1 (PRD D9)。
    仅对缺该键的行补, 已有不动; 原地原子写 (tmp+os.replace)。返回补的行数。"""
    touched = 0
    for mem_type in ("fact", "preference", "decision", "task", "plan"):
        mem_file = memories_dir / f"{mem_type}.json"
        if not mem_file.exists():
            continue
        with open(mem_file, "r", encoding="utf-8") as f:
            items = json.load(f)
        changed = False
        for it in items:
            meta = it.setdefault("metadata", {})
            if "shared_version" not in meta:
                meta["shared_version"] = 1
                changed = True
                touched += 1
        if changed:
            tmp = mem_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, mem_file)
    return touched


def _bump_version_for(row: Dict[str, Any]) -> int:
    """写路径落盘新 confirmed 行时调用: 返回该行的共享 version (单调 +1)。
    新行 = 1 (backfill 后所有行起步 1), 变更 = 旧 + 1。"""
    old = int((row.get("metadata") or {}).get("shared_version") or 0)
    return old + 1
