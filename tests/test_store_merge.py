"""T5a RED: store_merge.py against @data-engineer's merge contract.

Contract (group chat 2026-09-27, @data-engineer):
- memories/*.json union by memory_id: same id+content (sha1) -> 1; same id, diff
  content -> BOTH kept, default-side entry renamed <id>~default + merge_conflict audit row
- MEMORY.md / USER.md: line-level union, exact-duplicate lines deduped, first-seen
  order preserved, diff line counts go to the audit row
- consolidated/ append logs: concat by line-hash dedup (central first, default after),
  conflict lines kept both
- drafts_*.json: uuid-suffixed filenames; real collision -> default side renamed
  <name>~default. Central's own survivors (2 items) untouched.
- source_profile stamps NEVER rewritten
- audit: pre-snapshot both trees to archive/store-merge-<ts>/{central,default}/,
  append ONE store_merge event to consolidation_log.jsonl (files/entries/lines +
  conflict ids + snapshot path + actor)
- 0-loss: post entries = central + default + conflicts (kept twice)
- --dry-run changes nothing on disk
"""
import json
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
import store_merge  # noqa: E402  (ops tool, lives under tools/ — not wheel-packaged)


def _mk_tree(root: pathlib.Path, **files) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (list, dict)):
            p.write_text(json.dumps(content), encoding="utf-8")
        else:
            p.write_text(content, encoding="utf-8")


def _mem(mid, content, profile="4d"):
    return {"memory_id": mid, "content": content, "source_profile": profile,
            "level": "L1", "created_at": "2026-09-20T00:00:00"}


def test_union_same_id_same_content_dedups():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        # same id + same content but DIFFERENT source_profile stamps -> still
        # dedups to 1 entry: source_profile is a lineage stamp, not identity.
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "same", "4d")]})
        _mk_tree(default, **{"memories/m1.json": [_mem("a", "same", "default")]})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        kept = json.loads((central / "memories" / "m1.json").read_text())
        assert len(kept) == 1, kept                      # dedup across profiles
        assert kept[0]["memory_id"] == "a"
        assert rep["conflicts"] == []
        assert rep["identical"] == 1
        assert rep["loss"] == 0


def test_conflict_keeps_both_with_suffix():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "central version", "4d")]})
        _mk_tree(default, **{"memories/m1.json": [_mem("a", "default version", "default")]})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        kept = json.loads((central / "memories" / "m1.json").read_text())
        assert len(kept) == 2, kept                      # both survive
        assert kept[0]["memory_id"] == "a"
        assert any(e["memory_id"] == "a~default" and e["content"] == "default version"
                   for e in kept), kept
        assert rep["conflicts"] == ["a"]
        assert rep["loss"] == 0
        # source_profile stamps untouched
        assert all(e.get("source_profile") in ("4d", "default") for e in kept)


def test_default_only_entries_moved_in():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "x", "4d")]})
        _mk_tree(default, **{"memories/m2.json": [_mem("b", "y", "default")]})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        files = sorted(p.name for p in (central / "memories").glob("*.json"))
        assert "m2.json" in files, files                 # default-only file merged in
        assert rep["loss"] == 0


def test_md_line_union_dedup_first_seen_order():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"MEMORY.md": "line1\nline2\n"})
        _mk_tree(default, **{"MEMORY.md": "line2\nline3\n"})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        out = (central / "MEMORY.md").read_text()
        assert out == "line1\nline2\nline3\n", repr(out)  # dedup line2, first-seen order
        assert rep["loss"] == 0


def test_md_missing_side_is_copy():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{})
        _mk_tree(default, **{"USER.md": "u1\nu2\n"})
        store_merge.merge_trees(central, default, base / "archive", actor="coder")
        assert (central / "USER.md").read_text() == "u1\nu2\n"


def test_consolidated_append_dedup_central_first():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"consolidated/log.jsonl": 'e1\ne2\n'})
        _mk_tree(default, **{"consolidated/log.jsonl": 'e2\ne3\n'})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        out = (central / "consolidated" / "log.jsonl").read_text()
        assert out == "e1\ne2\ne3\n", repr(out)          # central order kept, e3 appended


def test_draft_collision_renames_default_side():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"drafts_20260927_x.json": [_mem("c1", "central survivor", "4d")]})
        _mk_tree(default, **{"drafts_20260927_x.json": [_mem("d1", "default", "default")]})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        c = json.loads((central / "drafts_20260927_x.json").read_text())
        assert [e["memory_id"] for e in c] == ["c1"], c   # central's own survivor untouched
        d = json.loads((central / "drafts_20260927_x~default.json").read_text())
        assert [e["memory_id"] for e in d] == ["d1"], d
        assert rep["loss"] == 0


def test_draft_no_collision_plain_merge():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(default, **{"drafts_20260921_z.json": [_mem("z", "dz", "default")]})
        store_merge.merge_trees(central, default, base / "archive", actor="coder")
        assert (central / "drafts_20260921_z.json").exists()


def test_zero_loss_invariant_and_audit_row():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "ca", "4d")],
                             "drafts_c.json": [_mem("c", "cc", "4d")]})
        _mk_tree(default, **{"memories/m1.json": [_mem("a", "da", "default")],
                             "memories/m2.json": [_mem("b", "db", "default")],
                             "drafts_d.json": [_mem("d", "dd", "default")]})
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        assert rep["loss"] == 0
        assert rep["pre_central_entries"] == 2
        assert rep["pre_default_entries"] == 3
        assert rep["post_central_entries"] == 5          # 2 + 3 + 1 conflict-duplicate
        # snapshot both trees pre-merge
        snaps = list((base / "archive").glob("store-merge-*"))
        assert len(snaps) == 1, snaps
        assert (snaps[0] / "central" / "memories" / "m1.json").exists()
        assert (snaps[0] / "default" / "drafts_d.json").exists()
        # exactly one store_merge audit event in the central consolidation log
        log = central / "consolidation_log.jsonl"
        rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
        ev = [r for r in rows if r.get("event") == "store_merge"]
        assert len(ev) == 1, rows
        e = ev[0]
        assert e["conflicts"] == ["a"]
        assert e["actor"] == "coder"
        assert e["snapshot"] == str(snaps[0])
        assert e["files"] >= 3 and e["entries"] == 5


def test_dry_run_changes_nothing():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "ca", "4d")]})
        _mk_tree(default, **{"memories/m2.json": [_mem("b", "db", "default")]})
        before = sorted(str(p) for p in central.rglob("*") if p.is_file())
        rep = store_merge.merge_trees(central, default, base / "archive", actor="coder",
                                      dry_run=True)
        after = sorted(str(p) for p in central.rglob("*") if p.is_file())
        assert before == after, "dry-run mutated central tree"
        assert rep["dry_run"] is True
        assert rep["loss"] == 0


def test_rerun_idempotent_no_double_conflict_rows():
    with tempfile.TemporaryDirectory() as td:
        base = pathlib.Path(td)
        central, default = base / "central", base / "default"
        _mk_tree(central, **{"memories/m1.json": [_mem("a", "ca", "4d")]})
        _mk_tree(default, **{"memories/m1.json": [_mem("a", "da", "default")]})
        r1 = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        assert r1["loss"] == 0
        # second run against the now-merged trees: the ~default entry already exists
        r2 = store_merge.merge_trees(central, default, base / "archive", actor="coder")
        kept = json.loads((central / "memories" / "m1.json").read_text())
        assert len(kept) == 2, kept                       # no 3rd copy
        assert r2["loss"] == 0
