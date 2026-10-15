#!/usr/bin/env bash
#
# Build one firmware node and run the symbol gate on the result.
#
# scripts/boot-check.sh builds and then boots. This builds only, which is what
# later phases need when producing many binaries: the golden build plus one per
# defect, where booting each one is a separate and much more expensive step.
#
# The symbol gate runs here too, deliberately. A defective binary whose injection
# targets were collected by the linker would produce a campaign that reports PASS
# for the wrong reason, and the cheapest place to catch that is at the build.
#
# Exit codes match boot-check.sh:
#   0  built and gated
#   1  the gate rejected the binary
#   2  the toolchain or environment was unusable
#
# Usage:
#   bash scripts/build-firmware.sh                       # bms, default board
#   bash scripts/build-firmware.sh bms                   # explicit node
#   bash scripts/build-firmware.sh bms --pristine        # force a clean build
#   BUILD_DIR=/tmp/x bash scripts/build-firmware.sh bms  # out-of-tree build
#
set -uo pipefail

export LC_ALL=C LANG=C TZ=UTC
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
export PYTHONHASHSEED=0

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

cd "$ROOT"

step() { echo; echo "--- $* ---"; }
info() { echo "    $*"; }
die()  { local c="$1"; shift; echo >&2; echo "ERROR: $*" >&2; echo >&2; exit "$c"; }

NODE="${1:-bms}"
case "${2:-}" in
    ""|--pristine) ;;
    *) die 2 "unknown argument: $2" ;;
esac
PRISTINE=auto
[ "${2:-}" = "--pristine" ] && PRISTINE=always

ENVFILE="$ROOT/.detect-env"
[ -f "$ENVFILE" ] || die 2 "$ENVFILE is missing. Run: bash scripts/setup.sh"
# shellcheck source=/dev/null
. "$ENVFILE"

APP="$ROOT/systems/powertrain/firmware/$NODE"
[ -d "$APP" ] || die 2 "no such firmware node: $APP"
[ -f "$APP/CMakeLists.txt" ] || die 2 "$APP has no CMakeLists.txt"

BOARD="${BOARD:-nucleo_h743zi}"
BUILD="${BUILD_DIR:-$APP/build}"
ELF="$BUILD/zephyr/zephyr.elf"
SYMLIST="$APP/injectable_symbols.txt"
LOG="$BUILD.log"

step "Building $NODE for $BOARD"
info "app:      $APP"
info "build:    $BUILD"
info "pristine: $PRISTINE"
info "log:      $LOG"

# BUILD_VERSION is pinned for the same reason boot-check.sh pins it: Zephyr
# derives the boot banner from `git describe` inside ZEPHYR_BASE, which makes the
# console a function of that tree's git state rather than of this repository.
# Output goes to a file and west's own status is checked; a `| tee` here would
# report tee's status, which almost always succeeds.
west build -b "$BOARD" -d "$BUILD" "$APP" "--pristine=$PRISTINE" \
    -- -DBUILD_VERSION=zephyr-v"$ZEPHYR_VERSION" >"$LOG" 2>&1
rc=$?
if [ $rc -ne 0 ]; then
    echo >&2; tail -40 "$LOG" >&2
    die 2 "build failed (exit $rc). Full log: $LOG"
fi
[ -f "$ELF" ] || die 2 "build reported success but produced no ELF at $ELF"
info "elf:      $ELF"
info "elf sha:  $(sha256sum "$ELF" | cut -d' ' -f1)"

step "Symbol gate"
if [ ! -f "$SYMLIST" ]; then
    die 2 "$SYMLIST is missing.

It is the single list driving both the -Wl,--undefined link flags and this gate.
Building without it yields a binary whose injection targets are unprotected and
unchecked, so this is refused rather than warned about."
fi
"$DETECT_PYTHON" "$ROOT/scripts/check_symbols.py" "$ELF" --from-list "$SYMLIST"
gate=$?
[ $gate -eq 0 ] || die 1 "the symbol gate rejected $ELF (exit $gate). See above."

step "Bit-rate assertion"
# Wired in here, not left to the caller, so it cannot be skipped. The board
# devicetree sets its own bus-speed; a dropped or misspelled overlay property is
# accepted silently by the devicetree compiler and leaves that default standing,
# putting every timing measurement out by a constant factor with nothing
# reporting it.
NETWORK="${NETWORK:-$ROOT/systems/powertrain/network.yml}"
BOARDS="${BOARDS:-$ROOT/harness/boards.yml}"
if [ ! -f "$NETWORK" ]; then
    die 2 "network.yml not found at $NETWORK (needed for the bit-rate assertion)"
fi
"$DETECT_PYTHON" "$ROOT/scripts/check_bitrate.py" "$BUILD" "$NODE" \
    --network "$NETWORK" --boards "$BOARDS"
rate_rc=$?
[ $rate_rc -eq 0 ] || die $rate_rc "the bit-rate assertion rejected $NODE (exit $rate_rc). See above."

echo
echo "PASS: $NODE built for $BOARD, injection targets resolve, bit rate confirmed."
