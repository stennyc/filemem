"""dream_pairs face (DREAM V2 C5 S3①/②) backport to the Python filemem
plugin — on-disk-truth tests. Drives the real provider against a tmp
storage root; verifies candidates (Jaccard coarse filter + directive
exclusion + dup-id guard + lane cap) and verdicts (merge fold = soft
superseded flag, contradiction = stale stamp, out-of-pair/self-pair
refusal) WITHOUT trusting self-report.

Run:  python3 /root/.hermes/plugins/filemem/tests/test_dream_pairs_face.py
"""
import json
import sys
import tempfile
import pathlib

sys.path.insert(0, "/root/.hermes/plugins/filemem")
import __init__ as fmem  # noqa: E402


def _make_provider(root: pathlib.Path):
    p = fmem.FileMemProvider()
    p.storage_dir = root / "filemem"
    p.memories_dir = p.storage_dir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir.mkdir(parents=True, exist_ok=True)
    p._decay = {"consolidation": {}, "search": {}, "injection": {}}
    return p


def _write_rows(p, rows_by_type):
    for mt, rows in rows_by_type.items():
        f = p.memories_dir / f"{mt}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        payload = [{**r, "metadata": r.get("metadata", {})} for r in rows]
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)


def test_dream_candidates_jaccard():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "d_a", "content": "the build pipeline runs on github actions with a cached docker layer",
             "created_at": "2026-01-01T00:00:00"},
            {"id": "d_b", "content": "the build pipeline uses github actions with a cached docker layer",
             "created_at": "2026-02-01T00:00:00"},
            {"id": "d_c", "content": "unrelated pasta recipe with basil and olive oil",
             "created_at": "2026-01-01T00:00:00"},
        ]})
        out = json.loads(p._tool_dream_pairs({"action": "candidates"}))
        assert out["ok"] is True
        ids = {(pr["a"]["id"], pr["b"]["id"]) for pr in out["pairs"]}
        assert ("d_a", "d_b") in ids, f"expected d_a/d_b high-overlap pair, got {ids}"
        assert not any("d_c" in (x, y) for x, y in ids), f"d_c low overlap should not pair, got {ids}"
        print("PASS dream candidates:", out["count"], "pair(s)")


def test_dream_merge_verdict_soft_supersede():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "d_a", "content": "the build pipeline runs on github actions with a cached docker layer",
             "created_at": "2026-01-01T00:00:00"},
            {"id": "d_b", "content": "the build pipeline uses github actions with a cached docker layer",
             "created_at": "2026-02-01T00:00:00"},
        ]})
        # merge with keep=d_b (the newer one): loser=d_a gets the soft
        # superseded flag + superseded_by=d_b.
        r = json.loads(p._tool_dream_pairs({"action": "verdicts", "verdicts": [
            {"face": "merge", "a_id": "d_a", "b_id": "d_b",
             "keep_id": "d_b", "reason": "d_b is the newer, canonical wording"}]}))
        assert r["merged"] == ["d_b<-d_a"], r
        # on-disk truth: d_a now carries metadata.superseded + superseded_by
        rows = json.loads((p.memories_dir / "fact.json").read_text())
        by_id = {x["id"]: x for x in rows}
        assert by_id["d_a"]["metadata"]["superseded"] is True, by_id["d_a"]["metadata"]
        assert by_id["d_a"]["metadata"]["superseded_by"] == "d_b", by_id["d_a"]["metadata"]
        assert "superseded" not in (by_id["d_b"]["metadata"] or {}) or \
            by_id["d_b"]["metadata"].get("superseded") is not True
        # and the folded loser now leaves the live pool on the next candidates pass
        out2 = json.loads(p._tool_dream_pairs({"action": "candidates"}))
        assert out2["count"] == 0, f"folded pair must drop out of the live pool, got {out2}"
        print("PASS dream merge verdict -> soft superseded, pool now empty")


def test_dream_contradiction_verdict_stale_stamp():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "c_a", "content": "the service binds to port 8080 by default", "created_at": "2026-01-01T00:00:00"},
            {"id": "c_b", "content": "the service binds to port 3000 by default", "created_at": "2026-01-01T00:00:00"},
        ]})
        r = json.loads(p._tool_dream_pairs({"action": "verdicts", "verdicts": [
            {"face": "contradiction", "a_id": "c_a", "b_id": "c_b",
             "stale_id": "c_a", "reason": "c_b is the current truth"}]}))
        assert r["staled"] == ["c_a"], r
        rows = json.loads((p.memories_dir / "fact.json").read_text())
        by_id = {x["id"]: x for x in rows}
        assert by_id["c_a"]["metadata"]["stale"] is True, by_id["c_a"]["metadata"]
        assert by_id["c_a"]["metadata"].get("stale_reason"), by_id["c_a"]["metadata"]
        print("PASS dream contradiction verdict -> stale stamp")


