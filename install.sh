#!/usr/bin/env bash
# filemem installer — idempotent, backs up existing installs, preserves live storage/
#
# Modes (T5b config-tier, @user decision 2026-09-27 "config 档"):
#   (no flag)      directory install to $HERMES_HOME/plugins/filemem (default/4d shape)
#   --pip          CODE plane: install the pre-built entry-point wheel into the TARGET
#                  interpreter, then refresh the central code tree from the wheel
#                  bytes (single code source — every profile reading
#                  $HERMES_HOME/plugins/filemem gets the same content as pip).
#   --central      DATA plane: one-time relocation of the live per-profile storage
#                  trees into the NEUTRAL ROOT <HERMES_HOME>/filemem (store_merge
#                  union, snapshot + audit, 0 raw delete). At runtime the plugin's
#                  _resolve_storage_root auto-lands every profile on that root, so
#                  the old trees go FROZEN after this (no-dual-write gate).
#                  Symlinks are OPTIONAL here — FM_SYMLINK=1 to still point each
#                  profile's code dir at the central tree.
#
# Env overrides:
#   HERMES_HOME       hermes home (default $HOME/.hermes)
#   FM_TARGET_PYTHON  target interpreter for --pip (default: auto-detect $HERMES_HOME/tools/python-*/bin/python3*)
#   FM_ENGINE_ROOT    hermes-agent engine tree for post-install verify (default auto-detect)
#   FM_FORCE          re-run --pip even if the EP is already registered
#   FM_DRY_RUN=1      plan only (no filesystem / no pip writes) for --central / --pip
#   FM_SYMLINK=1      --central: also (re)link each profile's code dir to central
#
# config-tier invariant: the pip wheel carries CODE ONLY; storage lives in the
# NEUTRAL ROOT $HERMES_HOME/filemem (auto-resolved, zero config for new profiles).
# pip (--pip) and storage (--central) are independent planes, both land before
# the single controlled gateway restart.

set -euo pipefail

# ── detect a WORKING generic python (for directory-install smoke tests) ──────────
PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HERMES_HOME/plugins/filemem"
CENTRAL="$DEST"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BAK="$DEST.bak.$TIMESTAMP"

# ── path normalization for native (non-MSYS) programs ─────────────────────────
# Under git-bash/MSYS, SCRIPT_DIR / HERMES_HOME are MSYS-form (/c/Users/...);
# a NATIVE Windows program (python.exe, pip) misreads them as C:\c\Users\... .
# `cygpath -m` converts to the portable forward-slash native form (C:/Users/...).
# On Linux (the 117 target) cygpath does not exist — the native vars stay empty
# and the script keeps using the shell's own paths (which are correct there).
SCRIPT_DIR_W=""
HERMES_HOME_W=""
if command -v cygpath >/dev/null 2>&1; then
  SCRIPT_DIR_W="$(cygpath -m "$SCRIPT_DIR")"
  HERMES_HOME_W="$(cygpath -m "$HERMES_HOME")"
fi
script_native() { [ -n "$SCRIPT_DIR_W" ] && echo "$SCRIPT_DIR_W" || echo "$SCRIPT_DIR"; }
home_native()   { [ -n "$HERMES_HOME_W" ] && echo "$HERMES_HOME_W" || echo "$HERMES_HOME"; }

# Portable native form of ANY path for arguments handed to native programs
# (python.exe / pip / a python -c argv). On Linux the shell path is already
# native, so this is the identity there.
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi }

MODE="dir"
case "${1:-}" in
  --pip)          MODE="pip"; shift ;;
  --central)      MODE="central"; shift ;;
  --all-profiles) MODE="all_profiles"; shift ;;
  dir|--)         MODE="dir"; shift ;;
  -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
  "")           MODE="dir" ;;
  *) echo "unknown mode: $1 (use dir / --pip / --central / --all-profiles)"; exit 2 ;;
esac

