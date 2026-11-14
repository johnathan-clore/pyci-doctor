import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from pyci_doctor.cli import EXIT_CANNOT_CHECK, EXIT_OK, EXIT_PROBLEM_FOUND, main, split_command
from pyci_doctor.flaky import (
    FlakyError,
    Outcome,
    classify,
    collect_result_files,
    failure_kind,
    github_annotations,
    markdown_summary,
    parse_junit,
    run_repeatedly,
    seeds_from_files,
)
from pyci_doctor.pytest_shuffle import shuffled_order

JUNIT = Path(__file__).parent / "fixtures" / "junit"
THREE_RUNS = JUNIT / "three_runs"


def by_id(histories):
    return {h.test_id.split("::")[1]: h for h in histories}


@pytest.fixture
def three_runs():
    files = collect_result_files([THREE_RUNS])
    return by_id(classify([parse_junit(f) for f in files]))


# --- Reading JUnit XML ---


def test_parse_reads_every_kind_of_result():
    outcomes = {o.test_id: o for o in parse_junit(THREE_RUNS / "run-2.xml")}
    assert len(outcomes) == 7
    assert outcomes["tests.test_api::test_health"].result == "passed"
    assert outcomes["tests.test_api::test_fetch_user"].result == "failed"
    assert "TimeoutError" in outcomes["tests.test_api::test_fetch_user"].message
    assert outcomes["tests.test_api::test_upload"].result == "error"
    assert outcomes["tests.test_math::test_windows_only"].result == "skipped"


def test_short_message_and_full_details_are_kept_separately():
    outcomes = {o.test_id: o for o in parse_junit(THREE_RUNS / "run-2.xml")}
    fetch = outcomes["tests.test_api::test_fetch_user"]
    assert fetch.message == "TimeoutError: The read operation timed out"
    assert "at tests/test_api.py:12" in fetch.details


def test_infrastructure_cause_found_deep_in_the_traceback():
    # The short message only says the request failed; the traceback shows why.
    run = [Outcome("t::a", "failed", "RuntimeError: request failed", "...\nTimeoutError: timed out\n...")]
    assert classify([run, [Outcome("t::a", "passed")]])[0].suspected_cause == "infrastructure"


def test_parse_accepts_a_bare_testsuite_root(tmp_path):
    f = tmp_path / "r.xml"
    f.write_text('<testsuite><testcase classname="a" name="b"/></testsuite>', encoding="utf-8")
    assert parse_junit(f) == [Outcome("a::b", "passed")]


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("broken.xml", "Invalid JUnit XML"),
        ("not_junit.xml", "not JUnit XML"),
        ("does_not_exist.xml", "File not found"),
    ],
)
def test_parse_rejects_bad_input_with_a_clear_message(filename, message):
    with pytest.raises(FlakyError, match=message):
        parse_junit(JUNIT / filename)


def test_folder_expands_to_its_xml_files_in_order():
    assert [f.name for f in collect_result_files([THREE_RUNS])] == ["run-1.xml", "run-2.xml", "run-3.xml"]


def test_empty_folder_is_an_error(tmp_path):
    with pytest.raises(FlakyError, match="No result files"):
        collect_result_files([tmp_path])


# --- Classifying tests across runs ---


def test_always_passing(three_runs):
    h = three_runs["test_health"]
    assert h.category == "stable-pass"
    assert h.results == ["passed", "passed", "passed"]


def test_always_failing(three_runs):
    h = three_runs["test_broken"]
    assert h.category == "stable-fail"
    assert h.suspected_cause == "test"


def test_flaky_because_of_infrastructure(three_runs):
    h = three_runs["test_fetch_user"]
    assert h.category == "flaky"
    assert (h.passed, h.failed) == (2, 1)
    assert h.suspected_cause == "infrastructure"
    assert "timed out" in h.first_failure


def test_flaky_because_of_the_test_itself(three_runs):
    h = three_runs["test_rounding"]
    assert h.category == "flaky"
    assert h.suspected_cause == "test"


def test_flaky_with_mixed_causes(three_runs):
    # Run 2: ConnectionResetError during setup. Run 3: a real assertion failure.
    h = three_runs["test_upload"]
    assert h.results == ["passed", "error", "failed"]
    assert h.suspected_cause == "mixed"


def test_skipped_every_time(three_runs):
    assert three_runs["test_windows_only"].category == "skipped"


def test_test_missing_from_a_run_is_recorded(three_runs):
    h = three_runs["test_new"]
    assert h.results == ["missing", "passed", "passed"]
    assert h.category == "stable-pass"


def test_test_that_disappears_later_is_recorded():
    runs = [[Outcome("t::a", "passed")], []]
    assert classify(runs)[0].results == ["passed", "missing"]


