"""P8 读面同步: build 出的 association 边被读面 _graph_static 真消费
(1-hop 邻域经 association 边带出 query 命不中的行) + 走 0.5 系数 (非默认 1.0).

完整链路 (先 build 再测, 不只看系数表):
  STEP1 build_graph -> graph.json 含 kind=association 边 (w=0.5)
  STEP2 search_index._graph_static(graph, pool) -> fc 经 association 边进
        fa 的 1-hop 邻域 (确定性 semantic/temporal 连不到的 fc 被带出)
  STEP3 _GRAPH_NEIGHBOR_KIND_COEF["association"]==0.5 (中等强度软边,
        弱于 semantic 1.0, 强于 co-occurrence 0.4, 非默认 1.0 最强)
"""
import sys
import json
import tempfile
import pathlib

sys.path.insert(0, "/root/.hermes/plugins/filemem")
sys.path.insert(0, "/root/.hermes/plugins/filemem/tools")
import build_graph as bg  # noqa: E402
import search_index as si  # noqa: E402


def _mk(root: pathlib.Path, rows, ledger_lines):
    mem = root / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "fact.json").write_text(json.dumps(rows, ensure_ascii=False))
    if ledger_lines:
        (root / "association_edges.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in ledger_lines))


def test_read_side_consumes_built_association_edge():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # fa/fb 高共现 (query 命中), fc 与 query 无 BM25/共现 -> 只靠 LLM 软边 fa<->fc
        _mk(root,
            [{"id": "fa", "type": "fact", "content": "payment gateway retries failed charges",
              "created_at": "2026-01-01"},
             {"id": "fb", "type": "fact", "content": "payment gateway retries failed charges 2 days",
              "created_at": "2026-01-01"},
             {"id": "fc", "type": "fact", "content": "on-call rotation 4-hour paging window",
              "created_at": "2026-01-05"}],
            [{"a": "fa", "b": "fc", "relation": "topic"}])
        # STEP1: build (必须先 build, 读面消费的是 build 出的 association 边)
        g = bg.build_graph(root)
        assert [e for e in g["edges"] if e["kind"] == "association"], "build must emit the association edge"
        # STEP2: 读面 _graph_static -> fc 经 association 边进 fa 的 1-hop 邻域
        static = si._graph_static(g, [r for r in json.loads((root / "memories" / "fact.json").read_text())])
        adjacency = static[0]
        assert "fc" in adjacency.get("fa", {}), \
            f"association edge must pull fc into fa's 1-hop neighborhood, got {adjacency}"
        assert adjacency["fa"]["fc"] == "association", adjacency["fa"]
        # 对照: 无 graph 时 fa 的 1-hop 邻域不含 fc
        static_ng = si._graph_static(None, json.loads((root / "memories" / "fact.json").read_text()))
        assert "fc" not in static_ng[0].get("fa", {}), "no graph -> fc not in fa's neighborhood"
        print("PASS: build 出的 association 边被读面 _graph_static 真消费 (fc 进 fa 1-hop 邻域)")


def test_read_side_association_coef_is_0p5():
    # STEP3: association 走 0.5 系数 (对齐 Go C6① 0.5 软边), 非默认 1.0 最强
    coef = si._GRAPH_NEIGHBOR_KIND_COEF["association"]
    assert coef == 0.5, coef
    assert coef < si._GRAPH_NEIGHBOR_KIND_COEF["semantic"], "assoc must be weaker than semantic"
    assert coef > si._GRAPH_NEIGHBOR_KIND_COEF["co-occurrence"], "assoc stronger than co-occurrence"
    print(f"PASS: 读面 association 系数 = {coef} (0.4 < 0.5 < 1.0, 软边中等强度)")


def main():
    tests = [
        test_read_side_consumes_built_association_edge,
        test_read_side_association_coef_is_0p5,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
