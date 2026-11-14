import json
import shutil
from pathlib import Path

import pytest

from pyci_doctor.cli import EXIT_CANNOT_CHECK, EXIT_OK, EXIT_PROBLEM_FOUND, main
from pyci_doctor.flaky import (
    FlakyError,
    Outcome,
    classify,
    collect_result_files,
    failure_kind,
    parse_junit,
    run_repeatedly,
)

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

    def fake_runner(command):
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
        run_repeatedly(["pytest"], 3, tmp_path, runner=lambda command: 4)


def test_fewer_than_two_runs_is_an_error(tmp_path):
    with pytest.raises(FlakyError, match="at least 2 runs"):
        run_repeatedly(["pytest"], 1, tmp_path, runner=lambda command: 0)


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
