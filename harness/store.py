"""Run storage with enforced provenance (Phase 1, TASK 1.10).

A run record is the only durable evidence that a campaign ever executed. Every
number that reaches the paper is read back out of one of these directories, so a
record that cannot say *which binaries*, *which emulator* and *which toolchain*
produced it is not weak evidence, it is no evidence. This module therefore
refuses to store one.

Layout written under ``results/runs/<run_id>/``::

    summary.json      verdict, counts, host_wall_seconds
    provenance.json   sha256 per binary, pinned tool versions
    replay.txt        the exact command that reproduces this run
    scenario.yml      the exact scenario executed
    events.log        every event, one JSON object per line, virtual microseconds
    trace/            execution trace, when one was captured
    COMPLETE          completion marker with a file manifest, written LAST

The central rule: a run without provenance is REFUSED AT WRITE and REFUSED AT
READ
-----------------------------------------------------------------------------
Both checks exist, deliberately and independently. ``read_run`` does not trust
``write_run``. Refusing only at write would assume that the writer is the only
way a directory can appear under ``results/runs/``, and it is not: a run
directory can be copied in from another machine, restored from a backup,
unpacked from an archive, hand-edited, or left behind by a job that was killed
between two ``write`` calls. Every one of those produces a directory that the
writer never validated. ``read_run`` re-runs the full provenance check on the
bytes it finds on disk, and refuses on its own authority.

Completeness: staging directory, atomic rename, and a marker written last
-------------------------------------------------------------------------
Two belts, because a half-written run that reads as complete is a silently wrong
number in a results table.

1. Everything is written into a hidden staging directory
   ``results/runs/.partial-<run_id>-<random>/`` and moved into place with a
   single ``os.rename``. A crash mid-write therefore leaves a hidden staging
   directory, never a partial run directory. ``list_runs`` ignores hidden
   entries, so the wreckage is never mistaken for a run.
2. The ``COMPLETE`` marker is written last, inside the staging directory, and
   carries a sha256 manifest of the record files. ``read_run`` requires the
   marker, requires the run id inside it to match the directory it was found in,
   and re-hashes every file the manifest names. A directory assembled by any
   other means, truncated by a full disk, or edited after the fact fails that
   check and is refused by name.

Overwrite policy: **an existing run id is refused, never replaced.**
Documented here because the task allows either. Refusal was chosen because a run
id is the name of a piece of evidence, and evidence is not overwritten in place.
Silently replacing one would mean that re-running a campaign under the same id
destroys the record it disagrees with, which is the single most useful thing to
have kept. Atomic replacement would protect against a *stale* result being read
as this run's, but the staging-plus-rename above already provides that, so
refusal costs nothing and preserves the earlier record. Removing a run
directory is left to an operator who means it.

Virtual time versus host wall clock
-----------------------------------
Every timestamp in this project is VIRTUAL time taken from the emulator. Host
wall clock is only ever used for cost accounting, never as a measurement of the
system under test. The one host-clock field in the record is named
``host_wall_seconds`` precisely so that it can never be read, quoted, or plotted
as a latency; see the comment on its validation below. Events carry
``t_virtual_us``, integer microseconds of virtual time, and an event without one
is refused rather than stored with a host timestamp standing in for it.

This module contains no project data: no node names, no message identifiers, no
peripheral names, no signal names. It stores whatever the data files describe
and never encodes what they contain.
"""

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Verdict",
    "RunRecord",
    "StoreError",
    "RunIdError",
    "RunExistsError",
    "RunNotFoundError",
    "IncompleteRunError",
    "ProvenanceError",
    "SummaryError",
    "EventError",
    "ReplayError",
    "TraceError",
    "IntegrityError",
    "REQUIRED_PROVENANCE_FIELDS",
    "REQUIRED_SUMMARY_FIELDS",
    "SCHEMA_VERSION",
    "write_run",
    "read_run",
    "list_runs",
    "run_path",
    "runs_root",
    "sha256_of_file",
    "sha256_of_text",
]


SCHEMA_VERSION = 1

SUMMARY_NAME = "summary.json"
PROVENANCE_NAME = "provenance.json"
REPLAY_NAME = "replay.txt"
SCENARIO_NAME = "scenario.yml"
EVENTS_NAME = "events.log"
TRACE_DIRNAME = "trace"
COMPLETION_MARKER = "COMPLETE"

