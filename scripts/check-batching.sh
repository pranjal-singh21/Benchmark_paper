#!/usr/bin/env bash
#
# TASK 1.13 / exit criterion 4.
#
# Batching is a CORRECTNESS requirement before it is a speed one. If N scenarios
# in one process answer differently from N processes, the speed gain is worse
# than useless, because the fast path is the one the sweep would use.
#
# Comparison uses harness/compare.py, the same rule the determinism check and the
# trace differ use. One rule, not three.
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

OUT="${OUT:-$HOME/detect-batching}"
EXEC="${EXEC:---serial}"
WINDOW="${WINDOW:-0}"
SCENARIOS="${SCENARIOS:-overtemp-boundary peer-heartbeat-present overtemp-well-past injection-lands silence-stops-traffic}"
COMMON="--network $ROOT/systems/powertrain/network.yml --boards $ROOT/harness/boards.yml
        --catalog $ROOT/systems/powertrain/catalog.yml --symbols $ROOT/systems/powertrain/symbols.json
        --root $ROOT"

rm -rf "$OUT"; mkdir -p "$OUT"
echo "=== Batching: $(echo $SCENARIOS | wc -w) scenarios, execution ${EXEC:-parallel}, window ${WINDOW}us ==="

echo
echo "--- one process per scenario ---"
u_start=$SECONDS
for s in $SCENARIOS; do
    "$DETECT_PYTHON" -m harness.run "$ROOT/scenarios/$s.yml" $COMMON $EXEC --timeout 1800 \
        --out "$OUT/unbatched/$s" >"$OUT/unbatched-$s.log" 2>&1
    printf "    %-28s exit=%s\n" "$s" "$?"
done
u_wall=$((SECONDS - u_start))
n=$(echo $SCENARIOS | wc -w)
echo "    total ${u_wall}s, $((u_wall / n))s per scenario"

echo
echo "--- one process for all of them ---"
paths=""
for s in $SCENARIOS; do paths="$paths $ROOT/scenarios/$s.yml"; done
b_start=$SECONDS
"$DETECT_PYTHON" -m harness.batch $paths $COMMON $EXEC --timeout 3600 \
    --out "$OUT/batched" >"$OUT/batched.log" 2>&1
b_rc=$?
b_wall=$((SECONDS - b_start))
grep -E "^  \[" "$OUT/batched.log" | sed 's/^/    /'
echo "    exit=$b_rc, total ${b_wall}s, $((b_wall / n))s per scenario"

echo
echo "--- comparison ---"
fail=0
printf "    %-28s %-12s %s\n" scenario "event log" "latencies"
for s in $SCENARIOS; do
    verdict="$("$DETECT_PYTHON" - "$OUT/unbatched/$s/events.log" "$OUT/batched/$s/events.log" "$WINDOW" <<'PY'
import sys
from harness import compare
try:
    a = compare.read(sys.argv[1]); b = compare.read(sys.argv[2])
except Exception as exc:
    print("MISSING"); raise SystemExit(0)
ok, why = compare.logs_equal(a, b, int(sys.argv[3]))
print("identical" if ok else "DIFFER")
if not ok:
    sys.stderr.write((why or "") + "\n")
PY
2>"$OUT/$s.diff")"
    lat="$("$DETECT_PYTHON" - "$OUT/unbatched/$s/summary.json" "$OUT/batched/$s/summary.json" <<'PY'
import json, sys
from harness import compare
try:
    a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
except Exception:
    print("-"); raise SystemExit(0)
d = [x["delta_us"] for x in compare.latency_delta(a, b) if x["delta_us"] is not None]
notes = [x["note"] for x in compare.latency_delta(a, b) if x["note"] and "outcome differs" in x["note"]]
print("VERDICT DIFFERS" if notes else ("max %dus" % max(d) if d else "none quoted"))
PY
)"
    printf "    %-28s %-12s %s\n" "$s" "$verdict" "$lat"
    [ "$verdict" = "identical" ] || { fail=1; head -4 "$OUT/$s.diff" | sed 's/^/        /'; }
done

per=$((b_wall / n))
echo
echo "--- cost ---"
echo "    unbatched $((u_wall / n))s per scenario"
echo "    batched   ${per}s per scenario"
echo "    target    under 20s"

echo
if [ $fail -eq 0 ] && [ "$per" -lt 20 ]; then
    echo "PASS (criterion 4): batched results identical to unbatched, ${per}s per scenario."
    exit 0
fi
[ $fail -ne 0 ] && echo "FAIL: batched and unbatched disagree. Find the state leak;"
[ $fail -ne 0 ] && echo "      do not keep a speed gain that changes answers."
[ "$per" -ge 20 ] && echo "FAIL: ${per}s per scenario, target is under 20s."
exit 1
