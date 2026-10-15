"""Resolve declared injection targets against an ELF symbol table.

This module is the bridge between two things that are written independently:

  * the declaration file, which states which symbols are injection targets and
    what physical quantity each one holds, and
  * the linked ELF, which is the only place the addresses actually exist.

It reads ``.symtab`` in-process with pyelftools, merges each definition with its
declared unit and range, infers a C type, and emits ``symbols.json``.

Why this is a gate and not a lookup
-----------------------------------
Zephyr compiles with ``-fdata-sections`` and links with ``--gc-sections``, so a
global that no root reaches is discarded from the binary. ``volatile`` does not
prevent this; ``volatile`` constrains the compiler, and the collection happens
in the linker.

A discarded injection target has no address. The harness would have nothing to
write to, the fault would never be injected, the firmware would behave correctly
for the input it actually received, and the campaign would report PASS. That is
a false negative in a verification tool, which is the worst class of bug this
project can have. So a declared symbol that is missing, undefined, common,
ambiguous, at address 0, or of size 0 stops the run and is named. No default
address is ever substituted: a plausible wrong address is worse than a stop.

Declaration file format
-----------------------
Whitespace separated. Blank lines and lines whose first non-space character is
``#`` are ignored; a trailing comment on a declaration line is *not* stripped,
because the build system reads the same file with the same rule and the two
readers must not disagree about what a line says.

Four line forms are accepted::

    <name>                                      bare, mid-migration: no unit,
                                                no range
    <name> not-injectable                       bare, force-kept and gated but
                                                not an injection target
    <name> <unit> <min> <max>                   declared
    <name> <unit> <min> <max> not-injectable    declared, gated, not a target

``<unit>`` is an opaque label. This module never checks it against a list of
known units: the vocabulary belongs to the system being tested, not to the
engine. ``<min>`` and ``<max>`` are integers, decimal, or ``0x``/``0o``/``0b``
prefixed. ``not-injectable`` is the only flag; it means the symbol is force-kept
at link time and still gated here, but must not appear in ``symbols.json`` as
something the harness may write to.

Anything else -- two or three columns, an unknown flag, a non-integer bound, a
reversed range, a repeated symbol -- is a hard failure naming the file, the line
number and the line. A declaration file is small and hand-written; guessing at
what a malformed line meant is how a target silently stops being injected.

C type inference
----------------
From the ELF's size plus the declared range's signedness only, never from the
symbol's name: sizes 1/2/4/8 give ``(u)int8_t``/``16_t``/``32_t``/``64_t``, and a
negative declared minimum makes it signed. With no declared range the signedness
is unknown, so the unsigned form is reported and the entry carries an explicit
note saying so. Any other size has no scalar type and is a hard failure.

Range checking
--------------
``SymbolTable.check_range(symbol, value)`` returns the value unchanged or raises.
It never clamps, wraps or truncates. A truncated write looks exactly like a
planted firmware defect, so a campaign that clamped one value would attribute a
detection to a defect that was never injected. A symbol with no declared range
cannot be validated, and ``check_range`` refuses it loudly rather than allowing
anything through.

CLI
---
::

    python -m harness.symbols <elf> <injectable_symbols.txt> [-o symbols.json]

With ``-o`` the human-readable report goes to stdout and the JSON to the file.
Without it the JSON goes to stdout and the report to stderr, so the output can
be piped.

Exit status
-----------
::

    0   every declared symbol resolved; symbols.json written
    1   at least one declared symbol is unusable; each failure is named
    2   the invocation, the declaration file or the ELF itself was unusable
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, NoReturn, Optional, Sequence, Tuple

try:
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
except ImportError as _exc:  # pragma: no cover - environment error, not logic
    raise ImportError(
        "pyelftools is not importable, so harness.symbols cannot read an ELF. "
        "This module must run under the project venv, not system python. "
        "Run: bash scripts/setup.sh python, then use $DETECT_PYTHON."
    ) from _exc


__all__ = [
    "FLAG_NOT_INJECTABLE",
    "SymbolError",
    "SymbolListError",
    "ElfReadError",
    "SymbolResolutionError",
    "CTypeInferenceError",
    "UnknownSymbolError",
    "UndeclaredRangeError",
    "NonIntegerValueError",
    "OutOfRangeError",
    "Declaration",
    "ElfSymbol",
    "ResolvedSymbol",
    "SymbolTable",
    "parse_declarations",
    "read_elf_symbols",
    "sha256_of_file",
    "infer_c_type",
    "resolve",
    "load_symbols_json",
    "main",
]


FLAG_NOT_INJECTABLE = "not-injectable"
_KNOWN_FLAGS = (FLAG_NOT_INJECTABLE,)

# Section indices that carry no final address. st_value means something else
# entirely for these, so accepting one would hand the harness a number that
# looks like an address and is not.
_NO_ADDRESS_SECTIONS = ("SHN_UNDEF", "SHN_COMMON")

_SIZE_TO_BITS = {1: 8, 2: 16, 4: 32, 8: 64}

_FALSE_NEGATIVE_NOTE = (
    "Without a resolvable address the harness has nothing to write to. The "
    "fault would never be injected, the firmware would behave correctly for the "
    "input it actually received, and the campaign would report PASS. That is a "
    "false negative, so the run stops here instead."
)

_RESOLUTION_HINTS = (
    "In likely order:\n"
    "  1. The linker collected it. Zephyr compiles with -fdata-sections and\n"
    "     links with --gc-sections, so an unreferenced global is dropped even\n"
    "     when declared volatile. Confirm with:\n"
    "       grep -A400 'Discarded input sections' <build>/zephyr/zephyr.map\n"
    "     Fix by giving the symbol its -Wl,--undefined flag, which the build\n"
    "     system derives from this same declaration file.\n"
    "  2. It is declared static, so it has no external linkage to resolve.\n"
    "  3. It was renamed in the firmware but not in the declaration file."
)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SymbolError(Exception):
    """Base class for every failure this module raises."""


class SymbolListError(SymbolError):
    """The declaration file could not be read or contains a malformed line."""


class ElfReadError(SymbolError):
    """The ELF could not be opened, or carries no symbol table to read."""


class SymbolResolutionError(SymbolError):
    """One or more declared symbols have no usable address in the ELF."""

    def __init__(self, message: str, failures: Sequence[Tuple[str, str]]):
        super().__init__(message)
        self.failures = list(failures)

    @property
    def symbols(self) -> List[str]:
        return [name for name, _ in self.failures]


class CTypeInferenceError(SymbolError):
    """A size has no scalar C type, so no type may be reported for it."""


class UnknownSymbolError(SymbolError):
    """A range check was asked for a symbol this table does not carry."""


class UndeclaredRangeError(SymbolError):
    """A range check was asked for a symbol that declared no range."""


class NonIntegerValueError(SymbolError):
    """A non-integer value was offered for a check. It is not converted."""


class OutOfRangeError(SymbolError):
    """A value falls outside the declared range. It is refused, not clamped."""

    def __init__(self, symbol: str, value, minimum: int, maximum: int,
                 unit: Optional[str]):
        self.symbol = symbol
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        self.unit = unit
        unit_text = "" if unit is None else " (unit: %s)" % unit
        super().__init__(
            "%s: value %s is outside the declared range [%s, %s]%s. "
            "The value is refused, not clamped and not wrapped: a truncated "
            "write is indistinguishable from a planted firmware defect, so a "
            "campaign that silently corrected it would credit a detection to a "
            "fault it never injected. Either write a value within [%s, %s] or "
            "widen the declared range in the declaration file."
            % (symbol, value, minimum, maximum, unit_text, minimum, maximum)
        )


# --------------------------------------------------------------------------
# Declaration file
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Declaration:
    """One line of the declaration file, parsed but not yet resolved."""

    name: str
    unit: Optional[str]
    minimum: Optional[int]
    maximum: Optional[int]
    injectable: bool
    line_number: int

    @property
    def has_range(self) -> bool:
        return self.minimum is not None and self.maximum is not None


def _fail_line(path, line_number: int, raw: str, problem: str,
               remedy: str) -> NoReturn:
    raise SymbolListError(
        "%s line %d: %s\n"
        "    %s\n"
        "  %s\n"
        "  Accepted forms are:\n"
        "    <name>\n"
        "    <name> %s\n"
        "    <name> <unit> <min> <max>\n"
        "    <name> <unit> <min> <max> %s"
        % (path, line_number, problem, raw.strip(), remedy,
           FLAG_NOT_INJECTABLE, FLAG_NOT_INJECTABLE)
    )


def _parse_bound(text: str, which: str, name: str, path, line_number: int,
                 raw: str) -> int:
    body = text[1:] if text[:1] in ("+", "-") else text
    # Base 10 unless explicitly prefixed, so a zero-padded decimal stays
    # decimal instead of being read as octal.
    base = 0 if body[:2].lower() in ("0x", "0o", "0b") else 10
    try:
        return int(text, base)
    except ValueError:
        pass
    _fail_line(
        path, line_number, raw,
        "the %s bound of %s is %r, which is not an integer."
        % (which, name, text),
        "Bounds are integers in the symbol's own unit: decimal, or 0x/0o/0b "
        "prefixed. A bound is not rounded or reinterpreted here, because a "
        "bound the tool invented would silently widen or narrow what the "
        "harness is allowed to write.",
    )


def parse_declarations(path) -> Dict[str, Declaration]:
    """Parse the declaration file into ``{name: Declaration}``.

    Order is preserved. Every malformed line is a hard failure naming the file,
    the line number and the line itself.
    """
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as exc:
        raise SymbolListError(
            "cannot read the declaration file %s: %s. Without it there is no "
            "list of injection targets, and an empty list would let a campaign "
            "run injecting nothing and still report PASS." % (path, exc)
        ) from exc

    declarations: Dict[str, Declaration] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        name = fields[0]
        unit: Optional[str] = None
        minimum: Optional[int] = None
        maximum: Optional[int] = None
        injectable = True

        if len(fields) == 1:
            pass
        elif len(fields) == 2:
            if fields[1] != FLAG_NOT_INJECTABLE:
                _fail_line(
                    path, line_number, raw,
                    "%s has 2 columns, which is not a valid form." % name,
                    "The only two-column form is the flag %r on its own. A unit "
                    "must be followed by both bounds; a half-declared range "
                    "cannot be range-checked and would let out-of-range writes "
                    "through unnoticed." % FLAG_NOT_INJECTABLE,
                )
            injectable = False
        elif len(fields) == 3:
            _fail_line(
                path, line_number, raw,
                "%s has 3 columns, so its range is incomplete." % name,
                "Give both bounds: <name> <unit> <min> <max>. A half-declared "
                "range cannot be range-checked and would let out-of-range "
                "writes through unnoticed.",
            )
        elif len(fields) in (4, 5):
            unit = fields[1]
            minimum = _parse_bound(fields[2], "minimum", name, path,
                                   line_number, raw)
            maximum = _parse_bound(fields[3], "maximum", name, path,
                                   line_number, raw)
            if minimum > maximum:
                _fail_line(
                    path, line_number, raw,
                    "%s declares minimum %d greater than maximum %d."
                    % (name, minimum, maximum),
                    "The bounds are not swapped for you. A reversed range "
                    "silently rejects every legal value, or, once corrected by "
                    "guesswork, accepts values the system never permits.",
                )
            if len(fields) == 5:
                if fields[4] not in _KNOWN_FLAGS:
                    _fail_line(
                        path, line_number, raw,
                        "%s carries unknown flag %r." % (name, fields[4]),
                        "The only accepted flag is %r. An unrecognised flag is "
                        "refused rather than ignored, because a flag that was "
                        "meant to exclude a symbol and was ignored would put a "
                        "non-target back in the injection set."
                        % FLAG_NOT_INJECTABLE,
                    )
                injectable = False
        else:
            _fail_line(
                path, line_number, raw,
                "%s has %d columns; at most 5 are defined."
                % (name, len(fields)),
                "A trailing comment on a declaration line is not stripped, "
                "because the build system reads this same file and would not "
                "strip it either. Put comments on their own # line.",
            )

        if name in declarations:
            first = declarations[name]
            raise SymbolListError(
                "%s: %s is declared twice, on line %d and line %d. The two "
                "declarations are not merged and the later one does not win: "
                "if they carry different units or ranges, either choice would "
                "silently range-check the harness against a contract nobody "
                "wrote. Delete one."
                % (path, name, first.line_number, line_number)
            )

        declarations[name] = Declaration(
            name=name,
            unit=unit,
            minimum=minimum,
            maximum=maximum,
            injectable=injectable,
            line_number=line_number,
        )

    return declarations


# --------------------------------------------------------------------------
# ELF
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ElfSymbol:
    """One named entry of ``.symtab``, as read."""

    name: str
    address: int
    size: int
    section: str

    @property
    def has_address(self) -> bool:
        return self.section not in _NO_ADDRESS_SECTIONS


def _section_name(elf, shndx) -> str:
    """Turn ``st_shndx`` into a printable section name.

    pyelftools hands back the string form (``SHN_UNDEF``, ``SHN_ABS``,
    ``SHN_COMMON``) for the reserved indices and an int otherwise.
    """
    if isinstance(shndx, str):
        return shndx
    try:
        section = elf.get_section(shndx)
    except Exception:  # index outside the section header table
        return "section-index-%d" % shndx
    name = getattr(section, "name", "")
    return name if name else "section-index-%d" % shndx


def read_elf_symbols(elf_path) -> Dict[str, List[ElfSymbol]]:
    """Read ``.symtab`` and return every named entry, grouped by name.

    Entries are returned as read, including undefined and common ones, so the
    caller can say *why* a symbol is unusable instead of only that it is.
    """
    path = Path(elf_path)
    if not path.is_file():
        raise ElfReadError("ELF not found: %s" % path)

    found: Dict[str, List[ElfSymbol]] = {}
    with open(path, "rb") as handle:
        try:
            elf = ELFFile(handle)
        except Exception as exc:
            raise ElfReadError(
                "%s is not a readable ELF: %s. Point this at the linked ELF "
                "(usually <build>/zephyr/zephyr.elf), not at a .bin, .hex or "
                ".map." % (path, exc)
            ) from exc

        table = elf.get_section_by_name(".symtab")
        if table is None or not isinstance(table, SymbolTableSection):
            raise ElfReadError(
                "%s carries no .symtab. It was probably stripped. The symbol "
                "map cannot be built from a stripped binary, and will not be "
                "faked from one." % path
            )

        for symbol in table.iter_symbols():
            if not symbol.name:
                continue
            # Section and file entries reuse section and file names, which can
            # collide with a real symbol name and would resolve to nonsense.
            if symbol["st_info"]["type"] in ("STT_SECTION", "STT_FILE"):
                continue
            found.setdefault(symbol.name, []).append(
                ElfSymbol(
                    name=symbol.name,
                    address=symbol["st_value"],
                    size=symbol["st_size"],
                    section=_section_name(elf, symbol["st_shndx"]),
                )
            )
    return found


def sha256_of_file(path) -> str:
    """Hex sha256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------