# The files covered by the COMPLETE manifest. trace/ is deliberately not
# covered: a trace can be large and is optional evidence, while these five are
# the record itself and must be intact or the run is not readable.
MANIFEST_FILES = (
    SUMMARY_NAME,
    PROVENANCE_NAME,
    REPLAY_NAME,
    SCENARIO_NAME,
    EVENTS_NAME,
)

_STAGING_PREFIX = ".partial-"

# Every field is mandatory. There is no default for any of them, because every
# plausible default is a lie: guessing the Renode version records a version that
# was not used, and an invented binary hash makes an unreproducible run look
# reproducible.
REQUIRED_PROVENANCE_FIELDS = (
    "binary_sha256",       # mapping: node id -> sha256 of that node's ELF
    "renode_version",
    "zephyr_version",
    "zephyr_sdk_version",
    "harness_git_commit",
    "scenario_sha256",     # sha256 of the scenario text stored beside it
)

REQUIRED_SUMMARY_FIELDS = (
    "verdict",
    "counts",
    "host_wall_seconds",
)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# A run id becomes a directory name. Anything that could escape results/runs/ or
# collide with the hidden staging prefix is refused rather than sanitised:
# rewriting "../../etc" into something safe would store the run under a name the
# caller never asked for, and the caller would then look for it under the name
# it did ask for and find nothing.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class Verdict(Enum):
    """The only verdicts this project has, with their process exit codes.

    These are stored, never invented. A campaign that produced something not in
    this list has produced a result nobody has defined the meaning of, and the
    store refuses it rather than filing it under the nearest match.

    Note the distinction from this module's own refusals: ``Verdict.REFUSED`` is
    a statement about the *run* (the scenario was rejected before execution).
    When ``write_run`` or ``read_run`` refuses, it raises ``StoreError``; it
    does not return an exit code and does not quietly downgrade a verdict.
    """

    PASS = 0
    FAIL = 1
    UNUSABLE = 2
    REFUSED = 3
    DRY_RUN = 4
    CRASHED = 5

    @property
    def exit_code(self):
        return self.value


_VERDICT_BY_NAME = {v.name: v for v in Verdict}
_VERDICT_BY_CODE = {v.value: v for v in Verdict}
_VERDICT_HELP = ", ".join("%s=%d" % (v.name, v.value) for v in Verdict)


class StoreError(Exception):
    """Base class for every refusal this module makes."""


class RunIdError(StoreError):
    """The run id is not usable as a run directory name."""


class RunExistsError(StoreError):
    """A run directory with this id already exists and will not be overwritten."""


class RunNotFoundError(StoreError):
    """No run directory with this id exists."""


class IncompleteRunError(StoreError):
    """The run directory is missing its completion marker."""


class ProvenanceError(StoreError):
    """Provenance is absent, incomplete, or malformed."""


class SummaryError(StoreError):
    """The summary is absent, incomplete, or malformed."""


class EventError(StoreError):
    """An event is malformed or is missing its virtual-time stamp."""


class ReplayError(StoreError):
    """The replay command is absent or malformed."""


class TraceError(StoreError):
    """A trace directory was named but is not usable."""


class IntegrityError(StoreError):
    """The bytes on disk disagree with the record's own manifest."""


# ---------------------------------------------------------------------------
# hashing helpers
# ---------------------------------------------------------------------------

def sha256_of_bytes(data):
    """Lowercase hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def sha256_of_text(text):
    """Lowercase hex sha256 of ``text`` encoded UTF-8.

    This is the hash that must appear in ``provenance["scenario_sha256"]``.
    """
    if not isinstance(text, str):
        raise TypeError(
            "sha256_of_text() takes str, got %s. Hash bytes with "
            "sha256_of_bytes() so the encoding is explicit." % type(text).__name__
        )
    return sha256_of_bytes(text.encode("utf-8"))


def sha256_of_file(path):
    """Lowercase hex sha256 of a file, read in binary. Use this for ELFs."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# location
# ---------------------------------------------------------------------------

def runs_root(root=None):
    """Return the directory that holds run directories.

    ``root`` wins when given (tests pass a tmp_path). Otherwise ``$DETECT_ROOT``
    from ``.detect-env``, otherwise the repository this module lives in.
    """
    if root is not None:
        base = Path(root)
    else:
        env_root = os.environ.get("DETECT_ROOT")
        base = Path(env_root) if env_root else Path(__file__).resolve().parent.parent
    return base / "results" / "runs"


