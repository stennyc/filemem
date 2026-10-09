"""Ops hardening batch (pm-approved, one commit):

1. utcnow deprecation: no `datetime.utcnow()` anywhere (use aware now(timezone.utc))
2. duplicate-plugin-dir guard: a backup dir under plugins/ that also registers
   as filemem MUST warn at startup (loud, not silent) — 117 incident 2026-09-27
3. 通知面去菜单 (t_05679311 C-site): the [未决草稿] injection is low-noise —
   no relay obligation, no file list, no internal storage filenames
"""
import sys, pathlib, tempfile, re

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from __init__ import FileMemProvider  # noqa: E402


def test_1_no_utcnow_calls():
    src = (pathlib.Path(__file__).resolve().parents[1] / "__init__.py").read_text(encoding="utf-8")
    assert "datetime.utcnow()" not in src, "utcnow() deprecated in py3.12+"


def test_2_duplicate_dirs_detected():
    tmp = pathlib.Path(tempfile.mkdtemp())
    # build <home>/plugins/filemem (+storage) and a sibling backup dir
    for d in ["filemem", "filemem.pre-x"]:
        (tmp / "plugins" / d).mkdir(parents=True)
        (tmp / "plugins" / d / "plugin.yaml").write_text("name: filemem\n")
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp / "plugins" / "filemem" / "storage"
    p.hermetis_home = tmp / "plugins" / "filemem" / "storage" / ".."
    p.storage_dir = tmp / "plugins" / "filemem" / "storage"
    p.storage_dir.mkdir()
    dups = p._warn_duplicate_plugin_dirs()
    assert [d.name for d in dups] == ["filemem.pre-x"], dups


def test_3_no_duplicate_dirs_when_clean():
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "plugins" / "filemem").mkdir(parents=True)
    (tmp / "plugins" / "filemem" / "plugin.yaml").write_text("name: filemem\n")
    (tmp / "plugins" / "other-plugin").mkdir()
    (tmp / "plugins" / "other-plugin" / "plugin.yaml").write_text("name: other\n")
    p = FileMemProvider.__new__(FileMemProvider)
    p.storage_dir = tmp / "plugins" / "filemem" / "storage"
    p.storage_dir.mkdir()
    assert p._warn_duplicate_plugin_dirs() == []


def test_4_notice_text_has_no_relay_obligation():
    """t_05679311 (C-site): the per-item relay menu is REMOVED. The notice
    must not oblige the model to report a file list or run a clarify menu,
    and must not surface internal draft filenames — it points at the
    read-only preview path instead."""
    p = FileMemProvider.__new__(FileMemProvider)
    import inspect, re
    src = inspect.getsource(p._pending_draft_notice)
    assert "请向用户报告" not in src, "leaky relay obligation must be absent"
    assert "文件清单" not in src, "file-list reporting must be absent"
    assert not re.search(r"drafts_[A-Za-z0-9_]+\.json", src), \
        "internal draft filenames must not be user-visible in the notice"
    assert "preview" in src, "notice must point at the read-only preview path"

