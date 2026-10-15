"""Load the network topology: which nodes exist, what they run, what they talk on.

WHY THIS EXISTS
    A scenario names nodes and buses. Something has to say what those names
    mean: which of them is a real compiled firmware and which is a scripted
    peer, which board each real node is emulated as, which bus each one is
    attached to, and which single node is the device under test. That mapping
    is ``network.yml``, and this module is the only thing that reads it.

    This module contains no topology. No node name, no bus name, no board
    name, no peripheral, no message identifier and no signal name appears
    anywhere below. It knows the SHAPE of a topology, never its content.
    ``harness/boards.yml`` is the one file in the repository allowed to name a
    peripheral, and this module reads that file rather than restating it.

WHAT IT REFUSES, AND WHY EACH REFUSAL IS WORTH A STOPPED RUN
    Every refusal below is a misconfiguration that, if it were tolerated or
    quietly repaired, would surface as a *firmware verdict* rather than as the
    configuration error it is. That conflation is the one thing this project
    must never do, so every one of them stops the load and names the offender.

    missing network.yml
        There is no default topology to fall back on. A topology invented here
        would be a topology nobody wrote.

    unknown board
        Without a board record there is no platform description, no CAN
        peripheral and no console, so the node cannot be emulated at all.

    type not exactly 'real' or 'scripted'
        These are the only two things the engine can start. A third spelling
        would silently match neither branch and the node would simply never
        exist, while the file that declares it looks fine.

    a real node with no elf or no boot_text
        No ELF is nothing to load. No boot text is no way to know the node
        ever came up: the run would proceed against a dead board and report
        whatever the silence implies.

    a scripted node with an elf
        A scripted node runs no firmware. An ELF here means the author
        believed it would be flashed, and it never would be.

    a node on an undeclared bus
        Renode connects nodes through a hub. A node attached to a bus nobody
        declared is attached to nothing: it boots, looks healthy, and silently
        carries no traffic (STATUS.md deviation 1).

    not exactly one dut
        The device under test is the node whose behaviour the campaign judges.
        Zero means nothing is judged; two means the verdict is ambiguous and
        whichever came first in the file would win by accident.

    a real node whose board bit rate differs from its bus bit rate
        This is STATUS.md deviation 13 in its network form. Both ends still
        work, every frame is still sent and received, and every timing
        measurement is wrong by the ratio of the two rates. Nothing reports
        it, so it is refused here.

WHAT IT DELIBERATELY DOES NOT DO
    It does not check that an ELF exists on disk. An ELF is a build artefact
    and is legitimately absent before the firmware is built; checking here
    would make a correct topology unloadable. The builder and the launcher
    check for the file they are about to use, where the error is actionable.

    It never repairs a value. A bit rate written as "500000" in quotes is
    refused, not un-quoted; a ``dut`` spelled ``yes`` is refused, not mapped
    onto true. Turning a malformed value into a valid one is falsification.

BOOLEANS
    ``harness.yaml_strict`` deliberately resolves no plain scalar to a
    boolean, so ``dut: true`` arrives here as the string "true". This module
    therefore accepts, for ``dut``, exactly: the plain scalars ``true`` and
    ``false``, or an explicitly tagged ``!!bool``. Every other spelling
    (``yes``, ``on``, ``True``, ``1``) is refused by name rather than guessed
    at, because a mis-read dut is a run that judges the wrong node.

CLI
    python -m harness.network <network.yml> [--boards <boards.yml>] [--json]

    exit 0  the topology loaded and every cross-check passed
    exit 1  the topology was refused; the reason names the offending thing
    exit 2  the invocation itself was unusable
"""

import argparse
import copy
import difflib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_HARNESS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _HARNESS_DIR.parent

# Support both `python -m harness.network` and `python harness/network.py`.
if __package__ in (None, ""):  # pragma: no cover - import-path plumbing
    sys.path.insert(0, str(REPO_ROOT))

from harness import yaml_strict  # noqa: E402

__all__ = [
    "NetworkError",
    "Bus",
    "Node",
    "Board",
    "Network",
    "load",
    "load_boards",
    "DEFAULT_BOARDS_PATH",
    "NODE_TYPE_REAL",
    "NODE_TYPE_SCRIPTED",
    "NODE_TYPES",
    "main",
]

DEFAULT_BOARDS_PATH = _HARNESS_DIR / "boards.yml"

# The only two things the engine can start. These are engine concepts, not
# project data: 'real' means a compiled ELF on an emulated board, 'scripted'
# means a peer driven by the harness.
NODE_TYPE_REAL = "real"
NODE_TYPE_SCRIPTED = "scripted"
NODE_TYPES = (NODE_TYPE_REAL, NODE_TYPE_SCRIPTED)