def _check_run_id(run_id):
    if not isinstance(run_id, str):
        raise RunIdError(
            "REFUSED: run id must be a str, got %s (%r). The run id becomes a "
            "directory name." % (type(run_id).__name__, run_id)
        )
    if not _RUN_ID_RE.match(run_id):
        raise RunIdError(
            "REFUSED: run id %r is not usable as a directory name.\n"
            "  A run id must start with a letter or digit and contain only\n"
            "  letters, digits, '.', '_' and '-', at most 128 characters. Path\n"
            "  separators and a leading '.' are refused so a run can never be\n"
            "  written outside results/runs/ and can never be mistaken for a\n"
            "  hidden staging directory.\n"
            "  Fix the caller to pass a clean id; it will not be sanitised for\n"
            "  you, because storing a run under a name you did not ask for is\n"
            "  worse than stopping." % run_id
        )
    return run_id


def run_path(run_id, root=None):
    """Absolute path of a run directory. Does not require it to exist."""
    _check_run_id(run_id)
    return runs_root(root) / run_id


# ---------------------------------------------------------------------------
# validation, shared by write and read
# ---------------------------------------------------------------------------

def _where(stage, run_id):
    return "%s of run %r" % (stage, run_id)


def _check_hex64(value, label, run_id, stage, exc):
    if not isinstance(value, str):
        raise exc(
            "REFUSED: %s: %s must be a 64-character lowercase hex sha256 "
            "string, got %s (%r)." % (_where(stage, run_id), label,
                                      type(value).__name__, value)
        )
    if not _HEX64_RE.match(value):
        raise exc(
            "REFUSED: %s: %s is not a sha256 digest: %r.\n"
            "  Expected 64 lowercase hex characters, as produced by\n"
            "  hashlib.sha256(...).hexdigest(). It is not re-cased or padded "
            "for you." % (_where(stage, run_id), label, value)
        )
    return value


def _validate_provenance(provenance, run_id, stage, scenario_text=None,
                         scenario_bytes=None):
    """Refuse anything that is not a complete, well-formed provenance record.

    Called by ``write_run`` before anything touches the disk, and again by
    ``read_run`` on the bytes found on disk. The second call is not redundant:
    the writer is not the only way a run directory can come into existence.

    Nothing here fills in a missing field. A record is complete or it is refused
    by the name of the first field that is not.
    """
    if not isinstance(provenance, dict):
        raise ProvenanceError(
            "REFUSED: %s: provenance must be a mapping, got %s.\n"
            "  Required fields: %s"
            % (_where(stage, run_id), type(provenance).__name__,
               ", ".join(REQUIRED_PROVENANCE_FIELDS))
        )

    for field in REQUIRED_PROVENANCE_FIELDS:
        if field not in provenance:
            raise ProvenanceError(
                "REFUSED: %s: provenance is missing required field %r.\n"
                "  Required fields are: %s\n"
                "  No default is supplied for any of them. A guessed tool "
                "version or an invented binary hash would make an "
                "unreproducible run look reproducible."
                % (_where(stage, run_id), field,
                   ", ".join(REQUIRED_PROVENANCE_FIELDS))
            )
        if provenance[field] is None:
            raise ProvenanceError(
                "REFUSED: %s: provenance field %r is None.\n"
                "  Present-but-empty is the same as missing. Record the real "
                "value or do not write the run." % (_where(stage, run_id), field)
            )

    binaries = provenance["binary_sha256"]
    if not isinstance(binaries, dict):
        raise ProvenanceError(
            "REFUSED: %s: provenance field 'binary_sha256' must be a mapping of "
            "node id -> sha256, got %s (%r)."
            % (_where(stage, run_id), type(binaries).__name__, binaries)
        )
    if not binaries:
        raise ProvenanceError(
            "REFUSED: %s: provenance field 'binary_sha256' is empty.\n"
            "  A run executed at least one binary; record the sha256 of every "
            "ELF involved, keyed by node id. An empty mapping would claim the "
            "run ran no code." % _where(stage, run_id)
        )
    for node_id, digest in binaries.items():
        if not isinstance(node_id, str) or not node_id.strip():
            raise ProvenanceError(
                "REFUSED: %s: 'binary_sha256' has a node id that is not a "
                "non-empty string: %r." % (_where(stage, run_id), node_id)
            )
        _check_hex64(digest, "binary_sha256[%r]" % node_id, run_id, stage,
                     ProvenanceError)

    for field in ("renode_version", "zephyr_version", "zephyr_sdk_version",
                  "harness_git_commit"):
        value = provenance[field]
        if not isinstance(value, str) or not value.strip():
            raise ProvenanceError(
                "REFUSED: %s: provenance field %r must be a non-empty string, "
                "got %s (%r).\n"
                "  This is a pinned version or commit; record what was actually "
                "used." % (_where(stage, run_id), field, type(value).__name__,
                           value)
            )

    _check_hex64(provenance["scenario_sha256"], "scenario_sha256", run_id,
                 stage, ProvenanceError)

    # The scenario hash must describe the scenario stored beside it. A
    # provenance record that hashes some other text is worse than one with no
    # hash at all, because it looks checked. The mismatch is reported, never
    # corrected: recomputing it here would turn a falsified record into a valid
    # one on the way past.
    if scenario_text is not None:
        actual = sha256_of_text(scenario_text)
    elif scenario_bytes is not None:
        actual = sha256_of_bytes(scenario_bytes)
    else:
        actual = None
    if actual is not None and actual != provenance["scenario_sha256"]:
        raise ProvenanceError(
            "REFUSED: %s: provenance 'scenario_sha256' does not describe the "
            "scenario.\n"
            "    recorded: %s\n"
            "    actual:   %s\n"
            "  It is not recomputed for you. Either the wrong scenario text was "
            "passed alongside this provenance, or the scenario was edited after "
            "the hash was taken."
            % (_where(stage, run_id), provenance["scenario_sha256"], actual)
        )
    return provenance


