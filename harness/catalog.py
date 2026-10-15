"""The CAN contract: message and signal definitions, and bit-level codec.

This module contains no message identifiers, no signal names and no enum
spellings. All of that lives in the catalogue file it loads. The engine reads the
contract; it never knows it.

Bit layout
----------
Little-endian, Intel style. The eight data bytes are read as a single 64-bit
little-endian integer: byte ``N`` occupies bits ``8N..8N+7``, and within a byte
bit 0 is the least significant. ``start_bit`` is the position of the signal's
least significant bit in that integer, and a signal of length ``L`` occupies bits
``start_bit .. start_bit + L - 1``.

Worked example. A signal at ``start_bit: 8``, ``length: 24`` occupies bits 8..31,
which is bytes 1, 2 and 3, least significant byte first. Given the value
``0x0BB80`` (48000)::

    byte:   0     1     2     3     4     5     6     7
    data:  --    80    BB    00    --    --    --    --

Two rules that exist because of specific defects
------------------------------------------------
**``signed:`` is mandatory.** A signal without it is a hard load failure, never a
default. Inferring signedness from a name, a unit or a comment puts project
knowledge into the engine, and it produces exactly the bug where a current of
-5000 mA reads back as 16772216.

**Encoding produces a (value, mask) pair, never an equality.** A real status
frame carries a rolling counter that changes on every transmission. An assertion
matched by equality would fail intermittently on frames that are otherwise
exactly right, and the time would be spent blaming the firmware.

The symmetry rule
-----------------
If :func:`encode` refuses a value, :func:`decode` must refuse the corresponding
bits, and vice versa. Encoding -1 into an unsigned 8-bit field is therefore
REFUSED rather than silently stored as 255. Without that rule an assertion can
pass against a value nobody ever wrote, which is a false positive in a
verification tool.

Exactness is enforced for the same reason. Scale and offset are handled as exact
rationals, and a physical value that is not exactly representable in the field is
refused rather than rounded. A rounded value that then decodes to something
slightly different is the same failure wearing different clothes.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from harness import yaml_strict


class CatalogError(Exception):
    """Base for every failure raised by this module."""


class CatalogLoadError(CatalogError):
    """The catalogue file is malformed or incomplete."""


class UnknownMessageError(CatalogError):
    """A message identifier is not in the catalogue."""


class UnknownSignalError(CatalogError):
    """A signal is not defined on the message it was requested for."""


class EnumError(CatalogError):
    """An enum name or value is not legal for the signal."""


class ValueRangeError(CatalogError):
    """A value does not fit the field, or is not exactly representable in it."""


def _quoted(values) -> str:
    return ", ".join(repr(v) for v in values) if values else "(none)"


def _as_fraction(value, what: str, where: str) -> Fraction:
    """Exact rational from an int, a float or a string.

    ``Fraction(str(x))`` rather than ``Fraction(x)``: the former reads 0.1 as one
    tenth, the latter as the binary double nearest to it. Rounding differences
    that small still produce off-by-one raw values at the edges of a field.
    """
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError, TypeError):
        raise CatalogLoadError(
            "%s: %s must be a number, got %r" % (where, what, value)
        ) from None


class Signal:
    """One signal's placement and interpretation within a message."""

    __slots__ = ("name", "start_bit", "length", "signed", "scale", "offset", "unit")

    def __init__(self, name, start_bit, length, signed, scale, offset, unit):
        self.name = name
        self.start_bit = start_bit
        self.length = length
        self.signed = signed
        self.scale = scale        # Fraction
        self.offset = offset      # Fraction
        self.unit = unit

    @property
    def raw_bounds(self) -> Tuple[int, int]:
        """The inclusive range of raw values the field can hold."""
        if self.signed:
            half = 1 << (self.length - 1)
            return -half, half - 1
        return 0, (1 << self.length) - 1

    def physical_bounds(self) -> Tuple[Fraction, Fraction]:
        lo, hi = self.raw_bounds
        a = lo * self.scale + self.offset
        b = hi * self.scale + self.offset
        return (a, b) if a <= b else (b, a)

    def to_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "start_bit": self.start_bit,
            "length": self.length,
            "signed": self.signed,
            "scale": _num(self.scale),
            "offset": _num(self.offset),
            "unit": self.unit,
        }


