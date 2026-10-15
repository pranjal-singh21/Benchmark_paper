#!/usr/bin/env bash
#
# Phase 0 proof: build the BMS firmware and boot it on an emulated STM32H743
# until it prints its banner over an emulated UART.
#
# Exit 0 means real compiled ARM firmware executed, instruction by instruction,
# inside Renode and reached its application entry point, and that the console it
# produced is byte-for-byte what it produced last time.
#
# Exit codes are distinct on purpose. An engine fault and a firmware failure must
# never be confused: counting a crash as a legitimate verdict is the worst bug a
# verification tool can have.
#
#   0  pass
#   1  the firmware or the check failed: a real, reportable negative result
#   2  the engine, toolchain or environment was unusable: no verdict was reached
#
# Usage:
#   bash scripts/boot-check.sh                    normal run
#   bash scripts/boot-check.sh --prove-gate       negative control: rebuild with
#                                                 one --undefined flag withheld
#                                                 and REQUIRE the symbol gate to
#                                                 fail
#   bash scripts/boot-check.sh --prove-infra-guard
#                                                 negative control: point Renode
#                                                 at an ELF that does not exist
#                                                 and REQUIRE the run to be
#                                                 classified as an engine fault
#
set -uo pipefail

# ---------------------------------------------------------------------------
# Pin the ambient environment.
#
# The exit criterion is a byte-identical hash, so every host-derived input that
# could reach the build or the console is fixed here rather than filtered out
# afterwards. Filtering hides a difference; pinning removes it.
#
#   LC_ALL/LANG       grep, sort and printf are locale sensitive
#   TZ                nothing on the hashed path prints a date, but Renode's own
#                     diagnostic log does, and a stray TZ makes two logs
#                     incomparable when diagnosing a mismatch
#   SOURCE_DATE_EPOCH consumed by gcc for __DATE__/__TIME__ and by ar/ld for
#                     embedded timestamps, making the ELF itself reproducible
#   PYTHONHASHSEED    west and cmake helpers iterate dictionaries
# ---------------------------------------------------------------------------
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

die() {
    local code="$1"; shift
    echo >&2
    echo "ERROR: $*" >&2
    echo >&2
    exit "$code"
}
fail()  { die 1 "$@"; }   # a real negative result
fault() { die 2 "$@"; }   # the engine could not produce a verdict

MODE=normal
case "${1:-}" in
    "")                   MODE=normal ;;
    --prove-gate)         MODE=prove-gate ;;
    --prove-infra-guard)  MODE=prove-infra-guard ;;
    *) fault "unknown argument: $1
Usage: boot-check.sh [--prove-gate | --prove-infra-guard]" ;;
esac

ENVFILE="$ROOT/.detect-env"
[ -f "$ENVFILE" ] || fault "$ENVFILE is missing.
The toolchain has not been set up in this clone. Run:
    bash scripts/setup.sh"
# shellcheck source=/dev/null
. "$ENVFILE"

APP="$ROOT/systems/powertrain/firmware/bms"
BUILD="$APP/build"
ELF="$BUILD/zephyr/zephyr.elf"
SYMLIST="$APP/injectable_symbols.txt"
PLATFORM="$ROOT/platforms/boards/bms_ecu.repl"
RESC="$ROOT/scripts/boot-check.resc"
OUT="$ROOT/results/boot-check"
UART_LOG="$OUT/uart.log"
RENODE_LOG="$OUT/renode.log"
BUILD_LOG="$OUT/build.log"
RENODE="$ROOT/scripts/renode"

BANNER="BMS ready"
# The firmware reports once a second and the .resc runs for five emulated
# seconds, so the console must carry exactly five reports. This is the clock
# check: a platform description with the wrong CPU frequency runs the firmware at
# the wrong rate while appearing entirely healthy, and the only symptom is a
# different number of lines inside a known window of virtual time.
EXPECTED_REPORTS=5
RENODE_TIMEOUT=300

# Wipe the entire run directory, not just the two logs. Renode's file backend
# does not truncate an existing capture: it renames the old one aside as
# <name>.1. Those rotations accumulate, and any later glob over this directory
# would silently fold a previous run into the current one.
rm -rf "$OUT"
mkdir -p "$OUT" || fault "cannot create $OUT"