def _coerce_verdict(value, run_id, stage):
    """Map a caller's verdict onto the fixed set, or refuse it by name."""
    if isinstance(value, Verdict):
        return value
    if isinstance(value, bool):
        # bool is a subclass of int; True would silently become FAIL.
        raise SummaryError(
            "REFUSED: %s: summary 'verdict' is a bool (%r). A verdict is one of "
            "%s." % (_where(stage, run_id), value, _VERDICT_HELP)
        )
    if isinstance(value, int):
        if value in _VERDICT_BY_CODE:
            return _VERDICT_BY_CODE[value]
        raise SummaryError(
            "REFUSED: %s: summary 'verdict' is exit code %d, which is not a "
            "defined verdict.\n"
            "  The defined verdicts are: %s\n"
            "  No new exit code is invented and no nearby code is substituted."
            % (_where(stage, run_id), value, _VERDICT_HELP)
        )
    if isinstance(value, str):
        if value in _VERDICT_BY_NAME:
            return _VERDICT_BY_NAME[value]
        raise SummaryError(
            "REFUSED: %s: summary 'verdict' is %r, which is not a defined "
            "verdict.\n"
            "  The defined verdicts are: %s\n"
            "  The spelling is exact and upper case; it is not normalised for "
            "you, because a verdict guessed from a near-miss spelling is a "
            "result nobody produced."
            % (_where(stage, run_id), value, _VERDICT_HELP)
        )
    raise SummaryError(
        "REFUSED: %s: summary 'verdict' must be a Verdict, its exact name, or "
        "its exit code, got %s (%r). Defined verdicts: %s"
        % (_where(stage, run_id), type(value).__name__, value, _VERDICT_HELP)
    )