# Accepted spellings for a boolean field. See the BOOLEANS note above.
TRUE_SPELLING = "true"
FALSE_SPELLING = "false"

TOP_LEVEL_REQUIRED = ("buses", "nodes")

BUS_REQUIRED = ("id", "type", "bitrate")
BUS_OPTIONAL = ()

NODE_REQUIRED = ("id", "type", "buses")
NODE_OPTIONAL = ("dut",)
REAL_REQUIRED = ("board", "elf", "boot_text")
REAL_OPTIONAL = ("tx_enable_symbol",)
SCRIPTED_REQUIRED = ()
SCRIPTED_OPTIONAL = ("emits", "period_ms", "default_signals")

# The schema of harness/boards.yml. These are field NAMES, which the loader of
# a file must necessarily know; the peripheral names themselves live only in
# that file. A board missing any of these is refused here rather than deep
# inside Renode, where a missing platform file costs a whole run.
BOARD_REQUIRED = (
    "repl",
    "zephyr_board",
    "can_peripheral",
    "uart_peripheral",
    "cpu_peripheral",
    "can_bitrate",
)
BOARD_OPTIONAL = ()

# An id ends up in file names, Renode machine names and log lines, so it is
# restricted to characters that survive all three.
_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_ID_RULE = "letters, digits, underscore, dot and hyphen, starting with a letter, digit or underscore"

_JSON_SCALARS = (str, int, float)


class NetworkError(Exception):
    """A refusal to load a topology. The message names the offending thing."""


@dataclass(frozen=True)
class Bus:
    """One communication bus, exactly as declared."""

    id: str
    type: str
    bitrate: int
    declared: dict


@dataclass(frozen=True)
class Node:
    """One node, exactly as declared, with the absent optionals set to None."""

    id: str
    type: str
    buses: tuple
    dut: bool
    board: str
    elf: str
    boot_text: str
    tx_enable_symbol: str
    emits: tuple
    period_ms: int
    default_signals: dict
    declared: dict

    @property
    def is_real(self):
        return self.type == NODE_TYPE_REAL

    @property
    def is_scripted(self):
        return self.type == NODE_TYPE_SCRIPTED


@dataclass(frozen=True)
class Board:
    """One board record from boards.yml, exactly as declared."""

    id: str
    repl: str
    zephyr_board: str
    can_peripheral: str
    uart_peripheral: str
    cpu_peripheral: str
    can_bitrate: int
    declared: dict


# ---------------------------------------------------------------------------
# Small validators. Each one names the file, the position, the key, the value
# and the type, because the caller acting on the message may be a model.
# ---------------------------------------------------------------------------


def _quoted_list(values):
    """Render ids for an error message, in a stable order."""
    items = sorted(str(v) for v in values)
    if not items:
        return "(none)"
    return ", ".join("'%s'" % item for item in items)


def _type_name(value):
    return type(value).__name__


def _suggestion(key, candidates):
    """'did you mean' for a misspelled key, or the empty string."""
    close = difflib.get_close_matches(str(key), [str(c) for c in candidates], n=1, cutoff=0.7)
    if not close:
        return ""
    return "\n  Did you mean '%s'?" % close[0]


def _require_mapping(value, what, where):
    if not isinstance(value, dict):
        raise NetworkError(
            "%s: %s must be a mapping, but is %s (%r).\n"
            "  Nothing is inferred from another shape: correct the file."
            % (where, what, _type_name(value), value)
        )
    for key in value:
        if not isinstance(key, str):
            raise NetworkError(
                "%s: %s has the non-string key %r (%s).\n"
                "  Every key in this file must be a plain string."
                % (where, what, key, _type_name(key))
            )
    return value


def _require_list(value, what, where, allow_empty=False):
    if not isinstance(value, list):
        raise NetworkError(
            "%s: %s must be a list, but is %s (%r)."
            % (where, what, _type_name(value), value)
        )
    if not value and not allow_empty:
        raise NetworkError(
            "%s: %s is an empty list.\n"
            "  An empty list here is refused rather than treated as absent,\n"
            "  because the two mean different things and the difference would\n"
            "  otherwise be silent. Either populate it or delete the key."
            % (where, what)
        )
    return value


