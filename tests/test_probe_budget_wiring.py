"""Probe budget 收口测试（owner 2026-11-12）：plan_probe / scheduled_probe
两个活体 LLM 探测面的预算门从"死文档承诺"变成"真接线"。

锁定三件事（全部走 construct 面 + 直接调 probe 面，不驱动 initialize()、
不碰盘上中央 store）：
  1. FileMemProvider() construct 后，四个 attr 都拿到确定值
     （_plan_probe_limit/_scheduled_probe_limit == 3 代码默认；
     _plan_probe_budget/_scheduled_probe_budget == {} 稳定 dict）
  2. budget dict 跨调用持久（同实例第二次调用能读到第一次 charge 的值
     —— 锁死 reviewer 2026-11-12 那条 NIT：以前部分构造实例每次调用
     都重建成空 dict，预算门永远不会 trip）
  3. DECAY_DEFAULTS（consolidation/__init__.py）与 decay.json 两侧的
     plan_probe / scheduled_probe 块值逐字一致（S2 egress parity 不变量
     的对应物：新增的 key 不是单侧漂移，是两侧同步扩展）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import __init__ as fm  # noqa: E402
from consolidation import DECAY_DEFAULTS  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _construct():
    """真实走 __init__（不是 __new__ 部分构造），保证测的是宿主路径
    真实会看到的 construct 结果。"""
    return fm.FileMemProvider()


def test_construct_seeds_probe_budget_dicts():
    p = _construct()
    assert isinstance(p._plan_probe_budget, dict), \
        "_plan_probe_budget must be a stable dict at construct time"
    assert isinstance(p._scheduled_probe_budget, dict), \
        "_scheduled_probe_budget must be a stable dict at construct time"
    assert p._plan_probe_budget == {}
    assert p._scheduled_probe_budget == {}


def test_construct_sets_code_default_probe_limits():
    p = _construct()
    assert p._plan_probe_limit == 3
    assert p._scheduled_probe_limit == 3


def test_scheduled_probe_budget_persists_across_calls():
    """锁死 NIT：同一实例连调 _scheduled_probe，第二次调用必须读到
    第一次 charge 的 budget[sid]（跨调用持久），而不是每次都重建成
    空 dict 导致预算门永不 trip。"""
    p = _construct()
    p._main_runtime = {"model": "x", "provider": "x", "base_url": "http://x",
                       "api_key": "***", "api_mode": "chat"}
    p._turn_active_sid = "s1"
    p._scheduled_probe_limit = 1
    # 手动塞一个已 charge 的状态进去（模拟第一次调用已经 charge 过）
    p._scheduled_probe_budget["s1"] = 1
    out = p._scheduled_probe("重启完跑 X")  # 宽表命中 "重启完"，但已超预算
    assert out is None
    # budget 没有被 +1（因为根本没进 LLM 调用分支），也没有被重建成空 dict
    assert p._scheduled_probe_budget["s1"] == 1
    assert isinstance(p._scheduled_probe_budget, dict) and p._scheduled_probe_budget is not None


def test_decay_defaults_carry_probe_blocks_in_lockstep():
    """DECAY_DEFAULTS（代码侧）必须跟 decay.json（盘上）一样带
    plan_probe / scheduled_probe 两个块，且值逐字相等 —— 否则 S2 egress
    parity 测试（test_s2_egress_governance）会把它们算成单侧漂移。"""
    decay_json = json.loads((PLUGIN_DIR / "decay.json").read_text(encoding="utf-8"))
    for block in ("plan_probe", "scheduled_probe"):
        assert block in DECAY_DEFAULTS, f"DECAY_DEFAULTS missing {block} block"
        assert DECAY_DEFAULTS[block] == decay_json[block], \
            f"{block} drifted between DECAY_DEFAULTS and decay.json: " \
            f"{DECAY_DEFAULTS[block]} != {decay_json[block]}"