def _validate_summary(summary, run_id, stage):
    """Refuse a summary that lacks a verdict, counts, or host_wall_seconds.

    Returns ``(canonical_summary, verdict)``. The canonical copy always carries
    both ``verdict`` (the name) and ``exit_code`` (the integer), so a reader
    never has to derive one from the other.
    """
    if not isinstance(summary, dict):
        raise SummaryError(
            "REFUSED: %s: summary must be a mapping, got %s. Required fields: %s"
            % (_where(stage, run_id), type(summary).__name__,
               ", ".join(REQUIRED_SUMMARY_FIELDS))
        )

    for field in REQUIRED_SUMMARY_FIELDS:
        if field not in summary:
            raise SummaryError(
                "REFUSED: %s: summary is missing required field %r.\n"
                "  Required fields are: %s"
                % (_where(stage, run_id), field,
                   ", ".join(REQUIRED_SUMMARY_FIELDS))
            )

    verdict = _coerce_verdict(summary["verdict"], run_id, stage)

    if "exit_code" in summary and summary["exit_code"] != verdict.exit_code:
        raise SummaryError(
            "REFUSED: %s: summary 'verdict' and 'exit_code' disagree: verdict "
            "%s is exit code %d, but 'exit_code' says %r.\n"
            "  One of the two is wrong and the store will not pick a winner."
            % (_where(stage, run_id), verdict.name, verdict.exit_code,
               summary["exit_code"])
        )

    counts = summary["counts"]
    if not isinstance(counts, dict):
        raise SummaryError(
            "REFUSED: %s: summary 'counts' must be a mapping of name -> integer "
            "count, got %s (%r)."
            % (_where(stage, run_id), type(counts).__name__, counts)
        )
    for name, value in counts.items():
        if not isinstance(name, str) or not name.strip():
            raise SummaryError(
                "REFUSED: %s: summary 'counts' has a key that is not a "
                "non-empty string: %r." % (_where(stage, run_id), name)
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise SummaryError(
                "REFUSED: %s: summary count %r is %s (%r), not an integer.\n"
                "  A count is a whole number of things that happened; a float "
                "or a bool means something computed it wrong."
                % (_where(stage, run_id), name, type(value).__name__, value)
            )
        if value < 0:
            raise SummaryError(
                "REFUSED: %s: summary count %r is negative (%d). It is not "
                "clamped to zero; a negative count means the counter is wrong "
                "and that must be fixed, not hidden."
                % (_where(stage, run_id), name, value)
            )

    # host_wall_seconds. The name is deliberate and must not be shortened to
    # "duration", "elapsed", "seconds" or anything else that could be read as a
    # measurement of the system under test. Every timestamp this project
    # measures is VIRTUAL time from the emulator; host wall clock varies with
    # machine load, host scheduling and emulator speed, and is recorded ONLY for
    # cost accounting (how much compute a campaign consumed). It must never
    # appear in a latency, a deadline, or a response-time result.
    wall = summary["host_wall_seconds"]
    if isinstance(wall, bool) or not isinstance(wall, (int, float)):
        raise SummaryError(
            "REFUSED: %s: summary 'host_wall_seconds' must be a number of "
            "seconds, got %s (%r).\n"
            "  It is host wall clock for cost accounting only, never a latency; "
            "latencies come from virtual time."
            % (_where(stage, run_id), type(wall).__name__, wall)
        )
    if not math.isfinite(wall):
        raise SummaryError(
            "REFUSED: %s: summary 'host_wall_seconds' is %r, which is not a "
            "finite number of seconds." % (_where(stage, run_id), wall)
        )
    if wall < 0:
        raise SummaryError(
            "REFUSED: %s: summary 'host_wall_seconds' is negative (%r).\n"
            "  It is not clamped to zero. A negative wall time means the clock "
            "was read wrong, and recording a plausible zero would hide that."
            % (_where(stage, run_id), wall)
        )

    canonical = dict(summary)
    canonical["verdict"] = verdict.name
    canonical["exit_code"] = verdict.exit_code
    return canonical, verdict


def _validate_events(events, run_id, stage):
    """Refuse events that carry no virtual-time stamp.

    ``t_virtual_us`` is integer microseconds of VIRTUAL time from the emulator.
    An event without one cannot be placed on the timeline the judge reasons
    over, and the only clock available to substitute would be the host's, which
    would silently turn cost accounting into a measurement.
    """
    if isinstance(events, (str, bytes, dict)) or not hasattr(events, "__iter__"):
        raise EventError(
            "REFUSED: %s: events must be a sequence of event mappings, got %s."
            % (_where(stage, run_id), type(events).__name__)
        )
    checked = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise EventError(
                "REFUSED: %s: event %d is %s, not a mapping: %r"
                % (_where(stage, run_id), index, type(event).__name__, event)
            )
        if "t_virtual_us" not in event:
            raise EventError(
                "REFUSED: %s: event %d has no 't_virtual_us'.\n"
                "  Every event carries integer microseconds of VIRTUAL time "
                "from the emulator. There is no fallback to host time: that "
                "would turn cost accounting into a measurement.\n"
                "  Event was: %r" % (_where(stage, run_id), index, event)
            )
        stamp = event["t_virtual_us"]
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            raise EventError(
                "REFUSED: %s: event %d has t_virtual_us=%r (%s), not an "
                "integer.\n"
                "  Virtual time is whole microseconds; it is not rounded for "
                "you." % (_where(stage, run_id), index, stamp,
                          type(stamp).__name__)
            )
        if stamp < 0:
            raise EventError(
                "REFUSED: %s: event %d has a negative t_virtual_us (%d). "
                "Virtual time starts at 0."
                % (_where(stage, run_id), index, stamp)
            )
        checked.append(event)
    return checked


def _validate_replay(replay_command, run_id, stage):
    if not isinstance(replay_command, str):
        raise ReplayError(
            "REFUSED: %s: replay_command must be the exact shell command "
            "string that reproduces this run, got %s (%r).\n"
            "  A list is not joined for you: shell quoting is subtle enough "
            "that a wrongly joined command would be recorded as reproducing a "
            "run it does not reproduce."
            % (_where(stage, run_id), type(replay_command).__name__,
               replay_command)
        )
    if not replay_command.strip():
        raise ReplayError(
            "REFUSED: %s: replay_command is empty.\n"
            "  replay.txt must contain the exact command that reproduces this "
            "run. No placeholder is written in its place."
            % _where(stage, run_id)
        )
    return replay_command


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------

def _json_bytes(obj):
    """Deterministic JSON: sorted keys, fixed indent, explicit UTF-8, LF only.

    Byte-identical input must produce a byte-identical file, on any host, or the
    determinism criterion for Phase 1 cannot be checked by hashing.
    """
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=True)
            + "\n").encode("utf-8")


