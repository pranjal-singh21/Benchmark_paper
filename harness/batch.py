"""Run many scenarios in one emulator process.

Roughly two thirds of a run is fixed startup: assembly load, JIT, platform
construction, ELF load. Batching pays that once instead of once per scenario, and
it is what makes serial execution affordable. Measured on this host, a typical
scenario costs about 24 s unbatched and about 9 s batched, against a 20 s target.

**Batching is a correctness requirement before it is a speed one.** If N
scenarios in one process give different answers from N processes, the speed gain
is worthless and actively dangerous, because the fast path would be the one the
sweep uses. `scripts/check-batching.sh` compares the two, using the same
`harness/compare.py` rule everything else uses.

Isolation between scenarios rests on three things:

* machines are removed and recreated, so each scenario starts from freshly
  loaded ELFs with no latched faults and no carried-over symbol values;
* `ct_reset` clears every accumulator in the in-emulator toolkit;
* the time origin moves, because virtual time is global and keeps running when
  machines are removed. Without that, every scenario after the first would carry
  its predecessors' elapsed time.

A crash in one scenario must not corrupt the others. The batch is executed as one
process, so a hard emulator crash ends the batch; what this guarantees is that a
scenario that FAILS, or whose assertions are unresolvable, leaves the following
scenarios' artefacts intact and correctly attributed. Any scenario whose event
log is missing after the batch is reported as exit 5, never as a firmware
verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import List

from harness import catalog as catalog_mod
from harness import compile as compile_mod
from harness import judge as judge_mod
from harness import network as network_mod
from harness import scenario as scenario_mod
from harness import symbols as symbols_mod
from harness import validate as validate_mod

EXIT_PASS, EXIT_FAIL, EXIT_UNUSABLE, EXIT_REFUSED, EXIT_DRY_RUN, EXIT_CRASHED = 0, 1, 2, 3, 4, 5

RENODE_ERROR_MARKERS = (
    "There was an error executing command",
    "Parameters did not match the signature",
    "Could not find file",
)


def run_batch(args) -> int:
    root = Path(os.path.abspath(args.root))
    out = Path(os.path.abspath(args.out))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print("--- Loading system files ---")
    net = network_mod.load(args.network, args.boards)
    cat = catalog_mod.load(args.catalog)
    syms = symbols_mod.load_symbols_json(args.symbols)

    items = []
    for path in args.scenarios:
        scen = scenario_mod.load(path)
        problems = validate_mod.validate(scen, network=net, catalog=cat, symbols=syms)
        if problems:
            sys.stderr.write("\n" + validate_mod.format_problems(scen, problems) + "\n")
            return EXIT_UNUSABLE
        sub = out / scen.id
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "scenario.yml").write_text(scen.text or "", encoding="utf-8")
        items.append((scen, sub))
    print("    %d scenario(s): %s" % (len(items), ", ".join(s.id for s, _ in items)))

    print("--- Compiling one script for the whole batch ---")
    try:
        script = compile_mod.compile_batch(items, net, cat, syms, root, out,
                                           serial=args.serial,
                                           quantum_us=args.quantum_us)
    except compile_mod.RefusedError as exc:
        sys.stderr.write("\nREFUSED: %s\n\n" % exc)
        return EXIT_REFUSED
    script_path = out / "batch.resc"
    script_path.write_text(script, encoding="utf-8")
    print("    %s" % script_path)

    if args.dry_run:
        print(script)
        return EXIT_DRY_RUN

    print("--- Executing ---")
    renode_log = out / "renode.log"
    wall_start = time.time()
    with open(renode_log, "wb") as log:
        proc = subprocess.run(
            ["timeout", str(args.timeout),
             str(root / "scripts" / "renode"),
             "--console", "--disable-xwt", "--plain",
             "-e", "include @%s" % script_path],
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, check=False)
    host_wall_seconds = time.time() - wall_start
    per_scenario = host_wall_seconds / max(1, len(items))
    print("    renode exit=%d, %.1f s total, %.1f s per scenario"
          % (proc.returncode, host_wall_seconds, per_scenario))

    if proc.returncode == 124:
        sys.stderr.write("\nERROR: the batch did not finish within %d s and was "
                         "killed. That is an engine fault, not a verdict.\n\n" % args.timeout)
        return EXIT_CRASHED

    log_text = renode_log.read_text(errors="replace")
    for marker in RENODE_ERROR_MARKERS:
        if marker in log_text:
            sys.stderr.write("\nERROR: Renode reported a failed monitor command "
                             "(%r). No verdicts are reported from this batch.\n\n" % marker)
            return EXIT_CRASHED

    print("--- Verdicts ---")
    worst = EXIT_PASS
    summary = {"per_scenario_seconds": round(per_scenario, 3),
               "host_wall_seconds": round(host_wall_seconds, 3),
               "scenarios": {}}
    for scen, sub in items:
        events_path = sub / "events.log"
        if not events_path.is_file():
            # Missing artefacts are an engine fault for THAT scenario, never a
            # firmware verdict, and never allowed to be read as a pass.
            print("  [%-4s] %s" % ("ENG", scen.id))
            sys.stderr.write("    no event log at %s\n" % events_path)
            summary["scenarios"][scen.id] = {"verdict": "CRASHED"}
            worst = max(worst, EXIT_CRASHED)
            continue
        events = judge_mod.parse_events(events_path.read_text(encoding="utf-8"))
        result = judge_mod.judge(events, uart_dir=sub)
        (sub / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        mark = "ok" if result["verdict"] == judge_mod.PASS else "FAIL"
        print("  [%-4s] %-28s %d/%d assertion(s)"
              % (mark, scen.id, result["counts"]["passed"], result["counts"]["total"]))
        summary["scenarios"][scen.id] = {"verdict": result["verdict"],
                                         "counts": result["counts"]}
        if result["verdict"] != judge_mod.PASS:
            worst = max(worst, EXIT_FAIL)

    (out / "batch-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print("    per-scenario cost: %.1f s" % per_scenario)
    return worst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run many scenarios in one emulator process.")
    ap.add_argument("scenarios", nargs="+")
    ap.add_argument("--network", required=True)
    ap.add_argument("--boards", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--quantum-us", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        return run_batch(args)
    except (scenario_mod.ScenarioError, catalog_mod.CatalogError) as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return EXIT_UNUSABLE
    except Exception:
        sys.stderr.write("\nENGINE CRASH -- no verdicts were determined.\n\n")
        traceback.print_exc()
        return EXIT_CRASHED


if __name__ == "__main__":
    sys.exit(main())
