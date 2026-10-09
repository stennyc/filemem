"""EP-3 story: 别名挖掘（宿主主模型 LLM 面 + 确定性人工 CLI 面 + 本地别名表）。

Acceptance coverage (spec EP-3 blueprint §5, 13+ gates):
1.  suggest_aliases 宿主面（真 llm_caller 代码 + scripted ctx）-> 5 aliases
    写回 aliases.json 该 topic 正确（归一化：去重/小写/排序）
2.  LLM 判无别名（空 aliases）-> 该 topic = [] 不写脏；LLM 抛异常 -> 单
    topic 保留未挖，批次继续不炸 tick
3.  增量：已有 topic 不重挖（LLM 只对 fresh topic 调用，旧条目原样保留）
4.  min_term_freq=3：高频词（3 条含 toolchain）进候选，低频词（2 条含
    build）不进
5.  max_topics_per_tick cap：3 个新 topic 只挖 cap 个，第 3 个滚下边界
6.  _expand_query_with_aliases 命中/未命中/已出现不重复
7.  spec fixture：一条 content 只含 toolchain 不含 build system；查
    build system 改前 0 命中 -> 展开后命中（改前必漏条目改后召回）
8.  aliases.json 坏 JSON / 非 dict 顶层 -> _load_aliases 返回 {} fail-soft
9.  向后兼容：无 aliases.json -> 展开 query byte-identical（is 门），
    搜索面零行为漂移
10. 配置：decay.json 与 DECAY_DEFAULTS 的 aliases 块逐键一致
11. 零 LLM / 零硬编码审计：expand 段 / mine 段 / CLI 全文 grep
    openai/anthropic/httpx/requests/task= / 硬编码模型名 = 0；
    suggest_aliases 段唯一 LLM 入口 ctx.llm.complete_structured（无 task
    key，temperature 0.0 / max_tokens 256）
12. 并发：4 个 CLI 进程同时 --add 同 topic 不撕裂（_shard_lock fcntl）
13. CLI --list 只读（前后 store hash 一致）/ --add / --remove / --prune
    确定性 + append-only 审计
14. 巩固边界挂载：_spawn_llm_consolidation._run 顺序 rescore -> confirm
    -> mine；mine 段异常不炸前两段

Portability: 合成 fixture（tempfile trees），不依赖活数据 / provider
实例；provider 是 partial (__new__)，同 test_ep_2_confirm。CLI 走
subprocess + --store tmp。零硬编码路径。
"""
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import threading
import types

import pytest  # noqa: F401

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from __init__ import FileMemProvider  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402
from _llm_host_fake import _MAIN_RUNTIME, install  # noqa: E402

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
LLM_CALLER = PLUGIN_DIR / "consolidation" / "llm_caller.py"
INIT_PY = PLUGIN_DIR / "__init__.py"
CLI_SCRIPT = PLUGIN_DIR / "tools" / "manage_aliases.py"
DECAY_JSON = PLUGIN_DIR / "decay.json"


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_provider(tmpdir: pathlib.Path, ctx_llm=None, decay=None) -> FileMemProvider:
    """Partial provider shell (test_ep_2 shape) + EP-3 attrs. The ``ctx_llm``
    arg is now a GATE MARKER (epic card 2): truthy turns the provider's
    _main_runtime snapshot ON (the host face is scripted per-test via
    _llm_host_fake.install, or the module is faked via _fake_llm_caller);
    None = standalone -> the mine face skips at the call-level gate."""
    p = FileMemProvider.__new__(FileMemProvider)
    p._lock = threading.Lock()
    p.storage_dir = tmpdir
    p.memories_dir = tmpdir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.sessions_dir = tmpdir / "sessions"
    p.sessions_dir.mkdir(exist_ok=True)
    p.episodic_dir = tmpdir / "episodic"
    p.episodic_dir.mkdir(exist_ok=True)
    p.knowledge_entries_path = None
    p.source_profile = "test"
    p.session_id = "s1"
    p._decay = dict(DECAY_DEFAULTS) if decay is None else decay
    p._main_runtime = _MAIN_RUNTIME if ctx_llm is not None else None
    p._alias_mining_in_flight = False
    return p


