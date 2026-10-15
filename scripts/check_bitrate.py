#!/usr/bin/env python3
"""Assert the firmware's effective CAN bit rate matches the declared one.

Why this exists
---------------
The board devicetree for this target sets ``bus-speed = <125000>`` itself. The
application overlay overrides it to the rate the system actually uses. If that
overlay is dropped, **nothing reports an error**: Zephyr does not warn about an
overlay that is not there, and the board's own default silently stands. Measured
on this project, with the overlay removed and a pristine build, the firmware
compiled cleanly at 125000 bit/s and this check was the only thing that noticed.

A *misspelled* property is a different story and is NOT silent: at Zephyr 3.5.0
an undeclared property on a node that has a binding is a hard devicetree error,
verified by deliberately writing ``bus_speed``. The silent case is the missing
overlay, and that is what this guards.

The firmware then runs at a quarter of the intended bit rate. Every frame is
still sent and received, every scenario still passes, and every timing
measurement in the project is wrong by a factor of four with nothing to show for
it. It is precisely the class of failure this project exists to measure, so the
engine must not be vulnerable to it.

The check therefore reads the **generated** devicetree, not the overlay. The
overlay states an intention; the generated devicetree is what was actually
compiled into the binary.

How the value is found
----------------------
``build/zephyr/include/generated/devicetree_generated.h`` contains a define
tying the chosen CAN bus to a node token::

    #define DT_CHOSEN_zephyr_canbus DT_N_S_soc_S_can_4000a000

and a define for that node's property::

    #define DT_N_S_soc_S_can_4000a000_P_bus_speed 500000

Resolving the chosen node first means this never hardcodes a peripheral address
or instance, so it works unchanged on a system whose CAN controller is a
different instance.

Exit status
-----------
    0   the effective rate matches the declared rate
    1   mismatch, or the property is absent; both bounds are printed
    2   the inputs were unusable and nothing was checked
"""

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: pyyaml is not importable.\n"
        "       Run this with the project venv interpreter, not system python.\n"
    )
    raise SystemExit(2)


CHOSEN_RE = re.compile(r"^#define\s+DT_CHOSEN_zephyr_canbus\s+(\S+)\s*$", re.M)


def die(code, message):
    sys.stderr.write("\nERROR: %s\n\n" % message)
    raise SystemExit(code)


def load_yaml(path, what):
    p = Path(path)
    if not p.is_file():
        die(2, "%s not found: %s" % (what, path))
    try:
        return yaml.safe_load(p.read_text())
    except Exception as exc:
        die(2, "could not parse %s (%s): %s" % (what, path, exc))


def declared_bitrate(network, boards, node_id):
    """The rate this node's bus is declared to run at, from network.yml."""
    nodes = {n["id"]: n for n in (network.get("nodes") or [])}
    if node_id not in nodes:
        die(2, "node '%s' is not in network.yml. Nodes present: %s"
               % (node_id, ", ".join(sorted(nodes)) or "(none)"))
    node = nodes[node_id]

    bus_ids = node.get("buses") or []
    if len(bus_ids) != 1:
        die(2, "node '%s' declares %d buses; this check handles exactly one."
               % (node_id, len(bus_ids)))
    bus_id = bus_ids[0]

    buses = {b["id"]: b for b in (network.get("buses") or [])}
    if bus_id not in buses:
        die(2, "node '%s' is on bus '%s', which network.yml does not define. "
               "Buses defined: %s"
               % (node_id, bus_id, ", ".join(sorted(buses)) or "(none)"))
    bus = buses[bus_id]
    if "bitrate" not in bus:
        die(2, "bus '%s' in network.yml declares no bitrate." % bus_id)
    declared = int(bus["bitrate"])

    # Cross-check boards.yml, which states the same number from the board's side.
    # Two declarations that disagree is itself a defect, and a silent one.
    board_name = node.get("board")
    if board_name and board_name in boards:
        board = boards[board_name] or {}
        if "can_bitrate" in board:
            board_rate = int(board["can_bitrate"])
            if board_rate != declared:
                die(2,
                    "network.yml and boards.yml disagree about the bit rate.\n"
                    "  network.yml bus '%s': %d\n"
                    "  boards.yml  '%s':     %d\n"
                    "Neither can be assumed correct, so nothing is checked."
                    % (bus_id, declared, board_name, board_rate))
    return declared, bus_id


def effective_bitrate(build_dir):
    """The rate actually compiled into the binary, from the generated devicetree."""
    header = Path(build_dir) / "zephyr" / "include" / "generated" / "devicetree_generated.h"
    if not header.is_file():
        die(2, "generated devicetree header not found:\n  %s\n"
               "Has the firmware been built?" % header)

    text = header.read_text()

    m = CHOSEN_RE.search(text)
    if not m:
        die(1,
            "the devicetree declares no chosen CAN bus.\n"
            "  looked for: #define DT_CHOSEN_zephyr_canbus ...\n"
            "  in:         %s\n"
            "Without a chosen canbus the firmware has no CAN controller to use, "
            "and the bit rate cannot be established." % header)
    token = m.group(1)

    prop = re.compile(r"^#define\s+%s_P_bus_speed\s+(\d+)\s*$" % re.escape(token), re.M)
    pm = prop.search(text)
    if not pm:
        die(1,
            "the chosen CAN node has NO bus-speed property.\n"
            "  chosen node token: %s\n"
            "  looked for:        #define %s_P_bus_speed ...\n"
            "  in:                %s\n\n"
            "The property is absent from the compiled devicetree, so the bit rate "
            "the firmware will use cannot be established and is not assumed."
            % (token, token, header))
    return int(pm.group(1)), token


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("build_dir", help="the application's build directory")
    ap.add_argument("node_id", help="the node id in network.yml, e.g. the DUT")
    ap.add_argument("--network", required=True, help="path to network.yml")
    ap.add_argument("--boards", required=True, help="path to harness/boards.yml")
    args = ap.parse_args(argv)

    network = load_yaml(args.network, "network.yml")
    boards = load_yaml(args.boards, "boards.yml") or {}

    declared, bus_id = declared_bitrate(network, boards, args.node_id)
    effective, token = effective_bitrate(args.build_dir)

    print("Bit-rate assertion: %s" % args.node_id)
    print("  declared in network.yml (bus '%s'): %d bit/s" % (bus_id, declared))
    print("  compiled into the binary:           %d bit/s" % effective)
    print("  chosen CAN node:                    %s" % token)

    if declared != effective:
        hi, lo = max(declared, effective), min(declared, effective)
        factor = float(hi) / float(lo)
        die(1,
            "CAN bit rate mismatch.\n"
            "  declared:  %d bit/s   (network.yml, bus '%s')\n"
            "  effective: %d bit/s   (generated devicetree)\n\n"
            "The binary runs at the effective rate. Every timing measurement "
            "taken from it is out by a factor of %.4g, and nothing else reports "
            "a problem.\n\n"
            "The cause is almost always the application overlay, and the failure "
            "is SILENT: if the overlay file is absent, Zephyr does not warn, it "
            "simply leaves the board's own default in place. Check that\n"
            "  <app>/boards/<board>.overlay\n"
            "exists and sets `bus-speed`.\n\n"
            "A MISSPELLED property is not the cause here: at this Zephyr version "
            "an undeclared property on a node that has a binding is a hard "
            "devicetree error, so the build would have failed rather than "
            "reaching this check."
            % (declared, bus_id, effective, factor))

    print("\nPASS: effective bit rate matches the declared rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
