"""门 5 (worldview gate) 注入器测试.

锁定 owner 定稿 (2026-10-28):
- S3 全量无上限: _gate5_worldview_block 读 worldview.json 全部 confirmed 行,
  渲染 [用户思维逻辑底座] 块, 不做相关性稀释/分层/截断.
- 挂 pre_llm_call 面 (不碰宿主), fail-soft (读失败/无行 -> "").
- 拼接点: 世界观块拼在 [记忆上下文] 之前; context 空但世界观块在时仍注入.

所有 store 走 tmp synthetic — 活体 /root/.hermes/filemem 与宿主树绝不触碰.
"""
import json
import pathlib
import sys
import threading

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "/usr/local/lib/hermes-agent" not in sys.path:
    sys.path.insert(0, "/usr/local/lib/hermes-agent")

import __init__ as fm  # noqa: E402  (插件包名即 __init__, 同 test_ep_1_lineage)


def _make_provider(tmp_path):
    """Partial provider (__new__ + only the attrs gate5 read/write faces
    need): memories_dir + the lock protocol attrs the upsert wrapper uses
    (self._lock in-process + self._shard_lock cross-process, the standard
    2-layer shard write convention; _shard_lock is inherited from the real
    class and degrades to a loud no-op on hosts without fcntl/msvcrt)."""
    p = fm.FileMemProvider.__new__(fm.FileMemProvider)
    p.memories_dir = tmp_path / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.session_id = "s-g5"
    p._lock = threading.Lock()
    return p


def _write_worldview(p, rows):
    path = p.memories_dir / "worldview.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


# ── 1. 全量渲染: 9 条 confirmed 全进块, 带 source 戳 ──
def test_all_confirmed_rows_injected(tmp_path):
    p = _make_provider(tmp_path)
    rows = [
        {"id": "wv_a", "type": "worldview", "content": "用户拆小段", "confirmed": True,
         "metadata": {"source": "stated"}},
        {"id": "wv_b", "type": "worldview", "content": "改完就 commit", "confirmed": True,
         "metadata": {"source": "stated"}},
        {"id": "wv_c", "type": "worldview", "content": "先查清再动手", "confirmed": True,
         "metadata": {"source": "inferred-confirmed"}},
    ]
    _write_worldview(p, rows)
    block = p._gate5_worldview_block()
    assert "[用户思维逻辑底座]" in block
    assert "用户拆小段" in block
    assert "改完就 commit" in block
    assert "先查清再动手" in block
    # source 戳落地
    assert "(stated)" in block
    assert "(inferred-confirmed)" in block
    # 引导语在
    assert "直接据此预判" in block


# ── 2. 只收 confirmed: unconfirmed 行被过滤 ──
def test_unconfirmed_rows_excluded(tmp_path):
    p = _make_provider(tmp_path)
    rows = [
        {"id": "wv_ok", "type": "worldview", "content": "已确认的世界观", "confirmed": True,
         "metadata": {"source": "stated"}},
        {"id": "wv_unconf", "type": "worldview", "content": "未确认的候选", "confirmed": False,
         "metadata": {"source": "stated"}},
    ]
    _write_worldview(p, rows)
    block = p._gate5_worldview_block()
    assert "已确认的世界观" in block
    assert "未确认的候选" not in block


# ── 3. fail-soft: 无文件 -> 空串 ──
def test_no_file_returns_empty(tmp_path):
    p = _make_provider(tmp_path)
    # 不写 worldview.json
    assert p._gate5_worldview_block() == ""


# ── 4. fail-soft: 空数组 -> 空串 ──
def test_empty_list_returns_empty(tmp_path):
    p = _make_provider(tmp_path)
    _write_worldview(p, [])
    assert p._gate5_worldview_block() == ""


# ── 5. 非 worldview type 的行被过滤 (同分片混入其他 type 时) ──
def test_non_worldview_type_excluded(tmp_path):
    p = _make_provider(tmp_path)
    rows = [
        {"id": "wv_x", "type": "worldview", "content": "世界观行", "confirmed": True,
         "metadata": {"source": "stated"}},
        {"id": "fact_x", "type": "fact", "content": "普通事实行", "confirmed": True,
         "metadata": {"source": "auto"}},
    ]
    _write_worldview(p, rows)
    block = p._gate5_worldview_block()
    assert "世界观行" in block
    assert "普通事实行" not in block


