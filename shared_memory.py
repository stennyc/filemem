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
import re
import sys
import threading
from pathlib import Path

# Splice the shared dev-venv site-packages dir onto sys.path BEFORE psycopg2
# is ever actually needed (it is lazily imported inside _PgConn.__init__,
# but the import surface for it must exist by then). Makes the splice a
# guaranteed side effect of importing shared_memory itself, regardless of
# which face (host in-process via __init__.py, a bare test import, a future
# caller) imports it. No-op when the host already resolves psycopg2.
import dep_bootstrap

dep_bootstrap.ensure_pg_dep_paths()
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 连接用参数 (fail-soft, 每次调用都读 —— 不缓存, 避免 config 改动后旧连接)
_MAX_CONNECT_ATTEMPTS = 1  # 不做重试风暴: 连不上就 no-op, 下次写再试

# Gated PG-face auto-install (owner 2, 2026-11-17): per-process guard — the
# config is re-read on EVERY shared-memory operation (by design, no cache),
# so the installer trigger must fire at most once per process; the 6h
# cooldown marker inside dep_bootstrap throttles across processes. The
# trigger sits BEHIND the enabled-gate, so hosts that never enable PG pay
# zero cost (no probe, no marker, no pip). Never raises.
_PG_AUTOINSTALL_TRIGGERED = False


def _maybe_trigger_pg_face_autoinstall() -> None:
    global _PG_AUTOINSTALL_TRIGGERED
    if _PG_AUTOINSTALL_TRIGGERED:
        return
    _PG_AUTOINSTALL_TRIGGERED = True
    try:
        dep_bootstrap.ensure_pg_face_deps_installed()
    except Exception as e:
        logger.debug("shared_memory: pg-face auto-install trigger skipped: %s", e)


def _load_shared_memory_config() -> Optional[Dict[str, Any]]:
    """读 plugins.entries.filemem.shared_memory 段。

    返回:
      None  -> 缺段 / enabled != true / 读 config 失败 (no-op)
      dict   -> 含 agent_id + pg{host,port,db,user,password}
    配置缺失 = 字节级不变 (owner 验收硬指标)。

    (a) 直读 config.yaml 文件, 不借 hermes_cli.config / ruamel (owner 定稿
    2026-10-23): host tools-python 缺 ruamel/psycopg2/pyyaml, 依赖路径靠
    dep_bootstrap 自动检测 (当前解释器自己的 site-packages, 不写死任何具体
    venv 路径), 配置解析优先级:
      1. ruamel.yaml 可用 -> 用它解析 (支持 anchor/alias)
      2. pyyaml 可用 -> 用它 (支持 anchor/alias)
      3. 都没有 (host tools python) -> 纯文本最小解析
    配置段结构固定 (agent_id + enabled + pg{host,port,db,user,password} +
    token), 纯文本解析足够; filemem 段是 pg anchor (&id001) 的定义处, 直接
    取定义处五键值, 无需解析 alias。"""
    raw = _read_config_yaml_text()
    if raw is None:
        return None
    parsed: Optional[Dict[str, Any]] = None
    try:
        from ruamel.yaml import YAML
        yaml_ = YAML(typ="safe")
        parsed = yaml_.load(raw) or {}
    except Exception:
        try:
            import yaml as _pyyaml
            parsed = _pyyaml.safe_load(raw) or {}
        except Exception:
            parsed = _parse_shared_memory_minimal(raw)
    try:
        entries = (parsed or {}).get("plugins", {}).get("entries", {})
        fm = entries.get("filemem") or {}
        settings = fm.get("settings") or {}
        # Three read paths, in priority order (kbs parity):
        # 1. Panel-saved scalar keys (.settings.sm_enabled, .settings.sm_agent_id, …)
        #    — the plugin.yaml config_schema shape (independent fields).
        # 2. Legacy .settings.shared_memory (dict) — intermediate shape.
        # 3. Legacy .shared_memory (dict) — original direct-entry layout.
        if "sm_enabled" in settings:
            enabled = bool(settings.get("sm_enabled", True))
            if not enabled:
                return None
            agent_id = str(settings.get("sm_agent_id") or "").strip()
            token = str(settings.get("sm_token") or "")
            pg = {
                "host": str(settings.get("sm_pg_host") or ""),
                "port": int(settings.get("sm_pg_port") or 5432),
                "db": str(settings.get("sm_pg_db") or ""),
                "user": str(settings.get("sm_pg_user") or ""),
                "password": str(settings.get("sm_pg_password") or ""),
            }
        else:
            sm = settings.get("shared_memory")
            if not isinstance(sm, dict):
                sm = fm.get("shared_memory")
            if not isinstance(sm, dict) or not sm.get("enabled"):
                return None
            agent_id = str(sm.get("agent_id") or "").strip()
            token = str(sm.get("token") or "")
            pg_raw = sm.get("pg") or {}
            pg = {
                "host": str(pg_raw.get("host") or ""),
                "port": int(pg_raw.get("port") or 5432),
                "db": str(pg_raw.get("db") or ""),
                "user": str(pg_raw.get("user") or ""),
                "password": str(pg_raw.get("password") or ""),
            }
        if not agent_id or not pg["host"] or not pg["db"]:
            return None
        _maybe_trigger_pg_face_autoinstall()  # 门禁在此: 只有 enabled 且配置完整才触发
        return {
            "agent_id": agent_id,
            "token": token,
            "pg": pg,
        }
    except Exception as e:
        logger.warning(f"shared_memory: config parse failed, no-op: {e}")
        return None


