#!/usr/bin/env bash
#
# One-command Latch bootstrap for macOS, Linux, and Windows Git Bash.
#
# From the project repo that should be wired:
#   curl --proto '=https' --tlsv1.2 -LsSf \
#     https://raw.githubusercontent.com/open-latch/latch/main/install.sh | bash
#
# The bootstrap owns only the Latch application checkout and its virtual
# environment. Agent configuration changes are delegated to latch_quickstart,
# which preserves unrelated settings and writes backups before managed edits.
set -euo pipefail

readonly DEFAULT_REPOSITORY="https://github.com/open-latch/latch.git"
readonly DEFAULT_REF="main"
readonly UV_VERSION="0.11.28"
readonly UV_INSTALLER_URL_DEFAULT="https://astral.sh/uv/${UV_VERSION}/install.sh"

main() {
die() {
  printf 'latch install: error: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '\n==> %s\n' "$*"
}

usage() {
  cat <<'EOF'
Install Latch and run its guided project quickstart.

Usage:
  bash install.sh [bootstrap options] [quickstart options]

Bootstrap options:
  --install-dir PATH  App checkout (platform data directory by default)
  --project PATH      Project to wire (the caller's current directory by default)
  --ref REF           Git branch, tag, or commit for a fresh install
  --upgrade           Explicitly update an existing clean checkout to --ref
  --dry-run           Print the bootstrap plan without network or filesystem writes
  -h, --help          Show this help

Other arguments are forwarded to latch_quickstart, for example:
  --agents claude|codex|cursor|both|all
  --seed-source claude|codex|cursor|both|all
  --cursor-history
  --no-seed

Rerunning without --upgrade keeps the installed source revision and reconciles
dependencies, wiring, doctor checks, and the initial-KB handoff. An upgrade
refuses a dirty checkout. Neither path deletes Latch's production KB.
EOF
}

default_install_dir() {
  [ -n "${HOME:-}" ] || die 'HOME is not set; pass --install-dir explicitly'
  case "$(uname -s 2>/dev/null || printf unknown)" in
    Darwin) printf '%s\n' "$HOME/Library/Application Support/Latch/app" ;;
    MINGW*|MSYS*|CYGWIN*)
      if [ -n "${LOCALAPPDATA:-}" ] && command -v cygpath >/dev/null 2>&1; then
        printf '%s\n' "$(cygpath -u "$LOCALAPPDATA")/Latch/app"
      else
        printf '%s\n' "$HOME/AppData/Local/Latch/app"
      fi
      ;;
    *) printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/latch/app" ;;
  esac
}

PROJECT="$(pwd -P)"
INSTALL_DIR="${LATCH_INSTALL_DIR:-$(default_install_dir)}"
REPOSITORY="${LATCH_INSTALL_REPOSITORY:-$DEFAULT_REPOSITORY}"
REF="${LATCH_INSTALL_REF:-$DEFAULT_REF}"
UPGRADE=0
DRY_RUN=0
REF_EXPLICIT=0
QUICKSTART_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir)
      [ "$#" -ge 2 ] || die '--install-dir needs a path'
      INSTALL_DIR="$2"
      shift 2
      ;;
    --project)
      [ "$#" -ge 2 ] || die '--project needs a path'
      PROJECT="$2"
      shift 2
      ;;
    --ref)
      [ "$#" -ge 2 ] || die '--ref needs a branch, tag, or commit'
      REF="$2"
      REF_EXPLICIT=1
      shift 2
      ;;
    --upgrade)
      UPGRADE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      QUICKSTART_ARGS+=("$@")
      break
      ;;
    *)
      QUICKSTART_ARGS+=("$1")
      shift
      ;;
  esac
done