# ── target interpreter for --pip: the one running the LIVE gateway ───────────────
# NOT the venv. On 117 that is $HERMES_HOME/tools/python-3.14.*/bin/python3.14
# (probed via /proc/<gw-pid>/exe). The gateway runs with -I so it scans engine root
# + its own stdlib + its own site-packages; a venv's site-packages is invisible.
detect_target_python() {
  if [ -n "${FM_TARGET_PYTHON:-}" ]; then echo "$(native "$FM_TARGET_PYTHON")"; return; fi
  local cand
  for cand in "$HERMES_HOME"/tools/python-*/bin/python3*; do
    if [ -x "$cand" ] && "$cand" -c 'import sys' >/dev/null 2>&1; then
      echo "$(native "$cand")"; return
    fi
  done
  echo "$PY"
}

# ── engine root for post-install verification ────────────────────────────────────
detect_engine_root() {
  if [ -n "${FM_ENGINE_ROOT:-}" ] && [ -d "$FM_ENGINE_ROOT" ]; then echo "$FM_ENGINE_ROOT"; return; fi
  local r
  for r in /usr/local/lib/hermes-agent "$HERMES_HOME/hermes-agent" /opt/hermes-agent; do
    [ -d "$r" ] && { echo "$r"; return; }
  done
  echo ""
}

