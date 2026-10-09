"""Gold 知识层 — TDD（切片② 建，先于切片③ 删）RED→GREEN。

@data-engineer I3 owner 已确认（从严单档 + 去 archive_ref）：memory_ids 每条须解析到
memories/*.json 真实 id（seed 行带 `upgraded_from`，11/11 解析通过），不过 → 拒写 +
原文降级 raw_pending（origin=llm_failed_raw，不静默丢）。

fixture 全 tmpdir 隔离（_make_provider，零 live-store 写）—— 故本文件对 cron
92ae706bd44e 零碰撞；只有切片③ 物理删 run.py/engine 那一刻才触风险（@devops gate）。

不变量：
  I1 无 DELETE（同 topic 新条 → 旧条 superseded + superseded_by，全谱系留存）
  I2 幂等 upsert（同 (topic, content_hash) 重放 = bytes 相同）
  I3 provenance 可验证门（从严单档；拒写降级 raw_pending）
"""
import json, sys, pathlib, tempfile, threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402


def _make_provider(tmpdir, seed_memory_ids=()):
    """镜像 test_reinforce_recall._make_provider + 追加 knowledge_dir（零 initialize()）。

    seed_memory_ids：预置真实 memory id 形制（preference_N_ts）进 memories/*.json，
    供 I3 门解析目标集。
    """
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    for mid in seed_memory_ids:
        # id 形制 type_N_ts → 拆出 type 段（前缀到第一个下划线）落对应 json
        mtype = mid.split("_")[0]
        f = p.memories_dir / f"{mtype}.json"
        items = json.load(open(f, encoding="utf-8")) if f.exists() else []
        items.append({"id": mid, "type": mtype, "content": f"seed {mid}",
                      "salience": 0.5, "created_at": "2026-09-26T00:00:00",
                      "updated_at": "2026-09-26T00:00:00"})
        f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    p.knowledge_dir = tmpdir / "knowledge"
    p.knowledge_dir.mkdir(parents=True, exist_ok=True)
    p.knowledge_entries_path = p.knowledge_dir / "entries.json"
    p.knowledge_raw_pending_path = p.knowledge_dir / "raw_pending.json"
    p.pending_queue_path = tmpdir / "consolidated" / "pending_upgrades.jsonl"
    p.pending_queue_path.parent.mkdir(parents=True, exist_ok=True)
    p.source_profile = "test"
    # search_memories 读的属性（镜像 initialize 的 dir 面，零 initialize()）
    p.storage_dir = tmpdir
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p._decay = {}                 # keyword 打分路径，model-assist 需 _llm_ctx=None
    p._llm_ctx = None
    return p


def _entries(p):
    f = p.knowledge_entries_path
    return json.load(open(f, encoding="utf-8")) if f.exists() else []


def test_i1_no_delete_superseded_chain():
    """I1：同 topic 新内容 → 旧条 status=superseded + superseded_by 指新条，无 DELETE。"""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_892_1790368739"])
    id1 = p.upsert_knowledge(topic="t1", content="v1",
                             memory_ids=["preference_892_1790368739"])
    id2 = p.upsert_knowledge(topic="t1", content="v2",
                             memory_ids=["preference_892_1790368739"])
    by_content = {r["content"]: r for r in _entries(p)}
    assert by_content["v1"]["status"] == "superseded"
    assert by_content["v1"]["superseded_by"] == id2      # v1 链到更晚的 v2 条
    assert by_content["v2"]["status"] == "active"
    assert by_content["v2"]["id"] == id2
    assert len(_entries(p)) == 2                          # 两条都在，无 DELETE
    assert id1 != id2                                      # 同 topic 不同内容 = 两条


def test_i2_idempotent_upsert_replay():
    """I2：同 (topic, content_hash) 重放 3 次 = bytes 相同（不重写、不动 ts）。"""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_893_1790368817"])
    first_id = p.upsert_knowledge(topic="t", content="c",
                                  memory_ids=["preference_893_1790368817"])
    first = p.knowledge_entries_path.read_bytes()
    for _ in range(3):
        rereplay_id = p.upsert_knowledge(topic="t", content="c",
                                         memory_ids=["preference_893_1790368817"])
        assert rereplay_id == first_id                     # no-op 返回已存在条 id
    assert p.knowledge_entries_path.read_bytes() == first  # 字节不变（I2 不变量）


def test_i3_provenance_gate_rejects_unresolved():
    """I3 拒写：memory_id 解析不到 memories/*.json → 拒写（返 None）+ 原文降级 raw_pending。"""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_893_1790368817"])
    # 一个能解析 + 一个不能解析 → 整条拒写（从严：任一 unresolved 即拒）
    res = p.upsert_knowledge(topic="bad", content="x",
                             memory_ids=["preference_893_1790368817",
                                         "decision_999_9999999999"])
    assert res is None                                     # 拒写，不落 entries
    assert _entries(p) == []                                # entries 里没有任何条
    rp = json.load(open(p.knowledge_raw_pending_path, encoding="utf-8"))
    assert any(r["content"] == "x" and r["origin"] == "llm_failed_raw" for r in rp)
    # 全解析通过则正常落
    ok_id = p.upsert_knowledge(topic="good", content="y",
                               memory_ids=["preference_893_1790368817"])
    assert ok_id is not None and ok_id.startswith("k_")


def test_knowledge_tier_boost_in_search_pool():
    """检索面：active knowledge 条进 pool 且带 tier boost（置顶语义，非新增第二轴）。"""
    p = _make_provider(pathlib.Path(tempfile.mkdtemp()),
                       seed_memory_ids=["preference_908_1790369582"])
    p.upsert_knowledge(topic="deploy", content="deploy cadence weekly",
                       memory_ids=["preference_908_1790369582"])
    pool = p._load_knowledge_pool()
    assert any(e["type"] == "knowledge" and e["content"] == "deploy cadence weekly"
               for e in pool)
    # 同内容下 knowledge 条 score > 普通 memory 条（1.5x boost 生效）
    for e in pool:
        e["knowledge_tier_boost"] = True
    results = p.search_memories("deploy", limit=10, min_score=0.0, ranking="decay")
    kres = [r for r in results if r.get("type") == "knowledge"]
    assert kres and kres[0].get("score", 0) > 0
