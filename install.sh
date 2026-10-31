#!/usr/bin/env bash
set -euo pipefail

# macOS and Debian/Ubuntu bootstrap. Never removes or resets existing files.
install_dir="${VYDLORA_INSTALL_DIR:-$HOME/vydlora}"
no_start=0; no_update=0; no_browser=0; skip_browser=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --directory) [ "$#" -ge 2 ] || { echo 'Missing --directory value.' >&2; exit 1; }; install_dir="$2"; shift 2 ;;
    --no-start) no_start=1; shift ;;
    --no-update) no_update=1; shift ;;
    --no-browser) no_browser=1; shift ;;
    --skip-browser) skip_browser=1; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
case "$install_dir" in /*) ;; *) install_dir="$PWD/$install_dir" ;; esac
fail() { printf '%s\n' "$*" >&2; exit 1; }
python_path=''
find_python() {
  local candidate
  for candidate in "$install_dir/engine/.venv/bin/python" "${LORA1_PYTHON:-}" python3.12 python3.13 python3 python; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys, venv; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
      python_path="$candidate"; return 0
    fi
  done
  return 1
}
supported_node() { command -v node >/dev/null 2>&1 && node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)' >/dev/null 2>&1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

printf 'Vydlora installer\nInstall folder: %s\n' "$install_dir"
case "$(uname -s)" in
  Darwin)
    if ! command -v brew >/dev/null 2>&1; then
      if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"
      elif [ -x /usr/local/bin/brew ]; then eval "$(/usr/local/bin/brew shellenv)"
      else
        printf 'Installing Homebrew from its official installer. macOS may request your permission.\n'
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [ -x /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; else eval "$(/usr/local/bin/brew shellenv)"; fi
      fi
    fi
    command -v git >/dev/null 2>&1 || brew install git
    supported_node || { brew install node@24; export PATH="$(brew --prefix node@24)/bin:$PATH"; }
    find_python || { brew install python@3.12; export PATH="$(brew --prefix python@3.12)/bin:$PATH"; }
    command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg
    ;;
  Linux)
    command -v apt-get >/dev/null 2>&1 || fail 'Automatic system setup supports Debian/Ubuntu. Use the README manual setup for other Linux distributions.'
    if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1 || ! command -v ffmpeg >/dev/null 2>&1 || ! find_python; then
      as_root apt-get update
      as_root apt-get install -y git curl ca-certificates xz-utils python3 python3-venv python3-pip ffmpeg
    fi
    find_python || fail 'This distribution needs Python 3.11 or newer. Use a current Debian/Ubuntu release, or install a supported Python and rerun.'
    if ! supported_node; then
      case "$(uname -m)" in x86_64) node_arch=x64 ;; aarch64|arm64) node_arch=arm64 ;; *) fail 'Automatic Node.js installation supports x64 and arm64 Linux.' ;; esac
      node_cache="${XDG_CACHE_HOME:-$HOME/.cache}/vydlora/node"
      mkdir -p "$node_cache"
      manifest="$node_cache/SHASUMS256.txt"
      curl -fsSL https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt -o "$manifest"
      node_archive="$(awk -v arch="$node_arch" '$2 ~ ("^node-v24[.].*-linux-" arch "[.]tar[.]xz$") { print $2; exit }' "$manifest")"
      [ -n "$node_archive" ] || fail 'The official Node.js download manifest did not contain this platform.'
      case "$node_archive" in *[!a-zA-Z0-9.-]*) fail 'Invalid archive name in the Node.js manifest.' ;; esac
      expected_sum="$(awk -v name="$node_archive" '$2 == name { print $1 }' "$manifest")"
      curl -fsSL "https://nodejs.org/dist/latest-v24.x/$node_archive" -o "$node_cache/$node_archive"
      "$python_path" -c 'import hashlib,sys; assert hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest() == sys.argv[2], "Node.js checksum mismatch"' "$node_cache/$node_archive" "$expected_sum"
      node_folder="${node_archive%.tar.xz}"
      tar -xJf "$node_cache/$node_archive" -C "$node_cache"
      export PATH="$node_cache/$node_folder/bin:$PATH"
    fi
    ;;
  *) fail 'Use install.ps1 on Windows, or the manual setup guide for this platform.' ;;
esac
supported_node || fail 'Node.js 22+ is unavailable.'
find_python || fail 'Python 3.11+ is unavailable.'

if [ -d "$install_dir/.git" ]; then
  remote="$(git -C "$install_dir" remote get-url origin)"
  case "$remote" in https://github.com/wavrix/vydlora|https://github.com/wavrix/vydlora.git|git@github.com:wavrix/vydlora|git@github.com:wavrix/vydlora.git) ;; *) fail 'This folder belongs to another repository. Choose a different --directory.' ;; esac
  if [ "$no_update" -eq 0 ]; then
    [ -z "$(git -C "$install_dir" status --porcelain)" ] || fail 'This checkout has source changes. Save/commit them or use --no-update; they will not be overwritten.'
    [ "$(git -C "$install_dir" branch --show-current)" = main ] || fail 'This checkout is not on main. Use --no-update to keep your branch.'
    git -C "$install_dir" pull --ff-only origin main
  fi
else
  if [ -e "$install_dir" ] && [ -n "$(ls -A "$install_dir")" ]; then fail 'The destination is not an empty Vydlora checkout. Choose another --directory.'; fi
  mkdir -p "$(dirname "$install_dir")"
  git clone --branch main https://github.com/wavrix/vydlora.git "$install_dir"
fi
cd "$install_dir"
setup_arguments=(scripts/setup-engine.mjs --python "$python_path")
if [ "$skip_browser" -eq 1 ]; then setup_arguments+=(--skip-browser)
elif [ "$(uname -s)" = Linux ]; then setup_arguments+=(--with-browser-deps); fi
node "${setup_arguments[@]}"
# Keep a restart command that also works when Node was installed in the user cache.
node_executable="$(command -v node)"
"$python_path" - "$node_executable" "$install_dir" <<'PY'
from pathlib import Path
import shlex, sys
destination = Path(sys.argv[2]) / '.runtime' / 'start-vydlora.sh'
destination.write_text('#!/usr/bin/env bash\nset -e\ncd ' + shlex.quote(sys.argv[2]) + '\nexec ' + shlex.quote(sys.argv[1]) + ' scripts/launch.mjs "$@"\n')
destination.chmod(0o700)
PY
printf 'Vydlora is installed in %s.\n' "$install_dir"
printf 'Start again with: bash "%s/.runtime/start-vydlora.sh"\n' "$install_dir"
if [ "$no_start" -eq 0 ]; then
  launch_arguments=(scripts/launch.mjs)
  [ "$no_browser" -eq 0 ] || launch_arguments+=(--no-browser)
  exec node "${launch_arguments[@]}"
fi
