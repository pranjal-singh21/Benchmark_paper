"""Tests for harness.store: run storage with enforced provenance.

The tests that matter most here are the refusals. A store that writes and reads
back correctly is easy; the property this module exists for is that an
incomplete or unprovenanced record is refused at write AND at read, separately,
so that a run directory which arrived by some other route than write_run is
still not readable as a result.

No project data appears in this file: node ids are 'node_a'/'node_b', versions
are obvious placeholders, and event kinds are engine vocabulary. The store never
encodes what the data files contain.
"""

import json

import pytest

from harness import store


SCENARIO = "name: example\nduration_us: 100000\nsteps: []\n"

EVENTS = [
    {"t_virtual_us": 0, "kind": "start"},
    {"t_virtual_us": 1500, "kind": "observation", "detail": "first"},
    {"t_virtual_us": 90000, "kind": "stop"},
]

REPLAY = "python -m harness.run --scenario results/runs/r/scenario.yml"


def make_provenance(scenario_text=SCENARIO, **overrides):
    provenance = {
        "binary_sha256": {"node_a": "a" * 64, "node_b": "b" * 64},
        "renode_version": "renode-placeholder-version",
        "zephyr_version": "zephyr-placeholder-version",
        "zephyr_sdk_version": "sdk-placeholder-version",
        "harness_git_commit": "c" * 40,
        "scenario_sha256": store.sha256_of_text(scenario_text),
    }
    provenance.update(overrides)
    return provenance


def make_summary(verdict="PASS", host_wall_seconds=12.5, counts=None):
    return {
        "verdict": verdict,
        "counts": {"injections": 3, "checks": 7} if counts is None else counts,
        "host_wall_seconds": host_wall_seconds,
    }


def write_ok(root, run_id="run_0001", **kwargs):
    """A complete, valid write. Individual tests override one thing at a time."""
    params = {
        "scenario_text": SCENARIO,
        "events": EVENTS,
        "summary": make_summary(),
        "provenance": make_provenance(),
        "replay_command": REPLAY,
        "root": root,
    }
    params.update(kwargs)
    scenario_text = params.pop("scenario_text")
    events = params.pop("events")
    summary = params.pop("summary")
    provenance = params.pop("provenance")
    return store.write_run(run_id, scenario_text, events, summary, provenance,
                           **params)


def forge_run_dir(root, run_id, provenance, scenario_text=SCENARIO,
                  summary=None, events=None, replay=REPLAY, marker=True):
    """Assemble a run directory WITHOUT using write_run.

    This is the whole point of the read-side gate: a run directory can be copied
    in from another machine, restored from a backup, or produced by an older
    tool. Nothing here goes through the writer's validation, so read_run has to
    do its own.
    """
    path = store.runs_root(root) / run_id
    path.mkdir(parents=True)
    summary = make_summary() if summary is None else summary
    events = EVENTS if events is None else events

    (path / store.SCENARIO_NAME).write_bytes(scenario_text.encode("utf-8"))
    (path / store.EVENTS_NAME).write_bytes(
        "".join(json.dumps(e) + "\n" for e in events).encode("utf-8"))
    (path / store.SUMMARY_NAME).write_bytes(
        json.dumps(summary).encode("utf-8"))
    (path / store.PROVENANCE_NAME).write_bytes(
        json.dumps(provenance).encode("utf-8"))
    (path / store.REPLAY_NAME).write_bytes((replay + "\n").encode("utf-8"))

    if marker:
        (path / store.COMPLETION_MARKER).write_bytes(json.dumps({
            "schema_version": store.SCHEMA_VERSION,
            "run_id": run_id,
            "has_trace": False,
            "files": {name: store.sha256_of_file(path / name)
                      for name in store.MANIFEST_FILES},
        }).encode("utf-8"))
    return path


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------

