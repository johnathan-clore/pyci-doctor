"""Tests for AI suggestions. No real model is ever called: every reply is canned."""

import io
import json
import urllib.error

import pytest

from pyci_doctor import ai
from pyci_doctor.check import WORKFLOW_PATH, Status, check_workflow
from pyci_doctor.cli import AI_HEADER, EXIT_CANNOT_CHECK, EXIT_OK, EXIT_PROBLEM_FOUND, main
from pyci_doctor.detect import ProjectProfile
from pyci_doctor.render import render_workflow


def make_profile(**overrides) -> ProjectProfile:
    values = dict(
        name="demo",
        layout="src",
        dependency_source="pyproject",
        install_command='python -m pip install -e ".[dev]"',
        test_runner="pytest",
        test_command="python -m pytest -v",
        python_versions=["3.11", "3.12", "3.13", "3.14"],
        has_ruff=False,
        has_coverage=False,
    )
    values.update(overrides)
    return ProjectProfile(**values)


PROFILE = make_profile()
BASE = render_workflow(PROFILE)

# A sensible improvement: also test 3.12, give Windows more time.
GOOD = BASE.replace('["3.11", "3.14"]', '["3.11", "3.12", "3.14"]').replace(
    "timeout-minutes: 15", "timeout-minutes: 20"
)


def reply(workflow: str, changes=None) -> str:
    if changes is None:
        changes = [{"summary": "Test Python 3.12 too", "reason": "Most users run it."}]
    return json.dumps({"changes": changes, "workflow": workflow})


def fake(*replies):
    """A client that returns (or raises) the given items in order and records each prompt."""
    queue = list(replies)
    prompts = []

    def client(prompt):
        prompts.append(prompt)
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    client.prompts = prompts
    return client


def failed_rules(result):
    return [r.rule for r in result.rules if not r.passed and r.severity == "error"]


def run(workflow_text, changes=None):
    return ai.suggest(PROFILE, BASE, fake(reply(workflow_text, changes)), sleep=lambda s: None)


# --- A good suggestion is accepted ---


def test_good_suggestion_is_accepted():
    result = run(GOOD)
    assert result.accepted
    assert failed_rules(result) == []


def test_baseline_workflow_passes_every_rule():
    # Our own generator's output must satisfy the rules we hold the model to.
    assert all(r.passed for r in ai.validate(BASE, PROFILE))


# --- Unsafe or broken suggestions are rejected, each by the right rule ---


@pytest.mark.parametrize(
    ("description", "workflow", "rule"),
    [
        ("drops Windows", GOOD.replace("windows-latest, ", ""), "all-platforms"),
        ("drops newest Python", GOOD.replace(', "3.14"]', "]"), "python-versions-kept"),
        (
            "unquoted versions",
            GOOD.replace('["3.11", "3.12", "3.14"]', "[3.11, 3.12, 3.14]"),
            "python-versions-are-strings",
        ),
        (
            "changes test command",
            GOOD.replace("run: python -m pytest -v", "run: python -m pytest -x"),
            "tests-still-run",
        ),
        (
            "hides failures with || true",
            GOOD.replace("run: python -m pytest -v", "run: python -m pytest -v || true"),
            "failures-not-hidden",
        ),
        (
            "continue-on-error",
            GOOD.replace("    timeout-minutes: 20", "    timeout-minutes: 20\n    continue-on-error: true"),
            "failures-not-hidden",
        ),
        ("write permissions", GOOD.replace("contents: read", "contents: write"), "read-only-permissions"),
        ("pull_request_target", GOOD.replace("  pull_request:\n", "  pull_request_target:\n"), "safe-triggers"),
        ("removes a timeout", GOOD.replace("    timeout-minutes: 20\n", ""), "job-timeouts"),
        (
            "uses secrets",
            GOOD.replace(
                "run: python -m pytest -v",
                "run: python -m pytest -v\n        env:\n          TOKEN: ${{ secrets.TOKEN }}",
            ),
            "no-secrets",
        ),
        (
            "curl | bash",
            GOOD.replace(
                "      - name: Run tests",
                "      - name: Setup\n        run: curl -sSL https://example.com/x.sh | bash\n\n"
                "      - name: Run tests",
            ),
            "no-remote-scripts",
        ),
        ("unpinned action", GOOD.replace("actions/checkout@v7", "actions/checkout"), "actions-pinned"),
        (
            "removes the test job",
            "name: CI\non: push\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
            "    timeout-minutes: 5\n    steps:\n      - run: echo hi\n",
            "test-job-kept",
        ),
        ("invalid YAML", "jobs: [unclosed\n", "valid-yaml"),
    ],
)
def test_unsafe_suggestion_is_rejected(description, workflow, rule):
    result = run(workflow)
    assert not result.accepted, description
    assert rule in failed_rules(result), f"{description}: expected {rule}, got {failed_rules(result)}"