def test_flaky_tests_are_listed_first():
    categories = [h.category for h in classify([parse_junit(f) for f in collect_result_files([THREE_RUNS])])]
    assert categories == sorted(categories, key=["flaky", "stable-fail", "skipped", "stable-pass"].index)


# --- Infrastructure vs test failures ---


@pytest.mark.parametrize(
    "message",
    [
        "TimeoutError: The read operation timed out",
        "requests.exceptions.ConnectionError: Max retries exceeded",
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        "ConnectionRefusedError: [Errno 111] Connection refused",
        "socket.gaierror: [Errno -3] Temporary failure in name resolution",
        "OSError: [Errno 28] No space left on device",
        "MemoryError",
        "HTTPError: 503 Service Unavailable",
        "HTTPError: 429 Too Many Requests",
        "openai.RateLimitError: rate limit reached",
    ],
)
def test_environment_problems_are_infrastructure(message):
    assert failure_kind(message) == "infrastructure"


@pytest.mark.parametrize(
    "message",
    [
        "AssertionError: assert 3 == 4",
        "assert settings.timeout == 30",  # mentions "timeout" but is an ordinary assertion
        "AssertionError: assert 429 == 200",  # a status code in an assertion is the test's verdict
        "KeyError: 'user_id'",
        "TypeError: unsupported operand type(s)",
        "",
    ],
)
def test_code_problems_are_test_failures(message):
    assert failure_kind(message) == "test"


# --- Running the suite repeatedly, with the subprocess faked ---


def test_run_repeatedly_writes_one_file_per_run(tmp_path):
    sources = iter(sorted(THREE_RUNS.glob("*.xml")))
    commands = []

    def fake_runner(command, env):
        commands.append(command)
        target = Path(command[-1].removeprefix("--junitxml="))
        shutil.copy(next(sources), target)
        return 1  # some tests failed; that's normal here

    files = run_repeatedly(["pytest", "-q"], 3, tmp_path / "out", runner=fake_runner)
    assert [f.name for f in files] == ["run-001.xml", "run-002.xml", "run-003.xml"]
    assert commands[0][:2] == ["pytest", "-q"]
    assert by_id(classify([parse_junit(f) for f in files]))["test_fetch_user"].category == "flaky"


def test_run_that_writes_no_results_is_an_error(tmp_path):
    with pytest.raises(FlakyError, match="Run 1 produced no results"):
        run_repeatedly(["pytest"], 3, tmp_path, runner=lambda command, env: 4)


def test_fewer_than_two_runs_is_an_error(tmp_path):
    with pytest.raises(FlakyError, match="at least 2 runs"):
        run_repeatedly(["pytest"], 1, tmp_path, runner=lambda command, env: 0)


# --- The `flaky` command ---


def test_flaky_command_report_and_exit_code(capsys):
    assert main(["flaky", str(THREE_RUNS)]) == EXIT_PROBLEM_FOUND
    out = capsys.readouterr().out
    assert out.startswith("7 tests across 3 runs: 3 flaky, 1 always failing, 2 always passing, 1 skipped")
    assert "tests.test_api::test_fetch_user  passed 2/3, failed 1/3, cause: infrastructure" in out
    assert "Missing from some runs" in out


