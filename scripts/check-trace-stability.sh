#!/usr/bin/env bash
#
# TASK 1.14a -- the decisive check, and the cheapest one.
#
#     t1 = capture(scenario, golden)
#     t2 = capture(scenario, golden)     same binary, same conditions
#     compare(t1, t2)  must be IDENTICAL
#
# If traces are stable, the residual transmit-order jitter is cosmetic: it moves
# the observer's ordering, not the guest's execution. Rung 4's divergence
# classifier can then attribute a difference to the defect, which is the whole
# basis of the central contribution.
#
# If traces are UNSTABLE, the classifier cannot tell defect-induced divergence
# from scheduling-induced divergence, and rung 4 is not implementable in this
# configuration whatever it costs.
#
# At least one scenario must involve BOTH machines exchanging frames. A
# single-machine scenario passes trivially and tells you nothing, because the
# jitter being tested for is cross-machine.
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

OUT="${OUT:-$HOME/detect-trace-stability}"
# peer-heartbeat-present and overtemp-boundary both involve the peer transmitting
# while the DUT runs, which is where cross-machine jitter lives.
SCENARIOS="${SCENARIOS:-overtemp-boundary peer-heartbeat-present overtemp-well-past injection-lands silence-stops-traffic}"
TIMEOUT="${TIMEOUT:-900}"
# EXEC selects the execution mode under test. Trace stability is the criterion
# that decides whether serial execution is optional or mandatory, so it must be
# measurable in both modes.
EXEC="${EXEC:-}"
COMMON="$EXEC --network $ROOT/systems/powertrain/network.yml --boards $ROOT/harness/boards.yml
        --catalog $ROOT/systems/powertrain/catalog.yml --symbols $ROOT/systems/powertrain/symbols.json
        --root $ROOT --timeout $TIMEOUT"

rm -rf "$OUT"; mkdir -p "$OUT"
echo "=== Trace stability: two captures of the SAME golden binary ==="
echo "    scenarios: $SCENARIOS"
echo "    execution: ${EXEC:-parallel (Renode default)}"
echo

fail=0
for s in $SCENARIOS; do
    for pass in a b; do
        "$DETECT_PYTHON" -m harness.run "$ROOT/scenarios/$s.yml" $COMMON \
            --out "$OUT/$s-$pass" >"$OUT/$s-$pass.log" 2>&1
        echo "$?" >"$OUT/$s-$pass.rc"
    done
    rc_a="$(cat "$OUT/$s-a.rc")"; rc_b="$(cat "$OUT/$s-b.rc")"

    verdict="$("$DETECT_PYTHON" - "$OUT/$s-a/trace.log" "$OUT/$s-b/trace.log" <<'PY'
import sys
from harness import compare
try:
    a = compare.read(sys.argv[1]); b = compare.read(sys.argv[2])
except Exception as exc:
    print("NO TRACE (%s)" % exc); raise SystemExit(0)
print(compare.compare_traces(a, b))
PY
)"
    # The event log is compared too, at window 0, so the difference between
    # "execution differs" and "only the observer's ordering differs" is visible.
    logw="$("$DETECT_PYTHON" - "$OUT/$s-a/events.log" "$OUT/$s-b/events.log" <<'PY'
import sys
from harness import compare
try:
    a = compare.read(sys.argv[1]); b = compare.read(sys.argv[2])
except Exception:
    print("-"); raise SystemExit(0)
w = compare.smallest_equal_window(a, b)
print("exact" if w == 0 else ("window=%sus" % w if w is not None else "NEVER"))
PY
)"
    printf "  %-26s exits=%s/%s  trace=%-58s events=%s\n" \
        "$s" "$rc_a" "$rc_b" "$verdict" "$logw"
    case "$verdict" in
        IDENTICAL) ;;
        *) fail=1 ;;
    esac
done

echo
if [ $fail -eq 0 ]; then
    echo "PASS (criterion 10): every trace IDENTICAL across repeated runs."
    echo "  Execution inside the guest is reproducible. Any residual difference"
    echo "  in the event log is the observer's ordering, not the firmware's"
    echo "  behaviour, so rung 4 can attribute divergence to the defect."
    exit 0
fi
echo "FAIL (criterion 10): a trace differed between two runs of the same binary."
echo "  The divergence classifier cannot separate defect-induced divergence from"
echo "  scheduling-induced divergence. Serial execution is mandatory; see"
echo "  PHASE1-AMENDMENT.md section 5, branch 3."
exit 1
