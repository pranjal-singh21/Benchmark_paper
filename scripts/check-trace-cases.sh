#!/usr/bin/env bash
#
# TASK 1.14b -- the trace differential, all THREE cases.
#
# Two of them are easy and one has to be built on purpose. If the classifier can
# only ever produce Case A, half of rung 4 does not exist, and that is the kind
# of gap that is discovered in Phase 3 with the whole experiment waiting on it.
#
#   1. boundary, golden vs canary      -> verdicts differ      -> DETECTED
#   2. well past, golden vs canary     -> traces IDENTICAL     -> CASE A
#   3. constructed, golden vs canary   -> traces DIFFER,
#                                         verdicts identical   -> CASE B
#
# Case 3 is the constructed one. At exactly the limit the canary raises a fault
# and the golden build does not, so execution genuinely diverges; the assertion
# then checks something true in BOTH builds, so the verdict is identical. That is
# an oracle failure: the stimulus was right and nothing was watching.
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$ROOT" in
  *" "*)
    DETECT_LINK="${DETECT_LINK:-$HOME/detect-master}"
    if [ "$(readlink -f "$DETECT_LINK" 2>/dev/null)" != "$(readlink -f "$ROOT")" ]; then
        rm -f "$DETECT_LINK"; ln -s "$ROOT" "$DETECT_LINK" || exit 2
    fi
    export DETECT_LINK
    exec bash "$DETECT_LINK/scripts/$(basename "${BASH_SOURCE[0]}")" "$@"
    ;;
esac
cd "$ROOT"
. "$ROOT/.detect-env"

OUT="${OUT:-$HOME/detect-trace-cases}"
GOLD="$ROOT/systems/powertrain/network.yml"
CANARY="$ROOT/harness/tests/fixtures/canary/network.yml"
COMMON="--boards $ROOT/harness/boards.yml --catalog $ROOT/systems/powertrain/catalog.yml
        --symbols $ROOT/systems/powertrain/symbols.json --root $ROOT --serial --timeout 1800"

rm -rf "$OUT"; mkdir -p "$OUT"

run() {  # <scenario> <network> <tag>
    "$DETECT_PYTHON" -m harness.run "$ROOT/scenarios/$1.yml" --network "$2" $COMMON \
        --out "$OUT/$3" >"$OUT/$3.log" 2>&1
    echo $?
}

classify() {  # <goldtag> <deftag>
    "$DETECT_PYTHON" - "$OUT/$1" "$OUT/$2" <<'PY'
import json, sys
from pathlib import Path
from harness import compare, divergence
g, d = Path(sys.argv[1]), Path(sys.argv[2])
gv = json.loads((g / "summary.json").read_text())["verdict"]
dv = json.loads((d / "summary.json").read_text())["verdict"]
c = divergence.classify(compare.read(g / "events.log"), compare.read(d / "events.log"), gv, dv)
print("%s|%s|%s|%s" % (c.case, gv, dv,
                       "IDENTICAL" if c.trace_result == compare.IDENTICAL else "DIVERGED"))
PY
}

echo "=== Trace differential: the three cases rung 4 depends on ==="
echo
printf "  %-22s %-12s %-9s %-9s %-11s %s\n" case expected golden defective traces got

fail=0
check() {  # <label> <scenario> <expected-case>
    local label="$1" scen="$2" expected="$3"
    run "$scen" "$GOLD"   "$label-golden"    >/dev/null
    run "$scen" "$CANARY" "$label-canary"    >/dev/null
    local out; out="$(classify "$label-golden" "$label-canary")"
    local got="${out%%|*}"; local rest="${out#*|}"
    local gv="${rest%%|*}"; rest="${rest#*|}"
    local dv="${rest%%|*}"; local tr="${rest##*|}"
    local mark=""
    [ "$got" = "$expected" ] || { mark="  <-- WRONG"; fail=1; }
    printf "  %-22s %-12s %-9s %-9s %-11s %s%s\n" \
        "$label" "$expected" "$gv" "$dv" "$tr" "$got" "$mark"
}

check "boundary"   overtemp-boundary   DETECTED
check "well-past"  overtemp-well-past  CASE_A
check "oracle"     case-b-oracle       CASE_B

echo
echo "=== the message a generator would receive for each ==="
for label in boundary well-past oracle; do
    echo
    echo "  --- $label ---"
    "$DETECT_PYTHON" - "$OUT/$label-golden" "$OUT/$label-canary" <<'PY'
import json, sys
from pathlib import Path
from harness import compare, divergence
g, d = Path(sys.argv[1]), Path(sys.argv[2])
gv = json.loads((g / "summary.json").read_text())["verdict"]
dv = json.loads((d / "summary.json").read_text())["verdict"]
c = divergence.classify(compare.read(g / "events.log"), compare.read(d / "events.log"), gv, dv)
for line in divergence.message(c).splitlines():
    print("      " + line)
PY
done

echo
if [ $fail -eq 0 ]; then
    echo "PASS: all three cases classified correctly, including CASE B."
    exit 0
fi
echo "FAIL: the classifier did not produce the expected case."
echo "      If it can only ever produce Case A, half of rung 4 does not exist."
exit 1
