"""The scenario language: the eleven verbs, and their parameters.

A scenario is a list of steps. Each step is a single-key mapping whose key is the
verb and whose value is the verb's parameters::

    id: overtemp-boundary
    description: the fault must not fire at the limit
    steps:
      - write_symbol:  { node: n1, symbol: g_x, value: 550 }
      - expect_no_can: { id: <message-id>, signals: {f: A}, for_ms: 100 }

This module knows the SHAPE of the language and nothing about any particular
system: no node names, no message identifiers, no signal names. Whether the
things a scenario names actually exist is :mod:`harness.validate`'s job, because
that needs the system's data files and this does not.

The verb table is the single source of truth for the language, and
:mod:`harness.validate`, :mod:`harness.compile` and the Phase 3 verb reference
all read it rather than each keeping their own copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping

from harness import yaml_strict


class ScenarioError(Exception):
    """The scenario is malformed. Nothing about the system was consulted."""


# name -> (required parameters, optional parameters)
#
# `label` is optional everywhere it appears but strongly wanted on assertions:
# it is what the verdict quotes, and an unlabelled failure is hard to act on.
VERBS: Dict[str, Dict[str, Any]] = {
    "mark":          {"required": {"text"},               "optional": set()},
    "run_for":       {"required": {"ms"},                 "optional": set()},
    "wait_uart":     {"required": {"node", "text", "timeout_ms"}, "optional": {"label"}},
    "write_symbol":  {"required": {"node", "symbol", "value"},    "optional": {"label"}},
    "expect_symbol": {"required": {"node", "symbol", "equals"},   "optional": {"label"}},
    "expect_can":    {"required": {"id", "within_ms"},    "optional": {"signals", "label"}},
    "expect_no_can": {"required": {"id", "for_ms"},       "optional": {"signals", "label"}},
    "can_send":      {"required": {"node", "id"},         "optional": {"signals", "data_hex", "label"}},
    "flood":         {"required": {"id", "utilisation_pct", "duration_ms"}, "optional": {"label"}},
    "node_signal":   {"required": {"node", "id", "signals"},      "optional": {"label"}},
    "node_silence":  {"required": {"node", "silence"},    "optional": {"label"}},
}

# Parameters naming a duration, checked for positivity and sanity.
DURATION_KEYS = {"ms", "timeout_ms", "within_ms", "for_ms", "duration_ms"}


def as_bool(value, key, where):
    """Parse a value the schema declares boolean.

    Accepts a real boolean or the exact spellings true/false in any case.
    There is deliberately no truthiness: 1, 0, yes, on and the empty string
    are all refused, naming what was found. See harness/catalog.py for the
    same rule applied to the contract.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise ScenarioError(
        "%s: '%s' must be true or false, got %r (%s)."
        % (where, key, value, type(value).__name__))


class Step:
    __slots__ = ("verb", "params", "index")

    def __init__(self, verb: str, params: Mapping[str, Any], index: int):
        self.verb = verb
        self.params = dict(params)
        self.index = index

    @property
    def where(self) -> str:
        return "step %d (%s)" % (self.index + 1, self.verb)

    def label(self) -> str:
        return self.params.get("label") or "%s at step %d" % (self.verb, self.index + 1)

    def __repr__(self):
        return "Step(%d, %s, %r)" % (self.index, self.verb, self.params)


class Scenario:
    __slots__ = ("id", "description", "steps", "source", "text")

    def __init__(self, id_, description, steps, source=None, text=None):
        self.id = id_
        self.description = description
        self.steps: List[Step] = steps
        self.source = source
        self.text = text

    def __repr__(self):
        return "Scenario(%r, %d steps)" % (self.id, len(self.steps))


def _suggest(name: str, candidates) -> str:
    """Nearest-neighbour hint, so a typo names its likely fix."""
    import difflib
    close = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return "  Did you mean '%s'?" % close[0] if close else ""


def parse(text: str, source: str = "<string>") -> Scenario:
    doc = yaml_strict.load(text)
    if not isinstance(doc, Mapping):
        raise ScenarioError("%s: a scenario must be a mapping at the top level, got %s"
                            % (source, type(doc).__name__))

    for key in ("id", "steps"):
        if key not in doc:
            raise ScenarioError("%s: missing required key '%s'" % (source, key))

    sid = doc["id"]
    if not isinstance(sid, str) or not sid.strip():
        raise ScenarioError("%s: 'id' must be a non-empty string, got %r" % (source, sid))

    raw_steps = doc["steps"]
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScenarioError("%s: 'steps' must be a non-empty list." % source)

    steps: List[Step] = []
    for index, raw in enumerate(raw_steps):
        where = "%s: step %d" % (source, index + 1)
        if not isinstance(raw, Mapping):
            raise ScenarioError("%s: each step must be a mapping of one verb to its "
                                "parameters, got %s" % (where, type(raw).__name__))
        if len(raw) != 1:
            raise ScenarioError(
                "%s: each step must name exactly one verb, found %d keys: %s\n"
                "Write one step per verb."
                % (where, len(raw), ", ".join(repr(k) for k in raw)))

        verb = list(raw)[0]
        params = raw[verb]

        if verb not in VERBS:
            raise ScenarioError(
                "%s: '%s' is not a verb.%s\nThe language has exactly these eleven: %s"
                % (where, verb, _suggest(verb, VERBS), ", ".join(sorted(VERBS))))

        if params is None:
            params = {}
        if not isinstance(params, Mapping):
            raise ScenarioError("%s: parameters for '%s' must be a mapping, got %s"
                                % (where, verb, type(params).__name__))

        spec = VERBS[verb]
        missing = spec["required"] - set(params)
        if missing:
            raise ScenarioError(
                "%s: '%s' is missing required parameter(s): %s\n  '%s' takes: %s"
                % (where, verb, ", ".join(sorted(missing)), verb,
                   ", ".join(sorted(spec["required"] | spec["optional"]))))

        unknown = set(params) - spec["required"] - spec["optional"]
        if unknown:
            first = sorted(unknown)[0]
            raise ScenarioError(
                "%s: '%s' does not take parameter(s): %s%s\n  '%s' takes: %s"
                % (where, verb, ", ".join(sorted(unknown)),
                   _suggest(first, spec["required"] | spec["optional"]), verb,
                   ", ".join(sorted(spec["required"] | spec["optional"]))))

        steps.append(Step(verb, params, index))

    return Scenario(sid, doc.get("description"), steps, source=source, text=text)


def load(path) -> Scenario:
    p = Path(path)
    if not p.is_file():
        raise ScenarioError("scenario file not found: %s" % p)
    return parse(p.read_text(encoding="utf-8"), source=str(p))
