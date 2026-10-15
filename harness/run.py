"""Run one scenario: validate, compile, execute, judge, store.

Exit codes are the project's, and they are load bearing::

    0  PASS       the firmware did what the scenario asserted
    1  FAIL       it did not. A real result, with real numbers.
    2  UNUSABLE   the inputs are broken. Nothing ran.
    3  REFUSED    definable, but no execution path exists
    4  DRY RUN    a script was compiled and nothing executed
    5  CRASHED    the engine raised an exception. Nothing was determined.

**Code 5 is not optional.** If an engine exception is reported as exit 1, a crash
is counted as a legitimate firmware failure, and the results table is silently
corrupted with detections that never happened. Every unexpected exception is
caught at the top level and becomes 5.

**The previous answer is cleared before launching.** Otherwise a crashed run
leaves the last run's event log in place, and it is read as this run's result.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from harness import catalog as catalog_mod
from harness import compile as compile_mod
from harness import judge as judge_mod
from harness import network as network_mod
from harness import scenario as scenario_mod
from harness import symbols as symbols_mod
from harness import validate as validate_mod

EXIT_PASS, EXIT_FAIL, EXIT_UNUSABLE, EXIT_REFUSED, EXIT_DRY_RUN, EXIT_CRASHED = 0, 1, 2, 3, 4, 5


def _say(msg):
    print(msg)
    sys.stdout.flush()


def run_once(args) -> int:
    root = Path(os.path.abspath(args.root))
    out = Path(os.path.abspath(args.out))

    _say("--- Preparing %s ---" % out)
    # Clear the previous answer BEFORE launching, so a crash cannot leave a stale
    # result to be attributed to this run.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    _say("--- Loading system files ---")
    scen = scenario_mod.load(args.scenario)
    net = network_mod.load(args.network, args.boards)
    cat = catalog_mod.load(args.catalog)
    syms = symbols_mod.load_symbols_json(args.symbols)
    _say("    scenario: %s (%d steps)" % (scen.id, len(scen.steps)))

    _say("--- Static validation ---")
    problems = validate_mod.validate(scen, network=net, catalog=cat, symbols=syms)
    if problems:
        sys.stderr.write("\n" + validate_mod.format_problems(scen, problems) + "\n")
        return EXIT_UNUSABLE
    _say("    %d step(s), nothing unresolved" % len(scen.steps))

    _say("--- Compiling ---")
    try:
        script = compile_mod.compile_scenario(scen, net, cat, syms, root, out,
                                              serial=args.serial,
                                              quantum_us=args.quantum_us)
    except compile_mod.RefusedError as exc:
        sys.stderr.write("\nREFUSED: %s\n\n" % exc)
        return EXIT_REFUSED
    script_path = out / "scenario.resc"
    script_path.write_text(script, encoding="utf-8")
    (out / "scenario.yml").write_text(scen.text or "", encoding="utf-8")
    _say("    %s" % script_path)

    if args.dry_run:
        _say("--- DRY RUN: nothing executed ---")
        return EXIT_DRY_RUN

    _say("--- Executing in Renode ---")
    renode = str(root / "scripts" / "renode")
    events_path = out / "events.log"
    renode_log = out / "renode.log"
    wall_start = time.time()
    with open(renode_log, "wb") as log:
        proc = subprocess.run(
            ["timeout", str(args.timeout),
             renode, "--console", "--disable-xwt", "--plain",
             "-e", "include @%s" % script_path],
            stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, check=False)
    host_wall_seconds = time.time() - wall_start
    _say("    renode exit=%d, %.1f s of host time" % (proc.returncode, host_wall_seconds))

    if proc.returncode == 124:
        sys.stderr.write("\nERROR: Renode did not finish within %d s and was killed.\n"
                         "The emulation hung. That is an engine fault, not a firmware "
                         "verdict.\n\n" % args.timeout)
        return EXIT_CRASHED

    log_text = renode_log.read_text(errors="replace")
    # Renode returns 0 even when a monitor command fails outright, so its exit
    # status alone cannot be trusted; see STATUS.md deviation 12.
    for marker in ("There was an error executing command",
                   "Parameters did not match the signature",
                   "Could not find file"):
        if marker in log_text:
            sys.stderr.write("\nERROR: Renode reported a failed monitor command but "
                             "still exited 0.\n  marker: %s\n  see %s\n"
                             "No verdict was reached.\n\n" % (marker, renode_log))
            return EXIT_CRASHED
    if "ENGINE ERROR" in log_text:
        sys.stderr.write("\nERROR: the in-emulator toolkit reported an engine error.\n"
                         "  see %s\nNo verdict was reached.\n\n" % renode_log)
        return EXIT_CRASHED

    if not events_path.is_file():
        sys.stderr.write("\nERROR: no event log was produced at %s.\n"
                         "Nothing was observed, so nothing is reported.\n\n" % events_path)
        return EXIT_CRASHED

    _say("--- Verdict ---")
    events = judge_mod.parse_events(events_path.read_text(encoding="utf-8"))
    result = judge_mod.judge(events, uart_dir=out)
    result["host_wall_seconds"] = round(host_wall_seconds, 3)
    print(judge_mod.format_report(result))

    import json
    (out / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return EXIT_PASS if result["verdict"] == judge_mod.PASS else EXIT_FAIL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one scenario end to end.")
    ap.add_argument("scenario")
    ap.add_argument("--network", required=True)
    ap.add_argument("--boards", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--serial", action="store_true")
    ap.add_argument("--quantum-us", type=int, default=0)
    args = ap.parse_args(argv)

    try:
        return run_once(args)
    except (scenario_mod.ScenarioError, catalog_mod.CatalogError) as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return EXIT_UNUSABLE
    except Exception:
        # Exit 5, never 1. A crash is not a firmware verdict, and counting it as
        # one silently corrupts every table built from these results.
        sys.stderr.write("\nENGINE CRASH -- no verdict was determined.\n\n")
        traceback.print_exc()
        return EXIT_CRASHED


if __name__ == "__main__":
    sys.exit(main())
