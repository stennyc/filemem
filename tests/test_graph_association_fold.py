"""P8 对齐 Go: build_graph 的 C6④ ledger fold — 把 associate 台账折进 graph.json
(kind=association, w=0.5) + 指纹纳入台账 digest.

对齐语义 (以 Go graph_build.go 为规格源):
- 台账行 {a, b, ...}, 端点对 (lo,hi) 排序; 两端都在 node set 才加边; 端点离池 skip
- 缺台账 = 空 content digest + 无边姿态 (零边, 不静默丢)
- 台账变更 -> 指纹必变 (fold re-runs); 台账被删 -> association 边按设计落
- 指纹 recipe = nodes + minimal edges + ledger digest (build 与 graph_fingerprint 一致)
- association 边排 3 类确定性边之后, 受 _TOTAL_CAP + _DEG_CAP 约束
"""
import sys
import json
import hashlib
import tempfile
import pathlib

sys.path.insert(0, "/root/.hermes/plugins/filemem")
sys.path.insert(0, "/root/.hermes/plugins/filemem/tools")
import build_graph as bg  # noqa: E402


def _mk(root: pathlib.Path, rows, ledger_lines):
    mem = root / "memories"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "fact.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    if ledger_lines:
        (root / "association_edges.jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in ledger_lines),
            encoding="utf-8")


def test_p8_association_edge_in_graph():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _mk(root,
            [{"id": "mem_x", "type": "fact", "content": "service binds to port 8080",
              "created_at": "2026-01-01"},
             {"id": "mem_y", "type": "fact", "content": "service binds to port 3000",
              "created_at": "2026-01-02"}],
            [{"a": "mem_x", "b": "mem_y", "relation": "contrast"}])
        g = bg.build_graph(root)
        assoc = [e for e in g["edges"] if e["kind"] == "association"]
        assert len(assoc) == 1, f"expect 1 association edge, got {assoc}"
        assert assoc[0]["w"] == 0.5, assoc[0]
        assert assoc[0]["src"] == "mem_x" and assoc[0]["dst"] == "mem_y", assoc[0]
        # ledger digest persisted on the doc
        assert g["ledger_digest"] == hashlib.sha256(
            (root / "association_edges.jsonl").read_bytes()).hexdigest()
        print("PASS P8: association edge (w=0.5) in graph + ledger_digest on doc")


def test_p8_fingerprint_idempotent():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _mk(root,
            [{"id": "a", "type": "fact", "content": "alpha row", "created_at": "2026-01-01"},
             {"id": "b", "type": "fact", "content": "beta row", "created_at": "2026-01-02"}],
            [{"a": "a", "b": "b", "relation": "related"}])
        g = bg.build_graph(root)
        # re-run drift check must match the build's own fingerprint
        assert bg.graph_fingerprint(g) == g["fingerprint"], \
            "graph_fingerprint must use the same recipe as build (incl. ledger)"
        print("PASS P8: build fingerprint == graph_fingerprint (idempotent recipe)")


def test_p8_ledger_change_flips_fingerprint():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _mk(root,
            [{"id": "a", "type": "fact", "content": "alpha row", "created_at": "2026-01-01"},
             {"id": "b", "type": "fact", "content": "beta row", "created_at": "2026-01-02"}],
            [{"a": "a", "b": "b", "relation": "v1"}])
        g1 = bg.build_graph(root)
        # append a second ledger line -> the fold MUST re-run (fingerprint flips)
        (root / "association_edges.jsonl").write_text(
            (root / "association_edges.jsonl").read_text()
            + json.dumps({"a": "a", "b": "b", "relation": "v2"}) + "\n")
        g2 = bg.build_graph(root)
        assert g2["fingerprint"] != g1["fingerprint"], "ledger change must flip fingerprint"
        assert g2["ledger_digest"] != g1["ledger_digest"]
        print("PASS P8: ledger change flips the fingerprint (fold re-runs)")


def test_p8_endpoint_left_pool_edge_drops():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # mem_x is in the pool; mem_gone is in the ledger but NOT a row
        _mk(root,
            [{"id": "mem_x", "type": "fact", "content": "alpha row", "created_at": "2026-01-01"}],
            [{"a": "mem_x", "b": "mem_gone", "relation": "dangling"}])
        g = bg.build_graph(root)
        assoc = [e for e in g["edges"] if e["kind"] == "association"]
        assert assoc == [], f"endpoint left the node set -> edge drops, got {assoc}"
        assert any("mem_gone" in n for n in g.get("association_notes", [])), \
            f"the skip must be noted, got {g.get('association_notes')}"
        print("PASS P8: ledger endpoint not in node set -> edge drops + noted")


def test_p8_missing_ledger_zero_posture():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        _mk(root, [{"id": "solo", "type": "fact", "content": "lone row",
                    "created_at": "2026-01-01"}], ledger_lines=None)
        g = bg.build_graph(root)
        # missing ledger = empty-content digest, no association edge
        assert g["ledger_digest"] == hashlib.sha256(b"").hexdigest()
        assert not any(e["kind"] == "association" for e in g["edges"])
        print("PASS P8: missing ledger -> empty digest + no association edge")


def main():
    tests = [
        test_p8_association_edge_in_graph,
        test_p8_fingerprint_idempotent,
        test_p8_ledger_change_flips_fingerprint,
        test_p8_endpoint_left_pool_edge_drops,
        test_p8_missing_ledger_zero_posture,
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
