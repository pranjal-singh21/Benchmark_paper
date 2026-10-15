#!/usr/bin/env bash
#
# DETECT toolchain bootstrap.
#
# Installs everything under $HOME. Never uses sudo. Idempotent: running it twice is
# harmless and fast, because every step is guarded by a marker that records the
# pinned version it was satisfied at.
#
# Every step announces itself before it runs. An empty output with a non-zero exit is
# indistinguishable from a catastrophe, so there is no silent step anywhere in here.
#
# Usage:
#   bash scripts/setup.sh              # all steps
#   bash scripts/setup.sh python       # one step: python renode sdk zephyr hosttools
#
set -euo pipefail

# ---------------------------------------------------------------- pinned versions
# Every version here is also recorded in TOOLCHAIN.md with the reason it is pinned.
RENODE_VERSION=1.16.1
ZEPHYR_VERSION=3.5.0
ZEPHYR_SDK_VERSION=0.16.8
CMAKE_PIN=3.31.6          # last 3.x; CMake 4 rejects cmake_minimum_required below 3.5
NINJA_PIN=1.11.1.4
WEST_PIN=1.2.0            # the west released alongside Zephyr 3.5.0

RENODE_URL="https://github.com/renode/renode/releases/download/v${RENODE_VERSION}/renode-${RENODE_VERSION}.linux-portable.tar.gz"
SDK_BASE="https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}"
GET_PIP_URL="https://bootstrap.pypa.io/get-pip.py"

# ---------------------------------------------------------------- paths
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# Re-enter through a space-free path.
#
# Zephyr's devicetree preprocessing splits paths at spaces. Measured directly,
# with this repository in a folder named "benchmark paper":
#
#   -- Application: /mnt/c/Users/djtde/Downloads/benchmark paper/.../bms
#   <command-line>: fatal error: /mnt/c/Users/djtde/Downloads/benchmark:
#                   No such file or directory
#
# Redirecting the build directory does not help: the failure is in handling the
# SOURCE path, so it happens wherever the output goes.
#
# CMake does not resolve a symlink back to its real path, so a space-free symlink
# to this repository is a usable build root. Every script re-executes itself
# through that symlink before doing any work, which means the toolchain never
# sees a path containing a space, and the repository can still live wherever the
# user wants it.
#
# After the exec, ROOT resolves to the symlink itself, which contains no space,
# so this branch cannot recurse.
# ---------------------------------------------------------------------------
case "$ROOT" in
  *" "*)
    DETECT_LINK="${DETECT_LINK:-$HOME/detect-master}"
    if [ "$(readlink -f "$DETECT_LINK" 2>/dev/null)" != "$(readlink -f "$ROOT")" ]; then
        rm -f "$DETECT_LINK"
        ln -s "$ROOT" "$DETECT_LINK" || {
            echo "ERROR: cannot create the space-free symlink at $DETECT_LINK" >&2
            echo "       The toolchain cannot build from a path containing a space." >&2
            exit 2
        }
    fi
    export DETECT_LINK
    exec bash "$DETECT_LINK/scripts/$(basename "${BASH_SOURCE[0]}")" "$@"
    ;;
esac

VENV="$HOME/detect-venv"
RENODE_HOME="$HOME/renode-${RENODE_VERSION}"
SDK_HOME="$HOME/zephyr-sdk-${ZEPHYR_SDK_VERSION}"
ZEPHYR_PROJECT="$HOME/zephyrproject"
ZEPHYR_BASE="$ZEPHYR_PROJECT/zephyr"
HOSTTOOLS="$HOME/detect-hosttools"
DOWNLOADS="$HOME/detect-downloads"
ENVFILE="$ROOT/.detect-env"

STEP=0
step()  { STEP=$((STEP+1)); echo; echo "=== STEP ${STEP}: $* ==="; }
info()  { echo "    $*"; }
die()   { echo; echo "FATAL: $*" >&2; exit 1; }