def _events_bytes(events):
    lines = [json.dumps(event, sort_keys=True, ensure_ascii=True)
             for event in events]
    text = "".join(line + "\n" for line in lines)
    return text.encode("utf-8")


def _write_file(path, data):
    """Write bytes. Binary mode throughout: no newline translation, ever.

    The repository lives on a Windows filesystem reached from WSL. Text mode
    would let a host decide the line endings, and the file's sha256 would then
    depend on which side wrote it.
    """
    with open(path, "wb") as handle:
        handle.write(data)


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------

def write_run(run_id, scenario_text, events, summary, provenance,
              trace_dir=None, *, replay_command, root=None):
    """Write one run record and return its path.

    ``replay_command`` is keyword-only and required: ``replay.txt`` is part of
    the layout, and there is no honest default for "the command that reproduces
    this run". A caller that omits it gets a loud TypeError naming it, which is
    the intended outcome; the alternative is a replay.txt containing a guess.

    Everything is validated before any directory is created, so a refused write
    leaves nothing at all on disk. The record is then staged in a hidden
    directory and moved into place with one rename.

    Refuses, by name, when: the run id is unusable; the run id already exists;
    provenance is incomplete or does not describe the scenario; the summary
    lacks a verdict, counts or host_wall_seconds; an event has no virtual-time
    stamp; the replay command is missing; a named trace directory is not there.
    """
    _check_run_id(run_id)
    stage = "write"

    if not isinstance(scenario_text, str):
        raise StoreError(
            "REFUSED: %s: scenario_text must be the exact scenario source as "
            "str, got %s. Decode bytes with an explicit encoding before "
            "storing them." % (_where(stage, run_id), type(scenario_text).__name__)
        )

    provenance = _validate_provenance(provenance, run_id, stage,
                                      scenario_text=scenario_text)
    canonical_summary, verdict = _validate_summary(summary, run_id, stage)
    checked_events = _validate_events(events, run_id, stage)
    replay_command = _validate_replay(replay_command, run_id, stage)

    source_trace = None
    if trace_dir is not None:
        source_trace = Path(trace_dir)
        if not source_trace.is_dir():
            raise TraceError(
                "REFUSED: %s: trace_dir %s is not an existing directory.\n"
                "  A caller that names a trace directory expects the trace to "
                "be stored. Skipping it silently would produce a run that looks "
                "as though no trace was captured."
                % (_where(stage, run_id), source_trace)
            )

    root_dir = runs_root(root)
    final = root_dir / run_id

    # Overwrite policy: refuse. See the module docstring.
    if final.exists():
        raise RunExistsError(
            "REFUSED: run %r already exists at %s and will not be "
            "overwritten.\n"
            "  A run id names a piece of evidence. Replacing it would destroy "
            "the record that disagrees with this one, which is the record most "
            "worth keeping.\n"
            "  Use a different run id, or remove that directory deliberately "
            "first." % (run_id, final)
        )

    root_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="%s%s-" % (_STAGING_PREFIX, run_id),
                                    dir=str(root_dir)))
    try:
        _write_file(staging / SCENARIO_NAME, scenario_text.encode("utf-8"))
        _write_file(staging / EVENTS_NAME, _events_bytes(checked_events))
        _write_file(staging / SUMMARY_NAME, _json_bytes(canonical_summary))
        _write_file(staging / PROVENANCE_NAME, _json_bytes(provenance))
        _write_file(staging / REPLAY_NAME,
                    (replay_command.rstrip("\n") + "\n").encode("utf-8"))

        has_trace = False
        if source_trace is not None:
            shutil.copytree(str(source_trace), str(staging / TRACE_DIRNAME))
            has_trace = True

        # The marker is written LAST and hashes what is already there. Until
        # this file exists the directory is not a run, and read_run says so.
        marker = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "verdict": verdict.name,
            "exit_code": verdict.exit_code,
            "has_trace": has_trace,
            "files": {name: sha256_of_file(staging / name)
                      for name in MANIFEST_FILES},
        }
        _write_file(staging / COMPLETION_MARKER, _json_bytes(marker))

        try:
            os.rename(str(staging), str(final))
        except OSError as exc:
            # Lost a race with another writer of the same id between the
            # existence check and here. Refuse; do not clobber.
            raise RunExistsError(
                "REFUSED: run %r could not be moved into place at %s: %s\n"
                "  Another writer very likely created that run id "
                "concurrently. Nothing was overwritten."
                % (run_id, final, exc)
            ) from exc
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise

    return final


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunRecord:
    """One complete run record, read back from disk and re-validated."""

    run_id: str
    path: Path
    verdict: Verdict
    exit_code: int
    host_wall_seconds: float
    counts: dict
    summary: dict
    provenance: dict
    scenario_text: str
    events: list
    replay_command: str
    trace_dir: object  # Path when a trace was captured, else None


