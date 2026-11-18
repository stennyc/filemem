"""filemem plugin-side sm_* config_schema manifest (kbs parity) + the reader.

2026-11-17: the plugins-side manifest exposes the CENTRAL shared-memory
(PostgreSQL) connection — the same fields kbs exposes (filemem and kbs
share one central sharedmem store). It is NOT the provider-side
auto_extract/min_search_score (those live in config_schema.py → the memory
provider panel). The dashboard plugin-settings panel writes the sm_* scalars
to plugins.entries.filemem.settings; shared_memory.py reads that panel layer
first, then falls back to the legacy nested .shared_memory dict.
"""
import pathlib
import sys
from contextlib import contextmanager

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _os_detect import resolve_kbs_dir  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
KBS_DIR = resolve_kbs_dir()  # OS-portable: finds kbs or knowledge-base-search
sys.path.insert(0, str(PLUGIN_DIR))

# (field, type, default) — must match kbs's config_schema exactly.
SM_FIELDS = [
    ("sm_enabled", "bool", True),
    ("sm_agent_id", "str", "local"),
    ("sm_token", "str", "${SM_TOKEN}"),
    ("sm_pg_host", "str", "127.0.0.1"),
    ("sm_pg_port", "int", 5432),
    ("sm_pg_db", "str", "sharedmem"),
    ("sm_pg_user", "str", "sharedmem"),
    ("sm_pg_password", "str", "${SM_PG_PASSWORD}"),
    ("sm_pg_sslmode", "str", "prefer"),
]


def test_plugin_yaml_declares_sm_manifest():
    """The manifest carries all 9 sm_* fields with the right types + defaults."""
    data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
    schema = data.get("config_schema")
    assert schema is not None, "plugin.yaml must declare a config_schema block"
    for key, typ, default in SM_FIELDS:
        assert key in schema, f"{key} missing from filemem config_schema"
        assert schema[key]["type"] == typ, f"{key} type = {schema[key]['type']}"
        assert schema[key]["default"] == default, f"{key} default"


