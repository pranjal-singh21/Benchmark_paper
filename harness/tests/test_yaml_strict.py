"""Tests for harness.yaml_strict.

The point of the module is that a plain OFF in a data file stays the string
"OFF". These tests check that, and they also check the premise: that the
stock SafeLoader really does corrupt those tokens. Without that control a
passing suite would prove nothing -- it would pass just as happily against
a loader that does not exist.

No data-file content appears here. Keys and values are deliberately
meaningless (a, b, outer, inner, items).
"""

import pytest
import yaml

from harness import yaml_strict


# The eighteen plain scalars YAML 1.1 turns into booleans. y/n/Y/N are in
# yaml_strict.COERCED_TOKENS as well, but PyYAML does not coerce them, so
# they are excluded from the control test's lower bound below.
BOOL_TOKENS = [
    "OFF", "ON", "NO", "YES", "TRUE", "FALSE",
    "off", "on", "no", "yes", "true", "false",
    "Off", "On", "No", "Yes", "True", "False",
]

# Mixed-case spellings PyYAML happens not to coerce today. They must still
# survive verbatim, and they must not be normalised in either direction.
MIXED_CASE_TOKENS = ["oFf", "OfF", "tRue", "yEs", "nO", "ON_", "offset"]


# --------------------------------------------------------------------------
# Control: the corruption this module prevents is real.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token", BOOL_TOKENS)
def test_stock_safeloader_really_coerces_these_tokens(token):
    """If this ever fails, the premise changed -- re-read yaml_strict before
    trusting any other test in this file."""
    coerced = yaml.safe_load("a: " + token)["a"]
    assert isinstance(coerced, bool), (
        "PyYAML {0} no longer coerces {1!r} (got {2!r}); the control for "
        "these tests is stale.".format(yaml.__version__, token, coerced)
    )


def test_stock_safeloader_is_not_mutated_by_import():
    """The fix must be scoped to StrictLoader. Patching the shared resolver
    table would change YAML parsing for every other library in the process."""
    assert yaml.safe_load("a: OFF")["a"] is False
    stock_bool_chars = {
        first
        for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
        for (tag, _regexp) in entries
        if tag == yaml_strict.BOOL_TAG
    }
    assert stock_bool_chars, (
        "yaml.SafeLoader lost its bool resolver -- yaml_strict mutated the "
        "shared table instead of copying it."
    )


# --------------------------------------------------------------------------
# The requirement: exact original spelling, everywhere it can appear.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("token", BOOL_TOKENS + MIXED_CASE_TOKENS)
def test_bool_token_loads_as_exact_string(token):
    value = yaml_strict.load("a: " + token)["a"]
    assert isinstance(value, str), (
        "{0!r} loaded as {1} {2!r}, not a string".format(
            token, type(value).__name__, value
        )
    )
    assert value == token, "spelling changed: {0!r} -> {1!r}".format(
        token, value
    )


@pytest.mark.parametrize("token", yaml_strict.COERCED_TOKENS)
def test_every_documented_token_is_preserved(token):
    """COERCED_TOKENS is the module's own list of at-risk spellings; all of
    it must survive, including the single letters y/n/Y/N."""
    assert yaml_strict.load("a: " + token)["a"] == token


def test_nested_mapping_preserves_tokens():
    document = (
        "outer:\n"
        "  inner:\n"
        "    a: OFF\n"
        "    b: Yes\n"
        "  deeper:\n"
        "    c: TRUE\n"
        "    d: no\n"
    )
    loaded = yaml_strict.load(document)
    assert loaded == {
        "outer": {
            "inner": {"a": "OFF", "b": "Yes"},
            "deeper": {"c": "TRUE", "d": "no"},
        }
    }
    assert not any(
        isinstance(v, bool) for v in loaded["outer"]["inner"].values()
    )


def test_list_preserves_tokens():
    document = "items: [OFF, ON, NO, YES, TRUE, FALSE]\n"
    assert yaml_strict.load(document)["items"] == [
        "OFF", "ON", "NO", "YES", "TRUE", "FALSE",
    ]


def test_block_list_of_mappings_preserves_tokens():
    document = (
        "items:\n"
        "  - a: Off\n"
        "    b: [on, off]\n"
        "  - a: YES\n"
        "    b: []\n"
    )
    assert yaml_strict.load(document)["items"] == [
        {"a": "Off", "b": ["on", "off"]},
        {"a": "YES", "b": []},
    ]


def test_tokens_as_mapping_keys_are_preserved():
    """Resolution applies to keys too; a key of OFF must not become False,
    which would also collide with a key of NO."""
    loaded = yaml_strict.load("OFF: 1\nNO: 2\nON: 3\n")
    assert loaded == {"OFF": 1, "NO": 2, "ON": 3}


def test_quoted_tokens_are_unchanged():
    loaded = yaml_strict.load("a: 'OFF'\nb: \"yes\"\n")
    assert loaded == {"a": "OFF", "b": "yes"}


# --------------------------------------------------------------------------
# Everything else in YAML must still work.
# --------------------------------------------------------------------------

