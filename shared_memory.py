"""filemem 侧内联共享同步 — 只推不拉版 (owner 定稿 2026-10-22 三扇门重构).

中央库 = 一台 Postgres 的 memories 表 (全量沉淀池, append 语义).
设计原则 (owner 定稿, 不再讨论):

- 只推不拉: 本机 filemem 数据源 (唯一权威, 自动注入面) 只往外推, 中央行
  绝不回写本地. 本地数据源只被本机写, 永不被中央覆盖.
- 中央 = "别人的参考记忆": 只读可查 (检索面), 不自动注入, 不回写本地.
  confirmed 是行上一个状态列, 别的设备查"共识" = 带 confirmed=true 过滤.
- 门控简化: 单向后每行只有原 device 能写, 不存在"同 id 多设备写冲突".
  中央 upsert = 原设备幂等覆盖 + 非原设备拒写 (token 鉴权 + device 归属).
- 组件形态: 内联本文件, 无 daemon / 无独立插件 / 无中间层.
- 配置 = 各 profile config.yaml plugins.entries.filemem.shared_memory
  (缺段 / enabled=false -> 整个模块 no-op, 本地行为字节级不变).
- 版本存 metadata.shared_version: 仅防"原设备自己"重放/乱序 (单调), 不做
  跨设备 tie-break (那套已随双向语义删除).

fail-soft: 任何 PG 不可达 / 配置缺失 / psycopg2 缺失 / 异常 -> 静默跳过,
绝不阻塞 filemem 本地写路径 (本地数据源始终安全).

砍掉 (相对 S2-S5 双向版):
- pull 增量腿 (last_pull 水位 / 全量首拉 / 增量拉) — 不拉了
- _rowdict_to_local / _merge_into_local / _gate_accepts — 中央行不进本地 shard,
  无跨设备门控 (单向无冲突), 无本地镜像门控
- S5 的 epoch 门控对齐 / shared_agent_id 回写 (为"本地镜像中央门控"服务, 已废)
保留:
- 推腿: 本机行 -> 中央 memories (幂等, 带 device 归属 + token 鉴权)
- 检索面: 查中央 (只读 SQL), 结果不落本地
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

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
    """中央层 token 硬约束 (owner S4 拍 (a)): push 前调
    require_agent(agent_id, sha256(token))。token_hash 不入行数据
    (memories 不存 token), 只做调用时校验; 中央 RAISE ->
    _TokenRejected (客户端语义"拒", 非"连不上"), 本地写仍 fail-soft。"""
    import hashlib
    token_hash = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    cur = conn.cur()
    try:
        cur.execute("SELECT require_agent(%s, %s)", (agent_id, token_hash))
    except Exception as e:
        raise _TokenRejected(str(e)) from e


# ── 行 -> 中央列 映射 (version 走 metadata.shared_version, 仅防重放) ──────

def _row_to_rowdict(row: Dict[str, Any]) -> Dict[str, Any]:
    """shard 行 -> 中央 memories 列 dict (全量存, confirmed 是行上状态列).

    只推不拉: 门 1 全量沉淀, 门 2 confirmed 共识 = 同一张表的不同查询过滤,
    所以这里不再过滤 confirmed — 所有行都推, confirmed 值原样带上去。"""
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


# ── 主入口: 写行后调用 (只推, 不拉) ──────────────────────────────────────

_local_lock = threading.Lock()


def sync_on_write(
    provider,
    new_row_ids: List[str],
) -> Dict[str, Any]:
    """provider = FileMemProvider 实例 (用其 memories_dir/source_profile)。
    new_row_ids = 本次写路径刚落盘的行 id (全量, confirmed+unconfirmed 都推
    门 1 沉淀池; 空 -> no-op 返回 skipped)。

    只推不拉: 本函数只有推腿, 无 pull / merge / 水位。中央行绝不进本地。
    返回 {"status": "ok"|"skipped"|"rejected", "pushed": n}
    """
    cfg = _load_shared_memory_config()
    if cfg is None:
        return {"status": "skipped", "reason": "config-absent-or-disabled"}
    if not new_row_ids:
        return {"status": "skipped", "reason": "no-ids", "pushed": 0}
    with _local_lock:  # 跨 provider 实例共享一把锁: 同机多 profile 并发推不撕裂
        try:
            return _push_locked(provider, cfg, new_row_ids)
        except _TokenRejected as e:
            # S4: 中央 token 校验拒 (unknown/revoked) —— 语义上"拒", 不是
            # fail-soft 跳过: 记 WARNING 让 owner 看到 token 失效, 但仍不阻塞
            # 本地写路径 (owner 原则: 本地数据源始终安全)。
            logger.warning(f"shared_memory: central token rejected for agent "
                           f"{cfg.get('agent_id')}: {e}")
            return {"status": "rejected", "reason": "token-rejected"}
        except Exception as e:
            # fail-soft: PG 不可达 / 部分失败 -> 本地写已完成, 推静默跳过
            logger.warning(f"shared_memory sync_on_write no-op (fail-soft): {e}")
            return {"status": "skipped", "reason": f"fail-soft: {e}"}


def _push_locked(provider, cfg: Dict[str, Any], new_row_ids: List[str]) -> Dict[str, Any]:
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        # S4 owner 拍 (a): 中央层 token 硬约束 — 推前过 require_agent,
        # 不通过中央 RAISE -> 客户端转 _TokenRejected (语义"拒", 非 fail-soft
        # 连不上)。token 空且中央没 agent_registry 行时也 RAISE (unknown)。
        _central_auth(conn, agent_id, cfg.get("token", ""))
        cur = conn.cur()

        # S5 双审 critical: 中央 token 硬约束 — upsert 尾参 p_token_hash,
        # 中央函数体内 require_agent 强制校验 (非原设备 / 持 PG 密码可绕过).
        import hashlib
        token_hash = hashlib.sha256((cfg.get("token") or "").encode("utf-8")).hexdigest()
        # 只推: 本机新落盘行 -> 中央 memories (全量, 幂等 upsert)。
        # 无 pull 腿 / 无水位推进 — agent_watermarks.last_pull 不再使用。
        pushed = 0
        for rid in new_row_ids:
            rowmap = _read_local_row(provider.memories_dir, rid)
            if rowmap is None:
                continue
            d = _row_to_rowdict(rowmap)
            # 本地行的 updated_at/created_at 是 ISO 字符串 (filemem 约定);
            # metadata jsonb 推中央前做一次 json 序列化兜底 (datetime 保险)。
            def _js(v):
                from datetime import datetime as _dt2
                return _dt2(v).isoformat() if isinstance(v, _dt2) else v
            cur.execute(
                "SELECT upsert_memory_row(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (d["id"], d["mem_type"], d["content"], d["level"], d["confirmed"],
                 d["source_profile"] or provider.source_profile, agent_id,
                 d["version"], _js(d["updated_at"]), _js(d["created_at"]),
                 json.dumps(d["metadata"], ensure_ascii=False,
                            default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)),
                 token_hash),
            )
            pushed += 1
        conn.commit()
        return {"status": "ok", "pushed": pushed}
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


def _bump_version_for(row: Dict[str, Any]) -> int:
    """写路径落盘新行时调用: 返回该行的共享 version (单调 +1, 仅防重放)。
    新行 = 1 (backfill 后所有行起步 1), 变更 = 旧 + 1。不做跨设备 tie-break。"""
    old = int((row.get("metadata") or {}).get("shared_version") or 0)
    return old + 1


def backfill_versions(memories_dir: Path) -> int:
    """存量行回填 metadata.shared_version=1。仅对缺该键的行补, 已有不动;
    原地原子写 (tmp+os.replace)。返回补的行数。"""
    import os
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


# ── 检索面: 查中央 (只读, 结果不落本地) ─────────────────────────────────

def search_central(
    cfg: Dict[str, Any], query: str, confirmed_only: bool = True,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """查中央 memories (别人的参考记忆)。只读, 返回行 dict 列表, 不写本地。

    confirmed_only=True -> 门 2 共识面 (confirmed=true);
    confirmed_only=False -> 门 1 全量沉淀池。
    这是"中央=别人的可参考记忆"的读入口, 结果由调用方(agent 模型)决定用不用,
    绝不自动注入本地 auto-inject 面, 也绝不回写本地数据源。
    """
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        _central_auth(conn, agent_id, cfg.get("token", ""))
        cur = conn.cur()
        cols = ("SELECT id, mem_type, content, level, confirmed, source_profile, "
                "agent_id, version, updated_at, metadata FROM memories")
        sql = cols + " WHERE (content ILIKE %s OR metadata::text ILIKE %s)"
        params: List[Any] = [f"%{query}%", f"%{query}%"]
        if confirmed_only:
            sql += " AND confirmed = TRUE"
        sql += " ORDER BY updated_at DESC NULLS LAST LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
        return [
            dict(zip(
                ["id", "mem_type", "content", "level", "confirmed",
                 "source_profile", "agent_id", "version", "updated_at", "metadata"], r
            )) for r in rows
        ]
    except _TokenRejected:
        return []
    except Exception as e:
        logger.warning(f"shared_memory search_central no-op (fail-soft): {e}")
        return []
    finally:
        conn.close()