def infer_c_type(size: int, minimum: Optional[int]) -> Tuple[str, bool]:
    """Return ``(c_type, signedness_assumed)`` from the ELF size and the range.

    The symbol's name is never consulted. A negative declared minimum makes the
    type signed; with no declared range the signedness is unknown, so the
    unsigned form is returned with ``signedness_assumed`` True and the caller
    must record that assumption.
    """
    bits = _SIZE_TO_BITS.get(size)
    if bits is None:
        raise CTypeInferenceError(
            "size %d has no scalar C type (only 1, 2, 4 and 8 do), so no type "
            "can be reported for it. Injecting into an aggregate through a "
            "guessed scalar type would write the wrong width at the wrong "
            "offset. Either declare a scalar member as the target, or extend "
            "this module deliberately to describe aggregates." % size
        )
    if minimum is None:
        return "uint%d_t" % bits, True
    return ("int%d_t" % bits if minimum < 0 else "uint%d_t" % bits), False


def _representable_bounds(size: int, signed: bool) -> Tuple[int, int]:
    bits = _SIZE_TO_BITS[size] if size in _SIZE_TO_BITS else size * 8
    if signed:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSymbol:
    """A declared symbol merged with its definition in the ELF."""

    name: str
    address: int
    size: int
    section: str
    c_type: str
    signedness_assumed: bool
    unit: Optional[str] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None

    @property
    def has_range(self) -> bool:
        return self.minimum is not None and self.maximum is not None

    def to_json_object(self) -> dict:
        entry = {
            "address": "0x%08x" % self.address,
            "size": self.size,
            "c_type": self.c_type,
            "unit": self.unit,
            "range": None if not self.has_range else [self.minimum,
                                                      self.maximum],
        }
        if self.signedness_assumed:
            entry["c_type_note"] = (
                "no range declared, so signedness is unknown; the unsigned "
                "form is reported from the ELF size alone"
            )
        return entry


