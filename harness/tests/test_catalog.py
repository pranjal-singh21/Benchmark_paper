"""Tests for harness.catalog.

Test files may contain project-shaped data; harness modules may not. The fixture
below is deliberately NOT the real powertrain catalogue: using the real one would
let a change to project data silently change what these tests assert.
"""

import textwrap

import pytest

from harness import catalog


FIXTURE = """
messages:
  - id: 0x111
    name: alpha
    dlc: 8
    sender: node_a
    signals:
      - name: code
        start_bit: 0
        length: 8
        signed: false
        scale: 1
        offset: 0
      - name: wide_unsigned
        start_bit: 8
        length: 24
        signed: false
        scale: 1
        offset: 0
        unit: mV
      - name: current
        start_bit: 32
        length: 16
        signed: true
        scale: 1
        offset: 0
        unit: mA
      - name: counter
        start_bit: 48
        length: 4
        signed: false
      - name: scaled
        start_bit: 56
        length: 8
        signed: false
        scale: 10
        offset: 5

enums:
  code:
    0: NONE
    1: FIRST
    2: SECOND
"""


def write(tmp_path, text, name="catalog.yml"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(text))
    return p


@pytest.fixture()
def cat(tmp_path):
    return catalog.load(write(tmp_path, FIXTURE))


# --------------------------------------------------------------- happy paths
def test_round_trip_every_signal(cat):
    """Encode then decode returns the input, for every signal at once."""
    given = {
        "code": "SECOND",
        "wide_unsigned": 48000,
        "current": -5000,
        "counter": 9,
        "scaled": 405,          # (405 - 5) / 10 = 40, exact
    }
    value, mask = cat.encode(0x111, given)
    data = value.to_bytes(8, "little")
    got = cat.decode(0x111, data)
    for name, expected in given.items():
        assert got[name] == expected, name


def test_signed_negative_round_trips_exactly(cat):
    """-5000 mA in, -5000 mA out. The bug this guards is 16772216."""
    value, _ = cat.encode(0x111, {"current": -5000})
    got = cat.decode(0x111, value.to_bytes(8, "little"))
    assert got["current"] == -5000
    assert got["current"] != 16772216


def test_bit_placement_is_little_endian(cat):
    """48000 at start_bit 8, length 24 lands in bytes 1..3, LSB first."""
    value, mask = cat.encode(0x111, {"wide_unsigned": 48000})
    data = value.to_bytes(8, "little")
    assert data[1] == 0x80 and data[2] == 0xBB and data[3] == 0x00
    assert mask.to_bytes(8, "little")[1:4] == b"\xff\xff\xff"


def test_scale_and_offset_apply_both_ways(cat):
    value, _ = cat.encode(0x111, {"scaled": 405})
    assert value.to_bytes(8, "little")[7] == 40
    assert cat.decode(0x111, value.to_bytes(8, "little"))["scaled"] == 405


def test_enum_resolves_both_directions(cat):
    value, _ = cat.encode(0x111, {"code": "FIRST"})
    assert value.to_bytes(8, "little")[0] == 1
    assert cat.decode(0x111, value.to_bytes(8, "little"))["code"] == "FIRST"


# ------------------------------------------------------- (value, mask), not ==
def test_mask_covers_only_named_signals(cat):
    """The whole reason assertions are (value, mask).

    A rolling counter changes on every transmission. If it were part of the
    match, an assertion on an unrelated signal would fail intermittently against
    frames that are otherwise exactly right.
    """
    value, mask = cat.encode(0x111, {"code": "FIRST"})
    assert mask == 0xFF

    # Two frames differing ONLY in the counter must both satisfy the match.
    a, _ = cat.encode(0x111, {"code": "FIRST", "counter": 1})
    b, _ = cat.encode(0x111, {"code": "FIRST", "counter": 7})
    assert a != b
    assert (a & mask) == (value & mask)
    assert (b & mask) == (value & mask)


# -------------------------------------------------------------- symmetry rule
def test_negative_into_unsigned_is_refused_not_wrapped(cat):
    """encode(-1) into an unsigned 8-bit field must REFUSE, not store 255."""
    with pytest.raises(catalog.ValueRangeError) as exc:
        cat.encode(0x111, {"code": -1})
    assert "code" in str(exc.value)
    assert "0..255" in str(exc.value)

    # The other direction: no byte pattern decodes 'code' to -1, so there is no
    # bit pattern that would have produced the refused value.
    for raw in range(256):
        assert cat.decode(0x111, bytes([raw, 0, 0, 0, 0, 0, 0, 0])).get("code") != -1