# ── locate the pre-built wheel (shipped in the repo so --pip works offline) ──────
find_wheel() {
  local w
  for w in "$SCRIPT_DIR"/dist/hermes_filemem-*.whl "$SCRIPT_DIR"/dist/*.whl; do
    [ -f "$w" ] && { echo "$(native "$w")"; return; }
  done
  echo ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: dir  (default) — directory install, storage preserved, no clobber
# ═══════════════════════════════════════════════════════════════════════════════
install_dir() {
  [ -n "$PY" ] || { echo "ERROR: no working python for the smoke test" >&2; exit 1; }
  if [ ! -f "$SCRIPT_DIR/plugin.yaml" ] || [ ! -f "$SCRIPT_DIR/__init__.py" ]; then
    echo "ERROR: run install.sh from a checkout containing plugin.yaml + __init__.py" >&2; exit 1
  fi
  echo "==> [dir] source : $SCRIPT_DIR"
  echo "==> [dir] target : $DEST"

  # 1. backup existing code (storage/ stays in place and is reused)
  if [ -d "$DEST" ] && [ ! -L "$DEST" ]; then
    echo "==> existing install — backing up code to $BAK (storage preserved)"
    mkdir -p "$BAK"
    for f in "$DEST"/*; do
      [ "$(basename "$f")" = "storage" ] && continue
      cp -a "$f" "$BAK"/ 2>/dev/null || true
    done
  fi

  mkdir -p "$(dirname "$DEST")"
  # 2. copy code, preserve live storage/ (upgrade = code-only swap)
  if [ -d "$DEST/storage" ]; then
    echo "==> preserving live storage/ (upgrading code only)"
    for item in "$SCRIPT_DIR"/*; do
      name="$(basename "$item")"
      case "$name" in
        storage|__pycache__|.pytest_cache|dist) continue ;;
        *) rm -rf "$DEST/$name"; cp -a "$item" "$DEST/$name" ;;
      esac
    done
  else
    rm -rf "$DEST"
    mkdir -p "$DEST"
    for item in "$SCRIPT_DIR"/*; do
      name="$(basename "$item")"
      [ "$name" = "storage" ] && continue
      [ "$name" = "dist" ] && continue
      cp -a "$item" "$DEST/$name"
    done
  fi
  find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

  # 3. CLI mirror (Windows dual-home)
  local LOCALAPPDATA_PLUGINS="${LOCALAPPDATA:-}/plugins"
  if [ -n "$LOCALAPPDATA_PLUGINS" ] && [ -d "$(dirname "$LOCALAPPDATA_PLUGINS")" ]; then
    local MIRROR="$LOCALAPPDATA_PLUGINS/filemem"
    if [ "$(realpath "$DEST" 2>/dev/null)" != "$(realpath "$MIRROR" 2>/dev/null || echo none)" ]; then
      echo "==> mirroring to CLI home: $MIRROR"
      mkdir -p "$(dirname "$MIRROR")"
      if [ -L "$MIRROR" ] || [ ! -e "$MIRROR" ]; then
        rm -rf "$MIRROR"; ln -sfn "$DEST" "$MIRROR" 2>/dev/null || cp -a "$DEST" "$MIRROR"
      fi
    fi
  fi

  # 4. config.yaml memory.provider (never clobber other content)
  local CONFIG="$HERMES_HOME/config.yaml"
  if [ -f "$CONFIG" ] && grep -qE '^\s*memory:' "$CONFIG"; then
    if grep -qE 'provider:\s*filemem' "$CONFIG"; then
      echo "==> config.yaml already sets memory.provider: filemem"
    else
      echo "WARN: config.yaml has a memory: section not set to filemem — inspect $CONFIG"
    fi
  else
    { echo "memory:"; echo "  provider: filemem"; } >> "$CONFIG"
    echo "==> appended memory.provider: filemem to $CONFIG"
  fi

  # 5. smoke test
  echo "==> [dir] smoke test"
  "$PY" - "$(native "$DEST")" <<'PYEOF'
import importlib.util, sys, pathlib
dest = sys.argv[1]
spec = importlib.util.spec_from_file_location("filemem_plugin", pathlib.Path(dest) / "__init__.py")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception:
    import ast; ast.parse((pathlib.Path(dest)/"__init__.py").read_text())
    print("  module syntax OK (runtime import deferred to Hermes)")
else:
    assert hasattr(m, "register"), "register() missing"
    print("  module import OK — register() present")
PYEOF
  echo "==> [dir] install complete. Live data: $DEST/storage ; backup: $BAK"
}

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: --pip  (T5b CODE plane) — EP wheel into target interpreter + refresh the
# central code tree from the wheel (single code source, @reviewer axis ②).
# ═══════════════════════════════════════════════════════════════════════════════
install_pip() {
  local TARGET; TARGET="$(detect_target_python)"
  echo "==> [pip] target interpreter: $TARGET"
  "$TARGET" -V 2>&1 | head -1 || true
  "$TARGET" -m pip --version 2>&1 | head -1 || { echo "ERROR: $TARGET has no pip module" >&2; exit 1; }

  # idempotence: already registered?
  local EP_PRESENT=0
  if "$TARGET" -I -c '
import importlib.metadata as m
eps = m.entry_points()
hit = [e for e in (eps.select(group="hermes_agent.memory_providers") if hasattr(eps,"select") else [e for e in eps.get("hermes_agent.memory_providers",[]) if e.group=="hermes_agent.memory_providers"])]
raise SystemExit(0 if any(e.name=="filemem" for e in hit) else 1)
' 2>/dev/null; then EP_PRESENT=1; fi
  if [ "$EP_PRESENT" = "1" ] && [ -z "${FM_FORCE:-}" ]; then
    echo "==> [pip] EP 'filemem' already registered in $TARGET (FM_FORCE=1 to reinstall)"
  else
    local WHEEL; WHEEL="$(find_wheel)"
    if [ -n "$WHEEL" ]; then
      echo "==> [pip] installing pre-built wheel (offline, no build): $WHEEL"
      if [ "${FM_DRY_RUN:-0}" = "1" ]; then
        echo "    (dry-run) would: $TARGET -m pip install --no-deps --no-build-isolation $WHEEL"
      else
        "$TARGET" -m pip install --no-deps --no-build-isolation "$WHEEL"
      fi
    else
      echo "WARN: no shipped wheel found — building from source (needs setuptools + possibly network)"
      "$TARGET" -m pip install --no-deps "$(native "$SCRIPT_DIR")"
    fi
  fi

  if [ "${FM_DRY_RUN:-0}" = "1" ]; then
    echo "==> [pip] (dry-run) skipping runtime verify + central refresh (plan only)"
    return 0
  fi

  verify_pip "$TARGET"

  # ── single code source: refresh the central code tree from the wheel bytes ──
  # The engine discovers the provider by directory BEFORE the EP fallback, so a
  # stale central tree would shadow the wheel and reintroduce dual code sources.
  # We copy the wheel's filemem/* package content over the central tree
  # (storage/ preserved — data lives in the neutral root after --central), so
  # every profile that reads $HERMES_HOME/plugins/filemem sees wheel bytes.
  refresh_central_from_wheel "$TARGET"
}

# Refresh $DEST's code files from the installed wheel package (code only; the
# live storage/ dir — now historically the OLD tree, frozen after --central —
# is left byte-for-byte untouched, no-dual-write gate).
refresh_central_from_wheel() {
  local TARGET="$1"
  echo "==> [pip] refreshing central code tree from wheel (single code source)"
  local WHEEL; WHEEL="$(find_wheel)"
  [ -n "$WHEEL" ] || { echo "WARN: no wheel to refresh from — central tree left as-is"; return 0; }
  local UNPACK
  UNPACK="$(mktemp -d)"
  if [ "${FM_DRY_RUN:-0}" = "1" ]; then
    echo "    (dry-run) would: extract $WHEEL -> $DEST (storage/ untouched), then md5-manifest-compare"
    rm -rf "$UNPACK"
    return 0
  fi
  "$TARGET" -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$(native "$WHEEL")" "$(native "$UNPACK")"
  local PKG="$UNPACK/filemem"
  [ -d "$PKG" ] || { echo "ERROR: wheel has no filemem/ package — $WHEEL"; rm -rf "$UNPACK"; exit 1; }
  mkdir -p "$DEST"
  # backup the current tree's CODE (snapshot, not raw delete)
  local BK="$HERMES_HOME/archive/filemem-code.pre-wheel-$TIMESTAMP"
  mkdir -p "$BK"
  for f in "$DEST"/*; do
    [ "$(basename "$f")" = "storage" ] && continue
    cp -a "$f" "$BK"/ 2>/dev/null || true
  done
  # copy code files (never the wheel's dist-info, never storage/)
  for f in "$PKG"/*; do
    local name; name="$(basename "$f")"
    [ "$name" = "__pycache__" ] && continue
    rm -rf "$DEST/$name" 2>/dev/null || true
    cp -a "$f" "$DEST/$name"
  done
  find "$DEST" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  # md5-manifest compare: every wheel file must be byte-identical in $DEST.
  # (frozen storage/, runtime *.lock files, and __pycache__ are out of scope)
  echo "    [pip] verifying central tree == wheel bytes (md5 manifest diff)"
  "$TARGET" - "$(native "$PKG")" "$(native "$DEST")" <<'PYEOF'
import hashlib, pathlib, sys
pkg, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
SKIP = ("storage", "__pycache__")
def manifest(root):
    out = {}
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(root).as_posix()
        if any(part in SKIP for part in f.parts) or f.suffix == ".lock" or rel.startswith("storage/"):
            continue
        out[rel] = hashlib.md5(f.read_bytes()).hexdigest()
    return out
a, b = manifest(pkg), manifest(dest)
missing = [k for k in a if b.get(k) != a[k]]
for k in missing:
    print(f"    MISMATCH: {k}")
for k in b:
    if k not in a:
        print(f"    (extra in dest, tolerated: {k})")
sys.exit(1 if missing else 0)
PYEOF
  [ $? -eq 0 ] || { echo "ERROR: central tree refresh mismatch"; rm -rf "$UNPACK"; exit 1; }
  echo "    [pip] central tree now == wheel bytes (md5-verified). backup: $BK"
  rm -rf "$UNPACK"
}

# post-install proof: EP listed in the TARGET interpreter + provider loads + is_available()==True
verify_pip() {
  local TARGET="$1" ENG; ENG="$(detect_engine_root)"
  [ -n "$ENG" ] || { echo "WARN: engine root not found — skipping runtime verify (EP metadata still checked)" >&2; }
  echo "==> [pip] verifying entry point is visible to $TARGET"
  "$TARGET" -I - "$(native "$ENG")" <<'PYEOF'
import sys, importlib.metadata as m
eng = sys.argv[1]
if eng:
    for p in (eng, eng + "/.."):
        pass
    sys.path.insert(0, eng)
eps = m.entry_points()
hit = [e for e in (eps.select(group="hermes_agent.memory_providers") if hasattr(eps,"select") else [e for e in eps.get("hermes_agent.memory_providers",[]) if e.group=="hermes_agent.memory_providers"])]
ep = next((e for e in hit if e.name == "filemem"), None)
print("  EP visible:", bool(ep), "| value:", (ep.value if ep else None), "| group hermes_agent.memory_providers")
if ep and eng:
    prov = ep.load()
    # resolve the provider instance the engine would construct
    from agent.memory_provider import MemoryProvider
    inst = prov() if isinstance(prov, MemoryProvider) else None
    if inst is None and isinstance(prov, type) and issubclass(prov, MemoryProvider):
        inst = prov()
    print("  provider is_available():", (inst.is_available() if inst else "<instance via register path>"))
assert ep, "EP filemem NOT registered in target interpreter — pip target was wrong"
print("  [pip] VERIFIED — entry point registered in the live-gateway interpreter")
PYEOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: --central  (T5b DATA plane) — one-time relocation of live storage into
# the NEUTRAL ROOT. The plugin's _resolve_storage_root auto-lands every profile
# on <HERMES_HOME>/filemem at runtime (config tier, zero-config for new
# profiles), so after this step the old per-profile trees are FROZEN — a write
# to one of them post-cutover is a silent re-fork (no-dual-write gate,
# @data-engineer: "union 完成后旧树一旦再被写就是静默分叉复发").
#
# Copy, never move (the source trees stay as a frozen snapshot; 0 raw delete).
# @devops transaction: gateway STOPPED before this runs (stop→copy→verify→start),
# so no live write can slip in between the copy and the restart.
# ═══════════════════════════════════════════════════════════════════════════════
install_central() {
  local NEUTRAL; NEUTRAL="${FM_NEUTRAL_ROOT:-$HERMES_HOME/filemem}"
  local DRY="${FM_DRY_RUN:-0}"
  local SYMLINK="${FM_SYMLINK:-0}"
  local ARCHIVE="$HERMES_HOME/archive"
  echo "==> [central] neutral root : $NEUTRAL"
  echo "==> [central] dry-run: $DRY | optional symlinks: $SYMLINK"

  # ── step 0: create the neutral root (the runtime auto-resolve target)
  if [ "$DRY" = "1" ]; then
    echo "    (dry-run) would: mkdir -p $NEUTRAL"
  else
    mkdir -p "$NEUTRAL"
    echo "    created (or kept) $NEUTRAL"
  fi

  # ── step 1: union every REAL existing storage tree into the neutral root ──
  # Dedup by resolved path: on 117, default/4d/central all alias ONE inode
  # (profiles/default is a top-level symlink to the base), so the same tree
  # must be merged exactly once. store_merge is idempotent anyway
  # (same id + content = already_merged, 0 loss), the dedup just keeps the
  # audit rows honest.
  local SRC REAL SEEN=" "
  for SRC in "$HERMES_HOME/plugins/filemem/storage" \
             "$HERMES_HOME"/profiles/*/plugins/filemem/storage; do
    [ -d "$SRC" ] || continue
    REAL="$(realpath "$SRC" 2>/dev/null || echo "$SRC")"
    case "$SEEN" in
      *" $REAL "*) echo "    $SRC: alias of $REAL — dedup, skip" ;;
      *)
        SEEN="$SEEN$REAL "
        echo "    union: $SRC -> $NEUTRAL"
        if [ "$DRY" = "1" ]; then
          echo "      (dry-run) would run: tools/store_merge.py --central $NEUTRAL --default $SRC --archive $ARCHIVE --actor coder"
        else
          "$PY" "$(script_native)/tools/store_merge.py" \
            --central "$(native "$NEUTRAL")" --default "$(native "$SRC")" \
            --archive "$(native "$ARCHIVE")" --actor coder
        fi
        ;;
    esac
  done

  # ── step 2 (OPTIONAL, config tier downgrades symlinks): re-point each
  # profile's CODE dir at the central code tree. Not required for storage
  # (auto-resolve handles it); kept for the code-discovery path of profiles
  # that don't get the EP. Off by default — FM_SYMLINK=1 to opt in.
  if [ "$SYMLINK" = "1" ]; then
    local pdir p LINK CENTRAL_CODE="$HERMES_HOME/plugins/filemem"
    for pdir in "$HERMES_HOME/profiles"/*/; do
      p="$(basename "$pdir")"; [ "$p" = "config.yaml" ] && continue
      LINK="$pdir/plugins/filemem"
      if [ -L "$LINK" ] && [ "$(realpath "$LINK")" = "$(realpath "$CENTRAL_CODE")" ]; then
        echo "    $p: code -> central (skip)"
        continue
      fi
      if [ -d "$LINK" ] && [ ! -L "$LINK" ]; then
        local BK="$ARCHIVE/$p.code-prelink.$TIMESTAMP"
        if [ "$DRY" = "1" ]; then
          echo "    $p: (dry-run) would: back up real code dir -> $BK, then symlink -> $CENTRAL_CODE"
        else
          mkdir -p "$ARCHIVE"; cp -a "$LINK" "$BK"; rm -rf "$LINK"
          ln -sfn "$CENTRAL_CODE" "$LINK"
          echo "    $p: code symlinked -> $CENTRAL_CODE (real dir backed up to $BK)"
        fi
      fi
    done
  fi

  # ── verify: neutral root is now the ONLY live store; old trees frozen ──
  echo "==> [central] topology report"
  echo "    neutral root (live)   : $NEUTRAL"
  for SRC in "$HERMES_HOME/plugins/filemem/storage" \
             "$HERMES_HOME"/profiles/*/plugins/filemem/storage; do
    [ -d "$SRC" ] || continue
    echo "    frozen old tree       : $(realpath "$SRC")  (mtime must not move post-cutover)"
  done
  echo "==> [central] done. Runtime auto-resolve lands every profile on $NEUTRAL; old trees are frozen snapshots."
  echo "    NOTE: run this with the gateway STOPPED (stop->copy->verify->start transaction, @devops)"
}