class SymbolTable:
    """The resolved injection targets, plus the provenance of the ELF."""

    def __init__(self, elf_path: str, elf_sha256: str, declared_from: str,
                 symbols: Dict[str, ResolvedSymbol],
                 excluded: Optional[Dict[str, ResolvedSymbol]] = None):
        self.elf_path = elf_path
        self.elf_sha256 = elf_sha256
        self.declared_from = declared_from
        self.symbols = dict(symbols)
        # Gated in the ELF, deliberately not injection targets. Kept so that
        # asking about one gets an answer that says why, not "unknown symbol".
        self.excluded = dict(excluded or {})

    # -- mapping-ish conveniences -----------------------------------------

    def __contains__(self, name) -> bool:
        return name in self.symbols

    def __getitem__(self, name) -> ResolvedSymbol:
        try:
            return self.symbols[name]
        except KeyError:
            raise UnknownSymbolError(self._unknown_message(name)) from None

    def __len__(self) -> int:
        return len(self.symbols)

    def __iter__(self):
        return iter(self.symbols)

    def names(self) -> List[str]:
        return list(self.symbols)

    def _unknown_message(self, name) -> str:
        if name in self.excluded:
            return (
                "%s is declared %s, so it is gated in the ELF but is not an "
                "injection target and carries no entry in the symbol map. "
                "Nothing may be written to it. Remove the flag from the "
                "declaration file if it is supposed to be a target."
                % (name, FLAG_NOT_INJECTABLE)
            )
        return (
            "%s is not in the symbol map built from %s. Known targets: %s. "
            "Add it to the declaration file (which also gives it its "
            "-Wl,--undefined flag) if it is meant to be injectable."
            % (name, self.declared_from,
               ", ".join(sorted(self.symbols)) or "(none)")
        )

    # -- range checking ----------------------------------------------------

    def check_range(self, symbol: str, value):
        """Return ``value`` unchanged, or raise naming the symbol.

        Never clamps, wraps or truncates, and refuses outright for a symbol
        that declared no range.
        """
        resolved = self.symbols.get(symbol)
        if resolved is None:
            raise UnknownSymbolError(self._unknown_message(symbol))

        if isinstance(value, bool) or not isinstance(value, int):
            raise NonIntegerValueError(
                "%s: value %r is a %s, not an integer, so it cannot be "
                "range-checked or written as %s. Convert it in the caller, "
                "where the rounding or encoding rule is known: converting it "
                "here would write a value nobody asked for and attribute the "
                "firmware's response to it."
                % (symbol, value, type(value).__name__, resolved.c_type)
            )

        if not resolved.has_range:
            raise UndeclaredRangeError(
                "%s declares no range, so %r cannot be validated and this "
                "check refuses to pass it. An unchecked write is how an "
                "impossible value reaches the firmware and gets reported as a "
                "detected defect. Give %s a unit and both bounds in %s: "
                "<name> <unit> <min> <max>."
                % (symbol, value, symbol, self.declared_from)
            )

        if value < resolved.minimum or value > resolved.maximum:
            raise OutOfRangeError(symbol, value, resolved.minimum,
                                  resolved.maximum, resolved.unit)
        return value

    # -- serialisation -----------------------------------------------------

    def to_json_object(self) -> dict:
        return {
            "generated_from": self.elf_path,
            "elf_sha256": self.elf_sha256,
            "declared_from": self.declared_from,
            "symbols": {name: sym.to_json_object()
                        for name, sym in self.symbols.items()},
        }

    def write_json(self, path) -> Path:
        path = Path(path)
        if path.parent and not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json_object(), indent=2) + "\n")
        return path


