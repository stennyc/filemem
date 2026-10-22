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

        def _fake_sm_handoff(cfg, task_ref, to_agent, summary, plan_id=None):
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