# Download to a temporary name and rename only on success, so an interrupted download
# can never be mistaken for a finished one by the idempotency guard.
fetch() {
    local url="$1" dest="$2"
    if [ -s "$dest" ]; then info "already downloaded: $(basename "$dest")"; return 0; fi
    info "downloading $url"
    curl -fSL --retry 3 --retry-delay 2 -o "${dest}.part" "$url" \
        || die "download failed: $url"
    mv "${dest}.part" "$dest"
    info "downloaded $(du -h "$dest" | cut -f1) -> $dest"
}

WANT="${1:-all}"
want() { [ "$WANT" = all ] || [ "$WANT" = "$1" ]; }

echo "DETECT setup"
echo "  repository : $ROOT"
echo "  step filter: $WANT"
mkdir -p "$DOWNLOADS"

# ============================================================ 1. python + venv
if want python; then
step "Python virtual environment at $VENV"
info "Ubuntu 24.04 ships no ensurepip, so pip is bootstrapped from get-pip.py."

if [ ! -x "$VENV/bin/python" ]; then
    info "creating venv without pip (--without-pip), because ensurepip is absent"
    python3 -m venv --without-pip "$VENV" || die "python3 -m venv failed"
else
    info "venv already present"
fi

if [ ! -x "$VENV/bin/pip" ]; then
    fetch "$GET_PIP_URL" "$DOWNLOADS/get-pip.py"
    info "bootstrapping pip into the venv"
    "$VENV/bin/python" "$DOWNLOADS/get-pip.py" --quiet || die "get-pip.py failed"
else
    info "pip already present in venv"
fi

info "venv python: $("$VENV/bin/python" -V 2>&1)"
info "venv pip   : $("$VENV/bin/pip" -V 2>&1 | cut -d' ' -f1-2)"

step "Python packages"
# cmake and ninja come from wheels because apt requires root, which this project
# does not use for toolchain components.
info "installing: pyelftools pyyaml pytest west==${WEST_PIN} cmake==${CMAKE_PIN} ninja==${NINJA_PIN}"
"$VENV/bin/pip" install --quiet --disable-pip-version-check \
    pyelftools pyyaml pytest \
    "west==${WEST_PIN}" "cmake==${CMAKE_PIN}" "ninja==${NINJA_PIN}" \
    || die "pip install failed"

info "verifying imports"
"$VENV/bin/python" -c "import elftools, yaml, pytest; print('    imports ok')" \
    || die "core python imports failed"
info "cmake: $("$VENV/bin/cmake" --version | head -1)"
info "ninja: $("$VENV/bin/ninja" --version)"
info "west : $("$VENV/bin/west" --version)"
fi

# ============================================================ 2. host tools, no root
if want hosttools; then
step "Host tools without root ($HOSTTOOLS)"
info "dtc and gperf are Zephyr host dependencies normally installed with apt."
info "apt install needs root, so the .deb archives are downloaded and unpacked"
info "into \$HOME with dpkg-deb -x, which does not."
mkdir -p "$HOSTTOOLS/bin" "$DOWNLOADS/deb"

install_deb() {
    local pkg="$1" probe="$2"
    if [ -e "$HOSTTOOLS/$probe" ]; then info "$pkg already unpacked"; return 0; fi
    info "fetching .deb for $pkg"
    ( cd "$DOWNLOADS/deb" && apt-get download "$pkg" >/dev/null 2>&1 ) || {
        echo "    WARNING: could not download $pkg. It is not installed."
        echo "    WARNING: if the Zephyr build later reports $pkg missing, that is why."
        return 1
    }
    local deb
    deb="$(ls -t "$DOWNLOADS/deb"/${pkg}_*.deb 2>/dev/null | head -1)"
    [ -n "$deb" ] || { echo "    WARNING: no .deb found for $pkg after download"; return 1; }
    info "unpacking $(basename "$deb")"
    dpkg-deb -x "$deb" "$HOSTTOOLS" || die "dpkg-deb -x failed for $deb"
}