def _check_keys(mapping, required, optional, where, what, other_type_keys=None, other_type=None):
    """Refuse an unknown key or a missing required one, naming it."""
    accepted = tuple(required) + tuple(optional)
    for key in mapping:
        if key in accepted:
            continue
        if other_type_keys and key in other_type_keys:
            raise NetworkError(
                "%s: %s declares '%s', which belongs only to a '%s' node.\n"
                "  This node is type '%s'.\n"
                "  Either delete '%s', or change the node's type to '%s'.\n"
                "  It is not ignored: a key the engine never reads is a\n"
                "  statement the author believed would take effect."
                % (where, what, key, other_type, _other(other_type), key, other_type)
            )
        raise NetworkError(
            "%s: %s declares the unknown key '%s'.\n"
            "  Accepted keys here: %s.\n"
            "  An unknown key is refused rather than ignored: a misspelled key\n"
            "  is silently dropped by every loader that tolerates it, and the\n"
            "  value the author wrote never takes effect.%s"
            % (where, what, key, _quoted_list(accepted), _suggestion(key, accepted))
        )
    missing = [key for key in required if key not in mapping]
    if missing:
        raise NetworkError(
            "%s: %s does not declare %s.\n"
            "  Required here: %s.\n"
            "  Declared: %s."
            % (
                where,
                what,
                _quoted_list(missing),
                _quoted_list(required),
                _quoted_list(mapping.keys()),
            )
        )


def _other(node_type):
    return NODE_TYPE_SCRIPTED if node_type == NODE_TYPE_REAL else NODE_TYPE_REAL


def _require_str(mapping, key, where, what):
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, str):
        raise NetworkError(
            "%s: %s has '%s' = %r (%s), but it must be a string."
            % (where, what, key, value, _type_name(value))
        )
    if not value.strip():
        raise NetworkError(
            "%s: %s has an empty '%s'.\n"
            "  An empty string is refused rather than treated as absent."
            % (where, what, key)
        )
    return value


def _require_id(mapping, key, where, what):
    value = _require_str(mapping, key, where, what)
    if not _ID_RE.match(value):
        raise NetworkError(
            "%s: %s has '%s' = %r, which is not a usable identifier.\n"
            "  Allowed: %s.\n"
            "  Ids appear in file names, machine names and log lines, so a\n"
            "  space or a leading hyphen would break at least one of them."
            % (where, what, key, value, _ID_RULE)
        )
    return value


def _require_positive_int(mapping, key, where, what):
    value = mapping[key]
    if isinstance(value, bool):
        raise NetworkError(
            "%s: %s has '%s' = %r (bool), but it must be a positive integer."
            % (where, what, key, value)
        )
    if isinstance(value, str):
        raise NetworkError(
            "%s: %s has '%s' = %r, which is a string, not an integer.\n"
            "  It is NOT converted. A quoted number here means either the\n"
            "  quotes are a mistake or the value is not a number at all, and\n"
            "  guessing which would turn a malformed value into a valid one.\n"
            "  Write it unquoted: %s: <integer>."
            % (where, what, key, value, key)
        )
    if not isinstance(value, int):
        raise NetworkError(
            "%s: %s has '%s' = %r (%s), but it must be a positive integer."
            % (where, what, key, value, _type_name(value))
        )
    if value <= 0:
        raise NetworkError(
            "%s: %s has '%s' = %d, which is not positive.\n"
            "  It is refused rather than clamped: a non-positive value here\n"
            "  has no meaning the engine could act on."
            % (where, what, key, value)
        )
    return value


def _require_bool(mapping, key, where, what):
    """Read a boolean field under the yaml_strict dialect."""
    value = mapping[key]
    if isinstance(value, bool):
        return value
    if value == TRUE_SPELLING:
        return True
    if value == FALSE_SPELLING:
        return False
    raise NetworkError(
        "%s: %s has '%s' = %r (%s), which is not a boolean this loader accepts.\n"
        "  harness.yaml_strict deliberately resolves no plain YAML scalar to a\n"
        "  boolean, so the accepted spellings are exactly:\n"
        "      %s: %s\n"
        "      %s: %s\n"
        "      %s: !!bool true      (explicitly tagged)\n"
        "  %r is none of these and is not mapped onto one of them."
        % (
            where,
            what,
            key,
            value,
            _type_name(value),
            key,
            TRUE_SPELLING,
            key,
            FALSE_SPELLING,
            key,
            value,
        )
    )


def _require_json_scalar(value, where, what, key):
    if value is None:
        return value
    if isinstance(value, bool):
        return value
    if not isinstance(value, _JSON_SCALARS):
        raise NetworkError(
            "%s: %s has '%s' = %r (%s), which is not a scalar.\n"
            "  Only strings, integers, floats and null may appear here."
            % (where, what, key, value, _type_name(value))
        )
    return value


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def _load_document(path, what):
    """Read one YAML document, turning every failure into a named refusal."""
    given = os.fspath(path)
    absolute = os.path.abspath(given)
    as_given = "" if given == absolute else " (given as %r, resolved against %r)" % (
        given,
        os.getcwd(),
    )
    try:
        document = yaml_strict.load_file(absolute)
    except yaml_strict.YamlFileNotFoundError as exc:
        raise NetworkError(
            "%s not found: %r%s\n"
            "  There is no default to fall back on and none is invented, so\n"
            "  the load stops here.\n"
            "  Create the file, or pass the correct path."
            % (what, absolute, as_given)
        ) from exc
    except yaml_strict.YamlParseError as exc:
        raise NetworkError(
            "%s is not well-formed YAML: %r\n"
            "  yaml_strict reported: %s" % (what, absolute, exc)
        ) from exc
    except yaml_strict.YamlStrictError as exc:
        raise NetworkError(
            "%s could not be read: %r\n"
            "  yaml_strict reported: %s" % (what, absolute, exc)
        ) from exc
    if document is None:
        raise NetworkError(
            "%s is empty: %r\n"
            "  An empty file is refused rather than read as an empty topology,\n"
            "  because a truncated or half-written file looks exactly like one."
            % (what, absolute)
        )
    return document, absolute