# ---------------------------------------------------------------------------
# 0. The toolchain must be the pinned one, or the result means nothing.
# ---------------------------------------------------------------------------
step "Toolchain"
[ -x "$RENODE" ]        || fault "Renode wrapper not found at $RENODE. Run: bash scripts/setup.sh"
[ -d "$ZEPHYR_BASE" ]   || fault "Zephyr workspace not found at $ZEPHYR_BASE. Run: bash scripts/setup.sh"
[ -x "$DETECT_PYTHON" ] || fault "venv python not found at $DETECT_PYTHON. Run: bash scripts/setup.sh"
command -v west >/dev/null || fault "west is not on PATH after sourcing $ENVFILE"

info "checking Renode version"
renode_raw="$("$RENODE" --version 2>&1 </dev/null)" \
    || fault "Renode would not start. This is a runtime problem, not a code problem.
Output was:
$renode_raw"
info "renode reports: $(echo "$renode_raw" | head -1)"
case "$renode_raw" in
    *"$RENODE_VERSION"*) info "Renode $RENODE_VERSION confirmed" ;;
    *) fault "Renode version mismatch.
  expected to contain: $RENODE_VERSION
  reported:            $renode_raw
Peripheral model behaviour differs across Renode releases and the determinism
claims are version specific, so a different version is refused rather than
tolerated." ;;
esac

zmaj="$(sed -n 's/^VERSION_MAJOR *= *//p'  "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zmin="$(sed -n 's/^VERSION_MINOR *= *//p'  "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zpatch="$(sed -n 's/^PATCHLEVEL *= *//p'   "$ZEPHYR_BASE/VERSION" | tr -d ' ')"
zver="$zmaj.$zmin.$zpatch"
[ "$zver" = "$ZEPHYR_VERSION" ] || fault "Zephyr version mismatch.
  expected: $ZEPHYR_VERSION
  found:    $zver
Newer Zephyr changes CAN filter and driver semantics, so this is pinned."
info "zephyr:  $zver"
info "sdk:     $ZEPHYR_SDK_VERSION at $ZEPHYR_SDK_INSTALL_DIR"

# ---------------------------------------------------------------------------
# 1. Build
#
# Zephyr derives the boot banner's build version from `git describe` run inside
# ZEPHYR_BASE (cmake/gen_version_h.cmake), so the banner is a function of the
# Zephyr checkout's git state rather than of anything in this repository. A
# commit in that tree, or a Zephyr unpacked from a tarball where git describe
# fails, changes the banner and therefore the hash for a reason that has nothing
# to do with the firmware. Pinning it removes git from the equation.
#
# This does not weaken the version check: the toolchain step above verifies the
# tag and the VERSION file directly, which is where a wrong Zephyr is caught.
# ---------------------------------------------------------------------------
CMAKE_EXTRA=(-DBUILD_VERSION=zephyr-v"$ZEPHYR_VERSION")
if [ "$MODE" = prove-gate ]; then
    step "Building the NEGATIVE CONTROL (-DDETECT_OMIT_UNDEFINED=g_gc_canary)"
    info "one --undefined flag is deliberately withheld; the symbol gate MUST fail"
    CMAKE_EXTRA+=(-DDETECT_OMIT_UNDEFINED=g_gc_canary)
    # A stale object tree would hide the difference, so this variant always
    # builds from scratch.
    rm -rf "$BUILD"
else
    step "Building systems/powertrain/firmware/bms for nucleo_h743zi"
fi

[ -f "$SYMLIST" ] || fault "injectable_symbols.txt missing at $SYMLIST"

# The build output goes to a file and the exit status checked is west's, not a
# pipeline element's. `west build ... | tee log` would report tee's status, which
# almost always succeeds, and has produced false green results before.
west build -b nucleo_h743zi -d "$BUILD" "$APP" --pristine=auto \
    -- "${CMAKE_EXTRA[@]}" >"$BUILD_LOG" 2>&1
build_rc=$?
if [ $build_rc -ne 0 ]; then
    echo >&2
    tail -40 "$BUILD_LOG" >&2
    fault "firmware build failed (exit $build_rc). Full log: $BUILD_LOG"
fi
info "build ok, log: $BUILD_LOG"
[ -f "$ELF" ] || fault "build reported success but produced no ELF at $ELF"
info "elf:     $ELF"
info "elf sha: $(sha256sum "$ELF" | cut -d' ' -f1)"

# ---------------------------------------------------------------------------
# 2. Symbol gate
# ---------------------------------------------------------------------------
step "Symbol gate: injection targets must exist in the ELF"
"$DETECT_PYTHON" "$ROOT/scripts/check_symbols.py" "$ELF" --from-list "$SYMLIST"
gate_rc=$?

if [ "$MODE" = prove-gate ]; then
    echo
    if [ $gate_rc -eq 0 ]; then
        fail "NEGATIVE CONTROL DID NOT FIRE.