def _read_config_yaml_text() -> Optional[str]:
    """定位并读 config.yaml 全文 (owner 定稿 2026-10-23, 机器级 agent_id).

    模型: filemem 数据源全机统一一份, 1 profile = 1 agent 但差异下沉到行级
    source_profile 列; agent_id/token 是机器级 (跨机 50 台 = 50 个 agent_id),
    不是 per-profile。所以本机读顺序:

    1. HERMES_CONFIG env (宿主显式指定, 最高优先)
    2. <HERMES_HOME>/config.yaml  (global, 机器级 — multiplex 宿主无 HERMES_PROFILE
       env, 本机 12 profile 共用一份机器级配置就配在这, 全机命中)
    3. <HERMES_HOME>/profiles/<name>/config.yaml (仅当 HERMES_PROFILE 有值且
       global 读不到/缺 shared_memory 段时才用, 兜底部署不规范的机器)

    全缺 -> None (no-op, 本地字节级不变)。"""
    import os
    explicit = os.environ.get("HERMES_CONFIG", "").strip()
    if explicit and os.path.isfile(explicit):
        try:
            with open(explicit, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        return None
    # global 优先 (机器级 agent_id 的本机正解)
    candidates = [f"{home}/config.yaml"]
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        candidates.append(f"{home}/profiles/{profile}/config.yaml")
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                continue
    return None


def _parse_shared_memory_minimal(raw: str) -> Dict[str, Any]:
    """纯文本最小解析: 只提取 plugins.entries.filemem 的共享内存配置。

    两种配置形态 (panel / legacy 写路径都产):
      1. panel 标量形态: filemem.settings.sm_* — 8 字段 manifest (kbs 同构),
         dashboard 插件设置面板写入处 (2026-11-17 sm_* manifest 定稿)。
      2. legacy 嵌套形态: filemem.shared_memory {enabled, agent_id, token, pg{...}}。
    逐行扫, 跟踪缩进 (plugins: -> entries: -> filemem: -> settings:/shared_memory:),
    配置块内取标量键 (sm_enabled 归一为 bool — 字符串 'false' 在 loader 的
    bool() 下会被误读为 enabled), pg 子块的五键原样取。pg 块可能是 anchor 定义
    (pg: &id001) — 取值键正常取; alias (pg: *id001) 在 filemem 段不会出现
    (filemem 是 &id001 的定义处)。

    2026-11-18 补形态 1: 旧解析只认形态 2 — 宿主解释器缺 yaml 库时, 面板落盘的
    sm_* 形态被读成"配置缺", enabled 门禁静默关闭, 而 PG face 自动安装
    (dep_bootstrap, 门禁=配置读成功后才触发) 死锁: 无 yaml -> 解析器读不到 sm_*
    -> 门禁 no-op -> 永不装 pyyaml -> 永久盲。"""
    import re
    sm_block: Dict[str, Any] = {}
    settings_block: Dict[str, Any] = {}
    pg_block: Dict[str, Any] = {}
    in_plugins = in_entries = in_filemem = False
    in_sm = in_pg = in_settings = False
    _sm_indent = 0
    _settings_indent = 0
    _entries_indent = 0
    _SM_KEYS = {"enabled", "agent_id", "token"}
    _PG_KEYS = {"host", "port", "db", "user", "password"}
    _SM_PANEL_KEYS = {"sm_enabled", "sm_agent_id", "sm_token",
                      "sm_pg_host", "sm_pg_port", "sm_pg_db",
                      "sm_pg_user", "sm_pg_password"}

    def _scalar(v: str):
        s = v.strip()
        if s.lower() in ("true", "yes"):
            return True
        if s.lower() in ("false", "no"):
            return False
        return s

    for line in raw.splitlines():
        stripped = line.rstrip()
        if not in_plugins:
            if re.match(r"^plugins:\s*$", stripped):
                in_plugins = True
            continue
        if not in_entries:
            m = re.match(r"^(\s+)entries:\s*$", stripped)
            if m:
                in_entries = True
                _entries_indent = len(m.group(1))
            continue
        if not in_filemem:
            m = re.match(r"^(\s+)(\S+):\s*$", stripped)
            if m and len(m.group(1)) == _entries_indent + 2 and m.group(2) == "filemem":
                in_filemem = True
            continue
        m = re.match(r"^(\s+)(\S+):(.*)$", stripped)
        if not m:
            continue
        indent, key, rest = len(m.group(1)), m.group(2), m.group(3)
        # 2026-11-18: panel 标量块 (settings.sm_*)。浅于 settings 块的行
        # 关块后【落回下方 in_sm 检查】(同一行可能是 legacy shared_memory:
        # 的块头); 同缩进的非 sm_* 键 (embedding_model 等) 跳过不关块。
        if in_settings:
            if indent < _settings_indent:
                in_settings = False
            elif key in _SM_PANEL_KEYS and rest.strip():
                settings_block[key] = _scalar(rest)
                continue
            else:
                continue
        if not in_sm:
            if key == "settings" and not rest.strip():
                in_settings = True
                _settings_indent = indent
                continue
            if key == "shared_memory" and not rest.strip():
                in_sm = True
                _sm_indent = indent
                continue
            if indent <= _entries_indent + 2:
                # left the filemem block
                break
            continue
        # in_sm
        if in_pg:
            if key in _PG_KEYS and rest.strip():
                pg_block[key] = _scalar(rest)
            elif key not in _PG_KEYS and indent <= _sm_indent + 2:
                in_pg = False
            continue
        if key in _SM_KEYS and rest.strip():
            sm_block[key] = _scalar(rest)
        elif key == "pg" and not pg_block:
            in_pg = True
            sm_block["pg"] = pg_block
        # leaving shared_memory
        if indent <= _sm_indent and key not in _SM_KEYS and key != "pg":
            break
    # 两种形态可同时存在 (panel 层优先 — loader 先查 sm_enabled):
    fm_block: Dict[str, Any] = {}
    if settings_block:
        fm_block["settings"] = settings_block
    if in_sm:
        sm_block.setdefault("pg", pg_block)
        fm_block["shared_memory"] = sm_block
    if not fm_block:
        return {}
    return {"plugins": {"entries": {"filemem": fm_block}}}


def _ensure_pg_dep_paths() -> None:
    """Make psycopg2 importable from the running interpreter.

    Superseded by dep_bootstrap.ensure_pg_dep_paths (shared single source of
    truth, 2026-11-11) — kept as a thin re-export wrapper so any existing
    caller/referencer of this exact name still resolves; the actual splice
    logic now lives in dep_bootstrap."""
    import dep_bootstrap
    dep_bootstrap.ensure_pg_dep_paths()


_ensure_pg_dep_paths()


class _PgConn:
    """最小 PG 连接封装 (psycopg2 惰性 import, 缺失 -> no-op)。

    缺 driver 时 (做法 3, owner 2026-11-12): 不再静默让 ModuleNotFoundError
    向上冒到调用方 fail-soft (那样只打 'No module named psycopg2', 用户不知
    要装什么)。这里单独 catch ImportError, 打一条【可执行修复指令】
    (<sys.executable> -m pip install psycopg2-binary), 再 raise 保留原
    fail-soft 语义 (调用方 except 接住 -> status=skipped, 本地面零影响)。
    """

    def __init__(self, pg: Dict[str, Any]):
        try:
            import psycopg2  # 惰性: 未装则走下面的 ImportError 引导面
        except ImportError as _imp_err:
            # 缺 driver = 中央池门关着。给一条人/运维能直接复制跑的修复指令,
            # 而非裸 ModuleNotFoundError (owner 做法 3: 声明 + 友好引导)。
            logger.warning(
                "shared_memory: psycopg2 driver missing (central pool gated "
                "off, local file face unaffected) — fix: %s -m pip install "
                "psycopg2-binary (see requirements.txt); central pool stays "
                "fail-soft until then", sys.executable)
            raise
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


def create_task(cfg: Dict[str, Any], agent_id: str, task_name: str) -> Optional[Dict[str, Any]]:
    """门 3: 中央发号 (全局唯一 task_ref, 50 设备跨机不撞). 只读鉴权 + 发号."""
    conn = _PgConn(cfg["pg"])
    try:
        _central_auth(conn, agent_id, cfg.get("token", ""))
        import hashlib
        token_hash = hashlib.sha256((cfg.get("token") or "").encode("utf-8")).hexdigest()
        cur = conn.cur()
        cur.execute("SELECT create_task(%s, %s, %s)", (agent_id, task_name, token_hash))
        row = cur.fetchone()
        conn.commit()
        return {"task_ref": row[0]} if row else None
    except _TokenRejected as e:
        logger.warning(f"shared_memory create_task token rejected: {e}")
        return None
    except Exception as e:
        logger.warning(f"shared_memory create_task no-op (fail-soft): {e}")
        return None
    finally:
        conn.close()


def handoff(cfg: Dict[str, Any], task_ref: str, to_agent: str,
            summary: str, plan_id: Optional[str] = None,
            repo_url: Optional[str] = None, branch: Optional[str] = None,
            commit: Optional[str] = None, setup: Optional[str] = None
            ) -> Optional[Dict[str, Any]]:
    """门 3: 交棒 (写 pending 棒, 可带 plan_id 指针 — 不传 PRD 正文).
    交方 = 本机 (cfg.agent_id). 单活: 该 task_ref 已有 pending 棒则中央拒.
    门 4: 涉代码交接时带 4 指针 (repo_url/branch/commit/setup) — 代码走中央
    Gitea 的 git push/pull, 棒只传指针 (只推不拉/中央=指针 同构). 中央 hand_off
    硬门槛: 任一指针非空则 repo_url+commit+setup 三项必齐, 缺一拒 (选 1+3)."""
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        _central_auth(conn, agent_id, cfg.get("token", ""))
        import hashlib
        token_hash = hashlib.sha256((cfg.get("token") or "").encode("utf-8")).hexdigest()
        cur = conn.cur()
        cur.execute(
            "SELECT hand_off(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (task_ref, agent_id, to_agent, summary, token_hash,
             plan_id, repo_url, branch, commit, setup))
        row = cur.fetchone()
        conn.commit()
        return {"status": "ok", "task_ref": task_ref, "version": row[0] if row else None}
    except _TokenRejected as e:
        logger.warning(f"shared_memory handoff token rejected: {e}")
        return None
    except Exception as e:
        logger.warning(f"shared_memory handoff no-op (fail-soft): {e}")
        return None
    finally:
        conn.close()