def test_write_then_read_round_trip(tmp_path):
    path = write_ok(tmp_path, "run_0001")

    assert path.is_dir()
    for name in ("summary.json", "provenance.json", "replay.txt",
                 "scenario.yml", "events.log", "COMPLETE"):
        assert (path / name).is_file(), "%s was not written" % name

    record = store.read_run("run_0001", root=tmp_path)

    assert record.run_id == "run_0001"
    assert record.path == path
    assert record.scenario_text == SCENARIO
    assert record.events == EVENTS
    assert record.replay_command == REPLAY
    assert record.counts == {"injections": 3, "checks": 7}
    assert record.provenance == make_provenance()
    assert record.trace_dir is None


def test_host_wall_seconds_survives_the_round_trip(tmp_path):
    write_ok(tmp_path, "run_wall", summary=make_summary(host_wall_seconds=41.75))

    record = store.read_run("run_wall", root=tmp_path)

    assert record.host_wall_seconds == 41.75
    assert record.summary["host_wall_seconds"] == 41.75
    # It stays under its own name. Anything that renamed it to 'duration' or
    # 'elapsed' would invite it being read as a latency, and every latency in
    # this project comes from virtual time.
    on_disk = json.loads((store.run_path("run_wall", root=tmp_path)
                          / store.SUMMARY_NAME).read_text())
    assert on_disk["host_wall_seconds"] == 41.75


def test_integer_host_wall_seconds_survives_as_a_number(tmp_path):
    write_ok(tmp_path, "run_wall_int", summary=make_summary(host_wall_seconds=0))
    assert store.read_run("run_wall_int", root=tmp_path).host_wall_seconds == 0


def test_events_keep_virtual_microseconds_exactly(tmp_path):
    write_ok(tmp_path, "run_events")
    record = store.read_run("run_events", root=tmp_path)
    assert [e["t_virtual_us"] for e in record.events] == [0, 1500, 90000]


def test_verdict_and_exit_code_round_trip_for_every_verdict(tmp_path):
    expected = {"PASS": 0, "FAIL": 1, "UNUSABLE": 2, "REFUSED": 3,
                "DRY_RUN": 4, "CRASHED": 5}
    for name, code in expected.items():
        run_id = "run_%s" % name.lower()
        write_ok(tmp_path, run_id, summary=make_summary(verdict=name))
        record = store.read_run(run_id, root=tmp_path)
        assert record.verdict is store.Verdict[name]
        assert record.exit_code == code
        assert record.summary["exit_code"] == code


def test_verdict_may_be_given_as_its_exit_code(tmp_path):
    write_ok(tmp_path, "run_by_code", summary=make_summary(verdict=5))
    record = store.read_run("run_by_code", root=tmp_path)
    assert record.verdict is store.Verdict.CRASHED
    assert record.exit_code == 5


# ---------------------------------------------------------------------------
# the central rule, at write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", store.REQUIRED_PROVENANCE_FIELDS)
def test_write_refuses_provenance_missing_one_field_and_names_it(tmp_path, field):
    provenance = make_provenance()
    del provenance[field]

    with pytest.raises(store.ProvenanceError) as excinfo:
        write_ok(tmp_path, "run_missing", provenance=provenance)

    message = str(excinfo.value)
    assert field in message, "the refusal must name the missing field"
    assert "run_missing" in message
    # Nothing partial is left behind, and no default was substituted.
    assert not (store.runs_root(tmp_path) / "run_missing").exists()


@pytest.mark.parametrize("field", store.REQUIRED_PROVENANCE_FIELDS)
def test_write_refuses_provenance_field_set_to_none(tmp_path, field):
    provenance = make_provenance()
    provenance[field] = None

    with pytest.raises(store.ProvenanceError) as excinfo:
        write_ok(tmp_path, "run_none", provenance=provenance)
    assert field in str(excinfo.value)


def test_write_refuses_empty_binary_map(tmp_path):
    with pytest.raises(store.ProvenanceError) as excinfo:
        write_ok(tmp_path, "run_nobins",
                 provenance=make_provenance(binary_sha256={}))
    assert "binary_sha256" in str(excinfo.value)