The build withheld -Wl,--undefined=g_gc_canary, so the linker should have
collected an unreferenced global and the gate should have reported it absent.
The gate passed instead, which means it is not actually checking what it claims
to check. A guard that has never been seen to fire is an untested claim, and
this one has now been seen not to fire."
    fi
    echo "PASS: the symbol gate fired as required (exit $gate_rc)."
    echo "      Withholding one --undefined flag caused the linker to discard"
    echo "      g_gc_canary and the gate to refuse the binary."
    echo
    echo "Rebuilding the normal binary so the tree is not left holding the"
    echo "negative control."
    rm -rf "$BUILD"
    west build -b nucleo_h743zi -d "$BUILD" "$APP" --pristine=auto \
        -- -DBUILD_VERSION=zephyr-v"$ZEPHYR_VERSION" >"$BUILD_LOG" 2>&1 \
        || fault "could not restore the normal build; see $BUILD_LOG"
    echo "Restored."
    exit 0
fi

[ $gate_rc -eq 0 ] || fail "the symbol gate rejected the binary (exit $gate_rc). See above."

# ---------------------------------------------------------------------------
# 3. Boot
# ---------------------------------------------------------------------------
RENODE_ELF="$ELF"
RENODE_ARGS=(
    -e "\$platform=@$PLATFORM"
    -e "\$elf=@$RENODE_ELF"
    -e "\$uartLog=@$UART_LOG"
    -e "include @$RESC"
)
if [ "$MODE" = prove-infra-guard ]; then
    # Provoke a failed monitor command that does NOT hang.
    #
    # Loading a platform file that does not exist makes Renode print
    #   There was an error executing command 'machine LoadPlatformDescription ...'
    #   Could not find file '...'
    # and then exit 0. That is exactly the condition the guard exists for, and it
    # is reached in about a second.
    #
    # Two earlier provers were discarded, and why is worth recording:
    #   - a nonexistent ELF also errors, but leaves a machine with no firmware,
    #     and the emulation then runs until the 300 s wall-clock timeout. It
    #     exercised the timeout path, not the marker path.
    #   - an unrecognised command exits 0 but emits none of the markers, so it
    #     proved nothing.
    # The .resc is deliberately not included here: this control is about whether
    # a failed command is detected, not about running the firmware.
    RENODE_ARGS=(
        -e "mach create \"probe\""
        -e "machine LoadPlatformDescription @$OUT/no-such-platform.repl"
        -e "quit"
    )
    step "Booting the NEGATIVE CONTROL for the engine-fault guard"
    info "loading a platform file that does not exist"
    info "Renode is expected to report the command as failed and still exit 0"
    info "the run MUST be classified as an engine fault, not a firmware verdict"
else
    step "Booting in Renode"
fi
info "platform: $PLATFORM"
info "script:   $RESC"
info "timeout:  ${RENODE_TIMEOUT}s of wall clock, guarding against a hang"

# The timeout distinguishes "the emulator never finished" from "the firmware
# failed". They are different results and must not share an exit code.
# stdin is closed deliberately. Renode's monitor is interactive: if a scripted
# command fails and stdin is still attached, it drops to a prompt and waits
# forever, which surfaces as a wall-clock timeout rather than as the command
# error it actually is. With stdin closed it reports the error and exits.
timeout "$RENODE_TIMEOUT" "$RENODE" --console --disable-xwt --plain \
    "${RENODE_ARGS[@]}" \
    >"$RENODE_LOG" 2>&1 </dev/null
renode_rc=$?

if [ "$MODE" = prove-infra-guard ]; then
    info "renode exit status was $renode_rc"
    [ $renode_rc -eq 0 ] || fault "the premise of this guard does not hold on this
Renode build: a failed monitor command produced exit $renode_rc rather than 0.
That is better behaviour than assumed, but the guard was written for exit 0 and
this needs re-checking before it is trusted."
fi

if [ $renode_rc -eq 124 ]; then
    fault "Renode did not finish within ${RENODE_TIMEOUT}s and was killed.
The emulation hung. This is an engine fault, not a firmware verdict, so it is
reported as such rather than as a failed test. See $RENODE_LOG"
fi
if [ $renode_rc -ne 0 ]; then
    echo >&2; tail -30 "$RENODE_LOG" >&2
    fault "Renode exited $renode_rc. See $RENODE_LOG"
fi