install_deb device-tree-compiler usr/bin/dtc  || true
install_deb libfdt1           usr/lib/x86_64-linux-gnu || true
install_deb gperf             usr/bin/gperf   || true

# Put the unpacked binaries on a single stable path so .detect-env can export one dir.
for b in dtc gperf; do
    if [ -x "$HOSTTOOLS/usr/bin/$b" ] && [ ! -e "$HOSTTOOLS/bin/$b" ]; then
        ln -sf "../usr/bin/$b" "$HOSTTOOLS/bin/$b"
        info "linked $b into $HOSTTOOLS/bin"
    fi
done

export LD_LIBRARY_PATH="$HOSTTOOLS/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
if [ -x "$HOSTTOOLS/bin/dtc" ]; then
    info "dtc: $("$HOSTTOOLS/bin/dtc" --version 2>&1 | head -1)"
else
    echo "    NOTE: dtc is absent. Zephyr uses its own Python devicetree parser for"
    echo "    NOTE: the build and only calls dtc for optional validation."
fi
[ -x "$HOSTTOOLS/bin/gperf" ] && info "gperf: $("$HOSTTOOLS/bin/gperf" --version 2>&1 | head -1)" || true
fi

# ============================================================ 3. Renode
if want renode; then
step "Renode ${RENODE_VERSION} (portable release, not a package manager)"
if [ -x "$RENODE_HOME/renode" ]; then
    info "already installed at $RENODE_HOME"
else
    fetch "$RENODE_URL" "$DOWNLOADS/renode-${RENODE_VERSION}.linux-portable.tar.gz"
    info "extracting to $RENODE_HOME"
    rm -rf "$RENODE_HOME" "$DOWNLOADS/renode-extract"
    mkdir -p "$DOWNLOADS/renode-extract"
    tar -xzf "$DOWNLOADS/renode-${RENODE_VERSION}.linux-portable.tar.gz" \
        -C "$DOWNLOADS/renode-extract" || die "tar extraction failed"
    # The tarball unpacks to a single versioned directory; move it, do not guess a name.
    inner="$(find "$DOWNLOADS/renode-extract" -maxdepth 1 -mindepth 1 -type d | head -1)"
    [ -n "$inner" ] || die "renode tarball did not contain a directory"
    mv "$inner" "$RENODE_HOME"
    rm -rf "$DOWNLOADS/renode-extract"
    info "installed to $RENODE_HOME"
fi
fi

# ============================================================ 4. Zephyr SDK
if want sdk; then
step "Zephyr SDK ${ZEPHYR_SDK_VERSION} (arm-zephyr-eabi only)"
GCC="$SDK_HOME/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
if [ -x "$GCC" ]; then
    info "already installed at $SDK_HOME"
else
    fetch "$SDK_BASE/zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-x86_64_minimal.tar.xz" \
          "$DOWNLOADS/zephyr-sdk-minimal.tar.xz"
    fetch "$SDK_BASE/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz" \
          "$DOWNLOADS/toolchain-arm.tar.xz"
    info "extracting SDK skeleton into \$HOME"
    rm -rf "$SDK_HOME"
    tar -xJf "$DOWNLOADS/zephyr-sdk-minimal.tar.xz" -C "$HOME" || die "SDK extraction failed"
    [ -d "$SDK_HOME" ] || die "expected $SDK_HOME after extraction, not found"
    info "extracting arm-zephyr-eabi toolchain into the SDK"
    tar -xJf "$DOWNLOADS/toolchain-arm.tar.xz" -C "$SDK_HOME" || die "toolchain extraction failed"
    info "registering the SDK (writes a cmake package entry under \$HOME/.cmake)"
    ( cd "$SDK_HOME" && ./setup.sh -t arm-zephyr-eabi -c ) || die "SDK setup.sh failed"
fi
[ -x "$GCC" ] || die "arm-zephyr-eabi-gcc missing at $GCC"
info "compiler: $("$GCC" --version | head -1)"
fi