[ -d "$PROJECT" ] || die "project directory does not exist: $PROJECT"
PROJECT="$(cd "$PROJECT" && pwd -P)"
case "$INSTALL_DIR" in
  /*|[A-Za-z]:/*) ;;
  *) INSTALL_DIR="$(pwd -P)/$INSTALL_DIR" ;;
esac
INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
UV_DIR="${LATCH_UV_DIR:-$INSTALL_PARENT/bin}"

if [ "$REF_EXPLICIT" -eq 1 ] && [ -d "$INSTALL_DIR/.git" ] && [ "$UPGRADE" -ne 1 ]; then
  die '--ref does not change an existing install; add --upgrade explicitly'
fi

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'Latch one-command bootstrap plan (no writes)\n'
  printf '  repository : %s\n' "$REPOSITORY"
  printf '  ref        : %s\n' "$REF"
  printf '  install dir: %s\n' "$INSTALL_DIR"
  printf '  project    : %s\n' "$PROJECT"
  if [ -d "$INSTALL_DIR/.git" ]; then
    [ "$UPGRADE" -eq 1 ] && printf '  source mode: explicit upgrade\n' \
      || printf '  source mode: keep current revision and reconcile\n'
  else
    printf '  source mode: staged fresh checkout\n'
  fi
  printf '  runtime    : private uv + Python 3.11 virtual environment\n'
  printf '  activation : guided quickstart, checks, then consented initial-KB review\n'
  if [ "${#QUICKSTART_ARGS[@]}" -gt 0 ]; then
    printf '  quickstart : %s\n' "${QUICKSTART_ARGS[*]}"
  else
    printf '  quickstart : interactive choices / safe defaults\n'
  fi
  exit 0
fi

command -v git >/dev/null 2>&1 || die 'git is required; install Git and rerun'

normalize_repository() {
  local value="$1" converted
  case "$value" in
    git@github.com:*) value="https://github.com/${value#git@github.com:}" ;;
    ssh://git@github.com/*) value="https://github.com/${value#ssh://git@github.com/}" ;;
  esac
  # Git for Windows records a Git Bash local path such as /c/Users/... as
  # C:/Users/... in remote.origin.url. Canonicalize both spellings before the
  # ownership comparison so the checkout does not reject its own staged clone.
  case "$value" in
    /*|[A-Za-z]:/*)
      if command -v cygpath >/dev/null 2>&1 \
          && converted="$(cygpath -am "$value" 2>/dev/null)"; then
        value="$converted"
      fi
      ;;
  esac
  value="${value%/}"
  value="${value%.git}"
  printf '%s\n' "$value"
}

validate_checkout() {
  local app="$1" top actual expected
  [ ! -L "$app" ] || die "refusing symlink install directory: $app"
  [ -d "$app/.git" ] || die "existing install path is not a Latch Git checkout: $app"
  top="$(git -C "$app" rev-parse --show-toplevel 2>/dev/null)" \
    || die "cannot read Git checkout: $app"
  top="$(cd "$top" && pwd -P)"
  [ "$top" = "$(cd "$app" && pwd -P)" ] \
    || die "install path is nested inside another Git checkout: $app"
  actual="$(git -C "$app" remote get-url origin 2>/dev/null)" \
    || die "existing checkout has no origin remote: $app"
  actual="$(normalize_repository "$actual")"
  expected="$(normalize_repository "$REPOSITORY")"
  [ "$actual" = "$expected" ] \
    || die "existing checkout origin is $actual, expected $expected; refusing overwrite"
  [ -f "$app/VERSION" ] && [ -f "$app/requirements.txt" ] \
    && [ -f "$app/src/quickstart.py" ] \
    || die "checkout is missing required Latch files: $app"
}

resolve_uv() {
  local candidate="${LATCH_UV:-}" checksum_shim_dir="" installer_path="$PATH"
  if [ -n "$candidate" ]; then
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return
    fi
    [ -x "$candidate" ] || die "LATCH_UV is not executable: $candidate"
    printf '%s\n' "$candidate"
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return
  fi
  if [ -x "$UV_DIR/uv" ]; then
    printf '%s\n' "$UV_DIR/uv"
    return
  fi
  if [ -x "${HOME:-}/.local/bin/uv" ]; then
    printf '%s\n' "$HOME/.local/bin/uv"
    return
  fi

  command -v curl >/dev/null 2>&1 \
    || die 'curl is required to bootstrap uv; install curl or set LATCH_UV'
  mkdir -p "$UV_DIR"
  local installer
  installer="$(mktemp "${TMPDIR:-/tmp}/latch-uv-installer.XXXXXX")"
  if ! curl --proto '=https' --tlsv1.2 -fsSL \
      "${LATCH_UV_INSTALLER_URL:-$UV_INSTALLER_URL_DEFAULT}" -o "$installer"; then
    rm -f "$installer"
    die 'could not download the official uv installer'
  fi
  # uv's installer expects GNU sha256sum. macOS provides the equivalent
  # shasum command, so supply a private compatibility shim rather than
  # accepting the installer's checksum-skipped fallback.
  if ! command -v sha256sum >/dev/null 2>&1 \
      && command -v shasum >/dev/null 2>&1; then
    checksum_shim_dir="$(mktemp -d "${TMPDIR:-/tmp}/latch-sha256sum.XXXXXX")"
    printf '%s\n' '#!/bin/sh' 'exec shasum -a 256 "$@"' \
      > "$checksum_shim_dir/sha256sum"
    chmod +x "$checksum_shim_dir/sha256sum"
    installer_path="$checksum_shim_dir:$PATH"
  fi
  # resolve_uv is called through command substitution; keep installer status
  # out of stdout so the function returns only the executable path.
  if ! PATH="$installer_path" UV_UNMANAGED_INSTALL="$UV_DIR" \
      sh "$installer" >&2; then
    rm -f "$installer"
    [ -z "$checksum_shim_dir" ] || rm -rf -- "$checksum_shim_dir"
    die 'the official uv installer failed'
  fi
  rm -f "$installer"
  [ -z "$checksum_shim_dir" ] || rm -rf -- "$checksum_shim_dir"
  [ -x "$UV_DIR/uv" ] || die "uv installer did not create $UV_DIR/uv"
  printf '%s\n' "$UV_DIR/uv"
}

STAGE_ROOT=""
cleanup_stage() {
  if [ -n "$STAGE_ROOT" ] && [ -d "$STAGE_ROOT" ]; then
    rm -rf -- "$STAGE_ROOT"
  fi
}
trap cleanup_stage EXIT

checkout_source() {
  local target="$1"
  mkdir -p "$target"
  git -C "$target" init --quiet
  git -C "$target" remote add origin "$REPOSITORY"
  git -C "$target" fetch --quiet --depth 1 origin "$REF"
  git -C "$target" checkout --quiet --detach FETCH_HEAD
  validate_checkout "$target"
}

runtime_python() {
  local app="$1"
  if [ -x "$app/.venv/bin/python" ]; then
    printf '%s\n' "$app/.venv/bin/python"
  elif [ -x "$app/.venv/Scripts/python.exe" ]; then
    printf '%s\n' "$app/.venv/Scripts/python.exe"
  else
    return 1
  fi
}

sqlite_vec_preflight() {
  local app="$1" python_path="$2"
  if ! "$python_path" -B -c '
import sys
sys.path.insert(0, sys.argv[1])
from doctor import OK, _VEC_PROBE, _run_probe
level, _ = _run_probe(_VEC_PROBE, "VEC_OK", 30, arch_hint=True)
if level != OK:
    raise SystemExit(1)
' "$app/src"; then
    printf 'sqlite-vec capability preflight failed for %s. The resolved interpreter may be built without SQLite extension loading support. Rebuild or replace the interpreter or %s/.venv, then rerun. No project or agent configuration was written; no app files were removed.\n' \
      "$python_path" "$app" >&2
    return 1
  fi
}

prepare_runtime() {
  local app="$1" python_path requirements
  python_path="$(runtime_python "$app" 2>/dev/null || true)"
  if [ -z "$python_path" ]; then
    UV_NO_CONFIG=1 "$UV" venv --python 3.11 "$app/.venv" || return
    python_path="$(runtime_python "$app" 2>/dev/null || true)"
  fi
  [ -n "$python_path" ] || return 1
  "$python_path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "existing Latch virtual environment is older than Python 3.11: $python_path"
  if [ -f "$app/requirements.lock" ]; then
    requirements="$app/requirements.lock"
    UV_NO_CONFIG=1 "$UV" pip install --python "$python_path" \
      --require-hashes -r "$requirements" || return
  else
    # Compatibility for pre-runtime-lock releases.
    requirements="$app/requirements.txt"
    UV_NO_CONFIG=1 "$UV" pip install --python "$python_path" -r "$requirements" \
      || return
  fi
  sqlite_vec_preflight "$app" "$python_path"
}

mkdir -p "$INSTALL_PARENT"
UV="$(resolve_uv)"

if [ -e "$INSTALL_DIR" ]; then
  validate_checkout "$INSTALL_DIR"
  if [ "$UPGRADE" -eq 1 ]; then
    dirty="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=normal)"
    [ -z "$dirty" ] \
      || die "upgrade refused because the install checkout is dirty; preserve or remove local changes first"
    OLD_COMMIT="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    note "Fetching explicit Latch upgrade ref $REF"
    git -C "$INSTALL_DIR" fetch --quiet --depth 1 origin "$REF"
    NEW_COMMIT="$(git -C "$INSTALL_DIR" rev-parse FETCH_HEAD)"
    git -C "$INSTALL_DIR" checkout --quiet --detach "$NEW_COMMIT"
    if ! prepare_runtime "$INSTALL_DIR"; then
      printf 'Runtime setup failed after source update; restoring %s.\n' "$OLD_COMMIT" >&2
      git -C "$INSTALL_DIR" checkout --quiet --detach "$OLD_COMMIT"
      prepare_runtime "$INSTALL_DIR" >/dev/null 2>&1 || true
      die 'upgrade rolled back; the previous checkout remains installed'
    fi
  else
    note 'Existing Latch checkout found; keeping its source revision'
    prepare_runtime "$INSTALL_DIR" \
      || die "runtime reconciliation failed; the checkout remains at $INSTALL_DIR"
  fi
else
  [ ! -L "$INSTALL_DIR" ] || die "refusing symlink install path: $INSTALL_DIR"
  STAGE_ROOT="$(mktemp -d "$INSTALL_PARENT/.latch-install.XXXXXX")"
  note "Fetching Latch $REF into a staging checkout"
  checkout_source "$STAGE_ROOT/app"
  [ ! -e "$INSTALL_DIR" ] \
    || die "install path appeared during bootstrap; refusing to merge into it: $INSTALL_DIR"
  mv "$STAGE_ROOT/app" "$INSTALL_DIR"
  rmdir "$STAGE_ROOT"
  STAGE_ROOT=""
  prepare_runtime "$INSTALL_DIR" \
    || die "runtime setup failed; the verified source checkout remains at $INSTALL_DIR for a safe rerun"
fi

PYTHON_PATH="$(runtime_python "$INSTALL_DIR")" \
  || die "Latch runtime is missing after setup: $INSTALL_DIR/.venv"
COMMIT="$(git -C "$INSTALL_DIR" rev-parse --short=12 HEAD)"
VERSION="$(tr -d '\r\n' < "$INSTALL_DIR/VERSION")"

note 'Running the guided Latch activation'
run_quickstart() {
  env LATCH_HOME="$INSTALL_DIR" LATCH_PYTHON="$PYTHON_PATH" \
    "$PYTHON_PATH" "$INSTALL_DIR/src/quickstart.py" \
    --project "$PROJECT" ${QUICKSTART_ARGS[@]+"${QUICKSTART_ARGS[@]}"}
}

if { exec 3</dev/tty; } 2>/dev/null; then
  if run_quickstart <&3; then
    QUICKSTART_RC=0
  else
    QUICKSTART_RC=$?
  fi
  exec 3<&-
else
  if run_quickstart; then
    QUICKSTART_RC=0
  else
    QUICKSTART_RC=$?
  fi
fi

if [ "$QUICKSTART_RC" -ne 0 ]; then
  printf '\nLatch app/runtime installation succeeded, but activation stopped with status %s.\n' \
    "$QUICKSTART_RC" >&2
  printf 'No app files were removed. Fix the reported preflight/check and rerun this command.\n' >&2
  exit "$QUICKSTART_RC"
fi

printf '\nLatch activation complete.\n'
printf '  version : %s (%s)\n' "$VERSION" "$COMMIT"
printf '  app     : %s\n' "$INSTALL_DIR"
printf '  project : %s\n' "$PROJECT"
printf '  unwire  : %s\n' "$INSTALL_DIR/bin/uninstall.sh"
printf 'The unwire command preserves the production KB and the app checkout.\n'
}

main "$@"
