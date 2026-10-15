"""Event log to verdict, with latencies.

Every timestamp read here is VIRTUAL microseconds from the emulator. Host wall
clock never enters a latency; it is recorded separately, and named
``host_wall_seconds`` so it cannot be mistaken for one.

The latency rule
----------------
    Quote the latency-to-budget ratio ONLY when the causing stimulus falls inside
    the assertion's own window.

Without it the report reads ``300.3 ms / 150 ms budget`` on a scenario that
actually answered 300 microseconds after the assertion was armed: a nonsense
number sitting beside a green PASS, produced by measuring from a stimulus in an
earlier step across two different clocks.

The same rule picks the interesting frame out of routine traffic **without the
engine knowing which identifier means "fault"**. There is no message identifier
anywhere in this module, and there must not be. A status frame is on the bus
every 100 ms; the frame that answers a stimulus is the one that arrives after it
and inside the window. That falls out of the timing relationship, so it keeps
working on a system whose identifiers this engine has never seen.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PASS = "PASS"
FAIL = "FAIL"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNUSABLE = 2
EXIT_CRASHED = 5

_LINE = re.compile(r"^\s*(\d+)\s{2}(\S+)\s*(.*)$")
_ARM_WINDOW = re.compile(r"within\s+(\d+)\s*ms|for\s+(\d+)\s*ms")
_MET_DELTA = re.compile(r"\(\+(\d+)\s*us")

# Kinds that resolve an assertion, and the verdict each implies.
OUTCOMES = {
    "EXPECT_MET": PASS,
    "EXPECT_UNMET": FAIL,
    "FORBID_MET": PASS,
    "FORBID_HIT": FAIL,
    "UART_MET": PASS,
    "UART_UNMET": FAIL,
    "SYMBOL_MET": PASS,
    "SYMBOL_UNMET": FAIL,
}
ARM_KINDS = {"EXPECT_ARM", "FORBID_ARM"}


class Event:
    __slots__ = ("us", "kind", "text", "line_no")

    def __init__(self, us: int, kind: str, text: str, line_no: int):
        self.us = us
        self.kind = kind
        self.text = text
        self.line_no = line_no

    def label(self) -> str:
        """The assertion label: everything before the trailing parenthetical."""
        cut = self.text.rfind("  (")
        return (self.text[:cut] if cut > 0 else self.text).strip()


def parse_events(text: str) -> List[Event]:
    events: List[Event] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            # A line the toolkit did not write. Not silently skipped: an event
            # log with unexplained content is not one to judge from.
            raise ValueError(
                "event log line %d is not in the expected form:\n  %r\n"
                "Every line is '<virtual microseconds>  <KIND>  <text>'."
                % (line_no, raw))
        events.append(Event(int(m.group(1)), m.group(2), m.group(3), line_no))
    return events


def _window_us(arm_text: str) -> Optional[int]:
    m = _ARM_WINDOW.search(arm_text)
    if not m:
        return None
    return int(m.group(1) or m.group(2)) * 1000


def _decide_uart(ev, uart_dir) -> Dict[str, Any]:
    """Decide a console assertion recorded during the run.

    Deferred to here because the emulator holds the console file open while it
    runs; see harness/can_toolkit.py. Reading the same file a person reads is
    deliberate: an assertion that watched a different stream could disagree with
    the artefact the run leaves behind.
    """
    node, wanted, label = (ev.text.split("|", 2) + ["", ""])[:3]
    record = {"label": label or ("uart: %s" % wanted), "kind": "UART",
              "at_us": ev.us, "armed_us": None, "budget_us": None,
              "latency_us": None, "latency_from": None, "latency_note": None}
    if uart_dir is None:
        record["outcome"] = FAIL
        record["latency_note"] = (
            "no console directory was given, so this assertion could not be "
            "decided. It is reported FAILED rather than assumed satisfied.")
        return record
    path = Path(uart_dir) / ("uart-%s.log" % node)
    if not path.is_file():
        record["outcome"] = FAIL
        record["latency_note"] = "no console capture at %s" % path
        return record
    content = path.read_text(errors="replace")
    if wanted in content:
        record["outcome"] = PASS
    else:
        record["outcome"] = FAIL
        record["latency_note"] = "%s never printed %r" % (node, wanted)
    return record


def judge(events: List[Event], uart_dir=None) -> Dict[str, Any]:
    assertions: List[Dict[str, Any]] = []
    arms: Dict[str, Event] = {}

    for index, ev in enumerate(events):
        if ev.kind in ARM_KINDS:
            arms[ev.label()] = ev
            continue
        if ev.kind == "UART_EXPECT":
            assertions.append(_decide_uart(ev, uart_dir))
            continue
        if ev.kind not in OUTCOMES:
            continue

        label = ev.label()
        arm = arms.get(label)
        record: Dict[str, Any] = {
            "label": label,
            "outcome": OUTCOMES[ev.kind],
            "kind": ev.kind,
            "at_us": ev.us,
            "armed_us": arm.us if arm else None,
            "budget_us": _window_us(arm.text) if arm else None,
            "latency_us": None,
            "latency_from": None,
            "latency_note": None,
        }

        # ---- the latency rule -------------------------------------------
        # The causing stimulus is the last one at or after the arming instant
        # and at or before the observation. A stimulus from an earlier step is
        # NOT a cause of this observation, and measuring from it produces a
        # number that spans two unrelated intervals.
        if arm is not None and OUTCOMES[ev.kind] == PASS and ev.kind == "EXPECT_MET":
            stimulus = None
            for prior in events[:index]:
                if prior.kind not in ("STIM", "INJ"):
                    continue
                if arm.us <= prior.us <= ev.us:
                    stimulus = prior
            if stimulus is not None:
                record["latency_us"] = ev.us - stimulus.us
                record["latency_from"] = stimulus.text.strip()
            else:
                delta = _MET_DELTA.search(ev.text)
                record["latency_note"] = (
                    "no stimulus inside this assertion's window; the causing "
                    "change happened in an earlier step, so no latency is "
                    "quoted. The frame arrived %s us after the assertion was "
                    "armed." % (delta.group(1) if delta else "?"))
        assertions.append(record)

    failed = [a for a in assertions if a["outcome"] == FAIL]
    return {
        "verdict": FAIL if failed else PASS,
        "assertions": assertions,
        "counts": {
            "total": len(assertions),
            "passed": len(assertions) - len(failed),
            "failed": len(failed),
        },
        "first_failure": failed[0]["label"] if failed else None,
        "frames_observed": sum(1 for e in events if e.kind in ("TX", "INJ")),
        "virtual_us": max((e.us for e in events), default=0),
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = []
    counts = result["counts"]
    lines.append("verdict: %s   (%d of %d assertion(s) passed)"
                 % (result["verdict"], counts["passed"], counts["total"]))
    lines.append("frames observed: %d over %d us of virtual time"
                 % (result["frames_observed"], result["virtual_us"]))
    lines.append("")
    for a in result["assertions"]:
        mark = "ok  " if a["outcome"] == PASS else "FAIL"
        lines.append("  [%s] %s" % (mark, a["label"]))
        if a["latency_us"] is not None:
            budget = a["budget_us"]
            if budget:
                lines.append("         %.3f ms after %s  (budget %.0f ms)"
                             % (a["latency_us"] / 1000.0, a["latency_from"],
                                budget / 1000.0))
            else:
                lines.append("         %.3f ms after %s"
                             % (a["latency_us"] / 1000.0, a["latency_from"]))
        elif a["latency_note"]:
            lines.append("         %s" % a["latency_note"])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Turn an event log into a verdict.")
    ap.add_argument("events")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    path = Path(args.events)
    if not path.is_file():
        sys.stderr.write("\nERROR: event log not found: %s\n"
                         "The run produced no record, so there is no verdict to "
                         "reach.\n\n" % path)
        return EXIT_UNUSABLE
    try:
        events = parse_events(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        sys.stderr.write("\nERROR: %s\n\n" % exc)
        return EXIT_UNUSABLE

    if not events:
        sys.stderr.write("\nERROR: the event log is empty. Nothing was observed, "
                         "so no verdict is reported.\n\n")
        return EXIT_UNUSABLE

    result = judge(events, uart_dir=path.parent)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_report(result))
    return EXIT_PASS if result["verdict"] == PASS else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