# ═══════════════════════════════════════════════════════════════════════════════
# MODE: --all-profiles — enable filemem in every profile's config.yaml (idempotent
# merge, no clobber), plus a top-level multiplex_profiles=true check.
#
# What it writes per profile:
#   memory:
#     provider: filemem
#     filemem: {}            # (or an existing non-empty value — never overwrote)
#   plugins:
#     entries:
#       filemem:             # (merged under the existing entries, no clobber)
#         allow_tool_override: false
#
# What it does NOT do: it does NOT write the top-level $HERMES_HOME/config.yaml
# (that is the default profile's config, already handled by `install_dir`); it
# targets ONLY profiles/*/config.yaml, so a host with no profiles/ sub-dir gets
# a "0 profiles — nothing to do" (not an error — a single-profile install has
# no multiplexed gateway to worry about).
#
# multiplex_profiles check: if $HERMES_HOME/config.yaml exists and lacks
# `multiplex_profiles: true`, print a WARNING (do NOT auto-set it — that key
# belongs to the host's gateway topology, not to the memory provider; the
# installer must not silently change gateway routing).
# ═══════════════════════════════════════════════════════════════════════════════
install_all_profiles() {
  local PROFILES_DIR="$HERMES_HOME/profiles"
  [ -d "$PROFILES_DIR" ] || {
    echo "==> [all-profiles] no $PROFILES_DIR — single-profile install (nothing to do)"
    return 0
  }

  # ── exclude list: comma-separated profile names to SKIP (owner constraint:
  #    never touch profiles that are not meant to have filemem).  Default is
  #    empty; the agent prompt passes FM_EXCLUDE=4d,amoi,culinary,default when
  #    the host owner wants those kept as-is.  FM_INCLUDE (comma list, optional)
  #    restricts to ONLY those names when set; when both are set, INCLUDE wins
  #    and EXCLUDE is ignored (explicit allowlist overrides blocklist).
  local INCLUDE="${FM_INCLUDE:-}" EXCLUDE="${FM_EXCLUDE:-}"
  # comma -> space for the case guards below (case patterns need " name " tokens)
  local _inc=" $(echo "$INCLUDE" | tr ',' ' ')" _exc=" $(echo "$EXCLUDE" | tr ',' ' ')"

  # ── multiplex check (loud, never auto-set) ──────────────────────────────────
  local TOP="$HERMES_HOME/config.yaml"
  if [ -f "$TOP" ] && ! grep -qE '^\s*multiplex_profiles:\s*true' "$TOP"; then
    echo "WARN: $TOP does not set multiplex_profiles: true — this host's gateway"
    echo "     may not be in multiplex mode. The installer will NOT auto-set that"
    echo "     key (it is host-topology, not provider config). If you intend to run"
    echo "     a multiplexed gateway, add it manually to $TOP before the gateway"
    echo "     restart."
  fi

  # ── per-profile enable ──────────────────────────────────────────────────────
  local pcfg pname enabled=0 skipped=0
  local _inc=" $INCLUDE " _exc=" $EXCLUDE "
  for pcfg in "$PROFILES_DIR"/*/config.yaml; do
    [ -f "$pcfg" ] || continue
    pname="$(basename "$(dirname "$pcfg")")"
    # INCLUDE wins: if set, only those profiles are touched
    if [ -n "$INCLUDE" ]; then
      case "$_inc" in *" $pname "*) : ;; *) echo "    [all-profiles] $pname: not in FM_INCLUDE — skip"; continue ;; esac
    fi
    # EXCLUDE: skip those profiles
    case "$_exc" in *" $pname "*) echo "    [all-profiles] $pname: in FM_EXCLUDE — skip"; skipped=$((skipped+1)); continue ;; esac
    echo "==> [all-profiles] $pcfg (profile: $pname)"

    # 1. memory.provider: filemem — idempotent: skip if already set
    if grep -qE '^\s*memory:' "$pcfg" && grep -qE '^\s*provider:\s*filemem' "$pcfg"; then
      echo "    memory.provider: filemem (already set — skip)"
    else
      if grep -qE '^\s*memory:' "$pcfg"; then
        # has memory: but provider not filemem — add the line under it (append if absent)
        if ! grep -qE '^\s*provider:' "$pcfg"; then
          "$PY" - "$pcfg" <<'PYEOF'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
txt = p.read_text()
# find the memory: block and insert provider: filemem right after the first indent
# that is at least 2 spaces (under memory:)
m = re.search(r'^(\s*)memory:\s*$', txt, flags=re.M)
if m:
    indent = m.group(1) + "  "
    # skip past existing keys until we find one that's not deeper-indented
    lines = txt.split('\n')
    mem_idx = txt[:m.end()].count('\n')
    insert_at = mem_idx + 1
    while insert_at < len(lines):
        if not lines[insert_at].strip() or lines[insert_at].startswith(indent):
            insert_at += 1
            continue
        break
    lines.insert(insert_at, f"{indent}provider: filemem")
    txt = '\n'.join(lines)
    p.write_text(txt)
    print(f"    appended memory.provider: filemem under existing memory: section")
PYEOF
        fi
      else
        { echo "memory:"; echo "  provider: filemem"; } >> "$pcfg"
        echo "    appended memory.provider: filemem (new section)"
      fi
    fi

    # 2. plugins.entries.filemem — idempotent: skip if already present
    if grep -qE '^\s*filemem:' "$pcfg"; then
      echo "    plugins.entries.filemem: present (skip — never clobber)"
    else
      # FM_EXCLUDE guard: do NOT add the plugins.entries.filemem block for
      # excluded profiles (owner constraint: those profiles must stay untouched).
      case "$_exc" in *" $pname "*)
        echo "    plugins.entries.filemem: profile in FM_EXCLUDE — skip (no clobber)"
        ;;
        *)
        echo "    plugins.entries.filemem: appending"
        "$PY" - "$pcfg" <<'PYEOF'
import sys, re, pathlib
p = pathlib.Path(sys.argv[1])
txt = p.read_text()
inserted = False
if re.search(r'^\s*plugins:\s*$', txt, flags=re.M):
    lines = txt.split('\n')
    out = []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if re.match(r'^\s*plugins:\s*$', lines[i]) and not inserted:
            j = i + 1
            found_entries = False
            while j < len(lines):
                if not lines[j].strip():
                    j += 1; continue
                if re.match(r'^\s*entries:\s*$', lines[j]):
                    found_entries = True
                    out.append("      filemem:")
                    out.append("        allow_tool_override: false")
                    inserted = True
                    break
                cur_indent = len(lines[j]) - len(lines[j].lstrip())
                if cur_indent <= 2:
                    break
                j += 1
            if not found_entries:
                out.append("  entries:")
                out.append("    filemem:")
                out.append("      allow_tool_override: false")
                inserted = True
        i += 1
    txt = '\n'.join(out)
if not inserted:
    txt += "\nplugins:\n  entries:\n    filemem:\n      allow_tool_override: false\n"
p.write_text(txt)
print("    appended plugins.entries.filemem (allow_tool_override: false)")
PYEOF
        ;;
      esac
    fi

    enabled=$((enabled+1))
  done

  echo "==> [all-profiles] $enabled profile(s) enabled, $skipped skipped. Restart each profile's gateway (or the multiplexed host gateway once) for the change to take effect."
}

case "$MODE" in
  dir)     install_dir ;;
  pip)     install_pip ;;
  central) install_central ;;
  all_profiles) install_all_profiles ;;
esac