def test_flaky_command_json(capsys):
    main(["flaky", str(THREE_RUNS), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["runs"] == 3
    assert data["tests"][0]["category"] == "flaky"


def test_flaky_command_clean_results_exit_0(tmp_path, capsys):
    f = tmp_path / "ok.xml"
    f.write_text('<testsuite><testcase classname="a" name="b"/></testsuite>', encoding="utf-8")
    assert main(["flaky", str(f), str(f)]) == EXIT_OK
    assert "0 flaky" in capsys.readouterr().out


def test_flaky_command_bad_file_exits_2(capsys):
    assert main(["flaky", str(JUNIT / "broken.xml")]) == EXIT_CANNOT_CHECK
    assert "Invalid JUnit XML" in capsys.readouterr().err


def test_flaky_command_rejects_files_and_runs_together(capsys):
    assert main(["flaky", str(THREE_RUNS), "--runs", "3"]) == EXIT_CANNOT_CHECK
    assert "not both" in capsys.readouterr().err


# --- End to end: real pytest, a test that is flaky on purpose ---

FLAKY_SUITE = '''
from pathlib import Path

COUNTER = Path(__file__).with_name("counter.txt")


def test_sometimes():
    # Deterministic "flakiness": passes on runs 1 and 3, fails on run 2.
    n = int(COUNTER.read_text()) if COUNTER.exists() else 0
    COUNTER.write_text(str(n + 1))
    assert n % 2 == 0


def test_always():
    assert True
'''


def test_runs_real_pytest_and_finds_the_flaky_test(tmp_path, monkeypatch, capsys):
    (tmp_path / "test_suite.py").write_text(FLAKY_SUITE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["flaky", "--runs", "3", "--out", "results", "--json"]) == EXIT_PROBLEM_FOUND

    data = json.loads(capsys.readouterr().out)
    tests = {t["test_id"].split("::")[1]: t for t in data["tests"]}
    assert tests["test_sometimes"]["results"] == ["passed", "failed", "passed"]
    assert tests["test_sometimes"]["suspected_cause"] == "test"
    assert tests["test_always"]["category"] == "stable-pass"
    assert len(list((tmp_path / "results").glob("run-*.xml"))) == 3


def test_progress_goes_to_stderr_so_json_stays_clean(tmp_path, monkeypatch, capfd):
    # capfd captures at the file-descriptor level, so it also sees output from the pytest subprocesses.
    (tmp_path / "test_suite.py").write_text(FLAKY_SUITE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["flaky", "--runs", "2", "--json"])
    captured = capfd.readouterr()
    json.loads(captured.out)  # would raise if pytest's own output had leaked into stdout
    assert "run 1/2" in captured.err


# --- Shuffled order: catching tests that depend on which test ran before them ---

ORDER_DEPENDENT_SUITE = '''
STATE = []


def test_expects_clean_state():
    # Passes in the normal order. Fails whenever test_leaves_state_behind runs first.
    assert STATE == []


def test_leaves_state_behind():
    STATE.append("leftover")
'''
CLEAN = "test_suite.py::test_expects_clean_state"
DIRTY = "test_suite.py::test_leaves_state_behind"


def seed_with_both_orders(runs: int) -> int:
    """A first seed whose runs include both orders, found with the plugin's own ordering function."""
    for base in range(1, 1000):
        orders = {tuple(shuffled_order([CLEAN, DIRTY], base + i)) for i in range(1, runs + 1)}
        if len(orders) == 2:
            return base
    raise AssertionError("no suitable seed")


def test_shuffled_order_is_reproducible_and_complete():
    ids = [f"tests/test_{f}.py::test_{n}" for f in "abc" for n in range(5)]
    assert shuffled_order(ids, 7) == shuffled_order(ids, 7)
    assert sorted(shuffled_order(ids, 7)) == sorted(ids)
    assert shuffled_order(ids, 7) != shuffled_order(ids, 8)


def test_shuffled_order_keeps_tests_grouped_by_file():
    ids = [f"tests/test_{f}.py::test_{n}" for f in "abc" for n in range(5)]
    files = [node_id.split("::")[0] for node_id in shuffled_order(ids, 3)]
    # Each file appears as one contiguous block.
    assert len([f for i, f in enumerate(files) if i == 0 or files[i - 1] != f]) == 3


def test_fixed_order_misses_an_order_dependent_test(tmp_path, monkeypatch, capsys):
    (tmp_path / "test_suite.py").write_text(ORDER_DEPENDENT_SUITE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["flaky", "--runs", "4", "--no-shuffle", "--json"]) == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert all(t["category"] == "stable-pass" for t in data["tests"])


def test_shuffled_runs_find_the_order_dependent_test_and_its_seeds(tmp_path, monkeypatch, capsys):
    (tmp_path / "test_suite.py").write_text(ORDER_DEPENDENT_SUITE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    base = seed_with_both_orders(4)

    assert main(["flaky", "--runs", "4", "--seed", str(base), "--json"]) == EXIT_PROBLEM_FOUND

    data = json.loads(capsys.readouterr().out)
    clean = next(t for t in data["tests"] if t["test_id"].endswith("test_expects_clean_state"))
    assert clean["category"] == "flaky"
    expected_bad = [base + i for i in range(1, 5) if shuffled_order([CLEAN, DIRTY], base + i)[0] == DIRTY]
    assert clean["failing_seeds"] == expected_bad
    assert data["seeds"] == [base + i for i in range(1, 5)]


def test_a_failing_seed_replays_the_failure(tmp_path):
    (tmp_path / "test_suite.py").write_text(ORDER_DEPENDENT_SUITE, encoding="utf-8")
    bad = next(s for s in range(1, 100) if shuffled_order([CLEAN, DIRTY], s)[0] == DIRTY)
    good = next(s for s in range(1, 100) if shuffled_order([CLEAN, DIRTY], s)[0] == CLEAN)

    def replay(seed, *extra):
        env = {**os.environ, "PYCI_DOCTOR_SEED": str(seed)}
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "pyci_doctor.pytest_shuffle", *extra]
        return subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)

    assert replay(bad, "-q").returncode == 1
    assert replay(good, "-q").returncode == 0
    # Without -q, pytest's header names the seed, so a log shows which order ran.
    assert f"test order shuffled by pyci-doctor, seed {bad}" in replay(bad).stdout


def test_shuffling_loads_a_copied_plugin_via_pythonpath(tmp_path):
    seen = {}

    def fake_runner(command, env):
        seen["command"], seen["env"] = command, env
        Path(command[-1].removeprefix("--junitxml=")).write_text("<testsuite/>", encoding="utf-8")
        return 0

    files = run_repeatedly(["pytest"], 2, tmp_path, runner=fake_runner, seed=100, environ={"PYTHONPATH": "keep"})
    assert [f.name for f in files] == ["run-001-seed-101.xml", "run-002-seed-102.xml"]
    assert seen["command"][1:3] == ["-p", "pyci_doctor_shuffle"]
    plugin_dir = Path(seen["env"]["PYTHONPATH"].split(os.pathsep)[0])
    assert (plugin_dir / "pyci_doctor_shuffle.py").is_file()
    assert seen["env"]["PYTHONPATH"].endswith(os.pathsep + "keep"), "existing PYTHONPATH must be kept"
    assert seen["env"]["PYCI_DOCTOR_SEED"] == "102"


def test_old_results_are_removed_before_a_new_session(tmp_path):
    (tmp_path / "run-007.xml").write_text("<testsuite/>", encoding="utf-8")

    def fake_runner(command, env):
        Path(command[-1].removeprefix("--junitxml=")).write_text("<testsuite/>", encoding="utf-8")
        return 0

    run_repeatedly(["pytest"], 2, tmp_path, runner=fake_runner)
    assert sorted(f.name for f in tmp_path.glob("run-*.xml")) == ["run-001.xml", "run-002.xml"]


def test_seeds_are_read_from_file_names():
    names = [Path("run-001-seed-5.xml"), Path("run-002.xml")]
    assert seeds_from_files(names) == [5, None]


# --- GitHub output: annotations on the PR and a summary on the run page ---


def three_run_histories():
    return classify([parse_junit(f) for f in collect_result_files([THREE_RUNS])])


def test_annotations_for_flaky_and_failing_tests():
    lines = github_annotations(three_run_histories(), 3)
    assert len(lines) == 4  # 3 flaky + 1 always failing
    fetch = next(line for line in lines if "test_fetch_user" in line)
    assert fetch.startswith("::warning title=Flaky test%3A tests.test_api%3A%3Atest_fetch_user::passed 2/3")
    assert "%0Afirst failure: TimeoutError" in fetch, "newlines must be escaped or GitHub cuts the message"
    assert any(line.startswith("::error title=Always failing") for line in lines)


def test_summary_table_lists_problems_only():
    table = markdown_summary(three_run_histories(), 3)
    assert table.startswith("### Flaky test check: 3 runs")
    assert "| `tests.test_api::test_fetch_user` | flaky | 2/3 | infrastructure |" in table
    assert "test_health" not in table


def test_github_mode_prints_annotations_and_writes_the_summary(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    main(["flaky", str(THREE_RUNS)])
    assert "::warning title=Flaky test" in capsys.readouterr().out
    assert "### Flaky test check" in summary.read_text(encoding="utf-8")


def test_json_output_never_contains_annotations(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    main(["flaky", str(THREE_RUNS), "--json"])
    json.loads(capsys.readouterr().out)


def test_no_annotations_outside_github(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    main(["flaky", str(THREE_RUNS)])
    assert "::warning" not in capsys.readouterr().out


# --- --command ---


def test_command_option_does_not_break_subcommand_dispatch(capsys):
    # Regression: --command once overwrote args.command, the chosen subcommand, so nothing ran.
    assert main(["flaky", str(THREE_RUNS), "--command", "uv run pytest"]) == EXIT_PROBLEM_FOUND
    assert capsys.readouterr().out.startswith("7 tests across 3 runs")


@pytest.mark.parametrize(
    ("text", "windows", "expected"),
    [
        ("uv run pytest", False, ["uv", "run", "pytest"]),
        ("uv run pytest", True, ["uv", "run", "pytest"]),
        (r"C:\Python314\python.exe -m pytest", True, [r"C:\Python314\python.exe", "-m", "pytest"]),
        (
            r'"C:\Program Files\Python\python.exe" -m pytest',
            True,
            [r"C:\Program Files\Python\python.exe", "-m", "pytest"],
        ),
        ("'/opt/my python/bin/python' -m pytest", False, ["/opt/my python/bin/python", "-m", "pytest"]),
    ],
)
def test_split_command(text, windows, expected):
    assert split_command(text, windows=windows) == expected


def test_custom_command_runs_real_pytest(tmp_path, monkeypatch, capsys):
    (tmp_path / "test_suite.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    command = f'"{sys.executable}" -m pytest'
    assert main(["flaky", "--runs", "2", "--command", command, "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["tests"][0]["category"] == "stable-pass"
