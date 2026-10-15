"""Strict YAML loading: plain scalars never become booleans.

WHY THIS EXISTS
    YAML 1.1 -- and therefore PyYAML's SafeLoader -- resolves eighteen
    plain scalars into booleans:

        yes Yes YES  no No NO  true True TRUE
        false False FALSE  on On ON  off Off OFF

    A data file that says OFF means the three characters "OFF". Turning it
    into Python ``False`` destroys the value AND its spelling, and it does
    so silently: nothing raises, the document still "loads", and the
    corruption only surfaces much later as a wrong comparison.

WHAT THIS MODULE DOES
    It defines a YAML dialect in which no plain scalar is ever resolved as
    a boolean. The implicit resolver for ``tag:yaml.org,2002:bool`` is
    removed from a SafeLoader subclass, so those scalars resolve as
    ``tag:yaml.org,2002:str`` and are constructed with their original text
    intact: "OFF" stays "OFF", "Off" stays "Off", "off" stays "off".

    The removal is done on a private copy of the resolver table, so the
    stock ``yaml.SafeLoader`` used by the rest of the process is NOT
    modified.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    It does not walk the loaded tree converting ``False`` back into a
    string. That approach must never be added here: once a scalar has
    become a Python ``bool`` its original spelling is gone, and a genuine
    boolean is indistinguishable from a coerced one -- the "repair" would
    be a guess presented as data.

BOOLEANS IN THIS DIALECT
    There are no implicit booleans. That is deliberate, not an oversight.
    A document that genuinely needs a boolean must say so explicitly with
    the standard tag, which is unambiguous because the intent is written
    into the document itself:

        a: !!bool true    ->  True   (explicit, kept)
        b: true           ->  "true" (plain scalar, stays a string)

    Nothing else in this dialect yields a ``bool``.

OUT OF SCOPE
    Only the bool resolver is touched. ``null`` / ``Null`` / ``NULL`` /
    ``~`` / empty still resolve to ``None``, and numbers, dates and the
    rest of the YAML 1.1 core schema are unchanged. If null coercion ever
    needs the same treatment, that is a separate, deliberate decision.

SELF-CHECK
    Import fails loudly if this dialect is not actually strict -- see
    ``_self_check`` at the bottom. A future PyYAML that resolves booleans
    by some other mechanism would make the resolver surgery a silent
    no-op; the self-check turns that into a crash naming the token.
"""

import os

import yaml

__all__ = [
    "StrictLoader",
    "YamlStrictError",
    "YamlFileNotFoundError",
    "YamlInputTypeError",
    "YamlParseError",
    "BOOL_TAG",
    "COERCED_TOKENS",
    "load",
    "load_all",
    "load_file",
    "load_all_file",
]

BOOL_TAG = "tag:yaml.org,2002:bool"

# Every plain scalar YAML 1.1 would coerce to a boolean. Used by the
# import-time self-check, and available to callers that need to report
# which spellings are at risk elsewhere (a different parser, or JSON).
COERCED_TOKENS = (
    "y", "Y", "yes", "Yes", "YES",
    "n", "N", "no", "No", "NO",
    "true", "True", "TRUE",
    "false", "False", "FALSE",
    "on", "On", "ON",
    "off", "Off", "OFF",
)


class YamlStrictError(Exception):
    """Base class for every error raised by this module."""


class YamlFileNotFoundError(YamlStrictError, FileNotFoundError):
    """A path handed to load_file()/load_all_file() does not exist, or is
    a directory rather than a file. Also a FileNotFoundError, so existing
    ``except FileNotFoundError`` handlers keep working."""


class YamlInputTypeError(YamlStrictError, TypeError):
    """load()/load_all() was handed something that is not YAML text."""


class YamlParseError(YamlStrictError, yaml.YAMLError):
    """The document is not well-formed YAML. Also a yaml.YAMLError, so
    existing ``except yaml.YAMLError`` handlers keep working."""


class StrictLoader(yaml.SafeLoader):
    """SafeLoader with the implicit bool resolver removed.

    Subclassing alone is not enough: ``yaml_implicit_resolvers`` is
    inherited from the base class, so it is rebuilt below into a copy that
    lives in this class's own __dict__.
    """


def _resolvers_without_bool(table):
    """Copy a resolver table, dropping only the bool entries.

    Filtering by TAG rather than by first-character key matters: the
    characters 'n' and 'N' carry the null resolver as well as the bool
    resolver, so deleting those keys outright would silently break
    ``null`` and ``~``.
    """
    return {
        first: [(tag, regexp) for (tag, regexp) in entries if tag != BOOL_TAG]
        for first, entries in table.items()
    }


StrictLoader.yaml_implicit_resolvers = _resolvers_without_bool(
    yaml.SafeLoader.yaml_implicit_resolvers
)


def _describe_source(stream_or_text):
    """Best-effort name for the thing being parsed, for error messages."""
    name = getattr(stream_or_text, "name", None)
    if isinstance(name, str) and name:
        return name
    return "<yaml text>"


def _reject_non_yaml_input(stream_or_text, func_name):
    """Refuse anything that is not YAML text, naming what arrived."""
    if isinstance(stream_or_text, (str, bytes, bytearray)):
        return
    if hasattr(stream_or_text, "read"):
        return
    if isinstance(stream_or_text, os.PathLike):
        raise YamlInputTypeError(
            "{0}() was given a filesystem path ({1!r}), not YAML text. Use "
            "{0}_file(path) to read a file, or pass the file's contents "
            "as text.".format(func_name, os.fspath(stream_or_text))
        )
    raise YamlInputTypeError(
        "{0}() needs YAML text (str/bytes) or an open stream, but was given "
        "{1}. Pass the document's text, or use {0}_file(path) for a file on "
        "disk.".format(func_name, type(stream_or_text).__name__)
    )