def test_sm_manifest_parity_with_kbs():
    """filemem + kbs share one central store, so the manifest field set,
    types, and template defaults are identical (a real value set in the
    panel overrides these fallbacks)."""
    if KBS_DIR is None:
        pytest.skip("kbs / knowledge-base-search plugin not installed on this host")
    fm = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    kbs = yaml.safe_load((KBS_DIR / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    assert set(fm) == set(kbs), f"field-set drift vs kbs: {set(fm) ^ set(kbs)}"
    for key in fm:
        assert fm[key]["type"] == kbs[key]["type"], f"{key} type drift vs kbs"
        assert fm[key]["default"] == kbs[key]["default"], f"{key} default drift vs kbs"


def _import_shared_memory():
    """Import the reader module; skip the reader-layer tests if this
    interpreter cannot load it (host tools-python may lack the dep paths)."""
    try:
        import shared_memory  # noqa
        return shared_memory
    except Exception as e:  # pragma: no cover - interpreter-dependent
        pytest.skip(f"shared_memory not importable here: {e}")


@contextmanager
def _with_fake_config_yaml(sm, yaml_text):
    real = sm._read_config_yaml_text
    sm._read_config_yaml_text = lambda: yaml_text
    try:
        yield
    finally:
        sm._read_config_yaml_text = real


def test_reader_panel_sm_layer_wins_over_legacy_dict():
    sm = _import_shared_memory()
    cfg_yaml = (
        "plugins:\n  entries:\n    filemem:\n"
        "      settings:\n"
        "        sm_enabled: true\n"
        "        sm_agent_id: agent-x\n"
        "        sm_token: tok-x\n"
        "        sm_pg_host: 10.0.0.5\n"
        "        sm_pg_port: 6432\n"
        "        sm_pg_db: sharedmem\n"
        "        sm_pg_user: sharedmem\n"
        "        sm_pg_password: pw-x\n"
        "      shared_memory:\n"        # legacy nested dict must LOSE
        "        enabled: true\n"
        "        agent_id: legacy-agent\n"
        "        token: legacy-tok\n"
        "        pg:\n"
        "          host: 127.0.0.1\n"
        "          db: legacy-db\n"
        "          user: sharedmem\n"
    )
    with _with_fake_config_yaml(sm, cfg_yaml):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["agent_id"] == "agent-x"          # panel layer won
    assert cfg["token"] == "tok-x"
    assert cfg["pg"]["host"] == "10.0.0.5"
    assert cfg["pg"]["port"] == 6432
    assert cfg["pg"]["password"] == "pw-x"


def test_reader_legacy_nested_dict_still_works():
    sm = _import_shared_memory()
    cfg_yaml = (
        "plugins:\n  entries:\n    filemem:\n"
        "      shared_memory:\n"
        "        enabled: true\n"
        "        agent_id: legacy-agent\n"
        "        token: legacy-tok\n"
        "        pg:\n"
        "          host: 127.0.0.1\n"
        "          db: sharedmem\n"
        "          user: sharedmem\n"
        "          password: legacy-pw\n"
    )
    with _with_fake_config_yaml(sm, cfg_yaml):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["agent_id"] == "legacy-agent"
    assert cfg["pg"]["host"] == "127.0.0.1"
    assert cfg["pg"]["password"] == "legacy-pw"


def test_reader_disabled_panel_sm_layer_noop():
    sm = _import_shared_memory()
    cfg_yaml = "plugins:\n  entries:\n    filemem:\n      settings:\n" \
               "        sm_enabled: false\n        sm_agent_id: a\n"
    with _with_fake_config_yaml(sm, cfg_yaml):
        assert sm._load_shared_memory_config() is None


# ---------------------------------------------------------------------------
# Minimal-parser leg: a yaml-less interpreter (host tools-python) must still
# read the panel-saved sm_* scalar shape — otherwise the enabled-gate fails
# and the PG-face auto-install (gated BEHIND a successful config load)
# deadlocks: no yaml -> minimal parser blind to sm_* -> no trigger -> no
# pyyaml install -> blind forever (2026-11-18 drift finding).
# ---------------------------------------------------------------------------

_PANEL_CFG_TEXT = (
    "language: ''\n"
    "plugins:\n"
    "  entries:\n"
    "    filemem:\n"
    "      settings:\n"
    "        sm_enabled: true\n"
    "        sm_agent_id: agent-x\n"
    "        sm_token: tok-x\n"
    "        sm_pg_host: 10.0.0.5\n"
    "        sm_pg_port: 6432\n"
    "        sm_pg_db: sharedmem\n"
    "        sm_pg_user: sharedmem\n"
    "        sm_pg_password: pw-x\n"
    "    knowledge-base-search:\n"
    "      settings:\n"
    "        sm_enabled: true\n"
    "      allow_tool_override: false\n"
)


def test_minimal_parser_panel_sm_shape():
    """The minimal text parser extracts the panel sm_* scalar block with
    sm_enabled coerced to a real bool (a string 'false' would read as
    enabled under the loader's bool() — a silent no-op flip)."""
    sm = _import_shared_memory()
    out = sm._parse_shared_memory_minimal(_PANEL_CFG_TEXT)
    fm = out["plugins"]["entries"]["filemem"]
    settings = fm["settings"]
    assert settings["sm_enabled"] is True
    assert settings["sm_agent_id"] == "agent-x"
    assert settings["sm_token"] == "tok-x"
    assert settings["sm_pg_host"] == "10.0.0.5"
    assert settings["sm_pg_port"] in ("6432", 6432)
    assert settings["sm_pg_db"] == "sharedmem"
    assert settings["sm_pg_user"] == "sharedmem"
    assert settings["sm_pg_password"] == "pw-x"


def test_minimal_parser_disabled_panel_sm_bool():
    """sm_enabled: false must parse to real False (not the string 'false'),
    so the yaml-less leg honors a disabled panel config."""
    sm = _import_shared_memory()
    text = _PANEL_CFG_TEXT.replace("sm_enabled: true\n", "sm_enabled: false\n", 1)
    out = sm._parse_shared_memory_minimal(text)
    fm = out["plugins"]["entries"]["filemem"]
    assert fm["settings"]["sm_enabled"] is False


def test_minimal_parser_legacy_nested_shape_unchanged():
    """Byte-stable regression guard: the legacy .shared_memory dict shape
    parses exactly as before the panel-shape addition."""
    sm = _import_shared_memory()
    text = (
        "plugins:\n  entries:\n    filemem:\n"
        "      shared_memory:\n"
        "        enabled: true\n"
        "        agent_id: legacy-agent\n"
        "        token: legacy-tok\n"
        "        pg:\n"
        "          host: 127.0.0.1\n"
        "          db: sharedmem\n"
        "          user: sharedmem\n"
        "          password: legacy-pw\n"
    )
    out = sm._parse_shared_memory_minimal(text)
    sm_block = out["plugins"]["entries"]["filemem"]["shared_memory"]
    assert sm_block["enabled"] is True
    assert sm_block["agent_id"] == "legacy-agent"
    assert sm_block["token"] == "legacy-tok"
    assert sm_block["pg"] == {"host": "127.0.0.1", "db": "sharedmem",
                              "user": "sharedmem", "password": "legacy-pw"}


def test_minimal_parser_no_sm_config_returns_empty():
    sm = _import_shared_memory()
    assert sm._parse_shared_memory_minimal("plugins:\n  entries:\n    other:\n") == {}


@contextmanager
def _block_yaml_modules():
    """Simulate a yaml-less interpreter: 'import yaml' / 'from ruamel.yaml
    import YAML' raise ImportError while blocked (None in sys.modules
    halts the import)."""
    saved = {}
    for name in ("yaml", "ruamel", "ruamel.yaml"):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = None
    try:
        yield
    finally:
        for name, v in saved.items():
            if v is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = v


def test_loader_yamlless_interpreter_reads_panel_sm_shape():
    """End-to-end yaml-less leg: ruamel + pyyaml both blocked -> the
    minimal parser carries the panel sm_* shape through the loader, the
    enabled-gate passes, and the auto-install trigger is REACHED (the
    pre-fix deadlock point). The trigger is mocked so no pip/marker side
    effects escape the test."""
    from unittest import mock
    sm = _import_shared_memory()
    with _block_yaml_modules(), _with_fake_config_yaml(sm, _PANEL_CFG_TEXT), \
            mock.patch.object(sm, "_maybe_trigger_pg_face_autoinstall") as trig:
        cfg = sm._load_shared_memory_config()
    assert cfg is not None, "yaml-less interpreter must still read the panel sm_* config"
    assert cfg["agent_id"] == "agent-x"
    assert cfg["token"] == "tok-x"
    assert cfg["pg"] == {"host": "10.0.0.5", "port": 6432, "db": "sharedmem",
                         "user": "sharedmem", "password": "pw-x",
                         "sslmode": "prefer"}
    assert trig.called, "enabled-gate pass must reach the PG-face auto-install trigger"


def test_loader_yamlless_interpreter_sm_disabled_returns_none():
    """sm_enabled: false in a yaml-less interpreter -> clean no-op, and the
    trigger is NOT reached (gate fails before it)."""
    from unittest import mock
    sm = _import_shared_memory()
    text = _PANEL_CFG_TEXT.replace("sm_enabled: true\n", "sm_enabled: false\n", 1)
    with _block_yaml_modules(), _with_fake_config_yaml(sm, text), \
            mock.patch.object(sm, "_maybe_trigger_pg_face_autoinstall") as trig:
        assert sm._load_shared_memory_config() is None
    assert not trig.called


# ---------------------------------------------------------------------------
# F4 client TLS (2026-11-18): sm_pg_sslmode flows panel -> loader -> legacy
# pg dict -> psycopg2.connect. Default "prefer" keeps every existing device
# byte-identical (libpq default); a live config sets "require".
# ---------------------------------------------------------------------------

_PANEL_CFG_SSLMODE = _PANEL_CFG_TEXT.replace(
    "sm_pg_password: pw-x\n",
    "sm_pg_password: pw-x\n        sm_pg_sslmode: require\n",
    1,
)


def test_reader_panel_sm_sslmode_require():
    sm = _import_shared_memory()
    with _with_fake_config_yaml(sm, _PANEL_CFG_SSLMODE):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["pg"]["sslmode"] == "require"


def test_reader_sslmode_defaults_to_prefer():
    sm = _import_shared_memory()
    cfg_yaml = (
        "plugins:\n  entries:\n    filemem:\n"
        "      settings:\n"
        "        sm_enabled: true\n"
        "        sm_agent_id: agent-x\n"
        "        sm_pg_host: 10.0.0.5\n"
        "        sm_pg_db: sharedmem\n"
    )
    with _with_fake_config_yaml(sm, cfg_yaml):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["pg"]["sslmode"] == "prefer"


def test_reader_legacy_nested_pg_sslmode():
    sm = _import_shared_memory()
    cfg_yaml = (
        "plugins:\n  entries:\n    filemem:\n"
        "      shared_memory:\n"
        "        enabled: true\n"
        "        agent_id: legacy-agent\n"
        "        pg:\n"
        "          host: 127.0.0.1\n"
        "          db: sharedmem\n"
        "          user: sharedmem\n"
        "          sslmode: verify-full\n"
    )
    with _with_fake_config_yaml(sm, cfg_yaml):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["pg"]["sslmode"] == "verify-full"


def test_minimal_parser_panel_sslmode_key():
    sm = _import_shared_memory()
    out = sm._parse_shared_memory_minimal(_PANEL_CFG_SSLMODE)
    settings = out["plugins"]["entries"]["filemem"]["settings"]
    assert settings["sm_pg_sslmode"] == "require"


def test_minimal_parser_legacy_pg_sslmode_key():
    sm = _import_shared_memory()
    text = (
        "plugins:\n  entries:\n    filemem:\n"
        "      shared_memory:\n"
        "        enabled: true\n"
        "        agent_id: legacy-agent\n"
        "        pg:\n"
        "          host: 127.0.0.1\n"
        "          db: sharedmem\n"
        "          sslmode: verify-ca\n"
    )
    out = sm._parse_shared_memory_minimal(text)
    sm_block = out["plugins"]["entries"]["filemem"]["shared_memory"]
    assert sm_block["pg"]["sslmode"] == "verify-ca"


def test_pg_conn_passes_sslmode_kwarg():
    """_PgConn must forward sslmode to psycopg2.connect; a pg dict without
    it (older config shape) must default to 'prefer'."""
    from unittest import mock
    sm = _import_shared_memory()
    calls = []

    class _FakeConn:
        autocommit = False

    def _fake_connect(**kw):
        calls.append(kw)
        return _FakeConn()

    fake_mod = mock.Mock()
    fake_mod.connect = _fake_connect
    with mock.patch.dict(sys.modules, {"psycopg2": fake_mod}):
        sm._PgConn({"host": "h", "port": 5432, "db": "d",
                    "user": "u", "password": "p", "sslmode": "require"})
        sm._PgConn({"host": "h", "port": 5432, "db": "d",
                    "user": "u", "password": "p"})
    assert calls[0]["sslmode"] == "require"
    assert calls[1]["sslmode"] == "prefer"


# ---------------------------------------------------------------------------
# Template placeholders (owner ruling 2026-11-18, item 4): the public-repo
# manifest ships ${SM_TOKEN} / ${SM_PG_PASSWORD}; a live config keeps real
# values (no ${} -> passthrough, byte-identical). Unset env -> "" (safe
# direction: central door closed, local face untouched).
# ---------------------------------------------------------------------------

_PANEL_CFG_PLACEHOLDERS = _PANEL_CFG_TEXT.replace(
    "sm_token: tok-x\n", "sm_token: ${SM_TOKEN}\n", 1,
).replace(
    "sm_pg_password: pw-x\n", "sm_pg_password: ${SM_PG_PASSWORD}\n", 1,
)


def test_env_placeholder_token_password_resolved(monkeypatch):
    sm = _import_shared_memory()
    monkeypatch.setenv("SM_TOKEN", "tok-live")
    monkeypatch.setenv("SM_PG_PASSWORD", "pw-live")
    with _with_fake_config_yaml(sm, _PANEL_CFG_PLACEHOLDERS):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["token"] == "tok-live"
    assert cfg["pg"]["password"] == "pw-live"


def test_env_placeholder_unset_yields_empty(monkeypatch):
    sm = _import_shared_memory()
    monkeypatch.delenv("SM_TOKEN", raising=False)
    monkeypatch.delenv("SM_PG_PASSWORD", raising=False)
    with _with_fake_config_yaml(sm, _PANEL_CFG_PLACEHOLDERS):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None  # gate only requires agent_id/host/db
    assert cfg["token"] == ""
    assert cfg["pg"]["password"] == ""


def test_env_placeholder_partial_pattern_passthrough():
    """A live value that merely CONTAINS ${...} is not a placeholder —
    only a full-match ${VAR} resolves (prevents accidental env lookups)."""
    sm = _import_shared_memory()
    text = _PANEL_CFG_TEXT.replace("sm_token: tok-x\n", "sm_token: abc${SM_TOKEN}def\n", 1)
    with _with_fake_config_yaml(sm, text):
        cfg = sm._load_shared_memory_config()
    assert cfg is not None
    assert cfg["token"] == "abc${SM_TOKEN}def"


def test_live_value_passthrough_unchanged():
    """Real (non-placeholder) values flow through byte-identical — the
    existing live configs are untouched by the placeholder feature."""
    sm = _import_shared_memory()
    with _with_fake_config_yaml(sm, _PANEL_CFG_TEXT):
        cfg = sm._load_shared_memory_config()
    assert cfg["token"] == "tok-x"
    assert cfg["pg"]["password"] == "pw-x"