def test_write_refuses_a_binary_hash_that_is_not_a_sha256(tmp_path):
    with pytest.raises(store.ProvenanceError) as excinfo:
        write_ok(tmp_path, "run_badhash",
                 provenance=make_provenance(binary_sha256={"node_a": "deadbeef"}))
    message = str(excinfo.value)
    assert "binary_sha256" in message and "node_a" in message


def test_write_refuses_scenario_hash_that_does_not_match_the_scenario(tmp_path):
    provenance = make_provenance(scenario_sha256=store.sha256_of_text("other"))

    with pytest.raises(store.ProvenanceError) as excinfo:
        write_ok(tmp_path, "run_mismatch", provenance=provenance)

    message = str(excinfo.value)
    assert "scenario_sha256" in message
    assert store.sha256_of_text(SCENARIO) in message, "name the actual hash"
    # It is reported, never silently recomputed into agreement.
    assert not (store.runs_root(tmp_path) / "run_mismatch").exists()


def test_write_refuses_summary_missing_a_required_field(tmp_path):
    for field in store.REQUIRED_SUMMARY_FIELDS:
        summary = make_summary()
        del summary[field]
        with pytest.raises(store.SummaryError) as excinfo:
            write_ok(tmp_path, "run_sum_%s" % field, summary=summary)
        assert field in str(excinfo.value)


def test_write_refuses_an_undefined_verdict(tmp_path):
    with pytest.raises(store.SummaryError) as excinfo:
        write_ok(tmp_path, "run_bad_verdict",
                 summary=make_summary(verdict="INCONCLUSIVE"))
    message = str(excinfo.value)
    assert "INCONCLUSIVE" in message
    assert "PASS=0" in message, "the refusal must list the defined verdicts"


def test_write_refuses_an_undefined_exit_code(tmp_path):
    with pytest.raises(store.SummaryError) as excinfo:
        write_ok(tmp_path, "run_code_9", summary=make_summary(verdict=9))
    assert "9" in str(excinfo.value)


def test_write_refuses_negative_host_wall_seconds_instead_of_clamping(tmp_path):
    with pytest.raises(store.SummaryError) as excinfo:
        write_ok(tmp_path, "run_neg_wall",
                 summary=make_summary(host_wall_seconds=-0.5))
    assert "host_wall_seconds" in str(excinfo.value)
    assert not (store.runs_root(tmp_path) / "run_neg_wall").exists()


def test_write_refuses_an_event_without_virtual_time(tmp_path):
    events = [{"t_virtual_us": 0, "kind": "start"}, {"kind": "stop"}]

    with pytest.raises(store.EventError) as excinfo:
        write_ok(tmp_path, "run_untimed", events=events)

    message = str(excinfo.value)
    assert "t_virtual_us" in message
    assert "event 1" in message, "the refusal must name which event"


def test_write_refuses_a_float_virtual_timestamp(tmp_path):
    with pytest.raises(store.EventError) as excinfo:
        write_ok(tmp_path, "run_float_t",
                 events=[{"t_virtual_us": 1.5, "kind": "start"}])
    assert "t_virtual_us" in str(excinfo.value)


def test_write_refuses_a_missing_replay_command(tmp_path):
    with pytest.raises(store.ReplayError) as excinfo:
        write_ok(tmp_path, "run_no_replay", replay_command="   ")
    assert "replay_command" in str(excinfo.value)


def test_write_refuses_a_trace_dir_that_is_not_there(tmp_path):
    missing = tmp_path / "no_such_trace"
    with pytest.raises(store.TraceError) as excinfo:
        write_ok(tmp_path, "run_no_trace", trace_dir=missing)
    assert "no_such_trace" in str(excinfo.value)


