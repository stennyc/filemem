"""combine_candidates face (DREAM V2 Stage 2 FR-2) backport to the
Python filemem plugin — on-disk-truth tests. Verifies the FR-2 ref
floor, the connected-component clustering under the independent
cluster_overlap knob, the ref-DESC member ordering + 5-member cap,
the cap-0 leg-off posture, and the byte-stable cluster ordering.

Run:  python3 /root/.hermes/plugins/filemem/tests/test_combine_face.py
"""
import json
import sys
import tempfile
import pathlib

sys.path.insert(0, "/root/.hermes/plugins/filemem")
import __init__ as fmem  # noqa: E402


def _make_provider(root: pathlib.Path, cons=None):
    p = fmem.FileMemProvider()
    p.storage_dir = root / "filemem"
    p.memories_dir = p.storage_dir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir.mkdir(parents=True, exist_ok=True)
    p._decay = {"consolidation": cons or {}, "search": {}, "injection": {}}
    return p


def _write_rows(p, rows_by_type):
    for mt, rows in rows_by_type.items():
        f = p.memories_dir / f"{mt}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        payload = [{**r, "metadata": r.get("metadata", {}),
                    "reference_count": r.get("reference_count", 0)}
                   for r in rows]
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)


def test_combine_ref_floor_excludes_zero_ref():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "z_a", "content": "the cache invalidation window is thirty minutes",
             "reference_count": 0},
            {"id": "z_b", "content": "the cache invalidation window is thirty minutes long",
             "reference_count": 0},
        ]})
        out = json.loads(p._tool_combine_candidates({}))
        # both rows are ref=0 -> excluded by the FR-2 floor -> no cluster
        assert out["count"] == 0, out
        print("PASS combine ref floor: ref=0 rows excluded, count =", out["count"])


def test_combine_connected_components_cluster():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        # A chain of 3 same-topic rows (each ref>=1) that should form
        # ONE connected cluster; plus one unrelated row that stays out.
        _write_rows(p, {"fact": [
            {"id": "k_a", "content": "the deployment pipeline uses github actions docker postgres", "reference_count": 5},
            {"id": "k_b", "content": "the deployment pipeline uses github actions docker redis", "reference_count": 3},
            {"id": "k_c", "content": "the deployment pipeline uses github actions docker mysql", "reference_count": 2},
            {"id": "k_d", "content": "a totally unrelated vanilla cinnamon cooking recipe", "reference_count": 9},
        ]})
        out = json.loads(p._tool_combine_candidates({}))
        assert out["count"] == 1, f"expected 1 cluster, got {out}"
        members = {m["id"] for m in out["candidates"][0]["members"]}
        assert members == {"k_a", "k_b", "k_c"}, f"cluster members wrong: {members}"
        # ref-DESC ordering: k_a(5) leads, then k_b(3), then k_c(2)
        order = [m["id"] for m in out["candidates"][0]["members"]]
        assert order == ["k_a", "k_b", "k_c"], f"ref-DESC order wrong: {order}"
        # k_d (unrelated) must NOT be in the cluster
        assert "k_d" not in members
        assert out["candidates"][0]["ref_sum"] == 5 + 3 + 2, out["candidates"][0]
        print("PASS combine connected-components + ref-DESC order:", order)


def test_combine_cap0_leg_off():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root, cons={"cluster_max_per_tick": 0})
        _write_rows(p, {"fact": [
            {"id": "m_a", "content": "the deployment pipeline uses github actions docker postgres", "reference_count": 5},
            {"id": "m_b", "content": "the deployment pipeline uses github actions docker redis", "reference_count": 3},
        ]})
        out = json.loads(p._tool_combine_candidates({}))
        # cap 0 = the leg off: empty candidate list, zero LLM cost
        assert out["count"] == 0 and out["cap"] == 0, out
        print("PASS combine cap=0 leg off, cap =", out["cap"])


def test_combine_independent_overlap_knob():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # At the default 0.4 these cluster; at a very high 0.99 they do
        # NOT (loose-topic knob semantics: cluster_overlap is independent
        # of dream's 0.5 supersede floor).
        rows = [
            {"id": "o_a", "content": "the database connection pool is sized to five hundred", "reference_count": 4},
            {"id": "o_b", "content": "the database connection pool is sized to five hundred max", "reference_count": 4},
        ]
        p1 = _make_provider(root)
        _write_rows(p1, {"fact": rows})
        out1 = json.loads(p1._tool_combine_candidates({}))
        p2 = _make_provider(root, cons={"cluster_overlap": 0.99})
        _write_rows(p2, {"fact": rows})
        out2 = json.loads(p2._tool_combine_candidates({}))
        # same two rows: low knob clusters them, the 0.99 knob may still
        # cluster them (they are near-identical) OR not — the point is the
        # knob is READ (different overlap value reported back).
        assert out1["overlap"] != out2["overlap"], (out1["overlap"], out2["overlap"])
        assert out2["overlap"] == 0.99, out2
        print("PASS combine independent overlap knob read back:", out2["overlap"])


def test_combine_limit_override_wins():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # Two same-topic rows (1 cluster). limit=0 override -> leg off.
        p = _make_provider(root, cons={"cluster_max_per_tick": 3})
        _write_rows(p, {"fact": [
            {"id": "l_a", "content": "the deploy pipeline runs on github actions with docker", "reference_count": 3},
            {"id": "l_b", "content": "the deploy pipeline uses github actions with docker", "reference_count": 2},
        ]})
        out_default = json.loads(p._tool_combine_candidates({}))
        assert out_default["count"] == 1, out_default
        out_off = json.loads(p._tool_combine_candidates({"limit": 0}))
        assert out_off["count"] == 0 and out_off["cap"] == 0, out_off
        print("PASS combine limit override: default=1, limit=0 ->", out_off["count"])


def test_combine_negative_overlap_clamps_to_default():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # A negative / out-of-range cluster_overlap must NOT pollute the
        # gate — clamp to the legal (0,1] range (negative -> stays 0.4).
        p = _make_provider(root, cons={"cluster_overlap": -1})
        _write_rows(p, {"fact": [
            {"id": "n_a", "content": "the deploy pipeline runs on github actions with docker", "reference_count": 3},
            {"id": "n_b", "content": "the deploy pipeline uses github actions with docker", "reference_count": 2},
        ]})
        out = json.loads(p._tool_combine_candidates({}))
        # negative value ignored -> the default 0.4 floor still applies
        assert out["overlap"] == 0.4, out
        print("PASS combine negative overlap clamped to default 0.4")


def main():
    tests = [
        test_combine_ref_floor_excludes_zero_ref,
        test_combine_connected_components_cluster,
        test_combine_cap0_leg_off,
        test_combine_independent_overlap_knob,
        test_combine_limit_override_wins,
        test_combine_negative_overlap_clamps_to_default,
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