def test_numbers_and_plain_strings_load_normally():
    document = (
        "a: 12\n"
        "b: -3\n"
        "c: 0x10\n"
        "d: 1.5\n"
        "e: plain text\n"
        "f: 0\n"
        "g: 1\n"
    )
    loaded = yaml_strict.load(document)
    assert loaded == {
        "a": 12, "b": -3, "c": 16, "d": 1.5,
        "e": "plain text", "f": 0, "g": 1,
    }
    assert isinstance(loaded["a"], int)
    assert isinstance(loaded["d"], float)
    # 0 and 1 must stay ints, not become booleans by another route.
    assert not isinstance(loaded["f"], bool)
    assert not isinstance(loaded["g"], bool)


def test_null_still_resolves_to_none():
    """Documented, deliberate scope limit: only bool resolution is removed."""
    loaded = yaml_strict.load("a: null\nb: ~\nc:\n")
    assert loaded == {"a": None, "b": None, "c": None}


def test_explicit_bool_tag_still_produces_a_bool():
    """The one unambiguous way to get a boolean: say so in the document."""
    loaded = yaml_strict.load("a: !!bool true\nb: !!bool OFF\nc: true\n")
    assert loaded["a"] is True
    assert loaded["b"] is False
    assert loaded["c"] == "true"


def test_load_accepts_bytes_and_streams():
    import io

    assert yaml_strict.load(b"a: OFF\n")["a"] == "OFF"
    assert yaml_strict.load(bytearray(b"a: OFF\n"))["a"] == "OFF"
    assert yaml_strict.load(io.StringIO("a: OFF\n"))["a"] == "OFF"


def test_load_all_returns_every_document():
    documents = yaml_strict.load_all("a: OFF\n---\na: ON\n")
    assert documents == [{"a": "OFF"}, {"a": "ON"}]


# --------------------------------------------------------------------------
# Files, and loud failure when the path is wrong.
# --------------------------------------------------------------------------

def test_load_file_reads_and_preserves_tokens(tmp_path):
    path = tmp_path / "sample.yaml"
    path.write_text("outer:\n  a: OFF\n  items: [YES, no]\n", encoding="utf-8")
    assert yaml_strict.load_file(path) == {
        "outer": {"a": "OFF", "items": ["YES", "no"]}
    }


def test_load_file_accepts_a_string_path(tmp_path):
    path = tmp_path / "sample.yaml"
    path.write_text("a: Off\n", encoding="utf-8")
    assert yaml_strict.load_file(str(path))["a"] == "Off"


def test_load_all_file_reads_every_document(tmp_path):
    path = tmp_path / "multi.yaml"
    path.write_text("a: OFF\n---\na: ON\n", encoding="utf-8")
    assert yaml_strict.load_all_file(path) == [{"a": "OFF"}, {"a": "ON"}]


def test_missing_file_raises_named_error(tmp_path):
    missing = tmp_path / "absent.yaml"
    with pytest.raises(yaml_strict.YamlFileNotFoundError) as info:
        yaml_strict.load_file(missing)
    message = str(info.value)
    assert "absent.yaml" in message
    assert "not found" in message.lower()


def test_missing_file_error_is_catchable_as_filenotfounderror(tmp_path):
    with pytest.raises(FileNotFoundError):
        yaml_strict.load_file(tmp_path / "absent.yaml")


def test_directory_path_raises_named_error(tmp_path):
    with pytest.raises(yaml_strict.YamlFileNotFoundError) as info:
        yaml_strict.load_file(tmp_path)
    assert "directory" in str(info.value).lower()


def test_malformed_file_names_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("a: [1, 2\nb: 3\n", encoding="utf-8")
    with pytest.raises(yaml_strict.YamlParseError) as info:
        yaml_strict.load_file(path)
    assert "broken.yaml" in str(info.value)


def test_malformed_text_raises_parse_error():
    with pytest.raises(yaml_strict.YamlParseError):
        yaml_strict.load("a: [1, 2\nb: 3\n")


def test_load_given_a_path_object_says_to_use_load_file(tmp_path):
    path = tmp_path / "sample.yaml"
    path.write_text("a: OFF\n", encoding="utf-8")
    with pytest.raises(yaml_strict.YamlInputTypeError) as info:
        yaml_strict.load(path)
    assert "load_file" in str(info.value)


def test_load_rejects_non_yaml_input():
    with pytest.raises(yaml_strict.YamlInputTypeError) as info:
        yaml_strict.load({"a": "OFF"})
    assert "dict" in str(info.value)


# --------------------------------------------------------------------------
# The mechanism itself.
# --------------------------------------------------------------------------

def test_strict_loader_has_no_bool_resolver():
    remaining = [
        first
        for first, entries in yaml_strict.StrictLoader.yaml_implicit_resolvers.items()
        for (tag, _regexp) in entries
        if tag == yaml_strict.BOOL_TAG
    ]
    assert remaining == []


def test_null_resolver_survived_the_bool_removal():
    """'n' and 'N' carry both resolvers; removing by character instead of by
    tag would have taken null with it."""
    null_chars = {
        first
        for first, entries in yaml_strict.StrictLoader.yaml_implicit_resolvers.items()
        for (tag, _regexp) in entries
        if tag == "tag:yaml.org,2002:null"
    }
    assert "n" in null_chars and "N" in null_chars
