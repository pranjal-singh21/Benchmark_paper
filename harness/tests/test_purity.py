"""The engine must contain no project data.

The claim this defends is the one that makes the study a method rather than a
case study: *the harness contains no system-specific data, and a second,
unrelated system runs on it unchanged*. That sentence appears in the paper's
external-validity section, and it is only worth writing because it is checkable.

It is a **test**, not a grep, and the difference matters. A grep goes stale the
moment a file is edited, and it has no idea what the project's node names or
signal spellings currently are. This reads them out of the data files at run
time, so renaming a node in ``network.yml`` immediately changes what is
forbidden. It also scans comments and docstrings deliberately: a peripheral name
in a comment is a sign the engine was written against one board, and the next
person will follow the comment.

Exemptions, all three declared here rather than scattered:

``harness/boards.yml``
    The one file where a peripheral name may appear. That is its entire job.

this file
    It has to name the things it forbids.

``harness/tests/``
    A test must be able to name what it tests, and a fixture is data by
    definition. The engine is what has to be portable, not its test suite. This
    is one exemption more than the phase specification lists; it is called out
    here so the difference is visible rather than quietly assumed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness import yaml_strict

HARNESS = Path(__file__).resolve().parent.parent
ROOT = HARNESS.parent

# Peripheral instance names. These are the ones that actually differ between the
# two systems in this study, plus the common spellings from other vendors so the
# check keeps meaning if the target changes.
PERIPHERAL_PATTERNS = [
    r"\bfdcan\d*\b",
    r"\busart\d*\b",
    r"\buart\d+\b",
    r"\bflexcan\d*\b",
    r"\bcan\d+\b",
]

# CAN identifiers, written the way this project writes them. Deliberately broad:
# any three-digit hex literal in the engine is suspicious, because the engine has
# no business naming a message.
MESSAGE_ID_PATTERN = r"0x[0-9a-fA-F]{3}\b"


def _engine_files():
    """Every file that must be pure: the engine modules and its own data files."""
    out = []
    for path in sorted(HARNESS.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".yml", ".yaml"):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == "harness/boards.yml":
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if "harness/tests/" in rel:
            continue
        if "__pycache__" in rel:
            continue
        out.append(path)
    return out


def _project_vocabulary():
    """Node, signal and enum names, read from the data files at run time."""
    nodes, signals, enums = set(), set(), set()

    net = ROOT / "systems" / "powertrain" / "network.yml"
    if net.is_file():
        doc = yaml_strict.load_file(net) or {}
        for n in doc.get("nodes") or []:
            if isinstance(n, dict) and n.get("id"):
                nodes.add(str(n["id"]))
        for b in doc.get("buses") or []:
            if isinstance(b, dict) and b.get("id"):
                nodes.add(str(b["id"]))

    cat = ROOT / "systems" / "powertrain" / "catalog.yml"
    if cat.is_file():
        doc = yaml_strict.load_file(cat) or {}
        for m in doc.get("messages") or []:
            if not isinstance(m, dict):
                continue
            if m.get("name"):
                signals.add(str(m["name"]))
            for s in m.get("signals") or []:
                if isinstance(s, dict) and s.get("name"):
                    signals.add(str(s["name"]))
        for table in (doc.get("enums") or {}).values():
            if isinstance(table, dict):
                for label in table.values():
                    enums.add(str(label))

    return nodes, signals, enums


def _hits(text, pattern, ignore_case=False):
    """Lines matching a pattern.

    Case sensitivity is a parameter because the two kinds of check need opposite
    answers, and getting it wrong makes the test useless in both directions.

    Peripheral names are matched case-INSENSITIVELY: FDCAN1 and fdcan1 are the
    same mistake.

    Project vocabulary is matched case-SENSITIVELY, and that is not fussiness.
    Enum spellings are shouted (`NONE`) while ordinary Python is not (`None`),
    and a case-insensitive check flags every `None` in the engine as a project
    enum. Observed: it produced five failures across four modules, none of them
    real. A check that cries wolf gets disabled, and then it guards nothing.
    """
    flags = re.IGNORECASE if ignore_case else 0
    return [
        (i, line.strip())
        for i, line in enumerate(text.splitlines(), 1)
        if re.search(pattern, line, flags)
    ]


def _report(path, found):
    rel = path.relative_to(ROOT).as_posix()
    lines = "\n".join("    line %d: %s" % (n, t) for n, t in found[:6])
    return (
        "%s contains project data:\n%s\n\n"
        "The engine reads the data files; it must never contain their content. "
        "A hardcoded peripheral, node or signal name is exactly what makes the "
        "harness non-portable, and the second system in this study uses "
        "different ones. If this name genuinely belongs somewhere, that "
        "somewhere is harness/boards.yml or a data file under systems/."
        % (rel, lines)
    )


@pytest.mark.parametrize("path", _engine_files(), ids=lambda p: p.name)
def test_no_peripheral_names(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    for pattern in PERIPHERAL_PATTERNS:
        found = _hits(text, pattern, ignore_case=True)
        assert not found, _report(path, found)


@pytest.mark.parametrize("path", _engine_files(), ids=lambda p: p.name)
def test_no_message_identifiers(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    found = _hits(text, MESSAGE_ID_PATTERN, ignore_case=True)
    assert not found, _report(path, found)


@pytest.mark.parametrize("path", _engine_files(), ids=lambda p: p.name)
def test_no_node_names(path):
    nodes, _, _ = _project_vocabulary()
    if not nodes:
        pytest.skip("no network.yml to read node names from")
    text = path.read_text(encoding="utf-8", errors="replace")
    for node in sorted(nodes):
        # Word-boundary matched so an unrelated English word is not flagged.
        found = _hits(text, r"\b%s\b" % re.escape(node))
        assert not found, _report(path, found)


@pytest.mark.parametrize("path", _engine_files(), ids=lambda p: p.name)
def test_no_signal_or_enum_names(path):
    _, signals, enums = _project_vocabulary()
    vocabulary = signals | enums
    if not vocabulary:
        pytest.skip("no catalog.yml to read signal names from")
    text = path.read_text(encoding="utf-8", errors="replace")
    for word in sorted(vocabulary):
        # Enum spellings such as NONE are ordinary English and ordinary Python.
        # Requiring at least four characters and a word boundary keeps the check
        # meaningful without flagging every use of the language.
        if len(word) < 4:
            continue
        found = _hits(text, r"\b%s\b" % re.escape(word))
        assert not found, _report(path, found)


def test_the_check_actually_scans_something():
    """A guard that inspects nothing passes trivially.

    If the file discovery ever breaks, every test above turns green while
    checking zero bytes. This pins that it found the engine.
    """
    files = _engine_files()
    assert len(files) >= 5, "expected the engine modules, found: %s" % (
        [f.name for f in files],)
    names = {f.name for f in files}
    assert "boards.yml" not in names, "boards.yml must be exempt"
    assert "test_purity.py" not in names, "this file must be exempt"


def test_exemptions_are_real_files():
    """The declared exemptions must exist, or they are hiding nothing."""
    assert (HARNESS / "boards.yml").is_file(), (
        "harness/boards.yml is exempt from this check but does not exist. "
        "Either the exemption is stale or the file was lost.")