def test_undescribed_changes_are_rejected():
    result = run(GOOD, changes=[])
    assert not result.accepted
    assert failed_rules(result) == ["changes-described"]


def test_every_rule_has_a_valid_severity():
    # Guards against a message string landing in the severity field by mistake.
    for result in (run(GOOD), run(GOOD, changes=[]), run(GOOD.replace("windows-latest, ", ""))):
        assert {r.severity for r in result.rules} <= {"error", "warning"}


def test_third_party_action_is_a_warning_not_a_rejection():
    workflow = GOOD.replace("actions/checkout@v7", "someone/checkout@v1")
    result = run(workflow)
    assert result.accepted
    warning = next(r for r in result.rules if r.rule == "only-official-actions")
    assert not warning.passed and warning.severity == "warning"


def test_unchanged_workflow_is_not_a_suggestion():
    result = run(BASE)
    assert result.unchanged
    assert not result.accepted


# --- Model replies that can't be used ---


@pytest.mark.parametrize(
    ("bad_reply", "message"),
    [
        ("Sure! Here is a better workflow.", "isn't valid JSON"),
        ('{"changes": []}', "no 'workflow'"),
        ('["not", "an", "object"]', "JSON object"),
        ('{"changes": "lots", "workflow": "x: 1"}', "'changes' must be a list"),
        ("   ", "empty reply"),
    ],
)
def test_unusable_reply_is_reported_not_raised(bad_reply, message):
    result = ai.suggest(PROFILE, BASE, fake(bad_reply), sleep=lambda s: None)
    assert not result.accepted
    assert message in result.error


def test_json_inside_a_code_fence_is_accepted():
    fenced = "```json\n" + reply(GOOD) + "\n```"
    assert ai.suggest(PROFILE, BASE, fake(fenced), sleep=lambda s: None).accepted


# --- Retries ---


def test_timeouts_are_retried_with_growing_waits():
    waits = []
    client = fake(TimeoutError("slow"), ConnectionError("reset"), reply(GOOD))
    result = ai.suggest(PROFILE, BASE, client, sleep=waits.append)
    assert result.accepted
    assert waits == [2.0, 4.0]
    assert len(client.prompts) == 3


def test_gives_up_after_three_attempts():
    client = fake(TimeoutError("a"), TimeoutError("b"), TimeoutError("c"))
    result = ai.suggest(PROFILE, BASE, client, sleep=lambda s: None)
    assert "after 3 attempts" in result.error


def test_other_errors_are_not_retried():
    client = fake(ValueError("bug in client"))
    with pytest.raises(ValueError):
        ai.suggest(PROFILE, BASE, client, sleep=lambda s: None)
    assert len(client.prompts) == 1


# --- The prompt ---


def test_prompt_contains_the_rules_and_the_workflow_but_no_code():
    client = fake(reply(GOOD))
    ai.suggest(PROFILE, BASE, client, sleep=lambda s: None)
    prompt = client.prompts[0]
    assert BASE in prompt
    assert "python -m pytest -v" in prompt
    assert "pull_request_target" in prompt
    assert '"name": "demo"' in prompt