def resolve(elf_path, declarations_path) -> SymbolTable:
    """Merge the declaration file with the ELF, or fail naming every problem."""
    declarations = parse_declarations(declarations_path)
    declared_from = str(declarations_path)
    elf_path_text = str(elf_path)

    if not declarations:
        raise SymbolListError(
            "%s declares no symbols. An empty symbol map means every later "
            "injection has no target, the faults are never written, and the "
            "campaign reports PASS having tested nothing. Refusing to emit it."
            % declared_from
        )

    found = read_elf_symbols(elf_path)

    symbols: Dict[str, ResolvedSymbol] = {}
    excluded: Dict[str, ResolvedSymbol] = {}
    failures: List[Tuple[str, str]] = []

    for name, declaration in declarations.items():
        entries = found.get(name, [])
        definitions = [e for e in entries if e.has_address]

        if not definitions:
            undefined = [e for e in entries if e.section == "SHN_UNDEF"]
            common = [e for e in entries if e.section == "SHN_COMMON"]
            if common:
                failures.append((
                    name,
                    "is a common (tentative) symbol in the ELF, whose st_value "
                    "is its alignment and not an address. Define it once, "
                    "initialised, in exactly one translation unit.",
                ))
            elif undefined:
                failures.append((
                    name,
                    "appears in .symtab only as an undefined reference "
                    "(SHN_UNDEF): something asks for it, nothing defines it. "
                    "It has no address in this ELF.",
                ))
            else:
                failures.append((
                    name,
                    "is absent from the .symtab of the ELF.",
                ))
            continue

        addresses = sorted({(e.address, e.size, e.section) for e in definitions})
        if len(addresses) > 1:
            failures.append((
                name,
                "has %d different definitions in .symtab (%s). Picking one "
                "would be a coin flip between addresses, so none is picked."
                % (len(addresses),
                   "; ".join("0x%08x size=%d in %s" % a for a in addresses)),
            ))
            continue

        definition = definitions[0]
        if definition.address == 0:
            failures.append((
                name,
                "resolved to address 0, which is not a usable injection target.",
            ))
            continue
        if definition.size == 0:
            failures.append((
                name,
                "has size 0 in .symtab, so there is nothing to write into.",
            ))
            continue

        if not declaration.injectable:
            excluded[name] = ResolvedSymbol(
                name=name,
                address=definition.address,
                size=definition.size,
                section=definition.section,
                c_type="",
                signedness_assumed=False,
                unit=declaration.unit,
                minimum=declaration.minimum,
                maximum=declaration.maximum,
            )
            continue

        try:
            c_type, assumed = infer_c_type(definition.size,
                                           declaration.minimum)
        except CTypeInferenceError as exc:
            failures.append((name, str(exc)))
            continue

        if declaration.has_range:
            signed = declaration.minimum < 0
            low, high = _representable_bounds(definition.size, signed)
            if declaration.minimum < low or declaration.maximum > high:
                failures.append((
                    name,
                    "declares range [%d, %d] (line %d of %s) but occupies %d "
                    "byte(s) in the ELF, which holds only [%d, %d] as %s. The "
                    "range is not narrowed to fit: one of the two statements "
                    "is wrong and only the author knows which."
                    % (declaration.minimum, declaration.maximum,
                       declaration.line_number, declared_from,
                       definition.size, low, high, c_type),
                ))
                continue

        symbols[name] = ResolvedSymbol(
            name=name,
            address=definition.address,
            size=definition.size,
            section=definition.section,
            c_type=c_type,
            signedness_assumed=assumed,
            unit=declaration.unit,
            minimum=declaration.minimum,
            maximum=declaration.maximum,
        )

    if failures:
        lines = [
            "%d of %d declared symbol(s) unusable in %s:"
            % (len(failures), len(declarations), elf_path_text),
            "",
        ]
        for name, why in failures:
            lines.append("  %s %s" % (name, why))
        lines.extend(["", _FALSE_NEGATIVE_NOTE, "", _RESOLUTION_HINTS])
        raise SymbolResolutionError("\n".join(lines), failures)

    if not symbols:
        raise SymbolListError(
            "%s declares %d symbol(s) and every one of them is flagged %s, so "
            "the symbol map would contain no injection targets at all. A "
            "campaign run against it would inject nothing and report PASS. "
            "Refusing to emit it."
            % (declared_from, len(declarations), FLAG_NOT_INJECTABLE)
        )

    return SymbolTable(
        elf_path=elf_path_text,
        elf_sha256=sha256_of_file(elf_path),
        declared_from=declared_from,
        symbols=symbols,
        excluded=excluded,
    )


