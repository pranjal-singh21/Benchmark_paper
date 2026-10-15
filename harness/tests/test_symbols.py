"""Tests for harness.symbols.

The ELF is never mocked. Most tests build a real ELF32 byte by byte and let
pyelftools parse it, which is what makes it possible to exercise the cases a
compiler will not produce on demand: an undefined reference, a common symbol, a
symbol at address 0, a symbol of size 0, a stripped binary. Two further tests
use real toolchain output when it is present -- a file compiled by the Zephyr
SDK, and the firmware ELF itself -- and skip when it is not, so the suite never
depends on a build tree that is gitignored.

Symbol names, units and paths here are invented for the fixtures. Nothing in
this file is read from, or names anything in, the system under test.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from harness.symbols import (
    ElfReadError,
    NonIntegerValueError,
    OutOfRangeError,
    SymbolListError,
    SymbolResolutionError,
    UndeclaredRangeError,
    UnknownSymbolError,
    infer_c_type,
    load_symbols_json,
    parse_declarations,
    read_elf_symbols,
    resolve,
    sha256_of_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SDK_GCC = Path(
    "/home/djtde/zephyr-sdk-0.16.8/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc"
)


# --------------------------------------------------------------------------
# A real ELF32, built here so the awkward cases can be produced on demand
# --------------------------------------------------------------------------

_E_IDENT = b"\x7fELF" + bytes([1, 1, 1, 0, 0]) + b"\x00" * 7
_BIND = {"local": 0, "global": 1, "weak": 2}
_TYPE = {"notype": 0, "object": 1, "func": 2, "section": 3, "file": 4}
_SPECIAL_SHNDX = {"undef": 0, "abs": 0xFFF1, "common": 0xFFF2}

_BSS_INDEX = 1
_SYM_ENTSIZE = 16
_SHDR_SIZE = 40
_EHDR_SIZE = 52


def _add_string(table: bytearray, text: str) -> int:
    offset = len(table)
    table.extend(text.encode("ascii") + b"\x00")
    return offset


def build_elf32(entries, with_symtab=True, bss_addr=0x20000000,
                bss_size=0x1000) -> bytes:
    """Assemble a little-endian ELF32 (EM_ARM) carrying the given symbols.

    ``entries`` is a list of dicts: ``name`` plus optional ``value``, ``size``,
    ``bind`` (local/global/weak), ``type`` (notype/object/func/section/file) and
    ``shndx`` (a section index, or "undef"/"abs"/"common").
    """
    strtab = bytearray(b"\x00")
    local_syms, global_syms = [], []
    for entry in entries:
        bind = _BIND[entry.get("bind", "global")]
        shndx = entry.get("shndx", _BSS_INDEX)
        if isinstance(shndx, str):
            shndx = _SPECIAL_SHNDX[shndx]
        record = (
            _add_string(strtab, entry["name"]),
            entry.get("value", 0),
            entry.get("size", 0),
            (bind << 4) | _TYPE[entry.get("type", "object")],
            0,
            shndx,
        )
        (local_syms if bind == 0 else global_syms).append(record)

    symbols = [(0, 0, 0, 0, 0, 0)] + local_syms + global_syms
    first_global = 1 + len(local_syms)

    if with_symtab:
        section_names = ["", ".bss", ".symtab", ".strtab", ".shstrtab"]
    else:
        section_names = ["", ".bss", ".shstrtab"]
    shstrtab = bytearray(b"\x00")
    name_offsets = {"": 0}
    for name in section_names[1:]:
        name_offsets[name] = _add_string(shstrtab, name)

    cursor = _EHDR_SIZE
    symtab_offset = cursor
    symtab_size = _SYM_ENTSIZE * len(symbols) if with_symtab else 0
    cursor += symtab_size
    strtab_offset = cursor
    strtab_size = len(strtab) if with_symtab else 0
    cursor += strtab_size
    shstrtab_offset = cursor
    cursor += len(shstrtab)
    cursor += (-cursor) % 4
    shoff = cursor

    shnum = len(section_names)
    headers = [(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)]
    headers.append((name_offsets[".bss"], 8, 0x3, bss_addr, shoff,
                    bss_size, 0, 0, 4, 0))
    if with_symtab:
        headers.append((name_offsets[".symtab"], 2, 0, 0, symtab_offset,
                        symtab_size, 3, first_global, 4, _SYM_ENTSIZE))
        headers.append((name_offsets[".strtab"], 3, 0, 0, strtab_offset,
                        strtab_size, 0, 0, 1, 0))
    headers.append((name_offsets[".shstrtab"], 3, 0, 0, shstrtab_offset,
                    len(shstrtab), 0, 0, 1, 0))

    blob = bytearray()
    blob += struct.pack(
        "<16sHHIIIIIHHHHHH", _E_IDENT, 2, 40, 1, 0x8000, 0, shoff,
        0x5000200, _EHDR_SIZE, 0, 0, _SHDR_SIZE, shnum, shnum - 1,
    )
    if with_symtab:
        for record in symbols:
            blob += struct.pack("<IIIBBH", *record)
        blob += strtab
    blob += shstrtab
    blob += b"\x00" * ((-len(blob)) % 4)
    for header in headers:
        blob += struct.pack("<10I", *header)
    return bytes(blob)


def write_elf(tmp_path, entries, name="fixture.elf", **kwargs) -> Path:
    path = tmp_path / name
    path.write_bytes(build_elf32(entries, **kwargs))
    return path


def write_declarations(tmp_path, text, name="declared_symbols.txt") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


# The three fixture symbols used by most tests: a 4-byte unsigned, a 2-byte
# signed (its negative minimum is what makes it signed) and a 1-byte flag.
THREE_ENTRIES = [
    {"name": "probe_alpha", "value": 0x20000100, "size": 4},
    {"name": "probe_beta", "value": 0x20000104, "size": 2},
    {"name": "probe_gamma", "value": 0x20000106, "size": 1},
]

THREE_DECLARATIONS = (
    "# a comment line is ignored\n"
    "\n"
    "probe_alpha   counts        0    500000\n"
    "probe_beta    milli-units  -400    1500\n"
    "probe_gamma   boolean         0       1\n"
)


@pytest.fixture()
def three_symbol_table(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, THREE_DECLARATIONS)
    return resolve(elf, declarations)


# --------------------------------------------------------------------------
# The fixture builder itself must produce something pyelftools really reads
# --------------------------------------------------------------------------


def test_fixture_elf_is_parsed_by_pyelftools(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    found = read_elf_symbols(elf)
    assert set(found) == {"probe_alpha", "probe_beta", "probe_gamma"}
    alpha = found["probe_alpha"][0]
    assert (alpha.address, alpha.size, alpha.section) == (0x20000100, 4, ".bss")
    assert alpha.has_address


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_all_declared_symbols_resolve(three_symbol_table):
    table = three_symbol_table
    assert sorted(table.names()) == ["probe_alpha", "probe_beta", "probe_gamma"]

    alpha = table["probe_alpha"]
    assert alpha.address == 0x20000100
    assert alpha.size == 4
    assert alpha.c_type == "uint32_t"
    assert alpha.unit == "counts"
    assert (alpha.minimum, alpha.maximum) == (0, 500000)

    # A negative declared minimum is the only thing that makes a type signed.
    assert table["probe_beta"].c_type == "int16_t"
    assert table["probe_gamma"].c_type == "uint8_t"


def test_json_shape_matches_the_declared_contract(three_symbol_table, tmp_path):
    out = three_symbol_table.write_json(tmp_path / "symbols.json")
    document = json.loads(out.read_text())

    assert document["generated_from"].endswith("fixture.elf")
    assert document["symbols"]["probe_alpha"] == {
        "address": "0x20000100",
        "size": 4,
        "c_type": "uint32_t",
        "unit": "counts",
        "range": [0, 500000],
    }
    assert document["symbols"]["probe_beta"]["range"] == [-400, 1500]


def test_emitted_json_carries_the_elf_sha256(tmp_path, three_symbol_table):
    elf = tmp_path / "fixture.elf"
    expected = hashlib.sha256(elf.read_bytes()).hexdigest()

    assert three_symbol_table.elf_sha256 == expected
    assert sha256_of_file(elf) == expected

    document = json.loads(
        three_symbol_table.write_json(tmp_path / "symbols.json").read_text()
    )
    assert document["elf_sha256"] == expected
    assert len(document["elf_sha256"]) == 64


def test_a_changed_elf_changes_the_recorded_sha256(tmp_path):
    declarations = write_declarations(tmp_path, THREE_DECLARATIONS)
    first = resolve(write_elf(tmp_path, THREE_ENTRIES, name="a.elf"),
                    declarations)
    moved = [dict(e) for e in THREE_ENTRIES]
    moved[0]["value"] = 0x20000200
    second = resolve(write_elf(tmp_path, moved, name="b.elf"), declarations)

    assert first.elf_sha256 != second.elf_sha256
    assert second["probe_alpha"].address == 0x20000200


# --------------------------------------------------------------------------
# The --gc-sections guard
# --------------------------------------------------------------------------


def test_symbol_absent_from_the_elf_is_a_hard_failure_naming_it(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path,
        THREE_DECLARATIONS + "probe_collected  counts  0  10\n",
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    assert excinfo.value.symbols == ["probe_collected"]
    message = str(excinfo.value)
    assert "probe_collected" in message
    assert "absent" in message
    # The reason a stopped run is preferable to a guessed address is stated,
    # because the message is what a later reader acts on.
    assert "false negative" in message
    assert "--gc-sections" in message


def test_undefined_reference_is_not_a_definition(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_elsewhere", "value": 0,
                          "size": 0, "shndx": "undef", "type": "notype"}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_elsewhere  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    message = str(excinfo.value)
    assert "probe_elsewhere" in message
    assert "SHN_UNDEF" in message


def test_common_symbol_has_no_address_and_is_refused(tmp_path):
    # st_value of a common symbol is its alignment. Accepting it would hand
    # the harness a small integer that looks exactly like an address.
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_tentative", "value": 4, "size": 4,
                          "shndx": "common"}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_tentative  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    message = str(excinfo.value)
    assert "probe_tentative" in message
    assert "alignment" in message


def test_address_zero_is_refused_naming_the_symbol(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_at_zero", "value": 0, "size": 4}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_at_zero  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    assert excinfo.value.symbols == ["probe_at_zero"]
    assert "address 0" in str(excinfo.value)


def test_size_zero_is_refused_naming_the_symbol(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_empty", "value": 0x20000200,
                          "size": 0}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_empty  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    assert excinfo.value.symbols == ["probe_empty"]
    assert "size 0" in str(excinfo.value)


def test_every_failing_symbol_is_named_not_only_the_first(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_empty", "value": 0x20000200,
                          "size": 0}],
    )
    declarations = write_declarations(
        tmp_path,
        THREE_DECLARATIONS
        + "probe_empty      counts  0  10\n"
        + "probe_collected  counts  0  10\n",
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    assert excinfo.value.symbols == ["probe_empty", "probe_collected"]


def test_two_definitions_of_one_name_are_ambiguous_not_a_coin_flip(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [
            {"name": "probe_twice", "value": 0x20000300, "size": 4,
             "bind": "local"},
            {"name": "probe_twice", "value": 0x20000400, "size": 4},
        ],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_twice  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    message = str(excinfo.value)
    assert "probe_twice" in message
    assert "0x20000300" in message and "0x20000400" in message


# --------------------------------------------------------------------------
# The not-injectable flag
# --------------------------------------------------------------------------


def test_flag_column_excludes_the_symbol_from_symbols_json(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_canary", "value": 0x20000200,
                          "size": 4}],
    )
    declarations = write_declarations(
        tmp_path,
        THREE_DECLARATIONS
        + "probe_canary  none  0  4294967295  not-injectable\n",
    )
    table = resolve(elf, declarations)
    document = json.loads(
        table.write_json(tmp_path / "symbols.json").read_text()
    )

    assert "probe_canary" not in document["symbols"]
    assert "probe_canary" not in table
    assert sorted(document["symbols"]) == ["probe_alpha", "probe_beta",
                                           "probe_gamma"]
    # It was still gated: the tool knows it and knows why it is not a target.
    assert table.excluded["probe_canary"].address == 0x20000200


def test_bare_line_may_carry_the_flag_without_a_range(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_canary", "value": 0x20000200,
                          "size": 4}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_canary  not-injectable\n"
    )
    table = resolve(elf, declarations)

    assert "probe_canary" not in table
    assert "probe_canary" in table.excluded


def test_a_not_injectable_symbol_is_still_gated_in_the_elf(tmp_path):
    # This is the point of the flag: the symbol is force-kept at link time so
    # that its disappearance proves the guard fires. Excluding it from the
    # injection set must not exclude it from the check.
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path,
        THREE_DECLARATIONS
        + "probe_canary  none  0  4294967295  not-injectable\n",
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    assert excinfo.value.symbols == ["probe_canary"]


def test_asking_a_flagged_symbol_for_a_range_says_why_not(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_canary", "value": 0x20000200,
                          "size": 4}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_canary  not-injectable\n"
    )
    table = resolve(elf, declarations)

    with pytest.raises(UnknownSymbolError) as excinfo:
        table.check_range("probe_canary", 1)
    assert "probe_canary" in str(excinfo.value)
    assert "not-injectable" in str(excinfo.value)


# --------------------------------------------------------------------------
# The bare (pre-migration) form
# --------------------------------------------------------------------------


def test_bare_line_loads_with_no_unit_and_no_range(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path, "probe_alpha\nprobe_beta\nprobe_gamma\n"
    )
    table = resolve(elf, declarations)

    alpha = table["probe_alpha"]
    assert alpha.unit is None
    assert alpha.minimum is None and alpha.maximum is None
    # Signedness is unknown without a range, so the unsigned form is reported
    # and the assumption is recorded rather than hidden.
    assert alpha.c_type == "uint32_t"
    assert table["probe_beta"].c_type == "uint16_t"

    entry = alpha.to_json_object()
    assert entry["unit"] is None
    assert entry["range"] is None
    assert "signedness is unknown" in entry["c_type_note"]


def test_bare_and_declared_forms_mix_in_one_file(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path,
        "probe_alpha  counts  0  500000\n"
        "probe_beta\n"
        "probe_gamma  boolean  0  1\n",
    )
    table = resolve(elf, declarations)

    assert table["probe_alpha"].unit == "counts"
    assert table["probe_beta"].unit is None
    assert table["probe_gamma"].maximum == 1


# --------------------------------------------------------------------------
# check_range
# --------------------------------------------------------------------------


def test_check_range_returns_the_value_unchanged_at_both_bounds(
        three_symbol_table):
    table = three_symbol_table
    assert table.check_range("probe_alpha", 0) == 0
    assert table.check_range("probe_alpha", 500000) == 500000
    assert table.check_range("probe_beta", -400) == -400
    assert table.check_range("probe_beta", 1500) == 1500
    assert table.check_range("probe_gamma", 1) == 1


@pytest.mark.parametrize("value", [500001, -1, 10 ** 9])
def test_check_range_refuses_out_of_range_quoting_value_and_both_bounds(
        three_symbol_table, value):
    with pytest.raises(OutOfRangeError) as excinfo:
        three_symbol_table.check_range("probe_alpha", value)

    message = str(excinfo.value)
    assert "probe_alpha" in message
    assert str(value) in message
    assert "0" in message and "500000" in message
    assert excinfo.value.minimum == 0
    assert excinfo.value.maximum == 500000
    assert excinfo.value.value == value


def test_check_range_never_truncates_into_the_valid_range(three_symbol_table):
    # A clamped write is indistinguishable from a planted firmware defect: the
    # campaign would credit a detection to a fault it never injected.
    with pytest.raises(OutOfRangeError):
        three_symbol_table.check_range("probe_gamma", 2)
    assert three_symbol_table["probe_gamma"].maximum == 1


def test_check_range_refuses_when_no_range_is_declared(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, "probe_alpha\n")
    table = resolve(elf, declarations)

    with pytest.raises(UndeclaredRangeError) as excinfo:
        table.check_range("probe_alpha", 7)

    message = str(excinfo.value)
    assert "probe_alpha" in message
    assert "declares no range" in message
    # Emphatically not "allow anything": even an obviously fine value fails.
    with pytest.raises(UndeclaredRangeError):
        table.check_range("probe_alpha", 0)


def test_check_range_refuses_an_unknown_symbol(three_symbol_table):
    with pytest.raises(UnknownSymbolError) as excinfo:
        three_symbol_table.check_range("probe_never_declared", 1)
    assert "probe_never_declared" in str(excinfo.value)


def test_check_range_refuses_a_non_integer_value(three_symbol_table):
    for value in (1.5, "1", None, True):
        with pytest.raises(NonIntegerValueError):
            three_symbol_table.check_range("probe_alpha", value)


# --------------------------------------------------------------------------
# Declaration file parsing
# --------------------------------------------------------------------------


def test_comments_and_blank_lines_are_ignored(tmp_path):
    declarations = write_declarations(
        tmp_path,
        "# header\n\n   \nprobe_alpha  counts  0  1\n\n#  trailer\n",
    )
    parsed = parse_declarations(declarations)
    assert list(parsed) == ["probe_alpha"]
    assert parsed["probe_alpha"].line_number == 4


def test_bounds_may_be_hexadecimal_or_zero_padded(tmp_path):
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  0x10  0xff\nprobe_beta  counts  007  09\n"
    )
    parsed = parse_declarations(declarations)
    assert (parsed["probe_alpha"].minimum, parsed["probe_alpha"].maximum) == (
        16, 255)
    assert (parsed["probe_beta"].minimum, parsed["probe_beta"].maximum) == (7, 9)


def test_three_column_line_is_refused_naming_the_line(tmp_path):
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  0\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)

    message = str(excinfo.value)
    assert "line 1" in message
    assert "probe_alpha" in message
    assert "3 columns" in message


def test_two_column_line_that_is_not_the_flag_is_refused(tmp_path):
    declarations = write_declarations(tmp_path, "probe_alpha  counts\n")
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)
    assert "2 columns" in str(excinfo.value)


def test_unknown_flag_is_refused_naming_the_flag(tmp_path):
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  0  1  not-injectible\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)

    message = str(excinfo.value)
    assert "not-injectible" in message
    assert "not-injectable" in message


def test_non_integer_bound_is_refused_naming_the_bound(tmp_path):
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  0  five\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)

    message = str(excinfo.value)
    assert "maximum" in message
    assert "five" in message


def test_reversed_range_is_refused_not_swapped(tmp_path):
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  100  10\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)
    assert "greater than maximum" in str(excinfo.value)


def test_duplicate_declaration_names_both_line_numbers(tmp_path):
    declarations = write_declarations(
        tmp_path,
        "probe_alpha  counts  0  10\n"
        "probe_beta   counts  0  10\n"
        "probe_alpha  counts  0  20\n",
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)

    message = str(excinfo.value)
    assert "probe_alpha" in message
    assert "line 1" in message and "line 3" in message


def test_trailing_comment_is_not_stripped_but_refused(tmp_path):
    # The build system reads this same file and would not strip it either, so
    # accepting it here would let the two readers disagree about a line.
    declarations = write_declarations(
        tmp_path, "probe_alpha  counts  0  10  # a note\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        parse_declarations(declarations)
    assert "columns" in str(excinfo.value)


def test_empty_declaration_file_is_refused(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, "# nothing declared here\n")
    with pytest.raises(SymbolListError) as excinfo:
        resolve(elf, declarations)
    assert "declares no symbols" in str(excinfo.value)


def test_a_file_of_only_flagged_symbols_is_refused(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path, "probe_alpha  not-injectable\nprobe_beta  not-injectable\n"
    )
    with pytest.raises(SymbolListError) as excinfo:
        resolve(elf, declarations)
    assert "no injection targets" in str(excinfo.value)


def test_missing_declaration_file_is_refused(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    with pytest.raises(SymbolListError) as excinfo:
        resolve(elf, tmp_path / "not_here.txt")
    assert "not_here.txt" in str(excinfo.value)


# --------------------------------------------------------------------------
# C type inference
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size,minimum,expected",
    [
        (1, 0, "uint8_t"), (2, 0, "uint16_t"),
        (4, 0, "uint32_t"), (8, 0, "uint64_t"),
        (1, -1, "int8_t"), (2, -400, "int16_t"),
        (4, -1, "int32_t"), (8, -1, "int64_t"),
    ],
)
def test_c_type_comes_from_size_and_declared_signedness(size, minimum, expected):
    c_type, assumed = infer_c_type(size, minimum)
    assert c_type == expected
    assert assumed is False


def test_c_type_without_a_range_is_unsigned_and_flagged_as_assumed():
    c_type, assumed = infer_c_type(4, None)
    assert c_type == "uint32_t"
    assert assumed is True


def test_a_size_with_no_scalar_type_is_a_failure_naming_the_symbol(tmp_path):
    elf = write_elf(
        tmp_path,
        THREE_ENTRIES + [{"name": "probe_struct", "value": 0x20000200,
                          "size": 12}],
    )
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_struct  counts  0  10\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    message = str(excinfo.value)
    assert "probe_struct" in message
    assert "size 12" in message


def test_a_range_too_wide_for_the_elf_size_is_a_failure_not_a_narrowing(
        tmp_path):
    elf = write_elf(tmp_path, [{"name": "probe_small", "value": 0x20000200,
                                "size": 1}])
    declarations = write_declarations(
        tmp_path, "probe_small  counts  0  500000\n"
    )
    with pytest.raises(SymbolResolutionError) as excinfo:
        resolve(elf, declarations)

    message = str(excinfo.value)
    assert "probe_small" in message
    assert "500000" in message
    assert "[0, 255]" in message


def test_a_signed_range_that_fits_is_accepted(tmp_path):
    elf = write_elf(tmp_path, [{"name": "probe_small", "value": 0x20000200,
                                "size": 1}])
    declarations = write_declarations(
        tmp_path, "probe_small  counts  -128  127\n"
    )
    table = resolve(elf, declarations)
    assert table["probe_small"].c_type == "int8_t"


# --------------------------------------------------------------------------
# Unusable ELFs
# --------------------------------------------------------------------------


def test_a_stripped_elf_is_refused(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES, with_symtab=False)
    with pytest.raises(ElfReadError) as excinfo:
        read_elf_symbols(elf)
    assert ".symtab" in str(excinfo.value)


def test_a_file_that_is_not_an_elf_is_refused(tmp_path):
    path = tmp_path / "not.elf"
    path.write_bytes(b"MZ this is not an ELF at all")
    with pytest.raises(ElfReadError) as excinfo:
        read_elf_symbols(path)
    assert "not a readable ELF" in str(excinfo.value)


def test_a_missing_elf_is_refused(tmp_path):
    with pytest.raises(ElfReadError) as excinfo:
        read_elf_symbols(tmp_path / "absent.elf")
    assert "absent.elf" in str(excinfo.value)


# --------------------------------------------------------------------------
# symbols.json as an input
# --------------------------------------------------------------------------


def test_symbols_json_round_trips_and_still_range_checks(tmp_path,
                                                         three_symbol_table):
    out = three_symbol_table.write_json(tmp_path / "symbols.json")
    reloaded = load_symbols_json(out)

    assert reloaded.elf_sha256 == three_symbol_table.elf_sha256
    assert reloaded["probe_alpha"].address == 0x20000100
    assert reloaded["probe_alpha"].c_type == "uint32_t"
    assert reloaded.check_range("probe_beta", -400) == -400
    with pytest.raises(OutOfRangeError):
        reloaded.check_range("probe_beta", -401)


def test_a_json_document_without_a_sha256_is_refused(tmp_path):
    path = tmp_path / "symbols.json"
    path.write_text(json.dumps({"generated_from": "x", "symbols": {}}))
    with pytest.raises(SymbolListError) as excinfo:
        load_symbols_json(path)
    assert "elf_sha256" in str(excinfo.value)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run_cli(*args):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "harness.symbols", *[str(a) for a in args]],
        cwd=str(REPO_ROOT), env=environment, capture_output=True, text=True,
    )


def test_cli_writes_symbols_json_and_exits_zero(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, THREE_DECLARATIONS)
    out = tmp_path / "symbols.json"

    result = run_cli(elf, declarations, "-o", out)

    assert result.returncode == 0, result.stderr
    document = json.loads(out.read_text())
    assert len(document["elf_sha256"]) == 64
    assert sorted(document["symbols"]) == ["probe_alpha", "probe_beta",
                                           "probe_gamma"]
    assert "probe_alpha" in result.stdout


def test_cli_without_o_writes_the_json_to_stdout(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, THREE_DECLARATIONS)

    result = run_cli(elf, declarations)

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["symbols"]["probe_gamma"]["c_type"] == "uint8_t"


def test_cli_exits_one_naming_the_collected_symbol(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(
        tmp_path, THREE_DECLARATIONS + "probe_collected  counts  0  10\n"
    )
    out = tmp_path / "symbols.json"

    result = run_cli(elf, declarations, "-o", out)

    assert result.returncode == 1
    assert "probe_collected" in result.stderr
    assert "--gc-sections" in result.stderr
    # Nothing is written when the map would be incomplete: a half-written
    # symbols.json is a map of targets a later run would trust.
    assert not out.exists()


def test_cli_exits_two_on_an_unusable_input(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    result = run_cli(elf, tmp_path / "absent.txt")
    assert result.returncode == 2
    assert "absent.txt" in result.stderr


def test_cli_notes_symbols_that_declared_no_range(tmp_path):
    elf = write_elf(tmp_path, THREE_ENTRIES)
    declarations = write_declarations(tmp_path, "probe_alpha\n")
    result = run_cli(elf, declarations, "-o", tmp_path / "symbols.json")

    assert result.returncode == 0
    assert "probe_alpha" in result.stderr
    assert "check_range refuses" in result.stderr


# --------------------------------------------------------------------------
# Real toolchain output, when this machine has it
# --------------------------------------------------------------------------


@pytest.mark.skipif(not SDK_GCC.is_file(),
                    reason="Zephyr SDK gcc not installed on this machine")
def test_reads_an_elf_produced_by_the_real_compiler(tmp_path):
    source = tmp_path / "fixture.c"
    source.write_text(
        "volatile unsigned int probe_alpha = 1u;\n"
        "volatile signed short probe_beta = -1;\n"
        "volatile unsigned char probe_gamma = 1u;\n"
    )
    elf = tmp_path / "compiled.elf"
    build = subprocess.run(
        [str(SDK_GCC), "-nostdlib", "-nostartfiles", "-Wl,-Ttext=0x8000",
         "-Wl,--entry=0x8000", str(source), "-o", str(elf)],
        capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr

    declarations = write_declarations(tmp_path, THREE_DECLARATIONS)
    table = resolve(elf, declarations)

    assert sorted(table.names()) == ["probe_alpha", "probe_beta", "probe_gamma"]
    assert table["probe_alpha"].size == 4
    assert table["probe_alpha"].c_type == "uint32_t"
    assert table["probe_beta"].c_type == "int16_t"
    assert all(symbol.address != 0 for symbol in table.symbols.values())
    assert table.elf_sha256 == hashlib.sha256(elf.read_bytes()).hexdigest()


def _firmware_elfs():
    return sorted(REPO_ROOT.glob("systems/*/firmware/*/build/zephyr/zephyr.elf"))


@pytest.mark.skipif(not _firmware_elfs(),
                    reason="no built firmware ELF on this machine "
                           "(build trees are gitignored)")
def test_reads_a_real_firmware_elf_without_special_casing_it():
    # Only the reader is exercised, not resolution: which symbols a local build
    # happens to contain is not this module's contract.
    elf = _firmware_elfs()[0]
    found = read_elf_symbols(elf)

    assert len(found) > 100
    for entries in found.values():
        for entry in entries:
            assert isinstance(entry.address, int)
            assert isinstance(entry.size, int)
            assert isinstance(entry.section, str) and entry.section
    assert len(sha256_of_file(elf)) == 64
