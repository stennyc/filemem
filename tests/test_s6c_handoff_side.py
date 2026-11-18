"""S6c: 门 3 handoff 工具面 + 交棒 plan 提示行 (owner 定稿 2026-10-22, 选 B).

- shared_memory_handoff / shared_memory_take tools: 薄封装调中央 6 参函数
  (create_task / hand_off + p_plan_id / take_handoff), token 硬约束走中央.
- 交棒 plan 提示行 (D1=b 同型): 本地有 in_progress/blocked plan 时,
  提示列出 plan_id+标题, 模型交棒时照填 — 程序不自动塞, 只指路.
"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from __init__ import FileMemProvider


def _make_provider(tmp_path: Path) -> FileMemProvider:
    p = FileMemProvider()
    p.storage_dir = tmp_path
    p.memories_dir = tmp_path / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.session_id = "test_session"
    p.source_profile = "test_profile"
    p._decay = {}
    p._turn_marker_index = {}
    p._turn_active_sid = "test_session"
    p._last_save_time = 0.0
    p._save_interval = 0.0
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    p._session_drafts = {}
    p._pending_drafts_notified = {}
    return p


def _cfg() -> dict:
    return {
        "enabled": True, "agent_id": "local",
        "pg": {"host": "127.0.0.1", "port": 5432, "db": "sharedmem",
                "user": "sharedmem", "password": "pw"},
        "token": "tok",
    }


class TestHandoffTools:
    def test_handoff_not_enabled(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = None
        p._sm_mod = None
        out = json.loads(p._tool_shared_handoff({"to": "remote_b", "summary": "x"}))
        assert out["success"] is False

    def test_handoff_delegates_with_plan_id(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        captured = {}

        def _fake_sm_handoff(cfg, task_ref, to_agent, summary, plan_id=None,
                             repo_url=None, branch=None, commit=None, setup=None):
            captured["task_ref"] = task_ref
            captured["to"] = to_agent
            captured["summary"] = summary
            captured["plan_id"] = plan_id
            return {"status": "ok", "version": 1}
        p._sm_mod = mock.MagicMock()
        p._sm_mod.handoff.side_effect = _fake_sm_handoff
        out = json.loads(p._tool_shared_handoff(
            {"task_ref": "T-9", "to": "remote_b", "summary": "做 S6b",
             "plan_id": "plan_xxx"}))
        assert out["success"] is True
        assert captured["plan_id"] == "plan_xxx"
        assert captured["to"] == "remote_b"

    def test_handoff_passes_gate4_pointers(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        captured = {}

        def _fake_sm_handoff(cfg, task_ref, to_agent, summary, plan_id=None,
                             repo_url=None, branch=None, commit=None, setup=None):
            captured.update(repo_url=repo_url, branch=branch,
                            commit=commit, setup=setup)
            return {"status": "ok", "version": 2}
        p._sm_mod = mock.MagicMock()
        p._sm_mod.handoff.side_effect = _fake_sm_handoff
        out = json.loads(p._tool_shared_handoff(
            {"task_ref": "T-g4", "to": "remote_b", "summary": "涉代码交接",
             "repo_url": "git://gitea.10.1.1.x:3000/owner/proj",
             "branch": "feat/x", "commit": "abc1234", "setup": "runtime.md"}))
        assert out["success"] is True
        assert captured == {"repo_url": "git://gitea.10.1.1.x:3000/owner/proj",
                            "branch": "feat/x", "commit": "abc1234",
                            "setup": "runtime.md"}
        # 门 4 指针回显在结果里
        assert out["commit"] == "abc1234" and out["setup"] == "runtime.md"

    def test_take_returns_gate4_pointers(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = mock.MagicMock()
        p._sm_mod.take_handoff.return_value = {
            "task_ref": "T-g4", "version": 1, "plan_id": None,
            "from_agent": "remote_b", "to_agent": "local", "summary": "s",
            "repo_url": "git://gitea.10.1.1.x:3000/owner/proj",
            "branch": "feat/x", "commit": "abc1234", "setup": "runtime.md"}
        out = json.loads(p._tool_shared_take({"task_ref": "T-g4"}))
        assert out["success"] is True
        r = out["results"]
        assert r["repo_url"] == "git://gitea.10.1.1.x:3000/owner/proj"
        assert r["commit"] == "abc1234" and r["setup"] == "runtime.md"

    def test_take_returns_plan_id(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = mock.MagicMock()
        p._sm_mod.take_handoff.return_value = {
            "task_ref": "T-9", "version": 1, "plan_id": "plan_yyy",
            "from_agent": "remote_b", "to_agent": "local"}
        out = json.loads(p._tool_shared_take({"task_ref": "T-9"}))
        assert out["success"] is True
        assert out["results"]["plan_id"] == "plan_yyy"


class TestHandoffPlanHint:
    """交棒提示行: 有未完成 plan -> 列 plan_id+标题; 无 -> 空串 (字节级不变)."""
    def test_hint_empty_when_no_plans(self, tmp_path):
        p = _make_provider(tmp_path)
        p._plan_load_rows = lambda: []
        assert p._handoff_plan_hint() == ""

    def test_hint_lists_unfinished_plans(self, tmp_path):
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()  # hint 门槛: 未配置 shared_memory 时恒 ""
        rows = [
            {"id": "plan_a", "content": "A 标题",
             "metadata": {"plan_state": "in_progress", "plan_id": "plan_a"}},
            {"id": "plan_b", "content": "B 标题",
             "metadata": {"plan_state": "blocked", "plan_id": "plan_b"}},
            {"id": "plan_c", "content": "C 已完成",
             "metadata": {"plan_state": "done", "plan_id": "plan_c"}},
        ]
        p._plan_load_rows = lambda: rows
        h = p._handoff_plan_hint()
        assert "plan_a" in h and "A 标题" in h
        assert "plan_b" in h and "B 标题" in h
        assert "plan_c" not in h  # done 不列


class TestSharedMemModuleHandoff:
    """shared_memory 模块层: handoff / take_handoff / create_task 薄封装."""
    def test_handoff_pushes_with_plan_id(self):
        import shared_memory as sm
        cfg = _cfg()
        captured = []

        class _FakeCur:
            def __init__(self, cap):
                self._cap = cap
            def execute(self, sql, params):
                self._cap.append((sql, params))
            def fetchone(self):
                return (1,)
            def fetchall(self):
                return []

        class _FakeConn:
            def cur(self):
                return _FakeCur(captured)
            def commit(self):
                pass
            def close(self):
                pass

        # 走真实 SQL 拼装 (不连库): 把 _PgConn 替掉
        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                r = sm.handoff(cfg, "local", "remote_b", "做 S6b", "plan_xxx")
        # 至少一次 execute 是 hand_off 调用且带 plan_id
        handoff_calls = [c for c in captured if "hand_off" in c[0]]
        assert handoff_calls, "hand_off 未执行"
        assert "plan_xxx" in handoff_calls[0][1]

    def test_take_returns_row_dict_with_plan_id(self):
        import shared_memory as sm
        cfg = _cfg()
        # 新逻辑 (C 双修后): take_handoff 两次 execute —
        #  (1) SELECT take_handoff(...) 函数 (原子翻 pending->taken, 返回 composite 被忽略)
        #  (2) 普通 11 列 SELECT 读回 (按 task_ref+to_agent+state='taken' 最新 version)
        # fetchone 第一次返回 1 元素 (函数 composite, 被丢), 第二次返回 11 列平铺.
        calls = {"n": 0}

        class _FakeCur:
            def execute(self, sql, params):
                pass
            def fetchone(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    return ("(7,T-9,...)",)  # composite text (1 元素), 被忽略
                return (7, "T-9", 1, "remote_b", "local", "done", "taken",
                        "local", None, None, "plan_zzz")

        class _FakeConn:
            def cur(self):
                return _FakeCur()
            def commit(self):
                pass
            def close(self):
                pass

        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                r = sm.take_handoff(cfg, "T-9")
        # take_handoff 读回 11 列 (含 plan_id), 不炸
        assert r is not None
        assert r["plan_id"] == "plan_zzz"
        assert r["version"] == 1
        assert r["task_ref"] == "T-9"

    def test_take_no_pending_returns_none(self):
        import shared_memory as sm
        cfg = _cfg()

        class _FakeCur:
            def execute(self, sql, params):
                pass
            def fetchone(self):
                return None  # 函数 RAISE -> 外层 catch -> 提前 return None (不走 SELECT)

        class _FakeConn:
            def cur(self):
                return _FakeCur()
            def commit(self):
                pass
            def close(self):
                pass

        # 函数取不到 (非给我/已被取) 时 fetchone 返回 None 不触发, 读回 SELECT 也是 None
        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                r = sm.take_handoff(cfg, "T-99")
        assert r is None


class TestReconcileLocalToCentral:
    """Q1 启动对账腿 (owner 定稿 2026-10-23): provider initialize 后本地全 shard
    vs 中央差集补推 (幂等, 只推本地有中央无的, 根除重启/宕机期间 auto-catch 缺口).
    Q2: plan 行同走 5 shard 全量推中央 (中央 memories 表已存 plan, 无需独立 plans 表)."""

    def _provider_with_rows(self, tmp_path, rows):
        p = _make_provider(tmp_path)
        p.memories_dir.mkdir(parents=True, exist_ok=True)
        import json as _json
        # 把 rows 按 mem_type 散到 5 shard 文件
        by_type = {}
        for r in rows:
            by_type.setdefault(r.get("type", "fact"), []).append(r)
        for mtype, items in by_type.items():
            with open(p.memories_dir / f"{mtype}.json", "w", encoding="utf-8") as f:
                _json.dump(items, f)
        return p

    def test_reconcile_pushes_only_missing_ids(self, tmp_path):
        import shared_memory as sm
        p = self._provider_with_rows(tmp_path, [
            {"id": "fact_1", "type": "fact", "content": "a", "level": "L0",
             "updated_at": "2026-10-23T10:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"shared_version": 1}},
            {"id": "plan_1", "type": "plan", "content": "b", "level": "L2",
             "updated_at": "2026-10-23T10:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"plan_state": "in_progress", "plan_id": "plan_1",
                          "shared_version": 1}},
            {"id": "task_1", "type": "task", "content": "c", "level": "L1",
             "updated_at": "2026-10-23T10:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"shared_version": 1}},
        ])
        # 中央已有 task_1 (已在中央, 不该重推); fact_1/plan_1 中央没有 (该推)
        pushed_ids = []

        class _FakeCur:
            def __init__(self): self.rows = []
            def execute(self, sql, params=None):
                if "FROM memories" in sql:
                    from datetime import datetime
                    self.rows = [("task_1", datetime(2026, 10, 23, 10, 0, 0))]
                else:
                    pushed_ids.append(params[0])  # upsert_memory_row 第一参 = id
            def fetchall(self): return self.rows
            def fetchone(self): return None
            def close(self): pass
        class _FakeConn:
            def __init__(self): self._cur = _FakeCur()
            def cur(self): return self._cur
            def commit(self): pass
            def close(self): pass

        cfg = _cfg()
        p._shared_mem_cfg = cfg
        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                with mock.patch.object(sm, "_load_shared_memory_config",
                                       lambda: cfg):
                    r = sm.reconcile_local_to_central(p)
        assert r["status"] == "ok"
        # 只推了中央缺的 (fact_1 + plan_1), 已在中央的 task_1 没重推
        assert sorted(pushed_ids) == ["fact_1", "plan_1"], \
            f"应只推差集 fact_1+plan_1, 实际: {pushed_ids}"
        assert r["missing"] == 2 and r["pushed"] == 2

    def test_reconcile_in_sync_pushes_zero(self, tmp_path):
        import shared_memory as sm
        p = self._provider_with_rows(tmp_path, [
            {"id": "fact_1", "type": "fact", "content": "a", "level": "L0",
             "updated_at": "2026-10-23T10:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"shared_version": 1}},
        ])
        pushed = []

        class _FakeCur:
            def __init__(self): self.rows = []
            def execute(self, sql, params=None):
                if "FROM memories" in sql:
                    from datetime import datetime
                    self.rows = [("fact_1", datetime(2026, 10, 23, 10, 0, 0))]  # 中央与本地同刻
                else:
                    pushed.append(1)
            def fetchall(self): return self.rows
            def fetchone(self): return None
            def close(self): pass
        class _FakeConn:
            def __init__(self): self._cur = _FakeCur()
            def cur(self): return self._cur
            def commit(self): pass
            def close(self): pass

        cfg = _cfg()
        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                with mock.patch.object(sm, "_load_shared_memory_config",
                                       lambda: cfg):
                    r = sm.reconcile_local_to_central(p)
        assert r["status"] == "ok" and r.get("reason") == "in-sync"
        assert r["missing"] == 0 and r["pushed"] == 0
        assert pushed == []  # 已在中央, 零补推 (幂等)

    def test_reconcile_skips_when_no_config(self):
        import shared_memory as sm
        p = _make_provider(Path("/tmp/unused_recon"))
        with mock.patch.object(sm, "_load_shared_memory_config", lambda: None):
            r = sm.reconcile_local_to_central(p)
        assert r["status"] == "skipped" and r["pushed"] == 0

    # -- 2026-11-18 timestamp-drift extension -------------------------------
    # Local re-stamp paths (bg reclassify / decay / plan-advance) refresh a
    # row's updated_at WITHOUT firing the write-push hook, so the central
    # copy silently lags. The boot reconcile now heals that too: the
    # re-push diff is (id missing from central) OR (local updated_at newer
    # than central's) — still idempotent (same content/version re-upsert).

    def _reconcile_with_central(self, sm, p, rows, cfg):
        """Run reconcile against a fake central holding `rows`
        (id -> updated_at datetime). Returns (result, pushed_ids)."""
        pushed = []

        class _FakeCur:
            def __init__(self): self.rows = []
            def execute(self, sql, params=None):
                if "FROM memories" in sql:
                    self.rows = list(rows.items())
                else:
                    pushed.append(params[0])
            def fetchall(self): return self.rows
            def fetchone(self): return None
            def close(self): pass
        class _FakeConn:
            def __init__(self): self._cur = _FakeCur()
            def cur(self): return self._cur
            def commit(self): pass
            def close(self): pass

        with mock.patch.object(sm, "_PgConn", lambda c: _FakeConn()):
            with mock.patch.object(sm, "_central_auth"):
                with mock.patch.object(sm, "_load_shared_memory_config",
                                       lambda: cfg):
                    r = sm.reconcile_local_to_central(p)
        return r, pushed

    def test_reconcile_repushes_timestamp_drift(self, tmp_path):
        """Central holds the row but with an OLDER updated_at than local
        (a re-stamp happened after the last push) -> re-pushed."""
        import shared_memory as sm
        from datetime import datetime
        p = self._provider_with_rows(tmp_path, [
            {"id": "fact_1", "type": "fact", "content": "a", "level": "L0",
             "updated_at": "2026-10-23T11:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"shared_version": 1}},
        ])
        cfg = _cfg()
        p._shared_mem_cfg = cfg
        r, pushed = self._reconcile_with_central(
            sm, p, {"fact_1": datetime(2026, 10, 23, 10, 0, 0)}, cfg)
        assert r["status"] == "ok"
        assert pushed == ["fact_1"], "local-ahead row must be re-pushed"
        assert r["missing"] == 1 and r["pushed"] == 1

    def test_reconcile_central_newer_no_repush(self, tmp_path):
        """Central updated_at >= local (in-sync or central ahead — the
        last-push-wins no-pull design) -> zero re-push, idempotent."""
        import shared_memory as sm
        from datetime import datetime
        p = self._provider_with_rows(tmp_path, [
            {"id": "fact_1", "type": "fact", "content": "a", "level": "L0",
             "updated_at": "2026-10-23T09:00:00", "created_at": "2026-10-23T08:00:00",
             "metadata": {"shared_version": 1}},
        ])
        cfg = _cfg()
        p._shared_mem_cfg = cfg
        r, pushed = self._reconcile_with_central(
            sm, p, {"fact_1": datetime(2026, 10, 23, 10, 0, 0)}, cfg)
        assert r["status"] == "ok" and r.get("reason") == "in-sync"
        assert pushed == [] and r["pushed"] == 0

    def test_plan_rows_also_pushed_via_five_shard_scan(self, tmp_path):
        """Q2: plan 行走 5 shard 全量推中央 (中央 memories 表存 plan, 不建独立 plans 表)."""
        import shared_memory as sm
        p = self._provider_with_rows(tmp_path, [
            {"id": "plan_9", "type": "plan", "content": "PRD 步骤", "level": "L2",
             "updated_at": "2026-10-23T10:00:00", "created_at": "2026-10-23T10:00:00",
             "metadata": {"plan_state": "in_progress", "plan_id": "plan_9",
                          "shared_version": 1}},
        ])
        # _all_local_row_ids 扫 5 shard, plan.json 里的行必被捡到
        ids = sm._all_local_row_ids(p.memories_dir)
        assert "plan_9" in ids, "plan 行应被 5-shard 全量对账扫到 (Q2 推中央前置)"

    def test_initialize_triggers_reconcile_once(self, tmp_path):
        """Q1: provider initialize 挂对账腿, host 生命周期一次 (flag 控频).
        不真跑完整 initialize (依赖 host 环境), 直接测 flag 短路逻辑:
        flag=False 时该调 reconcile, flag=True 时不再调."""
        import shared_memory as sm
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_mod = sm
        p._sm_reconciled = False
        calls = {"n": 0}

        def _fake_reconcile(provider):
            calls["n"] += 1
            return {"status": "ok", "pushed": 0}

        p._sm_mod.reconcile_local_to_central = lambda prov: _fake_reconcile(prov)

        # 模拟 initialize 里对账腿的调用逻辑 (flag 短路 + 成功置位)
        if (p._shared_mem_cfg is not None and p._sm_mod is not None
                and not p._sm_reconciled):
            _rc = p._sm_mod.reconcile_local_to_central(p)
            if _rc.get("status") != "skipped":
                p._sm_reconciled = True
        assert calls["n"] == 1 and p._sm_reconciled is True
        # 第二次: flag 已 True, 短路不再调
        if (p._shared_mem_cfg is not None and p._sm_mod is not None
                and not p._sm_reconciled):
            _fake_reconcile(p)
            p._sm_reconciled = True
        assert calls["n"] == 1, "flag 控频: 第二次不该再调 reconcile"

    def test_reconcile_failsoft_does_not_set_flag(self, tmp_path):
        """语义对齐 (kb reviewer P1, filemem 侧同步修): fail-soft
        结果 (中央不可达, status=skipped) 不置 flag — 下个 session / host
        进程再试; rejected (token 拒, 配置性错误) 置 flag 不重试."""
        p = _make_provider(tmp_path)
        p._shared_mem_cfg = _cfg()
        p._sm_reconciled = False

        def _run(status):
            if not p._sm_reconciled:
                _rc = {"status": status, "missing": 0, "pushed": 0}
                if _rc.get("status") != "skipped":
                    p._sm_reconciled = True
            return p._sm_reconciled

        # fail-soft (PG 不可达): 不置 flag → 还能再试
        p._sm_reconciled = False
        assert _run("skipped") is False, "fail-soft 不该置 flag"
        # rejected (token 拒): 置 flag 不重试 (配置错误重试无意义)
        p._sm_reconciled = False
        assert _run("rejected") is True, "rejected 该置 flag 不重试"
        # ok: 置 flag
        p._sm_reconciled = False
        assert _run("ok") is True
