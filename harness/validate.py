"""Static validation of a scenario against a system's own files.

No emulator, no firmware execution, no cost. Everything here is answerable from
``network.yml``, ``catalog.yml``, ``symbols.json`` and the scenario itself.

**The quality of these messages is the quality of rung 2.** In Phase 3 a language
model receives them and has to act on them, so every rejection names the thing
that was wrong and, where it can, the set of things that would have been right.
"Invalid symbol" is useless; naming the symbol and listing the available ones is
a message something can act on.

Six checks, per the phase specification:

===============================  ==================================================
check                            the message names
===============================  ==================================================
symbol exists and is writable    the symbol, and the available ones
message identifier exists        the identifier
signal exists on that message    the signal and the message
enum value is legal              the value and the legal set
value inside declared range      the value and both bounds
deadline positive and sane       the deadline
===============================  ==================================================
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, List, Optional

from harness import catalog as catalog_mod
from harness import scenario as scenario_mod
from harness.scenario import DURATION_KEYS, Scenario, Step


class ValidationError(Exception):
    """One or more scenario steps are not executable against this system."""


# A deadline longer than this is almost certainly a mistake, and a scenario that
# waits an hour of virtual time silently burns a sweep's budget.
MAX_DEADLINE_MS = 600_000


class Problem:
    __slots__ = ("where", "message")

    def __init__(self, where: str, message: str):
        self.where = where
        self.message = message

    def __str__(self):
        return "%s: %s" % (self.where, self.message)


def _known(values) -> str:
    return ", ".join(str(v) for v in sorted(values)) if values else "(none)"


def validate(scenario: Scenario, network=None, catalog=None, symbols=None) -> List[Problem]:
    """Return every problem found. An empty list means the scenario is executable.

    Every check is attempted for every step rather than stopping at the first
    failure. A generator fixing one problem at a time needs several round trips;
    one that receives all of them can fix them together.
    """
    problems: List[Problem] = []

    node_ids = set()
    real_ids = set()
    tx_symbols = {}
    if network is not None:
        try:
            node_ids = {n.id for n in network.nodes()}
            real_ids = {n.id for n in network.real_nodes()}
            tx_symbols = {n.id: getattr(n, "tx_enable_symbol", None)
                          for n in network.real_nodes()}
        except Exception as exc:  # pragma: no cover - defensive
            problems.append(Problem("network", "could not read the topology: %s" % exc))

    for step in scenario.steps:
        p = step.params
        where = step.where

        # ---------------------------------------------------------- node
        node = p.get("node")
        if node is not None and node_ids:
            if node not in node_ids:
                problems.append(Problem(where,
                    "node '%s' is not in the network.\n  Nodes defined: %s"
                    % (node, _known(node_ids))))
            elif step.verb in ("write_symbol", "expect_symbol") and node not in real_ids:
                problems.append(Problem(where,
                    "node '%s' is not a real node, so it has no memory to write.\n"
                    "  Real nodes: %s\n"
                    "  A scripted node emits scheduled traffic and executes no code."
                    % (node, _known(real_ids))))

        # -------------------------------------------------------- symbol
        if step.verb in ("write_symbol", "expect_symbol") and symbols is not None:
            name = p.get("symbol")
            available = symbols.names() if hasattr(symbols, "names") else []
            if name not in available:
                problems.append(Problem(where,
                    "symbol '%s' not found in ELF.\n  Available injectable symbols: %s"
                    % (name, _known(available))))
            else:
                value = p.get("value") if step.verb == "write_symbol" else p.get("equals")
                try:
                    symbols.check_range(name, value)
                except Exception as exc:
                    problems.append(Problem(where, str(exc)))

        if step.verb == "node_silence":
            try:
                scenario_mod.as_bool(p.get("silence"), "silence", where)
            except scenario_mod.ScenarioError as exc:
                problems.append(Problem(where, str(exc).split(": ", 1)[-1]))
            if node in tx_symbols and not tx_symbols.get(node):
                problems.append(Problem(where,
                    "node '%s' declares no tx_enable_symbol in network.yml, so its "
                    "transmission cannot be silenced." % node))

        # ------------------------------------------------------- message
        msg_id = p.get("id")
        if msg_id is not None and catalog is not None:
            try:
                message = catalog.message(msg_id)
            except catalog_mod.UnknownMessageError as exc:
                problems.append(Problem(where, str(exc)))
                message = None

            if message is not None:
                signals = p.get("signals") or {}
                if not isinstance(signals, dict):
                    problems.append(Problem(where,
                        "'signals' must be a mapping of signal name to value, got %s"
                        % type(signals).__name__))
                else:
                    for sig_name, sig_value in signals.items():
                        try:
                            catalog.signal(message.id, sig_name)
                        except catalog_mod.UnknownSignalError as exc:
                            problems.append(Problem(where, str(exc)))
                            continue
                        # Encoding is the honest test of legality: it applies the
                        # enum table, the scale and the field width in one place,
                        # so validation cannot drift from execution.
                        try:
                            catalog.encode(message.id, {sig_name: sig_value})
                        except (catalog_mod.EnumError, catalog_mod.ValueRangeError) as exc:
                            problems.append(Problem(where, str(exc)))

        # ------------------------------------------------------ deadline
        for key in DURATION_KEYS & set(p):
            value = p[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(Problem(where,
                    "'%s' must be a number of milliseconds, got %r (%s)"
                    % (key, value, type(value).__name__)))
            elif value <= 0:
                problems.append(Problem(where,
                    "'%s' must be positive, got %s.\n"
                    "  A deadline of zero or less can never be met, so the "
                    "assertion could only ever fail." % (key, value)))
            elif value > MAX_DEADLINE_MS:
                problems.append(Problem(where,
                    "'%s' is %s ms, beyond the %d ms this harness accepts.\n"
                    "  A scenario that waits that long burns the sweep's budget "
                    "for one answer." % (key, value, MAX_DEADLINE_MS)))

        if step.verb == "flood":
            pct = p.get("utilisation_pct")
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                problems.append(Problem(where,
                    "'utilisation_pct' must be a number, got %r" % (pct,)))
            elif not (0 < pct <= 100):
                problems.append(Problem(where,
                    "'utilisation_pct' must be greater than 0 and at most 100, got %s.\n"
                    "  It is a percentage of bus capacity, not a frame count."
                    % (pct,)))

    return problems


def format_problems(scenario: Scenario, problems: List[Problem]) -> str:
    lines = ["scenario '%s' has %d problem(s):" % (scenario.id, len(problems)), ""]
    for prob in problems:
        lines.append("  %s" % prob.where)
        for line in str(prob.message).splitlines():
            lines.append("      %s" % line)
        lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a scenario without executing it.")
    ap.add_argument("scenario")
    ap.add_argument("--network", required=True)
    ap.add_argument("--boards")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--symbols", required=True)
    args = ap.parse_args(argv)

    from harness import network as network_mod
    from harness import symbols as symbols_mod

    try:
        scen = scenario_mod.load(args.scenario)
    except scenario_mod.ScenarioError as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return 2

    net = network_mod.load(args.network, args.boards) if args.network else None
    cat = catalog_mod.load(args.catalog)
    syms = symbols_mod.load_symbols_json(args.symbols)

    problems = validate(scen, network=net, catalog=cat, symbols=syms)
    if problems:
        sys.stderr.write("\n" + format_problems(scen, problems) + "\n")
        return 1
    print("scenario '%s' is valid: %d step(s), nothing unresolved."
          % (scen.id, len(scen.steps)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
