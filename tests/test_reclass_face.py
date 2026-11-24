"""reclass_candidates face (DREAM V2 Stage 1 FR-1) backport to the
Python filemem plugin — on-disk-truth tests. Verifies the FR-1 v1 gate
(type_source == 'regex' rows ONLY; LLM-typed and legacy rows OUT),
the reference_count DESC ordering, the N-cap + leg-off posture, the
dry_run flag surfacing, and the limit override.

Run:  python3 /root/.hermes/plugins/filemem/tests/test_reclass_face.py
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


def test_reclass_only_regex_rows():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            # FR-1 v1 gate: ONLY type_source == "regex" rows qualify.
            {"id": "r_regex", "content": "some auto-typed fact",
             "reference_count": 3, "metadata": {"type_source": "regex"}},
            # LLM-typed -> OUT (idempotence: a reclassified row earns
            # no second verdict).
            {"id": "r_llm", "content": "some llm-typed fact",
             "reference_count": 9, "metadata": {"type_source": "llm"}},
            # legacy (no type_source stamp) -> NOT a candidate (v1
            # tight gate; the backfill is an owner decision).
            {"id": "r_legacy", "content": "some legacy fact",
             "reference_count": 7, "metadata": {}},
        ]})
        out = json.loads(p._tool_reclass_candidates({}))
        ids = [c["id"] for c in out["candidates"]]
        assert ids == ["r_regex"], f"only the regex row should qualify, got {ids}"
        print("PASS reclass FR-1 gate: only regex row, excluded:",
              [i for i in ("r_llm", "r_legacy") if i not in ids])


def test_reclass_ref_desc_ordering():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p = _make_provider(root)
        _write_rows(p, {"fact": [
            {"id": "a_low", "content": "low ref", "reference_count": 1,
             "metadata": {"type_source": "regex"}},
            {"id": "b_high", "content": "high ref", "reference_count": 8,
             "metadata": {"type_source": "regex"}},
            {"id": "c_mid", "content": "mid ref", "reference_count": 4,
             "metadata": {"type_source": "regex"}},
        ]})
        out = json.loads(p._tool_reclass_candidates({}))
        order = [c["id"] for c in out["candidates"]]
        # reference_count DESC: b_high(8) > c_mid(4) > a_low(1)
        assert order == ["b_high", "c_mid", "a_low"], order
        print("PASS reclass ref-DESC ordering:", order)


def test_reclass_cap_and_leg_off():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        # cap=2 default override via the hot-read knob
        p = _make_provider(root, cons={"reclass_max_per_tick": 2})
        _write_rows(p, {"fact": [
            {"id": "x1", "content": "1", "reference_count": 5, "metadata": {"type_source": "regex"}},
            {"id": "x2", "content": "2", "reference_count": 4, "metadata": {"type_source": "regex"}},
            {"id": "x3", "content": "3", "reference_count": 3, "metadata": {"type_source": "regex"}},
            {"id": "x4", "content": "4", "reference_count": 2, "metadata": {"type_source": "regex"}},
        ]})
        out = json.loads(p._tool_reclass_candidates({}))
        assert out["count"] == 2 and out["total"] == 4, out
        # limit override wins over the knob (0 = the leg off)
        out_lim0 = json.loads(p._tool_reclass_candidates({"limit": 0}))
        assert out_lim0["count"] == 0 and out_lim0["cap"] == 0, out_lim0
        out_lim = json.loads(p._tool_reclass_candidates({"limit": 1}))
        assert out_lim["count"] == 1 and out_lim["cap"] == 1, out_lim
        print("PASS reclass cap + limit override (0=leg off)")


def test_reclass_dry_run_surfaced():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        p_on = _make_provider(root, cons={"reclass_dry_run": True})
        out = json.loads(p_on._tool_reclass_candidates({}))
        assert out["dry_run"] is True, out
        p_off = _make_provider(root, cons={"reclass_dry_run": False})
        out2 = json.loads(p_off._tool_reclass_candidates({}))
        assert out2["dry_run"] is False, out2
        print("PASS reclass dry_run surfaced:", out["dry_run"], out2["dry_run"])


def main():
    tests = [
        test_reclass_only_regex_rows,
        test_reclass_ref_desc_ordering,
        test_reclass_cap_and_leg_off,
        test_reclass_dry_run_surfaced,
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