def take_handoff(cfg: Dict[str, Any], task_ref: str) -> Optional[Dict[str, Any]]:
    """门 3: 取棒 (原子翻 pending -> taken). 只取"交给我"的棒.
    返回 row dict (含 plan_id 指针 — 接手方按它调 filemem_plan 还原上下文).
    取不到 (不是交给我 / 已被取 / 无此 task_ref) -> None."""
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        _central_auth(conn, agent_id, cfg.get("token", ""))
        import hashlib
        token_hash = hashlib.sha256((cfg.get("token") or "").encode("utf-8")).hexdigest()
        cur = conn.cur()
        # take_handoff RETURNS handoffs (composite): psycopg2 fetchone 返回 1 元素
        # (composite text), 不是 11 列平铺. 直接解析 composite text 脆弱 (带引号的
        # 时间戳含逗号), 故原子 UPDATE 走函数, 读回走普通 11 列 SELECT.
        # 函数 RAISE (非交给我 / 已被取 / 无此 task_ref) -> 下方 catch 返回 None.
        try:
            cur.execute("SELECT take_handoff(%s, %s, %s)",
                        (task_ref, agent_id, token_hash))
            cur.fetchone()  # 触发原子翻 pending->taken
            conn.commit()
        except Exception:
            conn.close()
            return None
        # 读回: 普通 SELECT 15 列 (物理 ordinal, S8 后含门 4 的 4 指针), 按
        # (task_ref, to_agent=本agent, taken).
        try:
            cur.execute(
                "SELECT handoff_id, task_ref, version, from_agent, to_agent, summary, "
                "state, taken_by, created_at, taken_at, plan_id, "
                "repo_url, branch, commit, setup FROM handoffs "
                "WHERE task_ref = %s AND to_agent = %s AND state = 'taken' "
                "ORDER BY version DESC LIMIT 1",
                (task_ref, agent_id))
            row = cur.fetchone()
        except Exception:
            conn.close()
            raise
        if row is None:
            return None
        cols = ["handoff_id", "task_ref", "version", "from_agent", "to_agent",
                "summary", "state", "taken_by", "created_at", "taken_at",
                "plan_id", "repo_url", "branch", "commit", "setup"]
        return dict(zip(cols, row))
    except _TokenRejected as e:
        logger.warning(f"shared_memory take_handoff token rejected: {e}")
        return None
    except Exception as e:
        logger.warning(f"shared_memory take_handoff no-op (fail-soft): {e}")
        return None
    finally:
        conn.close()


