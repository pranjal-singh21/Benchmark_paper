#!/usr/bin/env bash
#
# Detached job runner with a heartbeat, for work that outlives the shell that
# started it.
#
# Phase 4 is thousands of runs over hours. A job that dies silently costs a
# night, so this exists to make "is it still alive?" answerable and "it died"
# recoverable.
#
# THREE THINGS IT GETS RIGHT, each because the naive version failed here:
#
# 1. LIVENESS IS MEASURED BY HEARTBEAT AGE, NEVER BY PROCESS NAME.
#    `pgrep -f check-determinism.sh` matches the wrapper shell whose own command
#    line contains that string, so it reports a dead job as alive. It did. A
#    heartbeat file with a timestamp cannot lie in that direction: if the job is
#    gone, the file stops being touched.
#
# 2. IT SURVIVES THE LAUNCHING SHELL. setsid --fork puts the job in its own
#    session so nothing follows the parent to the grave.
#
# 3. IT RECORDS WHY IT STOPPED. A missing status file plus a stale heartbeat is
#    "killed", which is a different thing from "exited non-zero", and on this
#    host the usual cause is the out-of-memory killer rather than anything in
#    the job.
#
# Usage:
#   scripts/job.sh start <name> <command...>
#   scripts/job.sh status <name>
#   scripts/job.sh wait <name> [timeout_s]
#   scripts/job.sh log <name>
#
set -uo pipefail

JOBS="${DETECT_JOBS:-$HOME/detect-jobs}"
mkdir -p "$JOBS"

HEARTBEAT_PERIOD=10
STALE_AFTER=45          # > 4 missed beats before a job is called dead

usage() { sed -n '2,40p' "$0"; exit 2; }

cmd_start() {
    local name="$1"; shift
    [ $# -ge 1 ] || usage
    rm -f "$JOBS/$name.status" "$JOBS/$name.heartbeat" "$JOBS/$name.log"
    : >"$JOBS/$name.log"

    setsid --fork bash -c '
        name="$1"; jobs_dir="$2"; shift 2
        # Heartbeat in the background; killed when the job finishes.
        (
            while true; do
                date +%s > "$jobs_dir/$name.heartbeat"
                sleep '"$HEARTBEAT_PERIOD"'
            done
        ) &
        hb=$!
        "$@" >>"$jobs_dir/$name.log" 2>&1
        rc=$?
        kill $hb 2>/dev/null
        echo "$rc" > "$jobs_dir/$name.status"
        date +%s > "$jobs_dir/$name.heartbeat"
    ' _ "$name" "$JOBS" "$@" </dev/null >/dev/null 2>&1

    sleep 2
    echo "started '$name'"
    echo "  log:    $JOBS/$name.log"
    echo "  status: scripts/job.sh status $name"
}

cmd_status() {
    local name="$1"
    local status_file="$JOBS/$name.status"
    local hb_file="$JOBS/$name.heartbeat"

    if [ -f "$status_file" ]; then
        local rc; rc="$(cat "$status_file")"
        echo "FINISHED exit=$rc"
        return 0
    fi
    if [ ! -f "$hb_file" ]; then
        echo "UNKNOWN (no heartbeat file; was it ever started?)"
        return 3
    fi
    local age=$(( $(date +%s) - $(cat "$hb_file") ))
    if [ "$age" -le "$STALE_AFTER" ]; then
        echo "RUNNING (heartbeat ${age}s ago)"
        return 1
    fi
    echo "DEAD (heartbeat ${age}s old, no exit status)"
    echo "  The job stopped without recording an exit code, so it was killed"
    echo "  rather than failing. On this host the usual cause is the"
    echo "  out-of-memory killer; check: dmesg | grep -i 'killed process'"
    return 4
}

cmd_wait() {
    local name="$1" timeout="${2:-3600}"
    local waited=0
    while [ "$waited" -lt "$timeout" ]; do
        local out; out="$(cmd_status "$name")"; local rc=$?
        case $rc in
            0) echo "$out"; return 0 ;;
            4|3) echo "$out"; return 1 ;;
        esac
        sleep 5; waited=$((waited + 5))
    done
    echo "TIMEOUT after ${timeout}s"; return 2
}

[ $# -ge 2 ] || usage
action="$1"; shift
case "$action" in
    start)  cmd_start "$@" ;;
    status) cmd_status "$@" ;;
    wait)   cmd_wait "$@" ;;
    log)    tail -n "${2:-40}" "$JOBS/$1.log" ;;
    *)      usage ;;
esac