def test_dream_contradiction_default_stale_older_of():
    """P4: 缺省 stale 目标 = olderOf 语义 (a 较旧 -> 不传 stale_id 时标 a)."""
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        # o_a 较旧 (01-01), o_b 较新 (03-01): 矛盾时缺省应标较旧的 o_a stale.
        _write_rows(p, {"fact": [
            {"id": "o_a", "content": "the service binds to port 8080", "created_at": "2026-01-01T00:00:00"},
            {"id": "o_b", "content": "the service binds to port 3000", "created_at": "2026-03-01T00:00:00"},
        ]})
        # NO stale_id: default target must be the OLDER row (o_a), not fixed b_id.
        r = json.loads(p._tool_dream_pairs({"action": "verdicts", "verdicts": [
            {"face": "contradiction", "a_id": "o_a", "b_id": "o_b",
             "reason": "o_b is the current truth"}]}))
        assert r["staled"] == ["o_a"], f"olderOf default must stamp the older row o_a, got {r}"
        rows = json.loads((p.memories_dir / "fact.json").read_text())
        by_id = {x["id"]: x for x in rows}
        assert by_id["o_a"]["metadata"]["stale"] is True, by_id["o_a"]["metadata"]
        # o_b (the newer, current truth) must NOT be staled.
        assert by_id["o_b"]["metadata"].get("stale") is not True, by_id["o_b"]["metadata"]
        print("PASS dream contradiction default -> olderOf (stamps the older row)")


def test_dream_out_of_pair_and_self_pair_refused():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "d_a", "content": "the build pipeline runs on github actions", "created_at": "2026-01-01T00:00:00"},
        ]})
        # (a) merge keep OUTSIDE the pair -> refused
        r1 = json.loads(p._tool_dream_pairs({"action": "verdicts", "verdicts": [
            {"face": "merge", "a_id": "d_a", "b_id": "ghost",
             "keep_id": "ghost", "reason": "keep a non-pair id"}]}))
        # b_id 'ghost' is out-of-pool so the pair itself is not live; the
        # keep guard fires: refused contains 'ghost'.
        assert "ghost" in r1["refused"], r1
        # (b) self-pair (a_id == b_id) -> refused + no apply
        _write_rows(p, {"fact": [
            {"id": "d_a", "content": "the build pipeline runs on github actions", "created_at": "2026-01-01T00:00:00"},
        ]})
        r2 = json.loads(p._tool_dream_pairs({"action": "verdicts", "verdicts": [
            {"face": "merge", "a_id": "d_a", "b_id": "d_a",
             "keep_id": "d_a", "reason": "self-pair must be refused"}]}))
        assert r2["merged"] == [] and "d_a" in r2["refused"], r2
        # on-disk truth: d_a must NOT have been superseded (self-pair refused)
        rows = json.loads((p.memories_dir / "fact.json").read_text())
        assert rows[0]["metadata"].get("superseded") is not True, rows[0]["metadata"]
        print("PASS out-of-pair + self-pair refusal, refused:", r1["refused"], r2["refused"])


def test_dream_directive_pair_exclusion():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        # directive table: any row matching 'always' is directive-signalled.
        p._decay["injection"]["directive_patterns"] = ["\\balways\\b"]
        _write_rows(p, {"fact": [
            {"id": "n_a", "content": "always run the tests before shipping", "created_at": "2026-01-01T00:00:00"},
            {"id": "n_b", "content": "always run the tests before shipping release", "created_at": "2026-01-01T00:00:00"},
            {"id": "n_c", "content": "the cache invalidation window is thirty minutes", "created_at": "2026-01-01T00:00:00"},
            {"id": "n_d", "content": "the cache invalidation window is thirty minutes long", "created_at": "2026-01-01T00:00:00"},
        ]})
        out = json.loads(p._tool_dream_pairs({"action": "candidates"}))
        ids = {(pr["a"]["id"], pr["b"]["id"]) for pr in out["pairs"]}
        # n_a/n_b both carry the directive signal -> excluded (ride the
        # recurring lane); n_c/n_d do not -> still a candidate.
        assert not any("n_a" in (x, y) or "n_b" in (x, y) for x, y in ids), \
            f"directive pair n_a/n_b must be excluded, got {ids}"
        assert ("n_c", "n_d") in ids, f"non-directive pair n_c/n_d should pair, got {ids}"
        print("PASS directive-pair exclusion, surviving pairs:", out["count"])


def main():
    tests = [
        test_dream_candidates_jaccard,
        test_dream_merge_verdict_soft_supersede,
        test_dream_contradiction_verdict_stale_stamp,
        test_dream_contradiction_default_stale_older_of,
        test_dream_out_of_pair_and_self_pair_refused,
        test_dream_directive_pair_exclusion,
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