def _all_local_row_ids(memories_dir) -> List[str]:
    """本地全 shard 行 id (门 1 全量沉淀对账用). 5 shard 全扫含 plan."""
    ids = []
    for mem_type in ("fact", "preference", "decision", "task", "plan"):
        mem_file = Path(memories_dir) / f"{mem_type}.json"
        if not mem_file.exists():
            continue
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                for it in json.load(f):
                    ids.append(it["id"])
        except Exception:
            continue
    return ids


def _all_local_rows_ts(memories_dir) -> List[Tuple[str, str]]:
    """本地全 shard (id, updated_at) 对 (2026-11-18 reconcile 时间戳漂移腿用).
    5 shard 全扫含 plan; updated_at 缺 -> 空串 (视为最早, 必落后 -> 若中央
    有同行则不触发重推, 行为安全)。"""
    rows: List[Tuple[str, str]] = []
    for mem_type in ("fact", "preference", "decision", "task", "plan"):
        mem_file = Path(memories_dir) / f"{mem_type}.json"
        if not mem_file.exists():
            continue
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                for it in json.load(f):
                    rows.append((it["id"], it.get("updated_at") or ""))
        except Exception:
            continue
    return rows


def _norm_ts(v: Any) -> str:
    """秒级墙钟归一 (reconcile 漂移判定): str() -> 'T'->' ' -> 去小数 ->
    [:19]。本地 shard updated_at 是 ISO 串 (可带 +08:00 尾巴), 中央读回是
    datetime (psycopg2, session tz) — 两者都归一成 'YYYY-MM-DD HH:MM:SS'
    可直接比 (session 墙钟; 若 PG session tz 与宿主差整小时, 最坏情况 =
    同行被幂等重推一次, 无数据损失)。"""
    return re.sub(r"\.\d+", "", str(v or "").replace("T", " "))[:19]


