"""Associate face (DREAM V2 FR-3 + C6④) backport to the Python filemem
plugin — on-disk-truth tests. Drives the real provider against a tmp
storage root; verifies candidates / verdicts / idempotency / out-of-pool
refusal / read-side expansion (off byte-stable vs on additive) without
trusting self-report.

Run:  python -m pytest /root/.hermes/plugins/filemem/tests/test_associate_face.py -v
or:   python /root/.hermes/plugins/filemem/tests/test_associate_face.py
"""
import json
import sys
import tempfile
import pathlib

sys.path.insert(0, "/root/.hermes/plugins/filemem")
import __init__ as fmem  # noqa: E402


def _make_provider(root: pathlib.Path):
    p = fmem.FileMemProvider()
    # Point the data plane at a fresh tmp root (the associate face only
    # touches storage_dir + memories_dir; no host LLM / decay needed).
    p.storage_dir = root / "filemem"
    p.memories_dir = p.storage_dir / "memories"
    p.memories_dir.mkdir(parents=True, exist_ok=True)
    p.storage_dir.mkdir(parents=True, exist_ok=True)
    p._decay = {"consolidation": {}}
    return p


def _write_rows(p, rows_by_type):
    """rows_by_type: {mem_type: [ {id, content, metadata?} ]}"""
    for mt, rows in rows_by_type.items():
        f = p.memories_dir / f"{mt}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for r in rows:
            item = dict(r)
            item.setdefault("metadata", {})
            payload.append(item)
        with open(f, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)


def test_candidates_jaccard_prescreen():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        # Two fact rows that share many tokens (same-shard gate holds).
        _write_rows(p, {"fact": [
            {"id": "f_a", "content": "the deployment runs on docker compose with postgres and redis backends"},
            {"id": "f_b", "content": "the deployment uses docker compose plus postgres and redis backends"},
            {"id": "f_c", "content": "unrelated cooking recipe with vanilla and cinnamon"},
        ]})
        out = json.loads(p._tool_associate({"action": "candidates"}))
        assert out["ok"] is True
        ids = set()
        for pr in out["pairs"]:
            ids.add((pr["a"]["id"], pr["b"]["id"]))
        # f_a / f_b should be a candidate pair (high overlap); f_c not.
        assert any(("f_a", "f_b") in (x, y) for x, y in ids for x in [0]) or ("f_a", "f_b") in ids or ("f_b", "f_a") in ids, \
            f"expected f_a/f_b candidate pair, got {ids}"
        assert not any("f_c" in (a, b) for a, b in ids), f"f_c should not pair, got {ids}"
        print("PASS candidates:", out["count"], "pair(s)")


def test_verdicts_commit_and_idempotent():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "f_a", "content": "the deployment runs on docker compose with postgres"},
            {"id": "f_b", "content": "the deployment uses docker compose plus postgres"},
        ]})
        verdicts = {"action": "verdicts", "verdicts": [
            {"a_id": "f_a", "b_id": "f_b", "relation": "relates-to",
             "reason": "both describe the same deployment"}]}
        r1 = json.loads(p._tool_associate(verdicts))
        assert r1["applied"] == 1 and r1["deduped"] == 0, r1
        # idempotent re-run: same edge -> dedup, not a second ledger line
        r2 = json.loads(p._tool_associate(verdicts))
        assert r2["applied"] == 0 and r2["deduped"] == 1, r2
        # on-disk truth: ledger has exactly one line
        ledger = root / "filemem" / "association_edges.jsonl"
        lines = [l for l in ledger.read_text().splitlines() if l.strip()]
        assert len(lines) == 1, f"ledger should have 1 line, got {len(lines)}"
        print("PASS verdicts commit + idempotency, ledger lines:", len(lines))


def test_verdicts_out_of_pool_refused():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "f_a", "content": "the deployment runs on docker"},
        ]})
        verdicts = {"action": "verdicts", "verdicts": [
            {"a_id": "f_a", "b_id": "ghost_zzz", "relation": "relates-to",
             "reason": "hallucinated endpoint"}]}
        r = json.loads(p._tool_associate(verdicts))
        assert r["applied"] == 0 and r["refused"] == ["ghost_zzz"], r
        # ledger must NOT have been written (refused, not applied)
        ledger = root / "filemem" / "association_edges.jsonl"
        n = 0 if not ledger.exists() else len([l for l in ledger.read_text().splitlines() if l.strip()])
        assert n == 0, f"refused verdict must not write ledger, got {n} lines"
        print("PASS out-of-pool refusal, refused:", r["refused"])


def test_expansion_off_byte_stable_on_additive():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "f_a", "content": "the deployment runs on docker compose"},
            {"id": "f_b", "content": "the deployment uses docker compose plus postgres"},
        ]})
        # commit an edge f_a <-> f_b
        p._tool_associate({"action": "verdicts", "verdicts": [
            {"a_id": "f_a", "b_id": "f_b", "relation": "relates-to", "reason": "same deploy"}]})
        hits = [{"id": "f_a", "score": 0.8}]
        # OFF (default): envelope untouched (no association keys)
        env_off = {"results": list(hits)}
        p._apply_association_expansion(env_off)
        assert "association_expanded" not in env_off, list(env_off.keys())
        # ON: additive keys, neighbor f_b at 0.5 coefficient
        p._decay.setdefault("consolidation", {})["association_expand"] = True
        env_on = {"results": list(hits)}
        p._apply_association_expansion(env_on)
        assert "association_expanded" in env_on, "ON must add the key"
        exp = env_on["association_expanded"]
        assert any(e["id"] == "f_b" and abs(e["score"] - 0.4) < 1e-6 for e in exp), exp
        assert any(e.get("via") == "f_a" for e in exp), exp
        print("PASS expansion off=byte-stable / on=additive; on =", exp)


def main():
    tests = [
        test_candidates_jaccard_prescreen,
        test_verdicts_commit_and_idempotent,
        test_verdicts_out_of_pool_refused,
        test_expansion_off_byte_stable_on_additive,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
