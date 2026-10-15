"""The one comparison rule. Batching, determinism and the trace differ all use it.

Two implementations of "are these logs the same" would eventually disagree, and
the disagreement would be discovered as an unreproducible result rather than as a
bug. So there is one function, and ``window_us`` is chosen once from the measured
jitter and used everywhere.

Why a window at all
-------------------
Renode advances each machine on its own host thread. Within a synchronisation
quantum the machines' relative order follows host scheduling, so two runs of the
same scenario can emit the same events in a different interleaving, a few tens of
microseconds apart. Nothing about the guest differs; the observer's ordering does.

Canonical ordering makes that difference disappear without hiding it: events
inside a window of ``window_us`` are sorted into a fixed order before comparison,
and the window is reported. A window of 0 is exact byte comparison.

The honest form of the resulting claim is a measured bound, not an assertion:

    verdicts and latencies reproducible to within X microseconds; event logs
    identical after canonical ordering within a W microsecond window

with X and W stated, against the smallest timing defect in the corpus. A bound
with a number attached is worth more than the word "identical".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NEWLINE = chr(10)

_LINE = re.compile(r"^\s*(\d+)\s{2}(\S+)\s*(.*)$")


class Line:
    __slots__ = ("us", "kind", "text", "raw")

    def __init__(self, us, kind, text, raw):
        self.us, self.kind, self.text, self.raw = us, kind, text, raw

    def key(self):
        """Ordering within a window: by kind then text, never by arrival."""
        return (self.kind, self.text)


def parse(text: str) -> List[Line]:
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            out.append(Line(-1, "UNPARSED", raw.strip(), raw))
            continue
        out.append(Line(int(m.group(1)), m.group(2), m.group(3).rstrip(), raw))
    return out


def canonicalise(lines: List[Line], window_us: int) -> List[str]:
    """Sort events into a fixed order inside each window.

    Timestamps are quantised to the window and events within one window are
    sorted by (kind, text). The quantised timestamp is kept, so an event that
    moves to a different window is still a difference: the window absorbs
    ordering jitter, not displacement.
    """
    if window_us <= 0:
        return [l.raw.rstrip() for l in lines]

    buckets: Dict[int, List[Line]] = {}
    for line in lines:
        buckets.setdefault(line.us // window_us, []).append(line)

    out: List[str] = []
    for bucket in sorted(buckets):
        for line in sorted(buckets[bucket], key=lambda l: l.key()):
            out.append("%d  %s  %s" % (bucket * window_us, line.kind, line.text))
    return out


def logs_equal(a: str, b: str, window_us: int = 0) -> Tuple[bool, Optional[str]]:
    """Compare two event logs. Returns (equal, first difference or None)."""
    ca = canonicalise(parse(a), window_us)
    cb = canonicalise(parse(b), window_us)
    if ca == cb:
        return True, None
    for i, (x, y) in enumerate(zip(ca, cb)):
        if x != y:
            return False, ("first difference at canonical line %d:\n  A: %s\n  B: %s"
                           % (i + 1, x, y))
    return False, ("logs agree for %d line(s) then differ in length: A has %d, B has %d"
                   % (min(len(ca), len(cb)), len(ca), len(cb)))


def smallest_equal_window(a: str, b: str, candidates=None) -> Optional[int]:
    """The smallest window at which two logs agree, or None if none does.

    This is how W is measured rather than guessed.
    """
    if candidates is None:
        candidates = [0, 10, 25, 50, 100, 200, 500, 1000, 2000, 5000]
    for w in candidates:
        ok, _ = logs_equal(a, b, w)
        if ok:
            return w
    return None


def latency_delta(summary_a: Dict[str, Any], summary_b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-assertion latency differences between two runs of the same scenario."""
    by_label_a = {x["label"]: x for x in summary_a.get("assertions", [])}
    by_label_b = {x["label"]: x for x in summary_b.get("assertions", [])}
    out = []
    for label in sorted(set(by_label_a) | set(by_label_b)):
        a, b = by_label_a.get(label), by_label_b.get(label)
        record = {"label": label, "delta_us": None, "note": None}
        if a is None or b is None:
            record["note"] = "present in only one run"
        elif a["outcome"] != b["outcome"]:
            record["note"] = "outcome differs: %s vs %s" % (a["outcome"], b["outcome"])
        elif a.get("latency_us") is None or b.get("latency_us") is None:
            record["note"] = "no latency quoted"
        else:
            record["delta_us"] = abs(a["latency_us"] - b["latency_us"])
        out.append(record)
    return out


# --------------------------------------------------------------------- traces
IDENTICAL = "IDENTICAL"