def reconcile_local_to_central(provider) -> Dict[str, Any]:
    """Q1 启动对账腿: provider initialize 后跑一次, 本地全 shard 行 vs 中央
    差集补推 (幂等, 只推需要推的, 不重推已在中央且本地不更新的). 根除
    "重启/宕机期间 auto-catch 没 fire 的缺口". 50 agent 规模控频: 每 host
    生命周期一次由调用方 flag 守 (filemem initialize / kb _svc() 语义),
    本函数只做差集 + 补推.

    2026-11-18 差集扩到【时间戳漂移】: 差集 = (id 中央无) 或 (本地
    updated_at 新于中央的)。re-stamp 路径 (bg reclassify / decay /
    plan-advance) 刷新本地行的 updated_at 但不 fire 写钩子, 中央副本静默
    滞后 — 启动对账每生命周期自愈一次 (幂等 upsert, 同内容/同 version 重推
    无害). 中央比本地新的行 (last-push-wins, 无拉腿) 不重推。

    返回 {"status": "ok"|"skipped", "missing": n, "pushed": n}.
    语义约定 (调用方 flag 控频依据, 与 kb 对账腿一致):
      - "ok" (in-sync 或 补推成功) / "rejected" (token 拒, 配置性错误): 调用方置 flag 不重试.
      - "skipped" + reason="fail-soft: ...": 临时故障 (PG 不可达), 调用方不置 flag,
        下次 initialize 再试.
      - "skipped" + reason="config-absent-or-disabled": 配置关闭, 调用方不跑本函数."""
    cfg = _load_shared_memory_config()
    if cfg is None:
        return {"status": "skipped", "reason": "config-absent-or-disabled",
                "missing": 0, "pushed": 0}
    conn = _PgConn(cfg["pg"])
    try:
        agent_id = cfg["agent_id"]
        _central_auth(conn, agent_id, cfg.get("token", ""))
        cur = conn.cur()
        # 查中央已有行 (id + updated_at — 漂移判定要中央侧墙钟; 仍不取
        # content, 差集判定够用, 中央行绝不进本地)
        cur.execute(
            "SELECT id, updated_at FROM memories WHERE agent_id = %s", (agent_id,))
        central_ts = {r[0]: _norm_ts(r[1]) for r in cur.fetchall()}
        # 本地全 shard (id, updated_at): 差集 = 中央缺 或 本地更新 (drift)
        local_rows = _all_local_rows_ts(provider.memories_dir)
        missing = [rid for rid, lts in local_rows
                   if rid not in central_ts or _norm_ts(lts) > central_ts[rid]]
        if not missing:
            return {"status": "ok", "missing": 0, "pushed": 0,
                    "reason": "in-sync"}
        # 差集补推 (复用 _push_locked 的幂等 upsert, 只推 missing)
        result = _push_locked(provider, cfg, missing)
        return {"status": result.get("status", "ok"),
                "missing": len(missing), "pushed": result.get("pushed", 0)}
    except _TokenRejected as e:
        logger.warning(f"shared_memory reconcile_local_to_central token rejected: {e}")
        return {"status": "rejected", "reason": "token-rejected",
                "missing": 0, "pushed": 0}
    except Exception as e:
        logger.warning(f"shared_memory reconcile_local_to_central no-op (fail-soft): {e}")
        return {"status": "skipped", "reason": f"fail-soft: {e}",
                "missing": 0, "pushed": 0}
    finally:
        conn.close()


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
        def _ts(v):
            """timestamptz -> ISO string (json.dumps 前必须去掉 datetime 对象)."""
            if v is None:
                return None
            if hasattr(v, "isoformat"):
                return v.isoformat()
            return v
        return [
            dict(zip(
                ["id", "mem_type", "content", "level", "confirmed",
                 "source_profile", "agent_id", "version", "updated_at", "metadata"],
                [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], _ts(r[8]), r[9]]
            )) for r in rows
        ]
    except _TokenRejected:
        return []
    except Exception as e:
        logger.warning(f"shared_memory search_central no-op (fail-soft): {e}")
        return []
    finally:
        conn.close()