# ---------------------------------------------------------------------------
# boards.yml
# ---------------------------------------------------------------------------


def load_boards(boards_path=None):
    """Load harness/boards.yml and return {board_id: Board}.

    boards.yml is a mapping of board id to record. It is the one file in the
    repository where a peripheral name may appear.
    """
    if boards_path is None:
        boards_path = DEFAULT_BOARDS_PATH
    document, absolute = _load_document(boards_path, "board definition file")
    where = absolute
    _require_mapping(document, "the board definition file", where)
    if not document:
        raise NetworkError(
            "%s: the board definition file declares no boards.\n"
            "  Every real node needs a board record, so an empty file cannot\n"
            "  support any topology." % where
        )

    boards = {}
    for board_id, record in document.items():
        if not _ID_RE.match(board_id):
            raise NetworkError(
                "%s: board id %r is not a usable identifier.\n"
                "  Allowed: %s." % (where, board_id, _ID_RULE)
            )
        what = "board '%s'" % board_id
        _require_mapping(record, what, where)
        _check_keys(record, BOARD_REQUIRED, BOARD_OPTIONAL, where, what)
        boards[board_id] = Board(
            id=board_id,
            repl=_require_str(record, "repl", where, what),
            zephyr_board=_require_str(record, "zephyr_board", where, what),
            can_peripheral=_require_str(record, "can_peripheral", where, what),
            uart_peripheral=_require_str(record, "uart_peripheral", where, what),
            cpu_peripheral=_require_str(record, "cpu_peripheral", where, what),
            can_bitrate=_require_positive_int(record, "can_bitrate", where, what),
            declared=copy.deepcopy(record),
        )
    return boards


# ---------------------------------------------------------------------------
# network.yml
# ---------------------------------------------------------------------------


def _load_bus(entry, index, where):
    what = "buses[%d]" % index
    _require_mapping(entry, what, where)
    if "id" not in entry:
        raise NetworkError(
            "%s: %s does not declare 'id'.\n"
            "  Declared: %s.\n"
            "  A bus with no id cannot be referenced by any node."
            % (where, what, _quoted_list(entry.keys()))
        )
    bus_id = _require_id(entry, "id", where, what)
    what = "bus '%s' (buses[%d])" % (bus_id, index)
    _check_keys(entry, BUS_REQUIRED, BUS_OPTIONAL, where, what)
    return Bus(
        id=bus_id,
        # The bus type is data: this module requires a non-empty string and
        # never restricts the spelling, because the set of protocols is the
        # topology's business, not the engine's.
        type=_require_str(entry, "type", where, what),
        bitrate=_require_positive_int(entry, "bitrate", where, what),
        declared=copy.deepcopy(entry),
    )