def _fact(rid: str, content: str, ts: str = "2026-09-15T00:00:00") -> dict:
    """L2 row (ttl None) — never expired, so it always survives pool assembly."""
    return {
        "id": rid, "type": "fact", "content": content, "level": "L2",
        "metadata": {}, "created_at": ts, "updated_at": ts,
        "salience": 0.5, "reference_count": 0, "ttl_days": None,
        "source": "auto",
    }


def _write_facts(p, rows: list) -> None:
    with open(p.memories_dir / "fact.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def _aliases_file(p) -> pathlib.Path:
    return p.storage_dir / "aliases.json"


class FakeCtxLLM:
    """Scripted ctx.llm host face: complete_structured returns canned parsed
    payloads (or raises), recording every call's kwargs."""

    def __init__(self, replies=None, raise_exc=None):
        self.replies = list(replies or [])
        self.raise_exc = raise_exc
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return types.SimpleNamespace(parsed=self.replies[
            min(len(self.calls) - 1, len(self.replies) - 1)])


def _ctx(replies=None, raise_exc=None) -> types.SimpleNamespace:
    return types.SimpleNamespace(llm=FakeCtxLLM(replies=replies, raise_exc=raise_exc))


def _fake_llm_caller(monkeypatch, per_topic):
    """Patch fm._load_llm_caller with a scripted fake llm_caller module:
    suggest_aliases(topic) -> per_topic[topic] (dict result, or raise).
    Returns the topic-call list for assertions."""
    calls = []

    def suggest(ctx, topic):
        calls.append(topic)
        r = per_topic.get(topic, Exception(f"unexpected topic {topic}"))
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(fm, "_load_llm_caller",
                        lambda: types.SimpleNamespace(suggest_aliases=suggest))
    return calls


def _run_cli(store: pathlib.Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI_SCRIPT), "--store", str(store), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=60,
    )


def _store_hash(store: pathlib.Path) -> str:
    blobs = []
    for f in sorted(store.rglob("*")):
        if f.is_file():
            blobs.append(hashlib.sha256(f.read_bytes()).hexdigest())
    return hashlib.sha256("|".join(blobs).encode()).hexdigest()


# ── 1. suggest_aliases 宿主面（真 llm_caller 代码 + scripted ctx）─────────