def load(stream_or_text):
    """Parse ONE YAML document and return it.

    Accepts YAML text (str, bytes, bytearray) or an open text/binary
    stream. Plain OFF/ON/NO/YES/TRUE/FALSE and every case variant come
    back as the exact strings that were written.

    Raises YamlInputTypeError if handed something that is not YAML text,
    and YamlParseError if the document is malformed.
    """
    _reject_non_yaml_input(stream_or_text, "load")
    if isinstance(stream_or_text, bytearray):
        stream_or_text = bytes(stream_or_text)
    try:
        return yaml.load(stream_or_text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise YamlParseError(
            "Malformed YAML in {0}: {1}".format(
                _describe_source(stream_or_text), exc
            )
        ) from exc


def load_all(stream_or_text):
    """Parse a multi-document YAML stream and return a list of documents.

    Same inputs and same errors as load(). The generator is drained here on
    purpose: a parse error raised lazily inside a caller's loop is far
    harder to attribute than one raised at the call site.
    """
    _reject_non_yaml_input(stream_or_text, "load_all")
    if isinstance(stream_or_text, bytearray):
        stream_or_text = bytes(stream_or_text)
    try:
        return list(yaml.load_all(stream_or_text, Loader=StrictLoader))
    except yaml.YAMLError as exc:
        raise YamlParseError(
            "Malformed YAML in {0}: {1}".format(
                _describe_source(stream_or_text), exc
            )
        ) from exc


def _read_checked(path, func_name):
    """Resolve, open and read a YAML file, naming exactly what was wrong."""
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise YamlInputTypeError(
            "{0}() needs a filesystem path (str or os.PathLike), but was "
            "given {1}.".format(func_name, type(path).__name__)
        ) from exc

    absolute = os.path.abspath(raw)

    if os.path.isdir(absolute):
        raise YamlFileNotFoundError(
            "YAML path is a directory, not a file: {0!r} (given as {1!r}). "
            "Name the file inside it.".format(absolute, raw)
        )

    if not os.path.exists(absolute):
        raise YamlFileNotFoundError(
            "YAML file not found: {0!r} (given as {1!r}, resolved against "
            "working directory {2!r}). Create the file, or correct the path "
            "passed to {3}().".format(absolute, raw, os.getcwd(), func_name)
        )

    try:
        with open(absolute, "r", encoding="utf-8") as handle:
            return handle.read(), absolute
    except UnicodeDecodeError as exc:
        raise YamlStrictError(
            "YAML file {0!r} is not valid UTF-8 ({1}). Re-save it as "
            "UTF-8.".format(absolute, exc)
        ) from exc
    except OSError as exc:
        raise YamlStrictError(
            "Cannot read YAML file {0!r}: {1}".format(absolute, exc)
        ) from exc


def load_file(path):
    """Read and parse ONE YAML document from ``path``.

    Raises YamlFileNotFoundError, naming the resolved absolute path, if the
    path is missing or is a directory. Raises YamlParseError, naming the
    file, if the document is malformed.
    """
    text, absolute = _read_checked(path, "load_file")
    try:
        return yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise YamlParseError(
            "Malformed YAML in file {0!r}: {1}".format(absolute, exc)
        ) from exc


def load_all_file(path):
    """Read a multi-document YAML file and return a list of documents.

    Same errors as load_file().
    """
    text, absolute = _read_checked(path, "load_all_file")
    try:
        return list(yaml.load_all(text, Loader=StrictLoader))
    except yaml.YAMLError as exc:
        raise YamlParseError(
            "Malformed YAML in file {0!r}: {1}".format(absolute, exc)
        ) from exc


def _self_check():
    """Prove at import time that this dialect really is strict.

    Removing a resolver is a structural change to somebody else's library.
    If a future PyYAML resolves booleans by a different mechanism, the
    surgery above becomes a no-op and every OFF in every data file quietly
    turns back into False. Checking the BEHAVIOUR, not the table, turns
    that into a crash that names the offending token.
    """
    leftover = sorted(
        repr(first)
        for first, entries in StrictLoader.yaml_implicit_resolvers.items()
        for (tag, _regexp) in entries
        if tag == BOOL_TAG
    )
    if leftover:
        raise YamlStrictError(
            "yaml_strict: the implicit bool resolver is still registered for "
            "first-character(s) {0} on StrictLoader. The resolver table "
            "layout of PyYAML {1} is not what this module expects; fix "
            "_resolvers_without_bool().".format(
                ", ".join(leftover), yaml.__version__
            )
        )

    document = "\n".join(
        "k{0}: {1}".format(index, token)
        for index, token in enumerate(COERCED_TOKENS)
    )
    loaded = yaml.load(document, Loader=StrictLoader)
    for index, token in enumerate(COERCED_TOKENS):
        value = loaded["k{0}".format(index)]
        if not isinstance(value, str) or value != token:
            raise YamlStrictError(
                "yaml_strict is NOT strict: the plain scalar {0!r} loaded as "
                "{1!r} ({2}) instead of the string {0!r}. PyYAML {3} coerces "
                "booleans somewhere this module does not disable. Do not "
                "load data files until this is fixed.".format(
                    token, value, type(value).__name__, yaml.__version__
                )
            )


_self_check()