class Diverged:
    """A trace difference, carrying what a Case A message needs to be written."""

    def __init__(self, checkpoint, machine, count_a, count_b, index):
        self.checkpoint = checkpoint
        self.machine = machine
        self.count_a = count_a
        self.count_b = count_b
        self.index = index

    def __eq__(self, other):
        return isinstance(other, Diverged) and vars(self) == vars(other)

    def __repr__(self):
        return ("DIVERGED(checkpoint=%r, machine=%r, instructions %d vs %d, "
                "delta %+d)" % (self.checkpoint, self.machine, self.count_a,
                                self.count_b, self.count_b - self.count_a))


def parse_trace(text: str) -> List[Tuple[int, str, Dict[str, int]]]:
    out = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        head, _, tail = raw.strip().partition("  ")
        us = int(head.strip())
        parts = tail.split()
        counts, label_parts = {}, []
        for part in parts:
            if "=" in part and part.split("=")[-1].lstrip("-").isdigit():
                name, value = part.rsplit("=", 1)
                counts[name] = int(value)
            else:
                label_parts.append(part)
        out.append((us, " ".join(label_parts), counts))
    return out


def compare_traces(a: str, b: str):
    """IDENTICAL, or the first checkpoint at which execution diverged."""
    ta, tb = parse_trace(a), parse_trace(b)
    for index in range(min(len(ta), len(tb))):
        us_a, label_a, counts_a = ta[index]
        us_b, label_b, counts_b = tb[index]
        if label_a != label_b:
            return Diverged(label_a or "(checkpoint %d)" % index, "-", 0, 0, index)
        for machine in sorted(set(counts_a) | set(counts_b)):
            ca, cb = counts_a.get(machine, -1), counts_b.get(machine, -1)
            if ca != cb:
                return Diverged(label_a, machine, ca, cb, index)
    if len(ta) != len(tb):
        return Diverged("(checkpoint count)", "-", len(ta), len(tb), min(len(ta), len(tb)))
    return IDENTICAL


def read(path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- observables
#
# WHAT A "TRACE" IS IN THIS PROJECT, AND WHY IT IS NOT AN INSTRUCTION COUNT
#
# The first implementation recorded each CPU's retired-instruction count at
# scenario checkpoints. It was wrong, and the way it was wrong is worth keeping:
# it PASSED every stability check while measuring nothing.
#
# Measured, on one binary, same virtual duration, provably different work
# (injecting a high value raises a fault and emits five status frames carrying
# a non-zero fault code; injecting a legal value emits none):
#
#     value=250  ->  ExecutedInstructions = 62639
#     value=900  ->  ExecutedInstructions = 62639
#
# Identical to the digit. On this build the counter tracks elapsed virtual time,
# not work done, so it is constant across behaviours and therefore constant
# across runs. That is why it looked stable in serial mode and unstable in
# parallel: it was reporting quantum drift, never execution.
#
# The replacement is the OBSERVABLE BEHAVIOUR: every frame that appeared on the
# bus, from any node, and every byte of console output. That is not a weaker
# substitute for a basic-block trace; for this project it is the more meaningful
# quantity, because the scenario language can only assert on observable things.
# "The two builds executed identically" is not what Case A needs to mean. What it
# needs to mean is "nothing an assertion in this language could see was
# different", and that is exactly this.
#
# The harness-side bookkeeping lines are excluded, because they describe what the
# TEST did rather than what the FIRMWARE did, and they are identical by
# construction between two runs of the same scenario.

OBSERVABLE_KINDS = ("TX", "TXN", "INJ", "UART")


def observable(events_text: str) -> str:
    """The firmware's observable behaviour, from an event log.

    Timestamps are kept: a frame arriving at a different instant is a genuine
    behavioural difference, and one of the defect families is precisely that.
    """
    out = []
    for line in parse(events_text):
        if line.kind in OBSERVABLE_KINDS:
            out.append("%d  %s  %s" % (line.us, line.kind, line.text))
    return NEWLINE.join(out)


def compare_observable(events_a: str, events_b: str):
    """IDENTICAL, or the first observable difference between two runs."""
    a, b = observable(events_a).splitlines(), observable(events_b).splitlines()
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return ObservableDiff(i + 1, a[i], b[i], len(a), len(b))
    if len(a) != len(b):
        longer = a if len(a) > len(b) else b
        return ObservableDiff(min(len(a), len(b)) + 1,
                              a[len(b)] if len(a) > len(b) else "(nothing)",
                              b[len(a)] if len(b) > len(a) else "(nothing)",
                              len(a), len(b))
    return IDENTICAL


class ObservableDiff:
    """The first point at which two builds behaved differently on the wire."""

    def __init__(self, line_no, a, b, count_a, count_b):
        self.line_no = line_no
        self.a = a
        self.b = b
        self.count_a = count_a
        self.count_b = count_b

    @property
    def at_us(self):
        try:
            return int(self.a.split()[0])
        except Exception:
            return None

    def __repr__(self):
        return ("DIVERGED(observable line %d of %d/%d)%s  golden: %s%s  defective: %s"
                % (self.line_no, self.count_a, self.count_b, NEWLINE, self.a,
                   NEWLINE, self.b))