def _load_node(entry, index, where):
    what = "nodes[%d]" % index
    _require_mapping(entry, what, where)
    if "id" not in entry:
        raise NetworkError(
            "%s: %s does not declare 'id'.\n"
            "  Declared: %s.\n"
            "  A node with no id cannot be named by a scenario."
            % (where, what, _quoted_list(entry.keys()))
        )
    node_id = _require_id(entry, "id", where, what)
    what = "node '%s' (nodes[%d])" % (node_id, index)

    if "type" not in entry:
        raise NetworkError(
            "%s: %s does not declare 'type'.\n"
            "  It must be exactly one of: %s."
            % (where, what, _quoted_list(NODE_TYPES))
        )
    node_type = entry["type"]
    if node_type not in NODE_TYPES:
        raise NetworkError(
            "%s: %s has type %r (%s), which is not a node type this engine can start.\n"
            "  'type' must be exactly one of: %s.\n"
            "    '%s'      the node runs a compiled firmware ELF on an emulated board\n"
            "    '%s'  the node is driven by the harness, with no firmware\n"
            "  %r is neither and is not mapped onto one of them: a node whose\n"
            "  type matches no branch would simply never be started, while the\n"
            "  file declaring it still looked correct."
            % (
                where,
                what,
                node_type,
                _type_name(node_type),
                _quoted_list(NODE_TYPES),
                NODE_TYPE_REAL,
                NODE_TYPE_SCRIPTED,
                node_type,
            )
        )

    if node_type == NODE_TYPE_REAL:
        required = NODE_REQUIRED + REAL_REQUIRED
        optional = NODE_OPTIONAL + REAL_OPTIONAL
        other_keys = SCRIPTED_REQUIRED + SCRIPTED_OPTIONAL
    else:
        required = NODE_REQUIRED + SCRIPTED_REQUIRED
        optional = NODE_OPTIONAL + SCRIPTED_OPTIONAL
        other_keys = REAL_REQUIRED + REAL_OPTIONAL

    _check_keys(
        entry,
        required,
        optional,
        where,
        what,
        other_type_keys=other_keys,
        other_type=_other(node_type),
    )

    # allow_empty: an empty list is refused by _check_bus_references, which can
    # say what an unattached node actually costs. Refusing it here instead
    # would leave that guard unreachable.
    bus_ids = _require_list(
        entry["buses"], "'buses'", "%s: %s" % (where, what), allow_empty=True
    )
    seen = []
    for position, raw_bus in enumerate(bus_ids):
        if isinstance(raw_bus, bool) or not isinstance(raw_bus, str) or not raw_bus.strip():
            raise NetworkError(
                "%s: %s has buses[%d] = %r (%s), but every entry must be a\n"
                "  non-empty bus id." % (where, what, position, raw_bus, _type_name(raw_bus))
            )
        if raw_bus in seen:
            raise NetworkError(
                "%s: %s lists bus '%s' twice (at positions %d and %d).\n"
                "  A duplicate is refused rather than collapsed: it usually\n"
                "  means one of the two was meant to be a different bus."
                % (where, what, raw_bus, seen.index(raw_bus), position)
            )
        seen.append(raw_bus)

    dut = False
    if "dut" in entry:
        dut = _require_bool(entry, "dut", where, what)

    board = None
    elf = None
    boot_text = None
    tx_enable_symbol = None
    emits = ()
    period_ms = None
    default_signals = {}

    if node_type == NODE_TYPE_REAL:
        board = _require_id(entry, "board", where, what)
        elf = _require_str(entry, "elf", where, what)
        boot_text = _require_str(entry, "boot_text", where, what)
        if "tx_enable_symbol" in entry:
            tx_enable_symbol = _require_id(entry, "tx_enable_symbol", where, what)
    else:
        if "emits" in entry:
            raw_emits = _require_list(entry["emits"], "'emits'", "%s: %s" % (where, what))
            ids = []
            for position, item in enumerate(raw_emits):
                if isinstance(item, bool) or not isinstance(item, int):
                    raise NetworkError(
                        "%s: %s has emits[%d] = %r (%s), but a message identifier\n"
                        "  must be an integer. Write it as a decimal or as 0x-hex;\n"
                        "  a quoted identifier is not converted."
                        % (where, what, position, item, _type_name(item))
                    )
                if item < 0:
                    raise NetworkError(
                        "%s: %s has emits[%d] = %d, which is negative."
                        % (where, what, position, item)
                    )
                if item in ids:
                    raise NetworkError(
                        "%s: %s emits the identifier %d twice (positions %d and %d).\n"
                        "  A duplicate is refused rather than de-duplicated: it\n"
                        "  changes what the node transmits."
                        % (where, what, item, ids.index(item), position)
                    )
                ids.append(item)
            emits = tuple(ids)
        if "period_ms" in entry:
            period_ms = _require_positive_int(entry, "period_ms", where, what)
        if emits and period_ms is None:
            raise NetworkError(
                "%s: %s declares 'emits' but no 'period_ms'.\n"
                "  An emitter with no period would be started and would then\n"
                "  transmit nothing, which reads on the bus exactly like a node\n"
                "  that is not attached at all.\n"
                "  Add 'period_ms', or delete 'emits'." % (where, what)
            )
        if period_ms is not None and not emits:
            raise NetworkError(
                "%s: %s declares 'period_ms' but no 'emits'.\n"
                "  There is nothing to send on that period.\n"
                "  Add 'emits', or delete 'period_ms'." % (where, what)
            )
        if "default_signals" in entry:
            signals = _require_mapping(
                entry["default_signals"], "'default_signals'", "%s: %s" % (where, what)
            )
            for signal_name, signal_value in signals.items():
                if not signal_name.strip():
                    raise NetworkError(
                        "%s: %s has an empty signal name in 'default_signals'."
                        % (where, what)
                    )
                _require_json_scalar(
                    signal_value, where, "%s 'default_signals'" % what, signal_name
                )
            default_signals = copy.deepcopy(signals)

    return Node(
        id=node_id,
        type=node_type,
        buses=tuple(seen),
        dut=dut,
        board=board,
        elf=elf,
        boot_text=boot_text,
        tx_enable_symbol=tx_enable_symbol,
        emits=emits,
        period_ms=period_ms,
        default_signals=default_signals,
        declared=copy.deepcopy(entry),
    )


