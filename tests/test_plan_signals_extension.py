#!/usr/bin/env python3
"""Owner 2026-11-21 信号词扩容 (R2 审计漏检修复, Go/Python 两侧同步).

The PLAN-kbs-dsh-R2.md false negative: the plan doc's vocabulary
("修复计划" / "先出一份计划" / the PLAN-* naming) hit the pre-screen
vocabulary on ZERO counts -> prescreen_skip -> zero LLM calls -> no
PENDING marker -> the plan never reached the clarify panel. The
extension adds:
  - layer A (_PLAN_LAND_SIGNALS): "plan-" (the PLAN-* doc-naming
    prefix; reaches the gate through the lowercased "[file: path]"
    slice-6 tag),
  - layer B (_MEMORY_TYPE_REGEX['plan'] CJK alternative): the bare
    "计划" single form (subsumes 实施/执行计划 et al.).
The pre-screen stays a COST GATE ONLY: the LLM verdict + the
confirm-chain double gate remain the backstop (the owner's
宁漏勿炸 / 双门兜底 rulings, unchanged).
"""
import re
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import __init__ as fm


def test_plan_prefix_in_land_signals():
    assert "plan-" in fm._PLAN_LAND_SIGNALS, \
        "the PLAN-* doc-naming prefix must be a layer-A land signal"


def test_bare_jihua_passes_plan_regex():
    pats = fm._MEMORY_TYPE_REGEX.get("plan", ())
    assert any(re.search(p, "先出一份计划") for p in pats), \
        "bare 计划 must pass the plan pre-screen regex (the R2 false-negative fix)"


def test_plan_doc_head_hits_layer_a():
    # the slice-6 probe input: the lowercased "[file: path]" tag + head
    content = "[file: /root/workspace/PLAN-kbs-dsh-R2.md]\n二审遗漏修复"
    lc = content.lower()
    assert any(s.lower() in lc for s in fm._PLAN_LAND_SIGNALS), \
        "the PLAN-* file-tag input must pass layer A"


def test_neutral_line_still_misses_both_layers():
    # the pin: the extension must not turn neutral lines into hits
    neutral = "the build caches the venv under /opt/cache"
    lc = neutral.lower()
    hit = (any(s.lower() in lc for s in fm._PLAN_LAND_SIGNALS)
           or any(re.search(p, lc) for p in fm._MEMORY_TYPE_REGEX.get("plan", ())))
    assert not hit, "a neutral line must stay a prescreen_skip"