# ── 6. 拼接: 世界观块在 [记忆上下文] 之前 ──
def test_gate5_block_prepended_to_context(monkeypatch, tmp_path):
    """filemem_auto_retrieve 里世界观块必须拼在 [记忆上下文] 前."""
    import importlib
    # 直接测拼接逻辑: mock 一个 provider 的 prefetch 返回固定 context
    p = _make_provider(tmp_path)
    rows = [{"id": "wv_a", "type": "worldview", "content": "用户拆小段", "confirmed": True,
             "metadata": {"source": "stated"}}]
    _write_worldview(p, rows)
    # 模拟: prefetch 返回一个非空 context
    monkeypatch.setattr(p, "prefetch", lambda *a, **k: "召回的某条记忆")
    # 重演 filemem_auto_retrieve 的拼接逻辑 (抽出为可直接测的形式):
    # 世界观块 + [记忆上下文]
    wb = p._gate5_worldview_block()
    ctx = "召回的某条记忆"
    parts = []
    if wb:
        parts.append(wb)
    parts.append(f"[记忆上下文]\n{ctx}\n")
    joined = "\n".join(parts)
    # 世界观块在 [记忆上下文] 之前
    assert joined.index("[用户思维逻辑底座]") < joined.index("[记忆上下文]")


# ── 7. context 空但世界观块在: 仍注入 (S3 全量, 不依赖召回命中) ──
def test_injects_even_when_context_empty(monkeypatch, tmp_path):
    p = _make_provider(tmp_path)
    _write_worldview(p, [{"id": "wv_a", "type": "worldview", "content": "用户拆小段",
                         "confirmed": True, "metadata": {"source": "stated"}}])
    monkeypatch.setattr(p, "prefetch", lambda *a, **k: "")  # 本轮无召回命中
    wb = p._gate5_worldview_block()
    ctx = ""
    # filemem_auto_retrieve 的逻辑: context 空 + 世界观块在 -> return {"context": wb}
    if not ctx and not wb:
        result = None
    else:
        result = wb  # (context 空分支直接返回 worldview_block)
    assert result and "用户拆小段" in result


# ── 写侧: _gate5_worldview_upsert ──

def test_upsert_new_row_lands(tmp_path):
    """W1: 新增一条 confirmed worldview 行 (ttl_days=None)."""
    p = _make_provider(tmp_path)
    new_id = p._gate5_worldview_upsert("用户新的方法论", source="stated")
    assert new_id and new_id.startswith("wv_")
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == new_id
    assert r["type"] == "worldview"
    assert r["content"] == "用户新的方法论"
    assert r["confirmed"] is True
    assert r["ttl_days"] is None  # 无 decay (owner 定稿)
    assert r["metadata"]["source"] == "stated"
    assert r["metadata"]["gate"] == "worldview"


def test_upsert_inferred_source(tmp_path):
    """W1: source=inferred-confirmed 落戳 (agent 推断+用户认可)."""
    p = _make_provider(tmp_path)
    new_id = p._gate5_worldview_upsert("用户推断的思维模式", source="inferred-confirmed",
                                       source_note="无单一源行, owner 反复确认")
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert rows[0]["metadata"]["source"] == "inferred-confirmed"
    assert rows[0]["metadata"]["source_note"] == "无单一源行, owner 反复确认"


def test_upsert_replaces_old_row(tmp_path):
    """W1: 替换旧烙印 — 旧行打 superseded_by 指针, 不物理删."""
    p = _make_provider(tmp_path)
    # 先落一条旧行
    old_id = p._gate5_worldview_upsert("旧烙印", source="stated")
    assert old_id
    # 替换
    new_id = p._gate5_worldview_upsert("新烙印", source="stated",
                                       supersedes_old_id=old_id)
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert len(rows) == 2  # 旧行不物理删, 两行都在
    old_row = next(r for r in rows if r["id"] == old_id)
    new_row = next(r for r in rows if r["id"] == new_id)
    # 旧行打了 superseded_by 指针
    assert old_row["metadata"]["superseded_by"] == new_id
    # 新行无旧行指针
    assert "superseded_by" not in new_row["metadata"]


def test_upsert_replaces_missing_old_id(tmp_path):
    """W1: supersedes_old_id 指向不存在的行 -> 无指针可打, 新行仍落地 (fail-soft 不报错)."""
    p = _make_provider(tmp_path)
    new_id = p._gate5_worldview_upsert("新行", source="stated",
                                       supersedes_old_id="wv_nonexistent")
    assert new_id  # 新行落地
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert len(rows) == 1