def _read_bytes(path, run_id, missing_exc):
    if not path.is_file():
        raise missing_exc(
            "REFUSED: run %r at %s is missing %s.\n"
            "  The record is incomplete and will not be read as a result."
            % (run_id, path.parent, path.name)
        )
    return path.read_bytes()


def _load_json(raw, path, run_id, exc):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise exc(
            "REFUSED: run %r: %s is not readable JSON: %s"
            % (run_id, path.name, err)
        ) from err


def read_run(run_id, root=None):
    """Read one run back, refusing anything that is not a complete record.

    This repeats every check ``write_run`` made, on purpose. A directory under
    ``results/runs/`` was not necessarily produced by this machine or by this
    code: it may have been copied in, restored, unpacked or hand-edited. Read
    validates the bytes in front of it and trusts nothing about how they got
    there.

    Refuses, naming the run id, when: the directory is absent; the COMPLETE
    marker is absent (a half-written or hand-assembled directory); the marker
    describes a different run id or schema; a manifested file's sha256 no longer
    matches; provenance.json is absent, incomplete or does not describe the
    stored scenario; the summary is incomplete; an event has no virtual-time
    stamp.
    """
    _check_run_id(run_id)
    stage = "read"
    path = runs_root(root) / run_id

    if not path.is_dir():
        raise RunNotFoundError(
            "REFUSED: run %r not found: %s does not exist." % (run_id, path)
        )

    # Gate 1: completeness. The marker is written last, so its absence means the
    # directory was never finished, or was never written by this store at all.
    marker_path = path / COMPLETION_MARKER
    if not marker_path.is_file():
        raise IncompleteRunError(
            "REFUSED: run %r at %s has no %s marker, so it is not a complete "
            "run and will not be read as one.\n"
            "  The marker is written last, after every other file. Its absence "
            "means the run was killed part way through, or the directory was "
            "assembled by something other than harness.store.write_run.\n"
            "  Re-run it. A partial record is not a result."
            % (run_id, path, COMPLETION_MARKER)
        )
    marker = _load_json(_read_bytes(marker_path, run_id, IncompleteRunError),
                        marker_path, run_id, IncompleteRunError)

    if marker.get("schema_version") != SCHEMA_VERSION:
        raise IncompleteRunError(
            "REFUSED: run %r was written with store schema version %r, but this "
            "harness reads version %d. The record is not silently reinterpreted."
            % (run_id, marker.get("schema_version"), SCHEMA_VERSION)
        )
    if marker.get("run_id") != run_id:
        raise IntegrityError(
            "REFUSED: run %r at %s carries a %s marker for run %r.\n"
            "  The directory was renamed or copied over another run. A run id "
            "is part of the record's identity, so the two cannot be "
            "reconciled here."
            % (run_id, path, COMPLETION_MARKER, marker.get("run_id"))
        )

    # Gate 2: integrity of the manifested files.
    manifest = marker.get("files")
    if not isinstance(manifest, dict):
        raise IntegrityError(
            "REFUSED: run %r: %s marker has no file manifest."
            % (run_id, COMPLETION_MARKER)
        )
    for name in MANIFEST_FILES:
        if name not in manifest:
            raise IntegrityError(
                "REFUSED: run %r: %s marker does not cover %s."
                % (run_id, COMPLETION_MARKER, name)
            )
        member = path / name
        if not member.is_file():
            raise IncompleteRunError(
                "REFUSED: run %r at %s is missing %s, which its %s marker "
                "lists. The record has been damaged since it was written."
                % (run_id, path, name, COMPLETION_MARKER)
            )
        actual = sha256_of_file(member)
        if actual != manifest[name]:
            raise IntegrityError(
                "REFUSED: run %r: %s does not match the sha256 recorded when "
                "the run was written.\n"
                "    recorded: %s\n"
                "    on disk:  %s\n"
                "  The file was truncated or edited after the fact. Its "
                "contents are not evidence of this run."
                % (run_id, name, manifest[name], actual)
            )
    if marker.get("has_trace") and not (path / TRACE_DIRNAME).is_dir():
        raise IncompleteRunError(
            "REFUSED: run %r recorded a trace, but %s/ is not present at %s."
            % (run_id, TRACE_DIRNAME, path)
        )

    # Gate 3: provenance, checked independently of the writer.
    prov_path = path / PROVENANCE_NAME
    if not prov_path.is_file():
        raise ProvenanceError(
            "REFUSED: run %r at %s has no %s, so it has no provenance and "
            "cannot be read.\n"
            "  A run without provenance cannot say which binaries, which "
            "emulator or which toolchain produced it, so its numbers mean "
            "nothing. Required fields are: %s\n"
            "  This is checked here as well as at write time because a run "
            "directory can be copied in, restored from a backup, or left "
            "behind by a killed job."
            % (run_id, path, PROVENANCE_NAME,
               ", ".join(REQUIRED_PROVENANCE_FIELDS))
        )
    provenance = _load_json(prov_path.read_bytes(), prov_path, run_id,
                            ProvenanceError)
    scenario_bytes = (path / SCENARIO_NAME).read_bytes()
    _validate_provenance(provenance, run_id, stage,
                         scenario_bytes=scenario_bytes)

    # Gate 4: the summary and the events, likewise re-checked.
    summary_path = path / SUMMARY_NAME
    summary_raw = _load_json(summary_path.read_bytes(), summary_path, run_id,
                             SummaryError)
    summary, verdict = _validate_summary(summary_raw, run_id, stage)

    events = []
    events_raw = (path / EVENTS_NAME).read_bytes().decode("utf-8")
    lines = events_raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the file's final newline, not a blank record
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise EventError(
                "REFUSED: run %r: %s line %d is blank. A blank line means the "
                "log was truncated or edited."
                % (run_id, EVENTS_NAME, number)
            )
        try:
            events.append(json.loads(line))
        except ValueError as err:
            raise EventError(
                "REFUSED: run %r: %s line %d is not readable JSON: %s"
                % (run_id, EVENTS_NAME, number, err)
            ) from err
    _validate_events(events, run_id, stage)

    try:
        scenario_text = scenario_bytes.decode("utf-8")
    except UnicodeDecodeError as err:
        raise StoreError(
            "REFUSED: run %r: %s is not valid UTF-8: %s"
            % (run_id, SCENARIO_NAME, err)
        ) from err

    replay_command = (path / REPLAY_NAME).read_bytes().decode("utf-8").rstrip("\n")

    trace = path / TRACE_DIRNAME
    return RunRecord(
        run_id=run_id,
        path=path,
        verdict=verdict,
        exit_code=verdict.exit_code,
        host_wall_seconds=summary["host_wall_seconds"],
        counts=summary["counts"],
        summary=summary,
        provenance=provenance,
        scenario_text=scenario_text,
        events=events,
        replay_command=replay_command,
        trace_dir=trace if trace.is_dir() else None,
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def list_runs(root=None):
    """Return every run id present under ``results/runs``, sorted.

    Hidden staging directories are excluded: they are in-flight or abandoned
    writes, not runs.

    Being listed does NOT mean a run is readable. This function deliberately
    does not filter out incomplete records, because hiding them would make a
    directory full of wreckage look like an empty results tree. Iterating this
    list and calling ``read_run`` on each id is the intended way to find out,
    and it will raise loudly on the damaged ones.
    """
    base = runs_root(root)
    if not base.is_dir():
        return []
    return sorted(entry.name for entry in base.iterdir()
                  if entry.is_dir() and not entry.name.startswith("."))