def test_a_named_trace_dir_is_stored(tmp_path):
    source = tmp_path / "trace_src"
    source.mkdir()
    (source / "trace_part_0.bin").write_text("trace-bytes\n")

    write_ok(tmp_path, "run_traced", trace_dir=source)
    record = store.read_run("run_traced", root=tmp_path)

    assert record.trace_dir is not None
    assert (record.trace_dir / "trace_part_0.bin").read_text() == "trace-bytes\n"


def test_write_refuses_a_run_id_that_could_escape_the_results_tree(tmp_path):
    for bad in ("../escape", "a/b", "", ".hidden", "x" * 200):
        with pytest.raises(store.RunIdError):
            write_ok(tmp_path, bad)


# ---------------------------------------------------------------------------
# the central rule, at read -- independently of the writer
# ---------------------------------------------------------------------------

def test_read_is_refused_when_provenance_is_deleted(tmp_path):
    write_ok(tmp_path, "run_0002")
    (store.run_path("run_0002", root=tmp_path) / store.PROVENANCE_NAME).unlink()

    with pytest.raises(store.StoreError) as excinfo:
        store.read_run("run_0002", root=tmp_path)

    message = str(excinfo.value)
    assert "run_0002" in message, "the refusal must name the run id"
    assert "provenance.json" in message


def test_read_is_refused_when_provenance_is_incomplete_on_disk(tmp_path):
    """The read gate does not trust the write gate.

    This directory never went through write_run, exactly as one copied in from
    another machine never did.
    """
    provenance = make_provenance()
    del provenance["renode_version"]
    forge_run_dir(tmp_path, "run_foreign", provenance)

    with pytest.raises(store.ProvenanceError) as excinfo:
        store.read_run("run_foreign", root=tmp_path)

    message = str(excinfo.value)
    assert "renode_version" in message, "name the field that is missing"
    assert "run_foreign" in message


def test_read_is_refused_when_the_stored_scenario_hash_does_not_match(tmp_path):
    """A record whose provenance hashes some other scenario is not evidence."""
    forge_run_dir(tmp_path, "run_swapped",
                  make_provenance(scenario_sha256="d" * 64))

    with pytest.raises(store.ProvenanceError) as excinfo:
        store.read_run("run_swapped", root=tmp_path)
    assert "scenario_sha256" in str(excinfo.value)


def test_read_is_refused_when_the_summary_on_disk_is_incomplete(tmp_path):
    summary = make_summary()
    del summary["host_wall_seconds"]
    forge_run_dir(tmp_path, "run_thin", make_provenance(), summary=summary)

    with pytest.raises(store.SummaryError) as excinfo:
        store.read_run("run_thin", root=tmp_path)
    assert "host_wall_seconds" in str(excinfo.value)


def test_read_is_refused_when_an_event_on_disk_has_no_virtual_time(tmp_path):
    forge_run_dir(tmp_path, "run_untimed_disk", make_provenance(),
                  events=[{"kind": "start"}])

    with pytest.raises(store.EventError) as excinfo:
        store.read_run("run_untimed_disk", root=tmp_path)
    assert "t_virtual_us" in str(excinfo.value)


def test_read_is_refused_for_a_run_that_does_not_exist(tmp_path):
    with pytest.raises(store.RunNotFoundError) as excinfo:
        store.read_run("run_absent", root=tmp_path)
    assert "run_absent" in str(excinfo.value)


# ---------------------------------------------------------------------------
# completeness
# ---------------------------------------------------------------------------

def test_a_half_written_run_does_not_read_as_complete(tmp_path):
    """Everything present except the marker, which is written last."""
    path = forge_run_dir(tmp_path, "run_half", make_provenance(), marker=False)
    assert (path / store.SUMMARY_NAME).is_file()
    assert (path / store.PROVENANCE_NAME).is_file()

    with pytest.raises(store.IncompleteRunError) as excinfo:
        store.read_run("run_half", root=tmp_path)

    message = str(excinfo.value)
    assert "run_half" in message
    assert store.COMPLETION_MARKER in message