def test_too_large_for_field_is_refused(cat):
    with pytest.raises(catalog.ValueRangeError) as exc:
        cat.encode(0x111, {"code": 256})
    assert "256" in str(exc.value) and "0..255" in str(exc.value)


def test_signed_underflow_is_refused(cat):
    with pytest.raises(catalog.ValueRangeError) as exc:
        cat.encode(0x111, {"current": -32769})
    assert "-32768..32767" in str(exc.value)


def test_every_decodable_value_re_encodes(cat):
    """The symmetry rule, exhaustively over two whole fields.

    Anything decode can produce, encode must accept. If this ever fails there is
    a bit pattern on the bus that the engine can observe but cannot express, and
    an assertion against it could never be written.
    """
    for raw in range(256):
        decoded = cat.decode(0x111, bytes([0, 0, 0, 0, 0, 0, 0, raw]))["scaled"]
        again, _ = cat.encode(0x111, {"scaled": decoded})
        assert again.to_bytes(8, "little")[7] == raw

    for raw in range(0, 1 << 16, 97):
        payload = bytes([0, 0, 0, 0]) + raw.to_bytes(2, "little") + bytes([0, 0])
        decoded = cat.decode(0x111, payload)["current"]
        again, _ = cat.encode(0x111, {"current": decoded})
        assert again.to_bytes(8, "little")[4:6] == raw.to_bytes(2, "little")


def test_inexact_scaled_value_is_refused_not_rounded(cat):
    """400 is not on the lattice for scale=10 offset=5; rounding would lie."""
    with pytest.raises(catalog.ValueRangeError) as exc:
        cat.encode(0x111, {"scaled": 400})
    msg = str(exc.value)
    assert "not exactly representable" in msg
    assert "395" in msg and "405" in msg


# ------------------------------------------------------------- load failures
def test_missing_signed_is_a_hard_load_failure(tmp_path):
    text = FIXTURE.replace("""      - name: current
        start_bit: 32
        length: 16
        signed: true
""", """      - name: current
        start_bit: 32
        length: 16
""")
    assert "signed: true" not in text.split("current")[1][:120]
    with pytest.raises(catalog.CatalogLoadError) as exc:
        catalog.load(write(tmp_path, text))
    assert "current" in str(exc.value)
    assert "signed" in str(exc.value)


def test_unknown_message_names_the_known_ones(cat):
    with pytest.raises(catalog.UnknownMessageError) as exc:
        cat.encode(0x999, {"code": 1})
    assert "0x999" in str(exc.value) and "alpha" in str(exc.value)


def test_unknown_signal_names_the_available_ones(cat):
    with pytest.raises(catalog.UnknownSignalError) as exc:
        cat.encode(0x111, {"nope": 1})
    assert "nope" in str(exc.value) and "current" in str(exc.value)


def test_illegal_enum_name_lists_the_legal_set(cat):
    with pytest.raises(catalog.EnumError) as exc:
        cat.encode(0x111, {"code": "NOT_A_CODE"})
    assert "NOT_A_CODE" in str(exc.value)
    assert "FIRST" in str(exc.value) and "SECOND" in str(exc.value)


def test_overlapping_signals_are_refused(tmp_path):
    text = FIXTURE.replace("        start_bit: 8\n        length: 24",
                           "        start_bit: 4\n        length: 24")
    with pytest.raises(catalog.CatalogLoadError) as exc:
        catalog.load(write(tmp_path, text))
    assert "overlap" in str(exc.value)


def test_signal_past_end_of_frame_is_refused(tmp_path):
    text = FIXTURE.replace("        start_bit: 56\n        length: 8",
                           "        start_bit: 60\n        length: 8")
    with pytest.raises(catalog.CatalogLoadError) as exc:
        catalog.load(write(tmp_path, text))
    assert "runs past the end" in str(exc.value)


def test_enum_for_unknown_signal_is_refused(tmp_path):
    text = FIXTURE + "  ghost:\n    0: NOTHING\n"
    with pytest.raises(catalog.CatalogLoadError) as exc:
        catalog.load(write(tmp_path, text))
    assert "ghost" in str(exc.value)


def test_yaml_off_is_not_coerced_to_false(tmp_path):
    """Guards trap 4 end to end: an enum spelled OFF must survive loading."""
    text = FIXTURE.replace("    0: NONE", "    0: OFF")
    c = catalog.load(write(tmp_path, text))
    assert c.enums["code"][0] == "OFF"
    value, _ = c.encode(0x111, {"code": "OFF"})
    assert c.decode(0x111, value.to_bytes(8, "little"))["code"] == "OFF"