# --- The Anthropic client, with the network faked ---


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_anthropic_client_sends_a_messages_request(monkeypatch):
    sent = {}

    def fake_urlopen(request, timeout):
        sent["url"] = request.full_url
        sent["headers"] = {k.lower(): v for k, v in request.header_items()}
        sent["body"] = json.loads(request.data)
        body = {"content": [{"type": "text", "text": "hello "}, {"type": "text", "text": "there"}]}
        return FakeResponse(json.dumps(body).encode())

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    text = ai.anthropic_client("key-123", model="some-model")("prompt text")

    assert text == "hello there"
    assert sent["url"] == ai.API_URL
    assert sent["headers"]["x-api-key"] == "key-123"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["model"] == "some-model"
    assert sent["body"]["messages"] == [{"role": "user", "content": "prompt text"}]


@pytest.mark.parametrize(("code", "expected"), [(529, ConnectionError), (503, ConnectionError), (401, ai.SuggestError)])
def test_anthropic_client_http_errors(monkeypatch, code, expected):
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(ai.API_URL, code, "error", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(expected):
        ai.anthropic_client("k")("p")


def test_anthropic_client_network_down_is_retryable(monkeypatch):
    def fake_urlopen(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ConnectionError):
        ai.anthropic_client("k")("p")


# --- The `suggest` command ---


@pytest.fixture
def project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1"\nrequires-python = ">=3.11"\n'
        '[project.optional-dependencies]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )
    assert main(["init", str(tmp_path)]) == EXIT_OK
    return tmp_path


def project_workflow(project):
    return (project / WORKFLOW_PATH).read_text(encoding="utf-8")


def good_reply_for(project):
    # Change something that doesn't depend on which Python versions are current.
    current = project_workflow(project)
    improved = current.replace("timeout-minutes: 15", "timeout-minutes: 20")
    assert improved != current
    return reply(improved, [{"summary": "Longer test timeout", "reason": "Windows runners are slower."}])


def test_suggest_command_shows_checks_and_diff_without_writing(project, capsys):
    before = project_workflow(project)
    assert main(["suggest", str(project)], client=fake(good_reply_for(project))) == EXIT_OK
    out = capsys.readouterr().out
    assert "PASS  all-platforms" in out
    assert "+    timeout-minutes: 20" in out
    assert "Accepted" in out
    assert project_workflow(project) == before, "nothing is written without --write"


def test_suggest_write_saves_and_marks_file_as_hand_maintained(project, capsys):
    assert main(["suggest", str(project), "--write"], client=fake(good_reply_for(project))) == EXIT_OK
    written = project_workflow(project)
    assert written.startswith(AI_HEADER + "\n")
    assert "# Generated by pyci-doctor" not in written
    assert "timeout-minutes: 20" in written
    # From now on `check` leaves the file alone instead of reporting drift.
    assert check_workflow(project).status is Status.NOT_GENERATED


def test_suggest_rejected_suggestion_exits_1_and_writes_nothing(project, capsys):
    before = project_workflow(project)
    bad = reply(before.replace("windows-latest, ", ""))
    assert main(["suggest", str(project), "--write"], client=fake(bad)) == EXIT_PROBLEM_FOUND
    assert "FAIL  all-platforms" in capsys.readouterr().out
    assert project_workflow(project) == before


def test_suggest_json_output(project, capsys):
    main(["suggest", str(project), "--json"], client=fake(good_reply_for(project)))
    data = json.loads(capsys.readouterr().out)
    assert data["accepted"] is True
    assert {c["rule"] for c in data["checks"]} >= {"all-platforms", "no-secrets"}


def test_suggest_without_api_key_exits_2(project, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["suggest", str(project)]) == EXIT_CANNOT_CHECK
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_suggest_model_failure_exits_2(project, capsys, monkeypatch):
    waits = []
    monkeypatch.setattr(ai.time, "sleep", waits.append)  # no real waiting in tests
    client = fake(TimeoutError("a"), TimeoutError("b"), TimeoutError("c"))
    assert main(["suggest", str(project)], client=client) == EXIT_CANNOT_CHECK
    assert "after 3 attempts" in capsys.readouterr().err
    assert waits == [2.0, 4.0], "the patched sleep must be used, so the test doesn't really wait"


def test_suggest_output_is_plain_ascii(project, capsys):
    # The Windows PowerShell 5.1 console can't encode symbols like a check mark.
    main(["suggest", str(project)], client=fake(good_reply_for(project)))
    capsys.readouterr().out.encode("ascii")
