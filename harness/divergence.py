"""Classify why a campaign failed to detect a defect.

This is the mechanism rung 4 is built on, and the reason it is a contribution
rather than a prompting trick: the two messages **point in opposite directions**,
and which one applies is derived from the emulator's execution trace rather than
guessed at.

Given a campaign run against both builds:

``DETECTED``
    The verdicts differ in the right direction: pass on golden, fail on the
    defective build. Nothing to explain.

``CASE_A`` -- stimulus failure
    The campaign passed on both, and the two builds executed **identically**. The
    campaign never made the difference matter. The fix is to change *what is
    injected, or when*.

``CASE_B`` -- oracle failure
    The campaign passed on both, but the two builds executed **differently**. The
    defect did change behaviour and nothing was watching the thing that changed.
    The fix is to change *what is checked*, or to tighten a deadline.

``FALSE_ALARM``
    The campaign failed on the golden build. Whatever else is true, this campaign
    cannot be evidence of anything: a test that fails on correct firmware is
    excluded by the definition of the detection set, not by a separate check.

A generator told only "you failed" has no gradient and wanders. One told which of
these applies moves along the right axis. Nobody has separated stimulus failure
from oracle failure for firmware, and the split is only computable because there
are two binaries, an emulator, and a trace.
"""

from __future__ import annotations

from harness import compare

DETECTED = "DETECTED"
CASE_A = "CASE_A"
CASE_B = "CASE_B"
FALSE_ALARM = "FALSE_ALARM"
INCONCLUSIVE = "INCONCLUSIVE"

PASS = "PASS"
FAIL = "FAIL"


class Classification:
    __slots__ = ("case", "trace_result", "golden_verdict", "defective_verdict", "detail")

    def __init__(self, case, trace_result, golden_verdict, defective_verdict, detail):
        self.case = case
        self.trace_result = trace_result
        self.golden_verdict = golden_verdict
        self.defective_verdict = defective_verdict
        self.detail = detail

    def __repr__(self):
        return "Classification(%s, golden=%s, defective=%s)" % (
            self.case, self.golden_verdict, self.defective_verdict)


def classify(golden_events: str, defective_events: str,
             golden_verdict: str, defective_verdict: str) -> Classification:
    """Which of the four outcomes this campaign reached, and why.

    The inputs are EVENT LOGS, and the divergence signal is the firmware's
    observable behaviour within them: every frame from every node, and every byte
    of console. See harness/compare.py for why this rather than an instruction
    count, which on this emulator tracks virtual time and not work.
    """
    trace_result = compare.compare_observable(golden_events, defective_events)
    identical = trace_result == compare.IDENTICAL

    if golden_verdict != PASS:
        return Classification(
            FALSE_ALARM, trace_result, golden_verdict, defective_verdict,
            "The campaign failed on the CORRECT build, so it is not evidence of a "
            "defect. Soundness is part of the definition of a detection: a campaign "
            "that raises a false alarm is excluded by construction, not by a "
            "separate check.")

    if defective_verdict == FAIL:
        return Classification(
            DETECTED, trace_result, golden_verdict, defective_verdict,
            "Passed on the correct build and failed on the defective one.")

    if identical:
        return Classification(
            CASE_A, trace_result, golden_verdict, defective_verdict,
            "The two builds executed IDENTICALLY under this campaign. The defect was "
            "never reached in a way that made it matter, so no assertion could have "
            "caught it. Change what you inject, or when.")

    return Classification(
        CASE_B, trace_result, golden_verdict, defective_verdict,
        "The two builds executed DIFFERENTLY under this campaign, and both still "
        "passed. The defect changed behaviour and nothing was watching the thing "
        "that changed. Change what you check, or tighten a deadline.")


def message(classification: Classification, max_lines: int = 40) -> str:
    """The feedback a generator receives. Deliberately short.

    A generator handed a full event log spends its attention parsing it and loses
    the thread by the third attempt, so this is capped. Verdict, one cause, one
    concrete instruction, nothing else.
    """
    c = classification
    lines = []
    if c.case == DETECTED:
        lines.append("DETECTED: the campaign passed on the correct build and failed "
                     "on the defective one.")
    elif c.case == FALSE_ALARM:
        lines.append("FALSE ALARM: your campaign failed on the CORRECT build.")
        lines.append("A campaign that fails on correct firmware is not evidence of a "
                     "defect and cannot count as a detection.")
        lines.append("Fix the campaign so it passes on the correct build first.")
    elif c.case == CASE_A:
        lines.append("NOT DETECTED, and the reason is the STIMULUS.")
        lines.append("Your campaign produced identical execution on both builds. It "
                     "never made the difference observable.")
        lines.append("Change what you inject, or when. Probing further from the "
                     "boundary will not help; the two builds behave the same there.")
    elif c.case == CASE_B:
        lines.append("NOT DETECTED, and the reason is the ORACLE.")
        lines.append("The two builds behaved DIFFERENTLY under your campaign, and "
                     "your assertions did not observe the difference.")
        detail = c.trace_result
        if hasattr(detail, "at_us") and detail.at_us is not None:
            lines.append("First difference at %d us of virtual time:" % detail.at_us)
            lines.append("  correct build:   %s" % detail.a)
            lines.append("  defective build: %s" % detail.b)
        lines.append("Change what you check, or tighten a deadline. The stimulus is "
                     "already right.")
    else:
        lines.append("INCONCLUSIVE.")
    return "\n".join(lines[:max_lines])