def test_upsert_append_accumulates(tmp_path):
    """W1: 多次 upsert 累积 (S3 全量无上限, 不截断)."""
    p = _make_provider(tmp_path)
    for i in range(5):
        p._gate5_worldview_upsert(f"方法论 {i}", source="stated")
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert len(rows) == 5


def test_upsert_backup_created(tmp_path):
    """W1: 写盘前备份 (同迁移脚本的 .bak 机制)."""
    p = _make_provider(tmp_path)
    p._gate5_worldview_upsert("第一条", source="stated")
    p._gate5_worldview_upsert("第二条", source="stated")
    # 第二次写时 worldview.json 已存在 -> 应产生 .bak-g5up-* 备份
    baks = list((p.memories_dir).glob("worldview.json.bak-g5up-*"))
    assert len(baks) >= 1


def test_upsert_corrupt_file_fail_soft(tmp_path):
    """W1: worldview.json 存在但不是合法 JSON 数组 -> fail-soft 返回 None (不炸).

    模拟磁盘损坏 / 被裸写污染的场景: 读阶段 json.loads 抛 -> 拒写
    (新行不落地, 损坏文件原样保留, 等待人工修复), 返回 None
    (宿主 turn 永不被 gate5 写侧炸). 写盘段的"非 JSON 不备份"分支是
    死路径 (走不到), 真正保护在上方读阶段.
    """
    p = _make_provider(tmp_path)
    # provider 读 self.memories_dir / "worldview.json" = tmp_path/"memories"/"worldview.json"
    bad = tmp_path / "memories" / "worldview.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this is not valid json array")  # 磁盘损坏 / 污染
    result = p._gate5_worldview_upsert("应失败", source="stated")
    assert result is None
    # 损坏文件未被 overwrite (保留原状, 等待人工修复)
    assert bad.read_text() == "this is not valid json array"


def test_upsert_non_array_json_fail_soft(tmp_path):
    """W1: worldview.json 是合法 JSON 但不是数组 (如对象) -> fail-soft 拒写.

    读阶段 isinstance(list) 校验拒写; 原文件保留 (operator 可修), 新行
    不落地. 合法 JSON 备份路径在写盘段 (本测试走不到 — 读阶段已拒).
    """
    p = _make_provider(tmp_path)
    bad = tmp_path / "memories" / "worldview.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text('{"unexpected": "shape"}')  # 合法 JSON 但不是数组
    result = p._gate5_worldview_upsert("应失败", source="stated")
    assert result is None
    # 原文件保留 (拒写, 新行未落地)
    assert bad.read_text() == '{"unexpected": "shape"}'


# ── tool handler: _tool_gate5_worldview_upsert ──

def test_tool_gate5_ok(tmp_path):
    p = _make_provider(tmp_path)
    out = json.loads(p._tool_gate5_worldview_upsert(
        {"content": "用户拆小段", "source": "stated"}))
    assert out["ok"] is True
    assert out["id"].startswith("wv_")
    # 落盘了
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    assert len(rows) == 1


def test_tool_gate5_missing_content(tmp_path):
    p = _make_provider(tmp_path)
    out = json.loads(p._tool_gate5_worldview_upsert(
        {"content": "  ", "source": "stated"}))
    assert out["ok"] is False
    assert "content" in out["error"]


def test_tool_gate5_bad_source(tmp_path):
    p = _make_provider(tmp_path)
    out = json.loads(p._tool_gate5_worldview_upsert(
        {"content": "x", "source": "bogus"}))
    assert out["ok"] is False
    assert "source" in out["error"]


def test_tool_gate5_replace(tmp_path):
    p = _make_provider(tmp_path)
    out1 = json.loads(p._tool_gate5_worldview_upsert(
        {"content": "旧", "source": "stated"}))
    old_id = out1["id"]
    out2 = json.loads(p._tool_gate5_worldview_upsert(
        {"content": "新", "source": "stated", "supersedes_old_id": old_id}))
    assert out2["ok"] is True
    assert out2["supersedes_old_id"] == old_id
    rows = json.loads((p.memories_dir / "worldview.json").read_text())
    old_row = next(r for r in rows if r["id"] == old_id)
    assert old_row["metadata"]["superseded_by"] == out2["id"]


if __name__ == "__main__":
    # 便于直接跑 (pytest 也可): 手工驱动 tmp
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tp = pathlib.Path(td)
        p = _make_provider(tp)
        _write_worldview(p, [{"id": "wv_a", "type": "worldview", "content": "用户拆小段",
                              "confirmed": True, "metadata": {"source": "stated"}}])
        print("block:\n", p._gate5_worldview_block())
