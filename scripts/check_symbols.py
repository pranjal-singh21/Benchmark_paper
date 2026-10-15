#!/usr/bin/env python3
"""Assert that every injection target survived into the ELF.

Zephyr compiles with -fdata-sections and links with --gc-sections, so a global
that no root reaches is discarded from the binary. ``volatile`` does not prevent
this; volatile constrains the compiler, and the collection happens in the
linker.

A discarded injection target has no address. The harness would have nothing to
write to, the fault would never be injected, the firmware would behave correctly
for the input it actually received, and the campaign would report PASS. That is
a false negative in a verification tool, which is the worst class of bug this
project can have.

This gate is the second of the two independent defences, the first being the
-Wl,--undefined flags added by CMakeLists.txt from the same symbol list. It
fails loudly and names the symbol. It never substitutes a default for a missing
address, because a plausible wrong address is worse than a stopped build.

Usage
-----
    check_symbols.py <zephyr.elf> <symbol> [<symbol> ...]
    check_symbols.py <zephyr.elf> --from-list <injectable_symbols.txt>

Exit status
-----------
    0   every requested symbol is present, with a non-zero address and size
    1   at least one symbol failed a check; each failure is named
    2   the invocation or the ELF itself was unusable
"""

import sys
from pathlib import Path

try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
except ImportError:  # pragma: no cover - environment error, not a logic error
    sys.stderr.write(
        "ERROR: pyelftools is not importable.\n"
        "       This script must run under the project venv, not system python.\n"
        "       Run: bash scripts/setup.sh python\n"
        "       Then: $DETECT_VENV/bin/python scripts/check_symbols.py ...\n"
    )
    raise SystemExit(2)


def parse_symbol_list(path):
    """Read the shared symbol list, applying the same rules CMake applies."""
    symbols = []
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # "<symbol> <unit> <min> <max> [flags]" - only the first field names the
        # symbol. Every listed symbol is gated, including ones flagged
        # not-injectable: those are force-kept at link time precisely so their
        # absence proves the linker collected them.
        symbols.append(line.split()[0])
    return symbols


def collect_symbols(elf_path):
    """Map every defined symbol name to (address, size, section index)."""
    found = {}
    with open(elf_path, "rb") as handle:
        elf = ELFFile(handle)
        tables = [s for s in elf.iter_sections() if isinstance(s, SymbolTableSection)]
        if not tables:
            sys.stderr.write(
                "ERROR: %s contains no symbol table.\n"
                "       It was probably stripped. The gate cannot verify a\n"
                "       stripped binary, and will not pretend it can.\n" % elf_path
            )
            raise SystemExit(2)
        for table in tables:
            for sym in table.iter_symbols():
                if not sym.name:
                    continue
                # A symbol with section index SHN_UNDEF (0) is a reference, not a
                # definition. Recording it would let an undefined reference pass
                # as a resolved address.
                shndx = sym["st_shndx"]
                if shndx == "SHN_UNDEF":
                    continue
                found[sym.name] = (
                    sym["st_value"],
                    sym["st_size"],
                    shndx,
                )
    return found


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2

    elf_path = argv[1]
    if argv[2] == "--from-list":
        if len(argv) != 4:
            sys.stderr.write("ERROR: --from-list takes exactly one file path.\n")
            return 2
        list_path = argv[3]
        if not Path(list_path).is_file():
            sys.stderr.write("ERROR: symbol list not found: %s\n" % list_path)
            return 2
        wanted = parse_symbol_list(list_path)
        source = list_path
    else:
        wanted = argv[2:]
        source = "command line"

    if not wanted:
        sys.stderr.write(
            "ERROR: no symbols requested from %s.\n"
            "       An empty check would pass without verifying anything, so it\n"
            "       is refused.\n" % source
        )
        return 2

    if not Path(elf_path).is_file():
        sys.stderr.write("ERROR: ELF not found: %s\n" % elf_path)
        return 2

    found = collect_symbols(elf_path)

    print("Symbol gate: %s" % elf_path)
    print("  symbols requested from %s: %d" % (source, len(wanted)))
    print()

    failures = []
    for name in wanted:
        entry = found.get(name)
        if entry is None:
            print("  %-18s ABSENT" % name)
            failures.append((name, "absent from the ELF symbol table"))
            continue
        addr, size, shndx = entry
        if addr == 0:
            print("  %-18s 0x%08x  size=%-4d  ADDRESS ZERO" % (name, addr, size))
            failures.append((name, "resolved to address 0, which is not a usable target"))
            continue
        if size == 0:
            print("  %-18s 0x%08x  size=%-4d  SIZE ZERO" % (name, addr, size))
            failures.append((name, "has size 0, so there is nothing to write into"))
            continue
        print("  %-18s 0x%08x  size=%-4d  shndx=%s" % (name, addr, size, shndx))

    print()
    if not failures:
        print("PASS: all %d symbol(s) present with a usable address." % len(wanted))
        return 0

    sys.stderr.write("\nFAIL: %d of %d injection target(s) unusable.\n\n" % (len(failures), len(wanted)))
    for name, why in failures:
        sys.stderr.write("  %-18s %s\n" % (name, why))
    sys.stderr.write(
        "\nWithout a resolvable address the harness has nothing to write to. The\n"
        "fault would never be injected, the firmware would behave correctly for\n"
        "the input it actually received, and the campaign would report PASS.\n"
        "That is a false negative, so the build stops here instead.\n"
        "\nIn likely order:\n"
        "  1. The linker collected it. Zephyr compiles with -fdata-sections and\n"
        "     links with --gc-sections, so an unreferenced global is dropped even\n"
        "     when declared volatile. Confirm with:\n"
        "       grep -A400 'Discarded input sections' <build>/zephyr/zephyr.map\n"
        "     Fix by adding the symbol to injectable_symbols.txt, which adds its\n"
        "     -Wl,--undefined flag.\n"
        "  2. It is declared static, so it has no external linkage to resolve.\n"
        "  3. It was renamed in the firmware but not in injectable_symbols.txt.\n"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