# ============================================================ 5. Zephyr sources
if want zephyr; then
step "Zephyr ${ZEPHYR_VERSION} via west into $ZEPHYR_PROJECT"
export PATH="$VENV/bin:$PATH"
command -v west >/dev/null || die "west not on PATH; run 'bash scripts/setup.sh python' first"

if [ ! -d "$ZEPHYR_BASE/.git" ]; then
    info "west init pinned to tag v${ZEPHYR_VERSION}"
    rm -rf "$ZEPHYR_PROJECT"
    west init -m https://github.com/zephyrproject-rtos/zephyr --mr "v${ZEPHYR_VERSION}" \
        "$ZEPHYR_PROJECT" || die "west init failed"
else
    info "west workspace already initialised"
fi

# Zephyr publishes each release under two tag spellings, "v3.5.0" and
# "zephyr-v3.5.0", and git describe returns whichever it picks. Accepting only
# one of them rejects a correctly pinned tree, so both are accepted and the
# authoritative check is the VERSION file, which the build itself reads.
actual_tag="$(cd "$ZEPHYR_BASE" && git describe --tags --exact-match 2>/dev/null || echo NONE)"
info "zephyr checkout tag: $actual_tag"
case "$actual_tag" in
    "v${ZEPHYR_VERSION}"|"zephyr-v${ZEPHYR_VERSION}") ;;
    *) die "zephyr is at tag '$actual_tag', expected v${ZEPHYR_VERSION} or zephyr-v${ZEPHYR_VERSION}.
Refusing to continue on an unpinned tree." ;;
esac

zmaj="$(sed -n 's/^VERSION_MAJOR *= *//p' "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zmin="$(sed -n 's/^VERSION_MINOR *= *//p' "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zpat="$(sed -n 's/^PATCHLEVEL *= *//p'    "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zver="$zmaj.$zmin.$zpat"
info "zephyr VERSION file: $zver"
[ "$zver" = "$ZEPHYR_VERSION" ] \
    || die "zephyr VERSION file reports $zver, expected $ZEPHYR_VERSION."

# west update is the long pole; guard it so a second run is fast.
if [ ! -d "$ZEPHYR_PROJECT/modules/hal/stm32" ]; then
    info "west update (fetches hal_stm32 and the other required modules)"
    ( cd "$ZEPHYR_PROJECT" && west update --narrow -o=--depth=1 ) || die "west update failed"
else
    info "west modules already present"
fi

info "installing Zephyr's own python requirements into the venv"
"$VENV/bin/pip" install --quiet --disable-pip-version-check \
    -r "$ZEPHYR_BASE/scripts/requirements-base.txt" || die "zephyr requirements failed"
fi

# ============================================================ 6. environment file
# Guarded like every other step so that concurrent single-step invocations, which is
# how the long downloads are run in parallel, cannot interleave writes to one file.
if want env; then
step "Writing $ENVFILE"
info "every other script sources this file and uses the venv python explicitly,"
info "because a silent fall back to system python fails like a code bug."
cat > "$ENVFILE" <<ENV
# Generated by scripts/setup.sh. Machine specific, not committed.
export DETECT_ROOT="$ROOT"
export DETECT_VENV="$VENV"
export DETECT_PYTHON="$VENV/bin/python"
export DETECT_HOSTTOOLS="$HOSTTOOLS"
export RENODE_HOME="$RENODE_HOME"
export RENODE_VERSION="$RENODE_VERSION"
export ZEPHYR_BASE="$ZEPHYR_BASE"
export ZEPHYR_VERSION="$ZEPHYR_VERSION"
export ZEPHYR_SDK_INSTALL_DIR="$SDK_HOME"
export ZEPHYR_SDK_VERSION="$ZEPHYR_SDK_VERSION"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export PATH="$VENV/bin:$HOSTTOOLS/bin:\$PATH"
export LD_LIBRARY_PATH="$HOSTTOOLS/usr/lib/x86_64-linux-gnu:\${LD_LIBRARY_PATH:-}"
ENV
sed 's/^/    /' "$ENVFILE"
fi

echo
echo "=== setup complete ($WANT) ==="
