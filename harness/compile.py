"""Scenario to Renode command script. This module's only output is that script.

Three things here are not negotiable, and each corresponds to a defect that has
already been paid for once:

**Peripheral names come from boards.yml, never from code.** A hardcoded
controller instance is exactly what makes the engine non-portable, and the second
system in this study uses a different one. ``harness/tests/test_purity.py``
enforces it.

**A missing symbol is a hard failure**, never a warning and never a skipped step.
A write to an address that does not exist injects nothing, the firmware behaves
correctly for the input it actually received, and the campaign reports PASS. That
is a false negative in a verification tool.

**Assertions compile to (value, mask)**, never to equality, because a real status
frame carries a counter that changes on every transmission.

On the shape of the emitted script
----------------------------------
Each assertion becomes three commands: arm, advance virtual time across the
window, settle. Running the whole window and then judging is equivalent to
stopping at the first match, because the matcher records the virtual instant the
frame arrived, and it is simpler and completely deterministic.

``wait_uart`` consumes its ENTIRE timeout of virtual time regardless of how early
the text appears, so every instant after it is shifted by that much. This is a
real wart. It is documented here, in the verb table, and in the reference the
Phase 3 generator reads, because a generator that does not know it will write
scenarios whose timing is systematically wrong.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from harness import catalog as catalog_mod
from harness import network as network_mod
from harness import scenario as scenario_mod
from harness import symbols as symbols_mod
from harness import validate as validate_mod

EXIT_OK = 0
EXIT_UNUSABLE = 2
EXIT_REFUSED = 3
EXIT_DRY_RUN = 4


class CompileError(Exception):
    """The scenario cannot be turned into an executable script."""


class RefusedError(CompileError):
    """Definable in the language, but no execution path exists for it."""


def _renode_time(ms) -> str:
    """Milliseconds to Renode's HH:MM:SS.ffffff form."""
    total_us = int(round(float(ms) * 1000.0))
    if total_us <= 0:
        raise CompileError("a duration must be positive, got %s ms" % ms)
    seconds, us = divmod(total_us, 1_000_000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return "%02d:%02d:%02d.%06d" % (hours, minutes, secs, us)


def _quote(text) -> str:
    """A monitor string literal.

    Scenario text reaches the event log, and the event log decides the verdict, so
    a label able to break out of its quotes could write a line that looks like an
    observation. The toolkit flattens what it receives as a second, independent
    defence; this one keeps the command line itself well formed.
    """
    s = "" if text is None else str(text)
    out = []
    for ch in s:
        if ch in '"\\':
            continue
        if ord(ch) < 32 or ord(ch) > 126:
            out.append(" ")
        else:
            out.append(ch)
    return '"%s"' % "".join(out).strip()


def _twos_complement(value: int, size_bytes: int) -> int:
    bits = size_bytes * 8
    if value < 0:
        return (1 << bits) + value
    return value


_WRITE_BY_SIZE = {1: "WriteByte", 2: "WriteWord", 4: "WriteDoubleWord",
                  8: "WriteQuadWord"}
_READ_BY_SIZE = {1: "ReadByte", 2: "ReadWord", 4: "ReadDoubleWord",
                 8: "ReadQuadWord"}


class Compiler:
    def __init__(self, network, catalog, symbols, root: Path, out_dir: Path,
                 serial: bool = False, quantum_us: int = 0):
        self.serial = serial
        self.quantum_us = int(quantum_us or 0)
        self._scenario_index = 0
        self._hub = "canHub0"
        self._suffix = "0"
        self.network = network
        self.catalog = catalog
        self.symbols = symbols
        self.root = Path(root)
        self.out = Path(out_dir)
        self.lines: List[str] = []

    def _rebase(self, path):
        """Express a path under the root the caller gave us.

        Data files may hand back absolute paths, and anything absolute is
        resolved through the PHYSICAL filesystem: os.getcwd() and
        Path.resolve() both follow symlinks, always. This repository is
        reached through a space-free symlink precisely because its real
        location contains a space the toolchain cannot handle, so an
        absolute path arriving here has that space back in it.

        Re-basing onto the root the caller supplied puts it back on the
        space-free path. A path outside the repository is left alone.
        """
        import os as _os
        p = Path(path)
        if not p.is_absolute():
            return self.root / p
        try:
            return self.root / p.relative_to(Path(_os.path.realpath(self.root)))
        except ValueError:
            return p

    def _add(self, line: str):
        # Renode splits an @path argument at whitespace, so a space anywhere in
        # a generated path produces a script that fails deep inside the emulator
        # with a confusing message. It is caught here, where the cause is
        # obvious, rather than there.
        if '@' in line:
            tail = line.split('@', 1)[1]
            for suffix in (' true', ' canHub'):
                if tail.endswith(suffix):
                    tail = tail[: -len(suffix)]
            if ' ' in tail:
                raise CompileError(
                    'a generated path contains a space:' + chr(10) + '  ' + line
                    + chr(10) + 'Renode splits @path arguments at whitespace. Pass'
                    + ' --root as the space-free path; this repository is'
                    + ' reachable at $HOME/detect-master.')
        self.lines.append(line)

    # ------------------------------------------------------------------ setup
    def _mach(self, node_id):
        """The Renode machine name for a logical node.

        They are the same string. Machines persist across a batch and are reset
        rather than recreated, so the names need not be unique, and keeping them
        equal is what makes a batched event log directly comparable with an
        unbatched one.
        """
        return node_id

    def _global_preamble(self):
        """Everything that is built once, whatever the batch size.

        Machines are created here and RESET between scenarios rather than
        removed and recreated. That is not a stylistic choice: removing a
        machine and building another in the same process crashes Renode
        natively, with a SIGSEGV inside the translation library on the second
        scenario of a batch. It happened with the machine's original name and it
        happened with a fresh name, so it is the lifecycle churn and not the
        name. `machine Reset` followed by reloading the ELF produces a verified
        fresh boot and does not crash.
        """
        self._add("# Generated by harness/compile.py. Do not edit.")
        self._add("")
        # Renode's own warnings on this target are voluminous diagnostics, not
        # observations. The event log is the record.
        self._add("logLevel 3")
        self._add("")
        # EXECUTION MODE AND QUANTUM.
        #
        # Renode advances each machine on its own host thread, so within a
        # synchronisation quantum their relative order follows host scheduling.
        # Measured: in parallel mode two runs of the same golden binary retire
        # different instruction counts, which makes rung 4's divergence
        # classifier unusable. Serial execution runs the machines one after
        # another in a fixed order and removes it entirely.
        #
        # On quantum and fidelity: the firmware ticks every 10 ms and the corpus
        # deadlines are 50 to 300 ms, so Renode's default quantum is orders of
        # magnitude finer than the tick of the guest itself and cannot change
        # which tick a frame lands in. Behaviour follows the tick.
        if self.quantum_us:
            self._add('emulation SetGlobalQuantum "%.6f"'
                      % (self.quantum_us / 1_000_000.0))
        if self.serial:
            self._add("emulation SetGlobalSerialExecution True")
        self._add("# execution mode: %s, quantum: %s"
                  % ("serial" if self.serial else "parallel",
                     ("%d us" % self.quantum_us) if self.quantum_us else "Renode default"))
        self._add("")
        self._add('emulation CreateCANHub "%s"' % self._hub)
        self._add("")

        for node in self.network.real_nodes():
            board = self.network.board_of(node.id)
            elf = self._rebase(self.network.elf_path(node.id))
            if not elf.is_file():
                raise CompileError(
                    "node '%s' declares an ELF that does not exist:\n  %s\n"
                    "Build it first: bash scripts/build-firmware.sh %s"
                    % (node.id, elf, node.id))
            self._add('mach create "%s"' % node.id)
            self._add("machine LoadPlatformDescription @%s" % self._rebase(board.repl))
            self._add("sysbus LoadELF @%s" % elf)
            self._add("connector Connect %s %s" % (board.can_peripheral, self._hub))
            self._add("")

        self._add("include @%s" % self._rebase("harness/can_toolkit.py"))
        for node in self.network.real_nodes():
            board = self.network.board_of(node.id)
            # A tap on EVERY real node, not only the device under test. Without
            # this an absence assertion on a frame the DUT never sends reports
            # clean while a peer floods the bus with it. The taps are attached
            # once and survive the resets, because the controllers do.
            self._add('ct_tap "%s" "%s" "%s"'
                      % (node.id, node.id, board.can_peripheral))
        self._add("")

    def _scenario_setup(self, scenario):
        """Return every machine to a fixed initial state for this scenario.

        Reset then reload the ELF: reset returns the peripherals and the CPU to
        their power-on state, and reloading restores .data and the entry point,
        after which Zephyr's own startup zeroes .bss. Verified to produce a
        console identical to a cold boot.

        The console capture is redirected to this scenario's own file, so each
        scenario's artefacts stand alone.
        """
        self._add("# ---- scenario: %s ----" % scenario.id)
        for node in self.network.real_nodes():
            board = self.network.board_of(node.id)
            self._add('mach set "%s"' % node.id)
            self._add("machine Reset")
            self._add("sysbus LoadELF @%s" % self._rebase(self.network.elf_path(node.id)))
            self._add("%s CreateFileBackend @%s true"
                      % (board.uart_peripheral, self.out / ("uart-%s.log" % node.id)))
        # After the machines are back at their initial state, and never before.
        self._add("ct_reset")
        self._add("")

    def _scenario_teardown(self):
        """Nothing to tear down.

        Machines are reset at the START of each scenario rather than removed at
        the end, so the next scenario's setup is the only thing that has to run.
        Kept as a named no-op because the batch structure reads better with the
        boundary visible, and because an earlier version did remove machines here
        and crashed the emulator.
        """
        self._add("")

    def _postamble(self):
        """Close a single-scenario script.

        The batch path does not use this: it emits the same artefacts per
        scenario and one `quit` at the very end. The two must stay in step, and
        the batching check is what proves they are, by requiring the event logs
        to be identical.
        """
        self._add("")
        self._add("ct_settle")
        self._add("ct_trace %s" % _quote("end of scenario"))
        self._add("ct_trace_flush @%s" % (self.out / "trace.log"))
        self._add("ct_flush @%s" % (self.out / "events.log"))
        self._add("quit")

    def _symbol_write(self, node_id, name, value):
        try:
            info = self.symbols[name]
        except Exception:
            info = None
        address = info.address if info is not None else None
        size = info.size if info is not None else None
        if address is None:
            raise CompileError(
                "symbol '%s' has no address.\n"
                "It is not in the ELF, so there is nothing to write to. A write "
                "that lands nowhere injects no fault, the firmware behaves "
                "correctly for the input it actually received, and the campaign "
                "reports PASS. That is a false negative, so this is refused."
                % name)
        self.symbols.check_range(name, value)
        writer = _WRITE_BY_SIZE.get(size)
        if writer is None:
            raise CompileError("symbol '%s' has size %s, which this harness cannot "
                               "write; sizes 1, 2, 4 and 8 are supported." % (name, size))
        raw = _twos_complement(int(value), size)
        self._add('mach set "%s"' % self._mach(node_id))
        self._add("sysbus %s 0x%X 0x%X" % (writer, address, raw))
        self._add('ct_stim "%s" "%s" "%s"' % (node_id, name, value))

    def _emit_step(self, step):
        p = step.params
        verb = step.verb

        if verb == "mark":
            self._add("ct_mark %s" % _quote(p["text"]))

        elif verb == "run_for":
            self._add('emulation RunFor "%s"' % _renode_time(p["ms"]))

        elif verb == "wait_uart":
            # Consumes the FULL timeout of virtual time; see the module docstring.
            self._add("ct_mark %s" % _quote(
                "wait_uart %s %r (consumes %s ms of virtual time)"
                % (p["node"], p["text"], p["timeout_ms"])))
            self._add('emulation RunFor "%s"' % _renode_time(p["timeout_ms"]))
            self._add("ct_uart_expect %s %s %s %s" % (
                _quote(p["node"]),
                _quote(str(self.out / ("uart-%s.log" % p["node"]))),
                _quote(p["text"]),
                _quote(p.get("label") or ("uart: %s" % p["text"]))))

        elif verb == "write_symbol":
            self._symbol_write(p["node"], p["symbol"], p["value"])

        elif verb == "expect_symbol":
            name = p["symbol"]
            try:
                info = self.symbols[name]
            except Exception:
                raise CompileError(
                    "symbol '%s' is not an injection target, so it cannot be read "
                    "back.\n  Available: %s"
                    % (name, ", ".join(self.symbols.names())))
            address, size = info.address, info.size
            reader = _READ_BY_SIZE.get(size)
            if reader is None:
                raise CompileError("symbol '%s' has size %s, which cannot be read "
                                   "back; sizes 1, 2, 4 and 8 are supported."
                                   % (name, size))
            self._add('mach set "%s"' % self._mach(p["node"]))
            self._add("ct_expect_symbol %s %s 0x%X %d %d %s" % (
                _quote(p["node"]), _quote(self._mach(p["node"])), address, size,
                _twos_complement(int(p["equals"]), size),
                _quote(p.get("label") or ("%s == %s" % (name, p["equals"])))))

        elif verb in ("expect_can", "expect_no_can"):
            message = self.catalog.message(p["id"])
            value, mask = self.catalog.encode(message.id, p.get("signals") or {})
            window = p["within_ms"] if verb == "expect_can" else p["for_ms"]
            command = "ct_expect" if verb == "expect_can" else "ct_forbid"
            self._add("%s %d %d %d %d %s" % (
                command, message.id, value, mask, int(window),
                _quote(p.get("label") or step.label())))
            self._add('emulation RunFor "%s"' % _renode_time(window))
            self._add("ct_settle")
            self._add("ct_trace %s" % _quote(step.where))

        elif verb == "can_send":
            message = self.catalog.message(p["id"])
            if "data_hex" in p and p["data_hex"]:
                payload = str(p["data_hex"]).replace("0x", "").replace(",", " ")
                data_bytes = [int(b, 16) for b in payload.split()]
            else:
                value, _ = self.catalog.encode(message.id, p.get("signals") or {})
                data_bytes = [(value >> (8 * i)) & 0xFF for i in range(message.dlc)]
            hexed = " ".join("%02X" % b for b in data_bytes)
            self._add("ct_inject %s %s %d %s" % (
                _quote(p["node"]),
                _quote(self.network.board_of(p["node"]).can_peripheral),
                message.id, _quote(hexed)))

        elif verb == "node_silence":
            node = self.network.node(p["node"])
            symbol = getattr(node, "tx_enable_symbol", None)
            if not symbol:
                raise CompileError(
                    "node '%s' declares no tx_enable_symbol in network.yml, so its "
                    "transmission cannot be silenced." % p["node"])
            silence = scenario_mod.as_bool(p["silence"], "silence", step.where)
            self._symbol_write(p["node"], symbol, 0 if silence else 1)

        elif verb in ("flood", "node_signal"):
            # Exit code 3 exists for exactly this: definable, but no execution
            # path. Emitting something that half works would be worse, because a
            # scenario using it would produce a verdict that looks real.
            raise RefusedError(
                "'%s' requires a scripted node, and this system declares none.\n"
                "  network.yml defines nodes: %s\n"
                "  A scripted node emits scheduled traffic and executes no code; "
                "add one with type: scripted before using this verb.\n"
                "  Refusing rather than producing a verdict that would look real."
                % (verb, ", ".join(n.id for n in self.network.nodes())))

        else:  # pragma: no cover - scenario.parse rejects unknown verbs first
            raise CompileError("no code generator for verb '%s'" % verb)

    # ---------------------------------------------------------------- compile
    def compile(self, scenario) -> str:
        self._global_preamble()
        self._scenario_setup(scenario)
        self._add("ct_mark %s" % _quote("scenario %s begins" % scenario.id))
        self._add("ct_trace %s" % _quote("start"))
        for step in scenario.steps:
            self._add("")
            self._add("# %s" % step.where)
            self._emit_step(step)
        self._postamble()
        return "\n".join(self.lines) + "\n"

    def compile_batch(self, items) -> str:
        """One script running many scenarios in one emulator process.

        items is a sequence of (scenario, out_dir). The global preamble is
        emitted once; each scenario then gets its own setup, steps, artefacts and
        teardown."""
        self._global_preamble()
        for scenario, out_dir in items:
            self.out = out_dir
            self._scenario_setup(scenario)
            self._add("ct_mark %s" % _quote("scenario %s begins" % scenario.id))
            self._add("ct_trace %s" % _quote("start"))
            for step in scenario.steps:
                self._add("")
                self._add("# %s" % step.where)
                self._emit_step(step)
            self._add("")
            self._add("ct_settle")
            self._add("ct_trace %s" % _quote("end of scenario"))
            self._add("ct_trace_flush @%s" % (out_dir / "trace.log"))
            self._add("ct_flush @%s" % (out_dir / "events.log"))
            self._scenario_teardown()
        self._add("quit")
        return "\n".join(self.lines) + "\n"


def compile_batch(items, network, catalog, symbols, root, out_dir,
                  serial: bool = False, quantum_us: int = 0) -> str:
    return Compiler(network, catalog, symbols, root, out_dir,
                    serial=serial, quantum_us=quantum_us).compile_batch(items)


def compile_scenario(scenario, network, catalog, symbols, root, out_dir,
                     serial: bool = False, quantum_us: int = 0) -> str:
    return Compiler(network, catalog, symbols, root, out_dir,
                    serial=serial, quantum_us=quantum_us).compile(scenario)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compile a scenario to a Renode script.")
    ap.add_argument("scenario")
    ap.add_argument("--network", required=True)
    ap.add_argument("--boards", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", required=True, help="directory for artefacts")
    ap.add_argument("--dry-run", action="store_true",
                    help="compile and print the script, execute nothing (exit 4)")
    ap.add_argument("--serial", action="store_true",
                    help="run machines serially in a fixed order (reproducible, slower)")
    ap.add_argument("--quantum-us", type=int, default=0,
                    help="synchronisation quantum in microseconds; 0 = Renode default")
    args = ap.parse_args(argv)

    try:
        scen = scenario_mod.load(args.scenario)
        net = network_mod.load(args.network, args.boards)
        cat = catalog_mod.load(args.catalog)
        syms = symbols_mod.load_symbols_json(args.symbols)
    except Exception as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return EXIT_UNUSABLE

    problems = validate_mod.validate(scen, network=net, catalog=cat, symbols=syms)
    if problems:
        sys.stderr.write("\n" + validate_mod.format_problems(scen, problems) + "\n")
        return EXIT_UNUSABLE

    out = Path(os.path.abspath(args.out))
    out.mkdir(parents=True, exist_ok=True)
    try:
        # os.path.abspath, NOT Path.resolve(): resolve() follows symlinks, and this
        # repository is reached through a space-free symlink precisely because the
        # real path contains a space that the toolchain cannot handle. Resolving
        # here would put that space back into every generated command.
        script = compile_scenario(scen, net, cat, syms,
                                  Path(os.path.abspath(args.root)), out,
                                  serial=args.serial, quantum_us=args.quantum_us)
    except RefusedError as exc:
        sys.stderr.write("\nREFUSED: %s\n\n" % exc)
        return EXIT_REFUSED
    except Exception as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return EXIT_UNUSABLE

    script_path = out / "scenario.resc"
    script_path.write_text(script, encoding="utf-8")

    if args.dry_run:
        print(script)
        print("# DRY RUN: compiled to %s, nothing executed." % script_path)
        return EXIT_DRY_RUN

    print(script_path)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