def test_a_run_directory_with_only_some_files_does_not_read_as_complete(tmp_path):
    path = store.runs_root(tmp_path) / "run_stub"
    path.mkdir(parents=True)
    (path / store.SUMMARY_NAME).write_text('{"verdict": "PASS"}')

    with pytest.raises(store.IncompleteRunError) as excinfo:
        store.read_run("run_stub", root=tmp_path)
    assert "run_stub" in str(excinfo.value)


def test_a_file_edited_after_the_write_is_detected(tmp_path):
    write_ok(tmp_path, "run_tampered")
    summary_path = (store.run_path("run_tampered", root=tmp_path)
                    / store.SUMMARY_NAME)
    summary_path.write_bytes(summary_path.read_bytes() + b" ")

    with pytest.raises(store.IntegrityError) as excinfo:
        store.read_run("run_tampered", root=tmp_path)
    assert "summary.json" in str(excinfo.value)


def test_a_run_directory_renamed_to_another_id_is_refused(tmp_path):
    write_ok(tmp_path, "run_original")
    (store.runs_root(tmp_path) / "run_original").rename(
        store.runs_root(tmp_path) / "run_renamed")

    with pytest.raises(store.IntegrityError) as excinfo:
        store.read_run("run_renamed", root=tmp_path)
    message = str(excinfo.value)
    assert "run_renamed" in message and "run_original" in message


# ---------------------------------------------------------------------------
# overwrite policy: refuse, documented in the module docstring
# ---------------------------------------------------------------------------

def test_write_refuses_to_overwrite_an_existing_run_id(tmp_path):
    write_ok(tmp_path, "run_0003")

    with pytest.raises(store.RunExistsError) as excinfo:
        write_ok(tmp_path, "run_0003",
                 summary=make_summary(verdict="FAIL", host_wall_seconds=1.0))

    assert "run_0003" in str(excinfo.value)
    # The first answer is intact: the second write destroyed nothing.
    assert store.read_run("run_0003", root=tmp_path).verdict is store.Verdict.PASS


def test_a_refused_write_leaves_no_staging_directory_behind(tmp_path):
    provenance = make_provenance()
    del provenance["harness_git_commit"]

    with pytest.raises(store.ProvenanceError):
        write_ok(tmp_path, "run_clean", provenance=provenance)

    base = store.runs_root(tmp_path)
    leftovers = list(base.iterdir()) if base.is_dir() else []
    assert leftovers == [], "a refused write must leave nothing on disk"


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def test_list_runs_is_empty_before_anything_is_written(tmp_path):
    assert store.list_runs(root=tmp_path) == []


def test_list_runs_returns_sorted_ids(tmp_path):
    write_ok(tmp_path, "run_b")
    write_ok(tmp_path, "run_a")
    write_ok(tmp_path, "run_c")
    assert store.list_runs(root=tmp_path) == ["run_a", "run_b", "run_c"]


def test_list_runs_ignores_abandoned_staging_directories(tmp_path):
    write_ok(tmp_path, "run_real")
    wreck = store.runs_root(tmp_path) / ".partial-run_real-abcdef"
    wreck.mkdir()
    (wreck / store.SUMMARY_NAME).write_text("{}")

    assert store.list_runs(root=tmp_path) == ["run_real"]


def test_list_runs_does_not_hide_an_incomplete_run(tmp_path):
    """Listing shows what is on disk; read is what enforces completeness.

    Hiding damaged records would make a directory of wreckage look like an
    empty results tree.
    """
    forge_run_dir(tmp_path, "run_damaged", make_provenance(), marker=False)
    assert store.list_runs(root=tmp_path) == ["run_damaged"]
    with pytest.raises(store.IncompleteRunError):
        store.read_run("run_damaged", root=tmp_path)


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

def test_identical_content_produces_identical_bytes(tmp_path):
    """Phase 1's hardest criterion is determinism, checked by hashing."""
    first = write_ok(tmp_path, "run_det_a")
    second = write_ok(tmp_path, "run_det_b")

    for name in store.MANIFEST_FILES:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