# ---------------------------------------------------------------------------
# 3a. Renode's exit status is not sufficient.
#
# Renode 1.16.1 returns 0 even when a monitor command fails outright. A failed
# `sysbus LoadELF` prints
#
#     There was an error executing command 'sysbus LoadELF @...'
#
# and the process still exits 0. Trusting the exit status alone would let a run
# in which the firmware was never loaded reach the console assertions, where it
# would present as an empty UART and be reported as a FIRMWARE failure. That is
# precisely the conflation of engine fault with test verdict this project must
# not make, so the log is scanned for the markers Renode actually emits.
# ---------------------------------------------------------------------------
renode_errors="$(grep -nE "There was an error executing command|Parameters did not match the signature|Could not find file|Unhandled exception" "$RENODE_LOG")"
if [ -n "$renode_errors" ]; then
    if [ "$MODE" = prove-infra-guard ]; then
        echo
        echo "PASS: the engine-fault guard fired as required."
        echo "      Renode exited 0 while reporting a failed monitor command, and"
        echo "      the guard classified it as an engine fault rather than letting"
        echo "      it reach the console assertions and be blamed on the firmware."
        echo
        echo "      markers observed:"
        echo "$renode_errors" | sed 's/^/        /'
        exit 0
    fi
    echo >&2
    echo "$renode_errors" >&2
    fault "Renode reported a failed monitor command but still exited 0.

Renode 1.16.1 returns 0 even when a command inside the script fails, so the exit
status alone cannot be trusted. The run above did not do what the script asked,
which means no verdict was reached. This is an engine fault, not a statement
about the firmware. Full log: $RENODE_LOG"
fi

if [ "$MODE" = prove-infra-guard ]; then
    fail "NEGATIVE CONTROL DID NOT FIRE.

Renode was pointed at an ELF that does not exist, so it should have reported a
failed monitor command and the guard should have classified the run as an engine
fault. Neither happened, which means the guard is not actually checking what it
claims to check. Full log: $RENODE_LOG"
fi

info "renode exited 0 with no command errors, log: $RENODE_LOG"

# ---------------------------------------------------------------------------
# 4. Console assertions
# ---------------------------------------------------------------------------
step "Checking the console output"

if [ ! -s "$UART_LOG" ]; then
    fail "the emulated UART produced no output at all.

Nothing reached usart3. Work through, in this order:
  1. Is usart3 the instance Zephyr's nucleo_h743zi board selects for console?
     (chosen { zephyr,console } in the board .dts)
  2. Do flashBank1 (0x08000000) and axiSram (0x24000000) in the platform match
     what the linker script expects?
  3. Did the CPU start at all? Check whether the PC advanced in $RENODE_LOG"
fi

echo "  console:"
sed 's/^/    | /' "$UART_LOG"
echo

grep -qF "$BANNER" "$UART_LOG" || fail "the firmware produced UART output but never printed \"$BANNER\".

The console works, so this is the firmware, not the platform. Check that main()
is reached and that CONFIG_PRINTK and CONFIG_UART_CONSOLE are set in prj.conf."
info "banner present: \"$BANNER\""

# Clock check. grep -c is used without a pipe so the count is grep's own output
# and the status checked is grep's.
reports="$(grep -c '^bms tick=' "$UART_LOG")" || reports=0
info "periodic reports: $reports (expected $EXPECTED_REPORTS in 5 s of virtual time)"
if [ "$reports" -ne "$EXPECTED_REPORTS" ]; then
    fail "wrong number of periodic reports: got $reports, expected $EXPECTED_REPORTS.

The firmware reports once per second and the emulation ran for exactly five
seconds of virtual time, so any other count means the firmware is not running at
the rate it thinks it is. The usual cause is a CPU clock frequency in the
platform description that disagrees with the frequency Zephyr's board definition
assumes. Firmware in that state looks entirely healthy and every latency it
produces is wrong by a constant factor, which is why this is checked here rather
than discovered in Phase 1.

A second cause, seen in Phase 0: the CAN controller not being connected to a
medium, which blocks can_send forever and stops the loop before its first print.
Check $RENODE_LOG for:
    fdcan1: FrameSent is not initialized. Is the controller connected to medium?"
fi

# ---------------------------------------------------------------------------
# 5. The hash
# ---------------------------------------------------------------------------
step "Console hash"
UART_SHA="$(sha256sum "$UART_LOG" | cut -d' ' -f1)"
echo "    sha256(console) = $UART_SHA"
echo "    bytes           = $(wc -c <"$UART_LOG")"

echo
echo "PASS: real firmware booted in Renode, printed \"$BANNER\", produced"
echo "      $reports periodic reports in 5 s of virtual time, and hashed to"
echo "      $UART_SHA"
exit 0
