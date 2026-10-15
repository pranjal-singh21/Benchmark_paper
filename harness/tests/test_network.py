"""Tests for harness.network.

Every fixture under fixtures/ is synthetic. No node, bus, board, peripheral,
signal or message identifier belonging to this project appears here, because
harness/ must not contain the content of the data it reads.

The refusal tests do not merely assert that an exception was raised. Each one
asserts that the message NAMES the offending thing, because the caller acting
on it may be a generator model, and "invalid configuration" is not something
anything can act on.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import network, yaml_strict

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOARDS_OK = FIXTURES / "boards_ok.yml"
NET_OK = FIXTURES / "net_ok.yml"
REPO_ROOT = Path(network.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def refuse(net_name, boards=BOARDS_OK):
    """Load a fixture that must be refused; return the message."""
    path = net_name if isinstance(net_name, Path) else FIXTURES / net_name
    with pytest.raises(network.NetworkError) as excinfo:
        network.load(path, boards_path=boards)
    return str(excinfo.value)


def names(message, *needles):
    """Assert the message names every one of these things."""
    for needle in needles:
        assert needle in message, (
            "the refusal does not name %r.\nmessage was:\n%s" % (needle, message)
        )


def load_ok():
    return network.load(NET_OK, boards_path=BOARDS_OK)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_loads_the_happy_path():
    net = load_ok()
    assert [bus.id for bus in net.buses()] == ["bus_one", "bus_two"]
    assert [node.id for node in net.nodes()] == ["node_one", "node_two", "node_three"]


def test_bus_fields_are_typed():
    bus = load_ok().bus("bus_one")
    assert bus.bitrate == 250000
    assert isinstance(bus.bitrate, int)
    assert bus.type == "link"


def test_real_and_scripted_partition_the_nodes():
    net = load_ok()
    assert [node.id for node in net.real_nodes()] == ["node_one", "node_two"]
    assert [node.id for node in net.scripted_nodes()] == ["node_three"]
    assert len(net.real_nodes()) + len(net.scripted_nodes()) == len(net.nodes())


def test_dut_is_the_single_declared_node():
    net = load_ok()
    assert net.dut().id == "node_one"
    # yaml_strict resolves no plain scalar to a boolean, so `dut: true` arrives
    # as the string "true". It must not be left as one.
    assert net.dut().dut is True
    assert net.node("node_two").dut is False


def test_real_node_fields():
    node = load_ok().node("node_one")
    assert node.is_real and not node.is_scripted
    assert node.board == "board_alpha"
    assert node.boot_text == "node one up"
    assert node.tx_enable_symbol == "g_example_enable"
    assert node.buses == ("bus_one",)


def test_scripted_node_fields():
    node = load_ok().node("node_three")
    assert node.is_scripted and not node.is_real
    assert node.emits == (17, 18)
    assert node.period_ms == 100
    assert node.default_signals == {"sig_one": "VAL_ONE", "sig_two": 3}
    assert node.elf is None
    assert node.board is None


def test_board_is_resolved_for_a_real_node():
    net = load_ok()
    board = net.board_of("node_one")
    assert board.id == "board_alpha"
    assert board.can_bitrate == 250000
    assert board.zephyr_board == "target_alpha"


def test_board_of_a_scripted_node_is_refused_by_name():
    net = load_ok()
    with pytest.raises(network.NetworkError) as excinfo:
        net.board_of("node_three")
    names(str(excinfo.value), "node_three", "scripted", "no board")


def test_nodes_on_a_bus():
    net = load_ok()
    assert [n.id for n in net.nodes_on("bus_two")] == ["node_three"]
    assert len(net.nodes_on("bus_one")) == 3


def test_elf_path_is_resolved_against_the_root(tmp_path):
    net = network.load(NET_OK, boards_path=BOARDS_OK, root=tmp_path)
    expected = (tmp_path / "systems/example/firmware/node_one/build/zephyr/zephyr.elf").resolve()
    assert net.elf_path("node_one") == expected
    # The ELF is a build artefact; a topology loads before it is built.
    assert not expected.exists()


# ---------------------------------------------------------------------------
# lookup failures name the id and list what exists
# ---------------------------------------------------------------------------


def test_unknown_node_lookup_names_the_id():
    net = load_ok()
    with pytest.raises(network.NetworkError) as excinfo:
        net.node("node_absent")
    names(str(excinfo.value), "'node_absent'", "'node_one'", "'node_three'")


def test_unknown_bus_lookup_names_the_id():
    net = load_ok()
    with pytest.raises(network.NetworkError) as excinfo:
        net.bus("bus_absent")
    names(str(excinfo.value), "'bus_absent'", "'bus_one'", "'bus_two'")


def test_unknown_board_lookup_names_the_id():
    net = load_ok()
    with pytest.raises(network.NetworkError) as excinfo:
        net.board("board_absent")
    names(str(excinfo.value), "'board_absent'", "'board_alpha'", "'board_beta'")


# ---------------------------------------------------------------------------
# the required refusals
# ---------------------------------------------------------------------------


def test_missing_network_file_is_refused_naming_the_path():
    missing = FIXTURES / "net_this_file_does_not_exist.yml"
    assert not missing.exists()
    message = refuse(missing)
    names(message, str(missing), "not found")


def test_missing_boards_file_is_refused_naming_the_path(tmp_path):
    missing = tmp_path / "boards_absent.yml"
    with pytest.raises(network.NetworkError) as excinfo:
        network.load(NET_OK, boards_path=missing)
    names(str(excinfo.value), str(missing), "not found")


def test_unknown_board_is_refused_naming_node_board_and_the_boards_that_exist():
    message = refuse("net_unknown_board.yml")
    names(
        message,
        "node 'node_one'",
        "board 'board_absent'",
        "'board_alpha'",
        "'board_beta'",
        str(BOARDS_OK),
    )


def test_bad_node_type_is_refused_naming_the_value():
    message = refuse("net_bad_type.yml")
    names(message, "node 'node_two'", "'emulated'", "'real'", "'scripted'")


def test_real_node_without_elf_is_refused_naming_the_key():
    message = refuse("net_real_no_elf.yml")
    names(message, "node 'node_one'", "does not declare 'elf'")


def test_real_node_without_boot_text_is_refused_naming_the_key():
    message = refuse("net_real_no_boot_text.yml")
    names(message, "node 'node_one'", "does not declare 'boot_text'")


def test_scripted_node_with_elf_is_refused_naming_node_and_key():
    message = refuse("net_scripted_with_elf.yml")
    names(message, "node 'node_two'", "'elf'", "'real'", "type 'scripted'")


def test_unknown_bus_reference_is_refused_naming_node_and_bus():
    message = refuse("net_unknown_bus.yml")
    names(message, "node 'node_two'", "bus 'bus_absent'", "'bus_one'")


def test_two_duts_are_refused_naming_the_count_and_which():
    message = refuse("net_two_duts.yml")
    names(message, "dut count: 2", "'node_one'", "'node_two'", "Exactly one")


def test_zero_duts_is_refused_naming_the_count_and_the_nodes():
    message = refuse("net_no_dut.yml")
    names(message, "dut count: 0", "'node_one'", "'node_two'", "Exactly one")


# ---------------------------------------------------------------------------
# further refusals: each is a silent misconfiguration if tolerated
# ---------------------------------------------------------------------------


def test_a_dut_spelled_yes_is_refused_not_interpreted():
    # yaml_strict gives no implicit booleans, so `dut: yes` is the string
    # "yes". Mapping it onto true would be a guess about intent.
    message = refuse("net_dut_bad_spelling.yml")
    names(message, "node 'node_one'", "'dut'", "'yes'", "true", "false")


def test_a_dut_on_a_scripted_node_is_refused_naming_the_node():
    message = refuse("net_dut_on_scripted.yml")
    names(message, "node 'node_two'", "'scripted'", "'real'")


def test_a_board_bitrate_that_disagrees_with_the_bus_is_refused_naming_both():
    message = refuse("net_bitrate_mismatch.yml")
    names(
        message,
        "node 'node_one'",
        "bus 'bus_one'",
        "board 'board_alpha'",
        "500000",
        "250000",
    )


def test_an_unknown_node_key_is_refused_and_suggests_the_real_one():
    message = refuse("net_unknown_key.yml")
    names(message, "node 'node_one'", "'boot_txt'", "Did you mean 'boot_text'?")


def test_a_duplicate_node_id_is_refused_naming_both_positions():
    message = refuse("net_duplicate_node.yml")
    names(message, "'node_one'", "nodes[0]", "nodes[1]", "twice")


def test_a_real_node_on_two_buses_is_refused_naming_the_buses():
    message = refuse("net_real_two_buses.yml")
    names(message, "node 'node_one'", "'bus_one'", "'bus_two'", "board 'board_alpha'")


def test_a_quoted_bitrate_is_refused_not_converted():
    message = refuse("net_quoted_bitrate.yml")
    names(message, "bus 'bus_one'", "'bitrate'", "'250000'", "NOT converted")


def test_emits_without_a_period_is_refused_naming_the_node():
    message = refuse("net_emits_no_period.yml")
    names(message, "node 'node_two'", "'emits'", "'period_ms'")


def test_a_node_on_no_bus_is_refused_naming_the_node():
    message = refuse("net_node_no_bus.yml")
    names(message, "node 'node_two'", "no bus", "'bus_one'")


def test_a_missing_top_level_key_is_refused_naming_it():
    message = refuse("net_missing_top_key.yml")
    names(message, "does not declare 'nodes'")


def test_an_empty_network_file_is_refused_naming_the_path():
    message = refuse("net_empty.yml")
    names(message, str(FIXTURES / "net_empty.yml"), "empty")


def test_a_board_missing_a_field_is_refused_naming_board_and_field():
    message = refuse("net_ok.yml", boards=FIXTURES / "boards_missing_field.yml")
    names(message, "board 'board_alpha'", "'cpu_peripheral'")


def test_a_bus_without_an_id_is_refused_naming_its_position():
    message = refuse("net_bus_no_id.yml")
    names(message, "buses[0]", "does not declare 'id'")


def test_a_node_without_a_type_is_refused_naming_the_node():
    message = refuse("net_node_no_type.yml")
    names(message, "node 'node_one'", "'type'", "'real'", "'scripted'")


def test_a_duplicate_bus_id_is_refused_naming_both_positions():
    message = refuse("net_duplicate_bus.yml")
    names(message, "'bus_one'", "buses[0]", "buses[1]", "twice")


def test_a_quoted_message_identifier_is_refused_not_converted():
    message = refuse("net_emits_string.yml")
    names(message, "node 'node_two'", "emits[0]", "'17'", "must be an integer")


def test_an_id_with_a_space_is_refused_naming_it():
    message = refuse("net_bad_id_chars.yml")
    names(message, "'node one'", "not a usable identifier")


def test_an_unknown_top_level_key_is_refused_naming_it():
    message = refuse("net_top_unknown_key.yml")
    names(message, "'nodez'", "unknown key", "Did you mean 'nodes'?")


# ---------------------------------------------------------------------------
# the shipped board definition file
# ---------------------------------------------------------------------------


def test_the_repository_boards_file_loads_and_matches_the_schema():
    boards = network.load_boards()
    assert boards, "harness/boards.yml defines no boards"
    for board_id, board in boards.items():
        assert board.id == board_id
        for field in network.BOARD_REQUIRED:
            assert field in board.declared, (
                "board %r in %s does not declare %r"
                % (board_id, network.DEFAULT_BOARDS_PATH, field)
            )
        assert isinstance(board.can_bitrate, int)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_json_matches_the_source(capsys):
    exit_code = network.main(
        ["--json", "--boards", str(BOARDS_OK), str(NET_OK)]
    )
    assert exit_code == 0
    document = json.loads(capsys.readouterr().out)

    source = yaml_strict.load_file(NET_OK)
    boards_source = yaml_strict.load_file(BOARDS_OK)

    assert document["buses"] == source["buses"]
    assert document["nodes"] == source["nodes"]
    assert document["boards"] == boards_source
    assert document["dut"] == "node_one"
    assert document["real_nodes"] == ["node_one", "node_two"]
    assert document["scripted_nodes"] == ["node_three"]
    assert document["network_path"] == str(NET_OK)
    assert document["boards_path"] == str(BOARDS_OK)


def test_cli_json_keeps_the_declared_spelling_of_dut(capsys):
    network.main(["--json", "--boards", str(BOARDS_OK), str(NET_OK)])
    document = json.loads(capsys.readouterr().out)
    declared = {node["id"]: node for node in document["nodes"]}
    # The engine reads True; the JSON reports what the file actually said.
    assert declared["node_one"]["dut"] == "true"


def test_cli_human_summary_names_the_dut(capsys):
    exit_code = network.main(["--boards", str(BOARDS_OK), str(NET_OK)])
    assert exit_code == 0
    out = capsys.readouterr().out
    names(out, "node_one", "node_three", "[DUT]", "bus_one")


def test_cli_refusal_exits_one_and_names_the_offender(capsys):
    exit_code = network.main(
        ["--boards", str(BOARDS_OK), str(FIXTURES / "net_unknown_board.yml")]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    names(err, "REFUSED:", "node 'node_one'", "board 'board_absent'")


def test_cli_bad_invocation_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        network.main([])
    assert excinfo.value.code == 2


def test_module_runs_as_a_script():
    """`python harness/network.py ...` must work, not only `python -m`."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "harness" / "network.py"),
            "--json",
            "--boards",
            str(BOARDS_OK),
            str(NET_OK),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dut"] == "node_one"