class Message:
    __slots__ = ("id", "name", "dlc", "sender", "signals")

    def __init__(self, id_, name, dlc, sender, signals):
        self.id = id_
        self.name = name
        self.dlc = dlc
        self.sender = sender
        self.signals: Dict[str, Signal] = signals

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "dlc": self.dlc,
            "sender": self.sender,
            "signals": [s.to_json() for s in self.signals.values()],
        }


def _num(frac: Fraction):
    """Render an exact rational as int when whole, else as float, for JSON."""
    return int(frac) if frac.denominator == 1 else float(frac)


def _require_bool(value, key: str, where: str) -> bool:
    """Parse a value the SCHEMA declares to be boolean.

    This is not inference and it is not the coercion the strict loader exists to
    prevent. Those are different things, and the difference is what makes this
    safe:

      - The strict loader refuses to guess at SIGNAL VALUES. A signal whose value
        is written ``OFF`` is a three-letter name in some enum, and YAML 1.1
        turning it into ``False`` silently corrupts the contract. Nothing in the
        document says what type that value has, so guessing is the bug.

      - This function parses a value at a position where the schema has already
        declared the type. ``signed:`` is boolean by definition of the format, so
        reading the text ``false`` as ``False`` is parsing, not guessing.

    Anything that is neither a boolean nor one of the two spellings is refused,
    naming what was found. There is deliberately no truthiness: 1, 0, "yes",
    "off" and "" are all rejected rather than interpreted.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    raise CatalogLoadError(
        "%s: '%s' must be true or false, got %r (%s).\n"
        "This project loads YAML with a strict loader that does not turn any "
        "plain scalar into a boolean, so the value arrives as written. Write it "
        "as 'true' or 'false'; 1, 0, yes and on are not accepted."
        % (where, key, value, type(value).__name__))


def _require(mapping, key, where):
    if key not in mapping:
        raise CatalogLoadError("%s: missing required key '%s'" % (where, key))
    return mapping[key]


def _parse_signal(raw, where: str) -> Signal:
    if not isinstance(raw, Mapping):
        raise CatalogLoadError("%s: each signal must be a mapping, got %s"
                               % (where, type(raw).__name__))

    name = _require(raw, "name", where)
    if not isinstance(name, str) or not name:
        raise CatalogLoadError("%s: signal 'name' must be a non-empty string, got %r"
                               % (where, name))

    here = "%s, signal '%s'" % (where, name)

    # signed is MANDATORY. This is the whole point; see the module docstring.
    if "signed" not in raw:
        raise CatalogLoadError(
            "%s: 'signed:' is missing.\n"
            "It is mandatory on every signal and has no default. Signedness "
            "cannot be inferred from the name, the unit or a comment: guessing "
            "it puts project knowledge into the engine and produces the bug "
            "where a negative value reads back as a large positive one.\n"
            "Add 'signed: true' or 'signed: false' to this signal." % here)
    signed = _require_bool(raw["signed"], "signed", here)

    start_bit = _require(raw, "start_bit", here)
    length = _require(raw, "length", here)
    for label, val in (("start_bit", start_bit), ("length", length)):
        if not isinstance(val, int) or isinstance(val, bool):
            raise CatalogLoadError("%s: '%s' must be an integer, got %r"
                                   % (here, label, val))
    if length <= 0:
        raise CatalogLoadError("%s: 'length' must be positive, got %d" % (here, length))
    if start_bit < 0:
        raise CatalogLoadError("%s: 'start_bit' must not be negative, got %d"
                               % (here, start_bit))
    if signed and length < 2:
        raise CatalogLoadError(
            "%s: a signed signal needs at least 2 bits; a 1-bit signed field can "
            "only represent 0 and -1, which is never what is meant." % here)

    scale = _as_fraction(raw.get("scale", 1), "scale", here)
    if scale == 0:
        raise CatalogLoadError("%s: 'scale' must not be zero; no value could be "
                               "recovered on decode." % here)
    offset = _as_fraction(raw.get("offset", 0), "offset", here)

    return Signal(name, start_bit, length, signed, scale, offset, raw.get("unit"))


class Catalog:
    """A loaded CAN contract."""

    def __init__(self, messages: Dict[int, Message], enums: Dict[str, Dict[int, str]],
                 source: Optional[str] = None):
        self._by_id = messages
        self._by_name = {m.name: m for m in messages.values()}
        self.enums = enums
        # name -> value, for encoding. Built once and checked for collisions.
        self._enum_to_value: Dict[str, Dict[str, int]] = {}
        for sig_name, mapping in enums.items():
            rev: Dict[str, int] = {}
            for value, label in mapping.items():
                if label in rev:
                    raise CatalogLoadError(
                        "enum '%s': the name %r is used for both value %d and %d. "
                        "Encoding it would be ambiguous."
                        % (sig_name, label, rev[label], value))
                rev[label] = value
            self._enum_to_value[sig_name] = rev
        self.source = source

    # ------------------------------------------------------------------ lookup
    def message(self, key) -> Message:
        if isinstance(key, str) and key in self._by_name:
            return self._by_name[key]
        if isinstance(key, int) and key in self._by_id:
            return self._by_id[key]
        known = ", ".join("0x%X (%s)" % (m.id, m.name)
                          for m in sorted(self._by_id.values(), key=lambda m: m.id))
        shown = ("0x%X" % key) if isinstance(key, int) else repr(key)
        raise UnknownMessageError(
            "message %s is not in the catalogue.\nMessages defined: %s"
            % (shown, known or "(none)"))

    def messages(self) -> List[Message]:
        return [self._by_id[k] for k in sorted(self._by_id)]

    def signal(self, message_key, signal_name: str) -> Signal:
        msg = self.message(message_key)
        if signal_name not in msg.signals:
            raise UnknownSignalError(
                "signal '%s' is not defined on message 0x%X (%s).\n"
                "Signals on that message: %s"
                % (signal_name, msg.id, msg.name, _quoted(msg.signals)))
        return msg.signals[signal_name]

    # ------------------------------------------------------------------ codec
    def _raw_from_physical(self, sig: Signal, value, msg: Message) -> int:
        """Physical (or enum name) to the raw field value, exactly."""
        here = "message 0x%X (%s), signal '%s'" % (msg.id, msg.name, sig.name)

        # Enum names resolve first, and resolve to a RAW value: an enum is a
        # label for a bit pattern, not a physical quantity to be scaled.
        if isinstance(value, str):
            table = self._enum_to_value.get(sig.name)
            if table is None:
                raise EnumError(
                    "%s: got the name %r, but this signal has no enum. Pass a "
                    "number." % (here, value))
            if value not in table:
                raise EnumError(
                    "%s: %r is not a legal value.\nLegal values: %s"
                    % (here, value, _quoted(sorted(table))))
            raw = table[value]
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueRangeError(
                    "%s: value must be a number or an enum name, got %r (%s)"
                    % (here, value, type(value).__name__))
            phys = _as_fraction(value, "value", here)
            quotient = (phys - sig.offset) / sig.scale
            if quotient.denominator != 1:
                lo, hi = sig.physical_bounds()
                raise ValueRangeError(
                    "%s: %s is not exactly representable.\n"
                    "  scale=%s offset=%s, so the value must satisfy "
                    "(value - offset) / scale being a whole number.\n"
                    "  nearest representable values: %s and %s\n"
                    "Rounding it here would put a different value on the bus "
                    "from the one asked for, and an assertion could then pass "
                    "against a value nobody wrote."
                    % (here, value, _num(sig.scale), _num(sig.offset),
                       _num((quotient.numerator // quotient.denominator) * sig.scale + sig.offset),
                       _num((quotient.numerator // quotient.denominator + 1) * sig.scale + sig.offset)))
            raw = int(quotient)

        lo, hi = sig.raw_bounds
        if not (lo <= raw <= hi):
            plo, phi = sig.physical_bounds()
            raise ValueRangeError(
                "%s: %s does not fit the field.\n"
                "  field: %d bits, %s\n"
                "  raw value would be %d, representable raw range is %d..%d\n"
                "  representable physical range is %s..%s\n"
                "The value is refused rather than truncated. A truncated write "
                "looks exactly like a firmware defect."
                % (here, value, sig.length, "signed" if sig.signed else "unsigned",
                   raw, lo, hi, _num(plo), _num(phi)))
        return raw

    def encode(self, message_key, signals: Mapping[str, Any]) -> Tuple[int, int]:
        """Signals to a ``(value, mask)`` pair over the 64-bit frame payload.

        Only the bits of the named signals appear in the mask, so a signal the
        caller did not mention -- a sequence counter, an integrity byte -- is not part
        the match. That is what makes an assertion survive a changing frame.
        """
        msg = self.message(message_key)
        value = 0
        mask = 0
        for name, raw_value in signals.items():
            sig = self.signal(msg.id, name)
            raw = self._raw_from_physical(sig, raw_value, msg)
            field_mask = (1 << sig.length) - 1
            value |= (raw & field_mask) << sig.start_bit
            mask |= field_mask << sig.start_bit
        return value, mask

    def decode(self, message_key, data) -> Dict[str, Any]:
        """Frame payload to physical values, one entry per signal on the message."""
        msg = self.message(message_key)

        if isinstance(data, int):
            payload = data
            width = msg.dlc
        else:
            payload = int.from_bytes(bytes(data), "little")
            width = len(bytes(data))
            if width != msg.dlc:
                raise ValueRangeError(
                    "message 0x%X (%s) is declared dlc=%d but %d bytes were given."
                    % (msg.id, msg.name, msg.dlc, width))

        out: Dict[str, Any] = {}
        for name, sig in msg.signals.items():
            field_mask = (1 << sig.length) - 1
            raw = (payload >> sig.start_bit) & field_mask
            if sig.signed and (raw >> (sig.length - 1)) & 1:
                raw -= 1 << sig.length

            table = self.enums.get(sig.name)
            if table is not None and raw in table:
                out[name] = table[raw]
                continue

            phys = raw * sig.scale + sig.offset
            out[name] = _num(phys)
        return out

    # ------------------------------------------------------------------ output
    def to_json(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "messages": [m.to_json() for m in self.messages()],
            "enums": {k: {str(i): v for i, v in sorted(t.items())}
                      for k, t in sorted(self.enums.items())},
        }


def _parse_enums(raw, where) -> Dict[str, Dict[int, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise CatalogLoadError("%s: 'enums' must be a mapping, got %s"
                               % (where, type(raw).__name__))
    out: Dict[str, Dict[int, str]] = {}
    for sig_name, table in raw.items():
        if not isinstance(table, Mapping):
            raise CatalogLoadError("%s: enum '%s' must be a mapping of value to "
                                   "name, got %s" % (where, sig_name, type(table).__name__))
        parsed: Dict[int, str] = {}
        for value, label in table.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise CatalogLoadError(
                    "%s: enum '%s' has a non-integer key %r. Keys are raw field "
                    "values." % (where, sig_name, value))
            if not isinstance(label, str) or not label:
                raise CatalogLoadError(
                    "%s: enum '%s' value %d maps to %r; it must be a non-empty "
                    "name." % (where, sig_name, value, label))
            parsed[value] = label
        out[sig_name] = parsed
    return out


def load(path) -> Catalog:
    """Load a catalogue, refusing anything ambiguous."""
    p = Path(path)
    doc = yaml_strict.load_file(p)
    where = str(p)

    if not isinstance(doc, Mapping):
        raise CatalogLoadError("%s: the catalogue must be a mapping at the top "
                               "level, got %s" % (where, type(doc).__name__))

    raw_messages = doc.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise CatalogLoadError("%s: 'messages' must be a non-empty list." % where)

    messages: Dict[int, Message] = {}
    names: Dict[str, int] = {}
    for index, raw in enumerate(raw_messages):
        mwhere = "%s: messages[%d]" % (where, index)
        if not isinstance(raw, Mapping):
            raise CatalogLoadError("%s: each message must be a mapping, got %s"
                                   % (mwhere, type(raw).__name__))
        mid = _require(raw, "id", mwhere)
        if not isinstance(mid, int) or isinstance(mid, bool):
            raise CatalogLoadError("%s: 'id' must be an integer, got %r" % (mwhere, mid))
        if mid in messages:
            raise CatalogLoadError("%s: message id 0x%X is defined twice." % (mwhere, mid))
        name = _require(raw, "name", mwhere)
        if name in names:
            raise CatalogLoadError("%s: message name %r is used by 0x%X as well."
                                   % (mwhere, name, names[name]))
        dlc = _require(raw, "dlc", mwhere)
        if not isinstance(dlc, int) or isinstance(dlc, bool) or not (0 <= dlc <= 8):
            raise CatalogLoadError("%s: 'dlc' must be an integer 0..8, got %r"
                                   % (mwhere, dlc))

        mwhere = "%s: message 0x%X (%s)" % (where, mid, name)
        raw_signals = raw.get("signals")
        if not isinstance(raw_signals, list) or not raw_signals:
            raise CatalogLoadError("%s: 'signals' must be a non-empty list." % mwhere)

        signals: Dict[str, Signal] = {}
        for sraw in raw_signals:
            sig = _parse_signal(sraw, mwhere)
            if sig.name in signals:
                raise CatalogLoadError("%s: signal '%s' is defined twice."
                                       % (mwhere, sig.name))
            end = sig.start_bit + sig.length
            if end > dlc * 8:
                raise CatalogLoadError(
                    "%s, signal '%s': occupies bits %d..%d, which runs past the "
                    "end of a %d-byte frame (%d bits)."
                    % (mwhere, sig.name, sig.start_bit, end - 1, dlc, dlc * 8))
            for other in signals.values():
                o_end = other.start_bit + other.length
                if sig.start_bit < o_end and other.start_bit < end:
                    raise CatalogLoadError(
                        "%s: signals '%s' (bits %d..%d) and '%s' (bits %d..%d) "
                        "overlap. One would silently corrupt the other."
                        % (mwhere, sig.name, sig.start_bit, end - 1,
                           other.name, other.start_bit, o_end - 1))
            signals[sig.name] = sig

        messages[mid] = Message(mid, name, dlc, raw.get("sender"), signals)
        names[name] = mid

    enums = _parse_enums(doc.get("enums"), where)

    known_signals = {s for m in messages.values() for s in m.signals}
    for sig_name in enums:
        if sig_name not in known_signals:
            raise CatalogLoadError(
                "%s: enum '%s' does not correspond to any signal.\n"
                "Signals defined: %s" % (where, sig_name, _quoted(sorted(known_signals))))

    return Catalog(messages, enums, source=str(p))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Load and inspect a CAN catalogue.")
    ap.add_argument("catalog", help="path to catalog.yml")
    ap.add_argument("--json", action="store_true", help="print the loaded contract as JSON")
    args = ap.parse_args(argv)
    try:
        cat = load(args.catalog)
    except CatalogError as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return 2
    if args.json:
        print(json.dumps(cat.to_json(), indent=2, sort_keys=True))
    else:
        for m in cat.messages():
            print("0x%03X %-16s dlc=%d sender=%s" % (m.id, m.name, m.dlc, m.sender))
            for s in m.signals.values():
                print("    %-18s bits %2d..%-2d %-8s scale=%s offset=%s %s"
                      % (s.name, s.start_bit, s.start_bit + s.length - 1,
                         "signed" if s.signed else "unsigned",
                         _num(s.scale), _num(s.offset), s.unit or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
