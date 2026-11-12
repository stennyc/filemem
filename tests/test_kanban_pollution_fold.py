"""kanban 整卡不进自动注入面（owner 2026-11-12 红线：任何和 kanban 相关的不入自动注入面）。

555e 实证：自动注入面 100% 被 kanban `[task]` 卡片霸占（两条 M6 卡片
`# Kanban task t_...` + `## Body` 全文 + `## Recent work` 串，15 条重复行、
12935 字节）→ provider 前缀缓存从 user 尾部起逐轮失效、read 锁死 8192。

修法（单插件内闭环，宿主零 diff、不跨插件、零新参数）：
- 读侧折出谓词 `_kanban_pollution_fold(row)`：content 命中现成写入口检测器
  `_is_kanban_task_prompt`（双特征硬门 `# Kanban task t_` 卡片头 + `## Body`）
  → 整条 item 折出 auto-inject 面（与 P1 relay-block fold 同构，P0-3 先例，
  写/读腿共用单源检测器不漂移）。
- 落点两个共用入口：
  (1) prefetch 过滤链（_relay_pollution_fold 之后）
  (2) P3 cold-start replay 的 _filter_replayed_snapshot
  行保留在 store + 显式 search 面，只砍自动注入面（fail-open：非 dict /
  content 非 str → 不折，绝不因谓词 bug 丢召回）。

验收面（本文件锁死）：
- T1 卡片 item（`# Kanban task t_` + `## Body`）→ _kanban_pollution_fold True
- T2 非卡片经验行（提及 t_xxx 但无卡片双特征）→ False（零误杀，P1 posture）
- T3 prefetch 过滤链折出卡片、保留非卡片
- T4 _filter_replayed_snapshot 折出卡片行、保留非卡片行
- T5 行非 dict / content 非 str → False（fail-open 不炸）

零宿主改动。

跑法（repo 根 /root/.hermes/plugins/filemem）：
  /root/.hermes/plugin-venv/bin/python -m pytest tests/test_kanban_pollution_fold.py -v
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402


def _card_item() -> dict:
    """555e 实证的两类之一：# Kanban task t_ 卡片头 + ## Body（双特征齐全）。"""
    content = (
        "# Kanban task t_1a8da114: M6 · 外网入口安全加固\n"
        "Assignee: coder\n"
        "Status:   running\n"
        "## Body\n"
        "M6 node 18080 改绑 10.1.1.117，pg/redis 维持 127.0.0.1。\n"
        "## Recent work by @coder\n- t_8fe12531 — M5 · 联调 + 测试完成"
    )
    return {
        "id": "t_1a8da114",
        "type": "task",
        "content": content,
        "metadata": {"confirmed": True},
    }


CARD_CONTENT = _card_item()["content"]


def _experience_item() -> dict:
    """555e 的 L100：提及 t_xxx / 9200 bug 根因，但**不是**卡片结构。"""
    return {
        "id": "exp_9200",
        "type": "task",
        "content": (
            "hermes-ui 9200 web-attach fix: window.hermesDesktop FS shim must NOT "
            "hardcode http://127.0.0.1:9200 in fetch — user accesses 9200 cross-machine "
            "via Tailscale. 提及 t_4c6252aa 验收。"
        ),
        "metadata": {"confirmed": True},
    }


def _make_provider(tmpdir: Path) -> FileMemProvider:
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.session_id = "s_kanban"
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    # source_profile=None → _resolve_session_profile 返回 None → P0-2 profile
    # 隔离腿 fail-open（不折 confirmed 行）；与 live 测试面（tmp 无 profile 戳）
    # 对齐。test_notice_content_face.py 的 "test" 口径是配合 _row(...) 给行打
    # source_profile 戳的；本文件 fixture 不打 source_profile 戳，用 None 让
    # profile 腿全收，聚焦 kanban fold 本身。
    p.source_profile = None
    p._session_drafts = {}
    p._last_recall = None
    p._pending_drafts_notified = {}
    p.min_search_score = 0.3
    p._last_save_time = 0
    p._save_interval = 30
    p._session_save_count = 0
    p._turn_failure = {}
    p._pending_failure_meta = {}
    p._failure_recent = {}
    p._turn_self_test = False
    p._turn_canary = False
    p._decay = {}
    p._pending_confirm = {}
    p._pending_confirm_cap = 50
    return p


import __init__ as _fm  # 单源谓词直接引（与代码同处，测试锁死其行为）


# ───────────────────────── T1：卡片命中折出 ─────────────────────────

def test_t1_card_item_folded():
    assert _fm._kanban_pollution_fold(_card_item()) is True


# ───────────────────────── T2：非卡片经验行不折（零误杀） ─────────────────────────

def test_t2_experience_item_kept():
    assert _fm._kanban_pollution_fold(_experience_item()) is False


# ───────────────────────── T3：prefetch 过滤链折卡片、留非卡片 ─────────────────────────

def test_t3_prefetch_filter_chain(tmp_path):
    p = _make_provider(tmp_path)
    card, exp = _card_item(), _experience_item()
    # 让 search 返回两条（卡片 + 经验行）
    p.search_memories = lambda *a, **k: [card, exp]
    # fixture 补全：_get_bringback_choice 默认 'fresh'（choice 文件不存在）→
    # prefetch 的 fresh-gate 把整块 results 清空。测试聚焦 kanban 折出本身，
    # 需 stub 成 'clean_start'（不带 fresh 短路）让过滤链走到 kanban fold。
    p._get_bringback_choice = lambda sid: "clean_start"
    ctx = p.prefetch("M6 外网加固", session_id="s_kanban")
    # 卡片全文被折出（卡片头 + 卡片 body 内容不出现）
    assert "# Kanban task t_1a8da114" not in ctx, "卡片应被折出自动注入面"
    assert "M6 node 18080 改绑" not in ctx, "卡片 body 应随卡折出"
    # 非卡片经验行保留
    assert "hermes-ui 9200 web-attach fix" in ctx, "非卡片经验行必须保留（零误杀）"


# ───────────────────────── T4：P3 replay 过滤器折卡片行 ─────────────────────────

def test_t4_replay_filter_drops_card_keeps_exp(tmp_path):
    p = _make_provider(tmp_path)
    card, exp = _card_item(), _experience_item()
    # 把两类 content 种进 pool（by_content 索引），让 replay 过滤面走到
    # 谓词判据（_auto_inject_eligible + _kanban_pollution_fold）。
    # MEMORY_TYPES = preference/fact/decision/task/plan/scheduled → 用 task.json
    import json as _json
    (p.memories_dir / "task.json").write_text(
        _json.dumps([card, exp]), encoding="utf-8")
    # 冻结块：`[task] <content>` 行形态（渲染面同款）
    block = f"[task] {CARD_CONTENT}\n[task] {exp['content']}"
    kept = p._filter_replayed_snapshot(block)
    assert "M6 node 18080 改绑" not in kept, "kanban 卡片行应被折出 P3 replay"
    assert "hermes-ui 9200 web-attach fix" in kept, "非卡片经验行必须保留"


# ───────────────────────── T5：fail-open（非 dict / content 非 str） ─────────────────────────

def test_t5_fail_open_no_crash():
    assert _fm._kanban_pollution_fold(None) is False
    assert _fm._kanban_pollution_fold("raw string not dict") is False
    assert _fm._kanban_pollution_fold({"id": "x", "content": 12345}) is False