def test_suggest_aliases_host_path_success(tmp_path, monkeypatch):
    """5 aliases 经真宿主面（consolidation.llm_caller.suggest_aliases，无
    task key）归一化 + 经 mine 面写回 aliases.json 该 topic 正确。"""
    p = _make_provider(tmp_path, ctx_llm=_ctx())
    fake, state = install(monkeypatch,
                          replies=[{"aliases": ["Toolchain", "build system",
                                                "CI pipeline", "Rust", "cargo",
                                                "Toolchain"]}])
    _write_facts(p, [_fact("m0", "toolchain"),
                     _fact("m1", "toolchain"),
                     _fact("m2", "toolchain")])
    p._mine_alias_candidates()
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    entry = data["topics"]["toolchain"]
    # 5 unique aliases, lowercased + sorted（'Toolchain' 去重归一）
    assert entry["aliases"] == ["build system", "cargo", "ci pipeline",
                                "rust", "toolchain"]
    assert entry["source"] == "llm"
    # host-face proof: 恰好一次调用，显式 main_runtime 快照随 call 走（epic card 2），
    # kwargs 无 task key（宿主主模型落点），temperature 0.0 / max_tokens 256
    assert state["calls"] == 1
    kw = state["kwarg_log"][0]
    assert kw["main_runtime"] == _MAIN_RUNTIME
    assert "task" not in kw
    assert kw["temperature"] == 0.0 and kw["max_tokens"] == 256
    # 审计行落地（append-only consolidation_log.jsonl）
    log = (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8")
    row = json.loads(log.strip().splitlines()[-1])
    assert row["action"] == "alias_mine" and row["topic"] == "toolchain"
    assert row["aliases_count"] == 5 and row["model_used"] is True


def test_suggest_aliases_fail_soft_empty_and_exception(tmp_path, monkeypatch):
    """LLM 判无别名（aliases 空）-> 该 topic = [] 不写脏；LLM 抛异常 ->
    该 topic 保留未挖（不落表），单条异常不炸批次/整 tick。"""
    p = _make_provider(tmp_path, ctx_llm=_ctx())
    per_topic = {"toolchain": {"aliases": [], "model_used": True},
                 "deployer": RuntimeError("host model down"),
                 "packager": {"aliases": ["pip", "poetry"], "model_used": True}}
    calls = _fake_llm_caller(monkeypatch, per_topic)
    _write_facts(p, [
        _fact("a1", "toolchain alpha"), _fact("a2", "toolchain beta"),
        _fact("a3", "toolchain gamma"),
        _fact("b1", "deployer delta"), _fact("b2", "deployer epsilon"),
        _fact("b3", "deployer zeta"),
        _fact("c1", "packager iota"), _fact("c2", "packager kappa"),
        _fact("c3", "packager lambda"),
    ])
    p._mine_alias_candidates()  # 必须不抛（单条异常 fail-soft）
    assert calls == ["deployer", "packager", "toolchain"]  # freq 平手 -> word-asc
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    topics = data["topics"]
    assert topics["toolchain"]["aliases"] == []              # 空判定，不写脏
    assert topics["packager"]["aliases"] == ["pip", "poetry"]
    assert "deployer" not in topics                           # 异常 -> 保留未挖
    log = [json.loads(l) for l in
           (p.storage_dir / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["topic"] for r in log] == ["packager", "toolchain"]  # deployer 无审计行
    assert all(r["action"] == "alias_mine" for r in log)
    assert log[0]["model_used"] is True and log[1]["model_used"] is True


def test_mine_incremental_no_remine(tmp_path, monkeypatch):
    """增量：aliases.json 已有 toolchain -> 巩固 tick 只对新 topic deployer
    调 LLM；旧条目原样保留（不重挖、不改动）。"""
    p = _make_provider(tmp_path, ctx_llm=_ctx())
    calls = _fake_llm_caller(monkeypatch,
                             {"deployer": {"aliases": ["k8s"], "model_used": True}})
    _write_facts(p, [
        _fact("a1", "toolchain alpha"), _fact("a2", "toolchain beta"),
        _fact("a3", "toolchain gamma"),
        _fact("b1", "deployer delta"), _fact("b2", "deployer epsilon"),
        _fact("b3", "deployer zeta"),
    ])
    old_entry = {"aliases": ["chain"], "source": "llm", "ts": "t0"}
    _aliases_file(p).write_text(json.dumps(
        {"topics": {"toolchain": old_entry}, "ts": "t0"}), encoding="utf-8")
    p._mine_alias_candidates()
    assert calls == ["deployer"]  # toolchain 已知 -> 零重挖
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    assert data["topics"]["toolchain"] == old_entry  # 逐字保留
    assert data["topics"]["deployer"]["aliases"] == ["k8s"]


def test_mine_min_term_freq_threshold(tmp_path, monkeypatch):
    """min_term_freq=3（DECAY_DEFAULTS）：3 条含 toolchain -> 进候选；
    2 条含 build -> 未过阈值，零 LLM 调用。"""
    p = _make_provider(tmp_path, ctx_llm=_ctx())
    calls = _fake_llm_caller(monkeypatch,
                             {"toolchain": {"aliases": ["x"], "model_used": True}})
    _write_facts(p, [
        _fact("a1", "we pin toolchain alpha"),
        _fact("a2", "i trust toolchain beta"),
        _fact("a3", "you audit toolchain gamma"),
        _fact("b1", "the build config x"),
        _fact("b2", "my build config y"),
    ])
    p._mine_alias_candidates()
    assert calls == ["toolchain"]  # build(df=2) 未进候选
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    assert list(data["topics"]) == ["toolchain"]
    # 阈值门 helper 直接验证：同源 tokenize（search_index），df>=3 only
    from search_index import tokenize
    freq = p._alias_topic_frequencies(tokenize)
    assert ("toolchain", 3) in freq
    assert not any(w == "build" for w, _ in freq)


def test_mine_max_topics_per_tick_cap(tmp_path, monkeypatch):
    """max_topics_per_tick=2：3 个新 topic 只挖 2，第 3 个滚下边界 tick。"""
    decay = dict(DECAY_DEFAULTS)
    decay["aliases"] = {"min_term_freq": 3, "max_topics_per_tick": 2}
    p = _make_provider(tmp_path, ctx_llm=_ctx(), decay=decay)
    calls = _fake_llm_caller(monkeypatch, {
        w: {"aliases": [f"alias-of-{w}"], "model_used": True}
        for w in ("alpha", "beta", "gamma")})
    _write_facts(p, [
        _fact("a1", "alpha one"), _fact("a2", "alpha two"), _fact("a3", "alpha three"),
        _fact("b1", "beta one"), _fact("b2", "beta two"), _fact("b3", "beta three"),
        _fact("c1", "gamma one"), _fact("c2", "gamma two"), _fact("c3", "gamma three"),
    ])
    p._mine_alias_candidates()
    assert calls == ["alpha", "beta"]  # 平手 word-asc；gamma 滚下 tick
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    assert set(data["topics"]) == {"alpha", "beta"}
    # 滚下边界：下一 tick 只补挖 gamma
    p._mine_alias_candidates()
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    assert set(data["topics"]) == {"alpha", "beta", "gamma"}


def test_mine_skips_without_ctx(tmp_path, monkeypatch):
    """_main_runtime is None（standalone）-> 跳过不是错误：零 LLM、零写。"""
    p = _make_provider(tmp_path, ctx_llm=None)
    calls = _fake_llm_caller(monkeypatch, {})
    _write_facts(p, [_fact(f"m{i}", f"toolchain one {i}") for i in range(3)])
    p._mine_alias_candidates()
    assert calls == [] and not _aliases_file(p).exists()


# ── 检索面：_expand_query_with_aliases + search_memories 调用点 ────────────


def test_expand_hit_appends_missing_aliases(tmp_path):
    """命中：query 'build system' + 表 {build system: [toolchain]} ->
    展开 'build system toolchain'（canonical 已在 query 中，只补 alias）；
    只含 alias 词的 query（token 命中面）也展开。"""
    p = _make_provider(tmp_path)
    _aliases_file(p).write_text(json.dumps({
        "topics": {"build system": {"aliases": ["toolchain"], "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    assert p._expand_query_with_aliases("build system") == "build system toolchain"
    out2 = p._expand_query_with_aliases("how do I manage toolchain")
    assert "build system" in out2  # alias token 命中 -> canonical 补入


def test_expand_miss_returns_original(tmp_path):
    """未命中：query 'python' 不在任何 topic/alias -> 原 query 一字不变
    （且为同一对象：纯读，零 I/O 写）。"""
    p = _make_provider(tmp_path)
    _aliases_file(p).write_text(json.dumps({
        "topics": {"build system": {"aliases": ["toolchain"], "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    q = "python"
    out = p._expand_query_with_aliases(q)
    assert out == q and out is q


def test_expand_already_present_no_duplicate(tmp_path):
    """已出现的 alias 不重复拼（纯增量：'toolchain' 已在 query -> 只补
    未出现的 'ci'）。"""
    p = _make_provider(tmp_path)
    _aliases_file(p).write_text(json.dumps({
        "topics": {"build system": {"aliases": ["toolchain", "ci"],
                                    "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    assert p._expand_query_with_aliases("build system with toolchain") == \
        "build system with toolchain ci"


def test_search_fixture_toolchain_recall(tmp_path):
    """spec fixture（改前必漏条目改后召回）：pool 一条 content 只含
    toolchain 不含 build system。查 'build system'：
      改前（无别名表）: native keyword gate 0 命中 -> []
      改后（别名表）  : 展开 'build system toolchain' -> 该条目进 eligible 池
    且 alias 只放宽不收紧：直接查 'toolchain' 原生路径仍命中。"""
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("tool_row", "our toolchain pins rustc nightly")])
    before = p.search_memories("build system", limit=5, min_score=0.0)
    assert before == [], "pre-EP-3 native query must 0-hit the toolchain-only row"
    _aliases_file(p).write_text(json.dumps({
        "topics": {"build system": {"aliases": ["toolchain"], "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    after = p.search_memories("build system", limit=5, min_score=0.0)
    assert [r["id"] for r in after] == ["tool_row"]
    direct = p.search_memories("toolchain", limit=5, min_score=0.0)
    assert [r["id"] for r in direct] == ["tool_row"]


def test_corrupt_aliases_fail_soft_search_unaffected(tmp_path):
    """坏 JSON / 非 dict 顶层 aliases.json -> _load_aliases {} fail-soft
    （留痕不炸）；搜索与展开退回原 query，宿主回合不受影响。"""
    p = _make_provider(tmp_path)
    _write_facts(p, [_fact("f1", "the deployer rolls out one")])
    _aliases_file(p).write_text("{this is not json", encoding="utf-8")
    assert p._load_aliases() == {}
    assert p._expand_query_with_aliases("the deployer") == "the deployer"
    res = p.search_memories("deployer", limit=5, min_score=0.0)  # 不炸
    assert [r["id"] for r in res] == ["f1"]
    _aliases_file(p).write_text("[1, 2, 3]", encoding="utf-8")  # 非 dict 顶层
    assert p._load_aliases() == {}


def test_no_alias_table_byte_identical(tmp_path):
    """向后兼容门：无 aliases.json（全新安装）-> _load_aliases {} 且展开
    query byte-identical（返回同一对象），搜索面零行为漂移。"""
    p = _make_provider(tmp_path)
    assert not _aliases_file(p).exists()
    assert p._load_aliases() == {}
    for q in ("build system", "部署 系统", ""):
        out = p._expand_query_with_aliases(q)
        assert out == q and out is q
    _write_facts(p, [_fact("f1", "we pin toolchain alpha")])
    native = p.search_memories("toolchain", limit=5, min_score=0.0)
    assert [r["id"] for r in native] == ["f1"]


def test_alias_table_round_trip_backward_compat(tmp_path, monkeypatch):
    """round-trip：mine 写 -> 读回完整；legacy 消费面容忍畸形条目形状
    （非 dict 条目 / 缺 aliases 键）-> 展开跳过该条目不炸。"""
    p = _make_provider(tmp_path, ctx_llm=_ctx())
    _fake_llm_caller(monkeypatch, {"toolchain": {"aliases": ["c1"], "model_used": True}})
    _write_facts(p, [_fact(f"a{i}", f"toolchain word{i}") for i in range(3)])
    p._mine_alias_candidates()
    data = json.loads(_aliases_file(p).read_text(encoding="utf-8"))
    assert data["topics"]["toolchain"]["aliases"] == ["c1"]  # 写-读回无损
    p2 = _make_provider(tmp_path)
    _aliases_file(p2).write_text(json.dumps(
        {"topics": {"toolchain": "oops", "solo": {"ts": "t"}}, "ts": "t"}),
        encoding="utf-8")
    out = p2._expand_query_with_aliases("toolchain here")
    assert out == "toolchain here"  # 非 dict 条目被容忍：canonical 命中但无可补 -> 原样


# ── 配置块逐键一致 ───────────────────────────────────────────────────────────


def test_config_aliases_key_by_key_identical():
    """decay.json 与 DECAY_DEFAULTS 的 aliases 块逐键一致（两文件均 >0），
    与 consolidation/injection/search 块并列不交叠。P3 起 block 含
    topic_min_len / topic_stopwords 两个噪声过滤键（双同步，逐键一致）。"""
    data = json.loads(DECAY_JSON.read_text(encoding="utf-8"))
    assert "aliases" in data and "aliases" in DECAY_DEFAULTS
    assert data["aliases"] == DECAY_DEFAULTS["aliases"]
    assert data["aliases"]["min_term_freq"] == 3
    assert data["aliases"]["max_topics_per_tick"] == 5
    assert set(data["aliases"]) == {
        "min_term_freq", "max_topics_per_tick",
        "topic_min_len", "topic_stopwords"}
    # P3 defaults: 过滤默认开启 (topic_min_len=2 丢单字符/纯数字) +
    # 英文噪声/技术通用词表 (小写精确匹配, 零第三方, 零硬编码中文规则)。
    assert data["aliases"]["topic_min_len"] == 2
    assert isinstance(data["aliases"]["topic_stopwords"], list)
    assert all(isinstance(w, str) and w == w.lower() for w in data["aliases"]["topic_stopwords"])
    for noise in ("the", "and", "true", "false", "none", "null", "root",
                  "user", "home", "main", "log", "json", "yaml", "config",
                  "file", "path", "http", "https", "api", "hermes"):
        assert noise in data["aliases"]["topic_stopwords"], f"default stopword missing: {noise}"


# ── 零 LLM / 零硬编码审计（grep 语义，不认 status）─────────────────────────


def test_zero_hardcoding_llm_caller_segment():
    """llm_caller.py suggest_aliases 段：model 赋值 / 第三方 SDK = 0；唯一
    LLM 入口 host call surface（_structured_call -> _HOST_CALL_LLM，epic
    card 2）且段内无 task key、无新增 import。"""
    src = LLM_CALLER.read_text(encoding="utf-8")
    seg = src.split("# ── EP-3: alias mining", 1)[1]
    assert not re.search(
        r'model\s*[:=]\s*["a-z]|openai|anthropic|httpx|requests', seg), \
        "hardcoded model/provider/SDK leaked into the EP-3 suggest_aliases segment"
    assert "_structured_call(" in seg
    assert "task=" not in seg and '"task"' not in seg and "'task'" not in seg
    assert not re.search(r"^\s*import |^\s*from ", seg, re.M)  # 段内无新增 import


def test_zero_hardcoding_init_segments():
    """__init__.py：expand 段（检索读面）零 LLM 接触（纯字符串 + JSON 读）；
    mine 段唯一 LLM 入口 = llm.suggest_aliases（宿主面），无 SDK / task= /
    模型名；巩固边界挂载在 _supersede_confirm_candidates 调用之后。"""
    src = INIT_PY.read_text(encoding="utf-8")
    expand_seg = src.split("def _expand_query_with_aliases", 1)[1].split(
        "def search_memories", 1)[0]
    for banned in ("openai", "anthropic", "httpx", "requests", "task=",
                   "complete_structured", "llm_caller", "suggest_aliases"):
        assert banned not in expand_seg, \
            f"LLM touch leaked into the EP-3 read face: {banned}"
    mine_seg = src.split("def _mine_alias_candidates", 1)[1].split(
        "def _spawn_llm_consolidation", 1)[0]
    for banned in ("openai", "anthropic", "httpx", "requests", "task=",
                   "complete_structured"):
        assert banned not in mine_seg, \
            f"SDK/hardcoding leaked into the EP-3 mine face: {banned}"
    assert "llm.suggest_aliases" in mine_seg  # the ONLY LLM entry
    # 巩固边界挂载顺序：confirm 调用在前，mine 调用在后（同一异步线程）
    run_seg = src.split("def _spawn_llm_consolidation", 1)[1]
    assert run_seg.index("_supersede_confirm_candidates()") < \
        run_seg.index("self._mine_alias_candidates()")
    # 检索面调用点：build_search_axes 吃展开后 query；legacy gate 用展开词
    sm_seg = src.split("def search_memories", 1)[1].split(
        "\n    def _memories_rotate_mb", 1)[0]
    assert "query_for_axes = self._expand_query_with_aliases(query)" in sm_seg
    assert "query_for_axes, decay=self._decay" in sm_seg
    assert "query_words = set(query_for_axes.lower().split())" in sm_seg


def test_cli_zero_llm_zero_hardcoded_paths():
    """CLI 全文：零 LLM import/调用、零第三方 SDK、零硬编码模型名/路径。"""
    src = CLI_SCRIPT.read_text(encoding="utf-8")
    import_lines = [l for l in src.splitlines()
                    if re.match(r"\s*(import |from .+ import )", l)]
    joined = "\n".join(import_lines)
    assert "llm" not in joined and "consolidation" not in joined
    assert not re.search(r"\bllm_caller\.\w+\(|complete_structured\(", src)
    for banned in ("openai", "anthropic", "httpx", "requests"):
        assert banned not in src
    assert not re.search(r'["\'](/root|/usr/local|C:\\\\)', src)
    assert 'os.environ.get("HERMES_HOME"' in src


# ── CLI：--list 只读 / --add / --remove / --prune 确定性 + 审计 ────────────


def test_cli_list_read_only(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "memories").mkdir()
    (store / "aliases.json").write_text(json.dumps({
        "topics": {"build system": {"aliases": ["toolchain"], "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    before = _store_hash(store)
    r = _run_cli(store, "--list")
    assert r.returncode == 0, r.stderr
    assert "[build system] -> [toolchain]" in r.stdout
    assert _store_hash(store) == before  # 零写门：前后 store 逐字节一致


def test_cli_add_remove_deterministic(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    r = _run_cli(store, "--add", "build system", "toolchain", "bazel")
    assert r.returncode == 0, r.stderr
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["build system"]["aliases"] == ["bazel", "toolchain"]
    assert data["topics"]["build system"]["source"] == "human"
    # 确定性补别名不删旧：再 add -> 并集
    r = _run_cli(store, "--add", "build system", "maven")
    assert r.returncode == 0, r.stderr
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["build system"]["aliases"] == ["bazel", "maven", "toolchain"]
    log = [json.loads(l) for l in
           (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [x["action"] for x in log] == ["alias_add_human", "alias_add_human"]
    assert log[0]["added"] == ["bazel", "toolchain"]
    # --remove 删 topic（其他 topic 原样保留）
    _run_cli(store, "--add", "k8s", "pod")
    r = _run_cli(store, "--remove", "build system")
    assert r.returncode == 0, r.stderr
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    assert set(data["topics"]) == {"k8s"}
    log = [json.loads(l) for l in
           (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[-1]["action"] == "alias_remove_human"
    assert log[-1]["topic"] == "build system" and log[-1]["existed"] is True


def test_cli_prune_zero_hit_streak(tmp_path):
    """惰性清理：连续 3 次 alias_search_hit 命中 0 的 alias 被剔除；有 1
    次非 0 命中的保留（fail-soft：无命中记录 -> 永不删）。"""
    store = tmp_path / "store"
    store.mkdir()
    (store / "aliases.json").write_text(json.dumps({
        "topics": {"build system": {"aliases": ["dead", "alive"], "source": "llm", "ts": "t"}},
        "ts": "t"}), encoding="utf-8")
    logf = store / "consolidation_log.jsonl"
    with open(logf, "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({"action": "alias_search_hit", "topic": "build system",
                                "alias": "dead", "hits": 0, "i": i}) + "\n")
        f.write(json.dumps({"action": "alias_search_hit", "topic": "build system",
                            "alias": "alive", "hits": 2}) + "\n")
    r = _run_cli(store, "--prune")
    assert r.returncode == 0, r.stderr
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    entry = data["topics"]["build system"]
    assert entry["aliases"] == ["alive"]
    assert entry["pruned"] == ["dead"]
    log = [json.loads(l) for l in logf.read_text(encoding="utf-8").splitlines()]
    assert log[-1]["action"] == "alias_prune"
    assert log[-1]["pruned"] == {"build system": ["dead"]}
    # 再跑一次：alive 无 0-hit 三连 -> 幂等 no-op
    r = _run_cli(store, "--prune")
    assert r.returncode == 0 and "no aliases pruned" in r.stdout
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["build system"]["aliases"] == ["alive"]


def test_cli_concurrent_add_no_tear(tmp_path):
    """并发门：4 个 CLI 进程同时 --add 同 topic（_shard_lock fcntl）->
    aliases.json 不撕裂且 4 条别名全在（N processes, 0 lost updates）；
    审计链 append-only 无丢行。"""
    store = tmp_path / "store"
    store.mkdir()
    procs = [subprocess.Popen(
        [sys.executable, str(CLI_SCRIPT), "--store", str(store),
         "--add", "build system", f"alias-{i}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
    outs = [p.communicate(timeout=60) for p in procs]
    for i, (out, err) in enumerate(outs):
        assert procs[i].returncode == 0, err
    data = json.loads((store / "aliases.json").read_text(encoding="utf-8"))
    assert data["topics"]["build system"]["aliases"] == \
        ["alias-0", "alias-1", "alias-2", "alias-3"]
    log = [json.loads(l) for l in
           (store / "consolidation_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(log) == 4 and all(x["action"] == "alias_add_human" for x in log)


# ── 巩固边界挂载（rescore -> confirm -> mine，同一异步线程）────────────────


def test_spawn_runs_rescore_then_confirm_then_mine(tmp_path, monkeypatch):
    """_spawn_llm_consolidation._run 顺序：_llm_consolidate ->
    _supersede_confirm_candidates -> _mine_alias_candidates；mine 段异常
    不炸前两段（独立 fail-soft try/except）。"""
    import agent.memory_provider as amp
    # S1b F1: the spawn now .start()s the host-created thread, so the fake
    # must return a .start()-able object (run the 7-segment body sync for
    # deterministic order assertions). interval=0 keeps this boundary-face
    # test throttle-free without touching any production gate default.
    decay = dict(DECAY_DEFAULTS)
    decay["consolidation"] = dict(DECAY_DEFAULTS["consolidation"],
                                   consolidation_interval_min=0)
    p = _make_provider(tmp_path, ctx_llm=_ctx(), decay=decay)
    order = []

    def _rec(name):
        def _fn(*a, **kw):
            order.append(name)
        return _fn

    p._llm_consolidate = _rec("llm_consolidate")
    p._supersede_confirm_candidates = _rec("supersede_confirm")
    p._mine_alias_candidates = _rec("alias_mine")
    class _FakeThread:
        def start(self):
            return None

    def _fake_spawn(fn, **kw):
        fn()  # sync-run _run body (rescore -> confirm -> mine order)
        return _FakeThread()

    monkeypatch.setattr(amp, "spawn_context_thread", _fake_spawn, raising=False)
    p._spawn_llm_consolidation()
    assert order == ["llm_consolidate", "supersede_confirm", "alias_mine"]
    # mine 异常 fail-soft：前两段照常完成，宿主回合不受影响
    order.clear()

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    p._mine_alias_candidates = _boom
    p._spawn_llm_consolidation()
    assert order[:2] == ["llm_consolidate", "supersede_confirm"]