def load_symbols_json(path) -> SymbolTable:
    """Rebuild a SymbolTable from an emitted symbols.json.

    For consumers that range-check at injection time, where the ELF may not be
    on the machine but symbols.json is. Every field is read as written; nothing
    is defaulted.
    """
    path = Path(path)
    try:
        document = json.loads(path.read_text())
    except OSError as exc:
        raise SymbolListError("cannot read %s: %s" % (path, exc)) from exc
    except ValueError as exc:
        raise SymbolListError("%s is not valid JSON: %s" % (path, exc)) from exc

    for key in ("generated_from", "elf_sha256", "symbols"):
        if key in document:
            continue
        raise SymbolListError(
            "%s has no %r key, so it is not a symbol map this module wrote. "
            "Regenerate it with: python -m harness.symbols <elf> "
            "<injectable_symbols.txt> -o %s" % (path, key, path)
        )

    symbols: Dict[str, ResolvedSymbol] = {}
    for name, entry in document["symbols"].items():
        try:
            address = int(str(entry["address"]), 0)
            size = int(entry["size"])
            c_type = entry["c_type"]
        except (KeyError, TypeError, ValueError) as exc:
            raise SymbolListError(
                "%s: entry for %s is malformed (%s). It is not repaired here; "
                "regenerate the file from the ELF." % (path, name, exc)
            ) from exc
        bounds = entry.get("range")
        minimum, maximum = (None, None) if bounds is None else (bounds[0],
                                                                bounds[1])
        symbols[name] = ResolvedSymbol(
            name=name,
            address=address,
            size=size,
            section=entry.get("section", ""),
            c_type=c_type,
            signedness_assumed="c_type_note" in entry,
            unit=entry.get("unit"),
            minimum=minimum,
            maximum=maximum,
        )

    return SymbolTable(
        elf_path=document["generated_from"],
        elf_sha256=document["elf_sha256"],
        declared_from=document.get("declared_from", str(path)),
        symbols=symbols,
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _report(table: SymbolTable, stream) -> None:
    stream.write("Symbol map: %s\n" % table.elf_path)
    stream.write("  declared in : %s\n" % table.declared_from)
    stream.write("  elf sha256  : %s\n" % table.elf_sha256)
    stream.write("  targets     : %d\n" % len(table.symbols))
    if table.excluded:
        stream.write("  gated only  : %d (%s)\n"
                     % (len(table.excluded), FLAG_NOT_INJECTABLE))
    stream.write("\n")
    for name, symbol in table.symbols.items():
        if symbol.has_range:
            declared = "[%d, %d] %s" % (symbol.minimum, symbol.maximum,
                                        symbol.unit)
        else:
            declared = "no range declared -- check_range will refuse it"
        stream.write("  %-24s 0x%08x  size=%-2d  %-9s %-14s %s\n"
                     % (name, symbol.address, symbol.size, symbol.c_type,
                        symbol.section, declared))
    for name, symbol in table.excluded.items():
        stream.write("  %-24s 0x%08x  size=%-2d  %-9s %-14s %s\n"
                     % (name, symbol.address, symbol.size, "-",
                        symbol.section, "gated, not an injection target"))
    stream.write("\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m harness.symbols",
        description=(
            "Resolve declared injection targets against an ELF symbol table "
            "and emit symbols.json."
        ),
        epilog=(
            "With -o the report goes to stdout and the JSON to the file. "
            "Without -o the JSON goes to stdout and the report to stderr. "
            "Exit 0 all resolved, 1 a declared symbol is unusable, 2 the "
            "inputs are unusable."
        ),
    )
    parser.add_argument("elf", help="the linked ELF to read .symtab from")
    parser.add_argument(
        "declarations",
        help="injectable_symbols.txt: the declared targets, units and ranges",
    )
    parser.add_argument("-o", "--output", default=None,
                        help="write symbols.json here instead of stdout")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        table = resolve(args.elf, args.declarations)
    except SymbolResolutionError as exc:
        sys.stderr.write("FAIL: %s\n" % exc)
        return 1
    except (SymbolListError, ElfReadError) as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 2

    if args.output:
        _report(table, sys.stdout)
        written = table.write_json(args.output)
        sys.stdout.write("wrote %s\n" % written)
    else:
        _report(table, sys.stderr)
        json.dump(table.to_json_object(), sys.stdout, indent=2)
        sys.stdout.write("\n")

    assumed = [n for n, s in table.symbols.items() if s.signedness_assumed]
    if assumed:
        sys.stderr.write(
            "NOTE: %d symbol(s) declare no range: %s. Their c_type is the "
            "unsigned form inferred from size alone, and check_range refuses "
            "to validate any value written to them. Give each a unit and both "
            "bounds in %s.\n"
            % (len(assumed), ", ".join(assumed), table.declared_from)
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