class Network:
    """A loaded, fully cross-checked topology."""

    def __init__(self, network_path, boards_path, buses, nodes, boards, root):
        self.network_path = network_path
        self.boards_path = boards_path
        self.root = root
        self._buses = tuple(buses)
        self._nodes = tuple(nodes)
        self._boards = dict(boards)
        self._bus_by_id = {bus.id: bus for bus in self._buses}
        self._node_by_id = {node.id: node for node in self._nodes}
        duts = [node for node in self._nodes if node.dut]
        if len(duts) != 1:
            # Unreachable through load(), which refuses this first. Kept so a
            # Network built by hand fails with the reason rather than an
            # IndexError from somewhere else.
            raise NetworkError(
                "%s: a Network was constructed with %d nodes declaring 'dut: true'; "
                "exactly one is required (%s)."
                % (network_path, len(duts), _quoted_list(node.id for node in duts))
            )
        self._dut = duts[0]

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, network_path, boards_path=None, root=None):
        """Load and cross-check a topology, or raise NetworkError naming why."""
        if boards_path is None:
            boards_path = DEFAULT_BOARDS_PATH
        if root is None:
            root = REPO_ROOT
        root = Path(root)

        boards = load_boards(boards_path)
        boards_absolute = os.path.abspath(os.fspath(boards_path))

        document, where = _load_document(network_path, "network topology file")
        _require_mapping(document, "the network topology file", where)
        _check_keys(document, TOP_LEVEL_REQUIRED, (), where, "the network topology file")

        buses = [
            _load_bus(entry, index, where)
            for index, entry in enumerate(_require_list(document["buses"], "'buses'", where))
        ]
        nodes = [
            _load_node(entry, index, where)
            for index, entry in enumerate(_require_list(document["nodes"], "'nodes'", where))
        ]

        _check_unique(buses, "bus", "buses", where)
        _check_unique(nodes, "node", "nodes", where)
        _check_bus_references(nodes, buses, where)
        _check_single_dut(nodes, where)
        _check_boards(nodes, buses, boards, where, boards_absolute)

        return cls(
            network_path=where,
            boards_path=boards_absolute,
            buses=buses,
            nodes=nodes,
            boards=boards,
            root=root,
        )

    # -- accessors --------------------------------------------------------

    def buses(self):
        """Every bus, in declaration order."""
        return self._buses

    def nodes(self):
        """Every node, in declaration order."""
        return self._nodes

    def real_nodes(self):
        """Every node that runs a compiled firmware, in declaration order."""
        return tuple(node for node in self._nodes if node.is_real)

    def scripted_nodes(self):
        """Every node driven by the harness, in declaration order."""
        return tuple(node for node in self._nodes if node.is_scripted)

    def dut(self):
        """The single device under test. Guaranteed to exist by the loader."""
        return self._dut

    def boards(self):
        """Every board record from boards.yml, keyed by id."""
        return dict(self._boards)

    def bus(self, bus_id):
        """Look up a bus by id, or refuse naming it and listing the known ids."""
        try:
            return self._bus_by_id[bus_id]
        except KeyError:
            raise NetworkError(
                "%s: no bus with id %r.\n"
                "  Buses declared: %s."
                % (self.network_path, bus_id, _quoted_list(self._bus_by_id))
            ) from None

    def node(self, node_id):
        """Look up a node by id, or refuse naming it and listing the known ids."""
        try:
            return self._node_by_id[node_id]
        except KeyError:
            raise NetworkError(
                "%s: no node with id %r.\n"
                "  Nodes declared: %s."
                % (self.network_path, node_id, _quoted_list(self._node_by_id))
            ) from None

    def board(self, board_id):
        """Look up a board by id, or refuse naming it and the boards that exist."""
        try:
            return self._boards[board_id]
        except KeyError:
            raise NetworkError(
                "%s: no board with id %r.\n"
                "  Boards defined there: %s."
                % (self.boards_path, board_id, _quoted_list(self._boards))
            ) from None

    def board_of(self, node_or_id):
        """The board record a real node is emulated as."""
        node = self._as_node(node_or_id)
        if not node.is_real:
            raise NetworkError(
                "%s: node '%s' is type '%s' and has no board.\n"
                "  Only a '%s' node is emulated on a board."
                % (self.network_path, node.id, node.type, NODE_TYPE_REAL)
            )
        return self.board(node.board)

    def nodes_on(self, bus_id):
        """Every node attached to a bus, in declaration order."""
        self.bus(bus_id)
        return tuple(node for node in self._nodes if bus_id in node.buses)

    def elf_path(self, node_or_id):
        """The absolute path of a real node's ELF, resolved against the root.

        The file is NOT required to exist: it is a build artefact, and a
        topology is legitimately loadable before the firmware is built.
        """
        node = self._as_node(node_or_id)
        if not node.is_real:
            raise NetworkError(
                "%s: node '%s' is type '%s' and has no elf."
                % (self.network_path, node.id, node.type)
            )
        return (self.root / node.elf).resolve()

    def _as_node(self, node_or_id):
        if isinstance(node_or_id, Node):
            return self.node(node_or_id.id)
        return self.node(node_or_id)

    # -- serialisation ----------------------------------------------------

    def to_dict(self):
        """A JSON-safe view. 'buses', 'nodes' and 'boards' are exactly as declared."""
        return {
            "network_path": str(self.network_path),
            "boards_path": str(self.boards_path),
            "buses": [copy.deepcopy(bus.declared) for bus in self._buses],
            "nodes": [copy.deepcopy(node.declared) for node in self._nodes],
            "boards": {
                board_id: copy.deepcopy(board.declared)
                for board_id, board in self._boards.items()
            },
            "dut": self._dut.id,
            "real_nodes": [node.id for node in self.real_nodes()],
            "scripted_nodes": [node.id for node in self.scripted_nodes()],
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Cross-checks across the whole document
# ---------------------------------------------------------------------------


def _check_unique(items, kind, collection, where):
    """Refuse a repeated id, naming the two positions as the file spells them.

    `collection` is the top-level key ('buses', 'nodes'), so the positions in
    the message are the ones the author can actually find in the file.
    """
    seen = {}
    for index, item in enumerate(items):
        if item.id in seen:
            raise NetworkError(
                "%s: %s id '%s' is declared twice, at %s[%d] and %s[%d].\n"
                "  The second would shadow the first, so whichever the engine\n"
                "  happened to keep would be an accident of ordering."
                % (where, kind, item.id, collection, seen[item.id], collection, index)
            )
        seen[item.id] = index


def _check_bus_references(nodes, buses, where):
    declared = [bus.id for bus in buses]
    for node in nodes:
        if not node.buses:
            raise NetworkError(
                "%s: node '%s' is attached to no bus.\n"
                "  Buses declared: %s.\n"
                "  A node on no bus can neither send nor receive: it would boot,\n"
                "  look healthy, and carry no traffic at all."
                % (where, node.id, _quoted_list(declared))
            )
        for bus_id in node.buses:
            if bus_id not in declared:
                raise NetworkError(
                    "%s: node '%s' references bus '%s', which is not declared in\n"
                    "  this file's 'buses'.\n"
                    "  Buses declared: %s.\n"
                    "  Either add a bus with id '%s', or correct the reference in\n"
                    "  node '%s'. It is not created implicitly: a node attached to\n"
                    "  a bus nobody declared is attached to nothing, and that is\n"
                    "  silent on the wire."
                    % (where, node.id, bus_id, _quoted_list(declared), bus_id, node.id)
                )


def _check_single_dut(nodes, where):
    duts = [node for node in nodes if node.dut]
    if len(duts) == 1:
        if not duts[0].is_real:
            raise NetworkError(
                "%s: node '%s' declares 'dut: true' but is type '%s'.\n"
                "  The device under test is the firmware the campaign judges, so\n"
                "  it must be a '%s' node. A '%s' node runs no firmware and there\n"
                "  would be nothing to detect a defect in."
                % (where, duts[0].id, duts[0].type, NODE_TYPE_REAL, NODE_TYPE_SCRIPTED)
            )
        return
    described = ", ".join("'%s' (%s)" % (node.id, node.type) for node in nodes)
    if not duts:
        raise NetworkError(
            "%s: no node declares 'dut: true'. Exactly one must.\n"
            "  dut count: 0\n"
            "  nodes declared (%d): %s\n"
            "  The device under test is the node whose behaviour the campaign\n"
            "  judges. With none declared there is nothing to judge, and no node\n"
            "  is picked by default."
            % (where, len(nodes), described)
        )
    raise NetworkError(
        "%s: %d nodes declare 'dut: true'. Exactly one must.\n"
        "  dut count: %d\n"
        "  nodes with 'dut: true': %s\n"
        "  nodes declared (%d): %s\n"
        "  With more than one the verdict is ambiguous, and whichever came\n"
        "  first in the file would win by accident of ordering."
        % (
            where,
            len(duts),
            len(duts),
            _quoted_list(node.id for node in duts),
            len(nodes),
            described,
        )
    )


def _check_boards(nodes, buses, boards, where, boards_absolute):
    bus_by_id = {bus.id: bus for bus in buses}
    for node in nodes:
        if not node.is_real:
            continue
        if node.board not in boards:
            raise NetworkError(
                "%s: node '%s' declares board '%s', which is not defined in the\n"
                "  board definition file.\n"
                "  board file: %s\n"
                "  boards defined there: %s\n"
                "  Either correct the board of node '%s', or add '%s' to that file\n"
                "  with the fields: %s.\n"
                "  Nothing is substituted: without a board record there is no\n"
                "  platform description, no CAN peripheral and no console, so the\n"
                "  node could not be emulated at all."
                % (
                    where,
                    node.id,
                    node.board,
                    boards_absolute,
                    _quoted_list(boards),
                    node.id,
                    node.board,
                    _quoted_list(BOARD_REQUIRED),
                )
            )
        board = boards[node.board]
        if len(node.buses) != 1:
            raise NetworkError(
                "%s: node '%s' is a '%s' node on %d buses (%s), but board '%s'\n"
                "  declares a single CAN peripheral.\n"
                "  board file: %s\n"
                "  There is no way to say which peripheral each bus attaches to,\n"
                "  and picking one would silently leave the other unconnected.\n"
                "  Either put the node on one bus, or extend the board schema to\n"
                "  describe a second controller."
                % (
                    where,
                    node.id,
                    NODE_TYPE_REAL,
                    len(node.buses),
                    _quoted_list(node.buses),
                    board.id,
                    boards_absolute,
                )
            )
        bus = bus_by_id[node.buses[0]]
        if bus.bitrate != board.can_bitrate:
            raise NetworkError(
                "%s: node '%s' is on bus '%s' at %d bit/s, but its board '%s'\n"
                "  declares %d bit/s.\n"
                "  board file: %s\n"
                "  bus '%s' bitrate:        %d\n"
                "  board '%s' can_bitrate:  %d\n"
                "  Neither value is preferred over the other and neither is\n"
                "  adjusted. Both ends would still work: every frame would be\n"
                "  sent and received, and every timing measurement would be wrong\n"
                "  by the ratio of the two rates, with nothing reporting it.\n"
                "  Make the two agree."
                % (
                    where,
                    node.id,
                    bus.id,
                    bus.bitrate,
                    board.id,
                    board.can_bitrate,
                    boards_absolute,
                    bus.id,
                    bus.bitrate,
                    board.id,
                    board.can_bitrate,
                )
            )


def load(network_path, boards_path=None, root=None):
    """Load and cross-check a topology. See Network.load."""
    return Network.load(network_path, boards_path=boards_path, root=root)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(net, stream):
    stream.write("network: %s\n" % net.network_path)
    stream.write("boards:  %s\n" % net.boards_path)
    stream.write("\nbuses (%d):\n" % len(net.buses()))
    for bus in net.buses():
        stream.write(
            "  %-16s type=%-8s bitrate=%d\n" % (bus.id, bus.type, bus.bitrate)
        )
    stream.write("\nnodes (%d):\n" % len(net.nodes()))
    for node in net.nodes():
        detail = "buses=%s" % ",".join(node.buses)
        if node.is_real:
            detail += "  board=%s  elf=%s" % (node.board, node.elf)
            if node.tx_enable_symbol:
                detail += "  tx_enable_symbol=%s" % node.tx_enable_symbol
        else:
            if node.emits:
                detail += "  emits=%s" % ",".join("0x%x" % i for i in node.emits)
            if node.period_ms is not None:
                detail += "  period_ms=%d" % node.period_ms
        if node.dut:
            detail += "  [DUT]"
        stream.write("  %-16s %-9s %s\n" % (node.id, node.type, detail))
    stream.write("\nreal: %d   scripted: %d   dut: '%s'\n" % (
        len(net.real_nodes()), len(net.scripted_nodes()), net.dut().id))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="network.py",
        description="Load and cross-check the network topology.",
        epilog="exit 0 loaded and cross-checked; 1 refused, naming the offender; "
               "2 the invocation was unusable.",
    )
    parser.add_argument("network", help="path to the network topology file (network.yml)")
    parser.add_argument(
        "--boards",
        default=None,
        help="path to the board definition file (default: %s)" % DEFAULT_BOARDS_PATH,
    )
    parser.add_argument(
        "--root",
        default=None,
        help="root the elf paths are resolved against (default: %s)" % REPO_ROOT,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the loaded topology as JSON instead of a human summary",
    )
    args = parser.parse_args(argv)

    try:
        net = Network.load(args.network, boards_path=args.boards, root=args.root)
    except NetworkError as exc:
        sys.stderr.write("REFUSED: %s\n" % exc)
        return 1

    if args.json:
        sys.stdout.write(net.to_json() + "\n")
    else:
        _print_summary(net, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
