from pathlib import Path

import pytest

from pyci_doctor.check import WORKFLOW_PATH, Status, check_workflow
from pyci_doctor.cli import EXIT_CANNOT_CHECK, EXIT_OK, EXIT_PROBLEM_FOUND, main

REPO_ROOT = Path(__file__).parent.parent

PYPROJECT = (
    '[project]\nname = "x"\nversion = "1"\nrequires-python = ">=3.11"\n'
    '[project.optional-dependencies]\ndev = ["pytest"]\n'
)


@pytest.fixture
def project(tmp_path):
    """A small valid project with a freshly generated workflow."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    assert main(["init", str(tmp_path)]) == EXIT_OK
    return tmp_path


def workflow(project_dir: Path) -> Path:
    return project_dir / WORKFLOW_PATH


def write_exact(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")


# --- check_workflow(): one test per status ---


def test_fresh_workflow_is_up_to_date(project):
    result = check_workflow(project)
    assert result.status is Status.UP_TO_DATE
    assert result.ok


def test_missing_workflow(tmp_path):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    result = check_workflow(tmp_path)
    assert result.status is Status.MISSING
    assert not result.ok


def test_project_change_makes_workflow_out_of_date(project):
    # Adding ruff means init would now add a lint job.
    (project / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    result = check_workflow(project)
    assert result.status is Status.OUT_OF_DATE
    assert "+  lint:" in result.diff


def test_hand_edit_makes_workflow_out_of_date(project):
    path = workflow(project)
    write_exact(path, path.read_text(encoding="utf-8").replace("timeout-minutes: 15", "timeout-minutes: 60"))
    result = check_workflow(project)
    assert result.status is Status.OUT_OF_DATE
    assert "-    timeout-minutes: 60" in result.diff
    assert "+    timeout-minutes: 15" in result.diff


def test_crlf_only_difference_is_reported_separately(project):
    path = workflow(project)
    write_exact(path, path.read_text(encoding="utf-8").replace("\n", "\r\n"))
    result = check_workflow(project)
    assert result.status is Status.LINE_ENDINGS
    assert not result.ok


def test_hand_written_workflow_is_skipped_unless_strict(project):
    write_exact(workflow(project), "name: My own CI\non: push\n")

    assert check_workflow(project).status is Status.NOT_GENERATED
    assert check_workflow(project).ok

    strict = check_workflow(project, strict=True)
    assert strict.status is Status.OUT_OF_DATE
    assert "-name: My own CI" in strict.diff


def test_init_options_are_remembered_through_the_header(project):
    # Generated with --all-pythons, so check must regenerate with --all-pythons too,
    # otherwise it would wrongly report the extra Python versions as drift.
    assert main(["init", str(project), "--force", "--all-pythons"]) == EXIT_OK
    text = workflow(project).read_text(encoding="utf-8")
    assert text.splitlines()[0].endswith("--all-pythons")
    assert '["3.11", "3.12", "3.13", "3.14", "3.15"]' in text
    assert check_workflow(project).status is Status.UP_TO_DATE


# --- The `check` command: output and exit codes ---


def test_check_command_up_to_date(project, capsys):
    assert main(["check", str(project)]) == EXIT_OK
    assert "up to date" in capsys.readouterr().out


def test_check_command_prints_diff_and_exits_1(project, capsys):
    (project / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")
    assert main(["check", str(project)]) == EXIT_PROBLEM_FOUND
    captured = capsys.readouterr()
    assert "out of date" in captured.err
    assert captured.out.startswith("--- .github/workflows/ci.yml (current)")


def test_check_command_missing_exits_1(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    assert main(["check", str(tmp_path)]) == EXIT_PROBLEM_FOUND
    assert "run: pyci-doctor init" in capsys.readouterr().err


def test_check_command_line_endings_message(project, capsys):
    path = workflow(project)
    write_exact(path, path.read_text(encoding="utf-8").replace("\n", "\r\n"))
    assert main(["check", str(project)]) == EXIT_PROBLEM_FOUND
    assert ".gitattributes" in capsys.readouterr().err


def test_check_command_exits_2_when_project_cannot_be_inspected(project, capsys):
    (project / "pyproject.toml").write_text("[project\n", encoding="utf-8")
    assert main(["check", str(project)]) == EXIT_CANNOT_CHECK
    assert "Invalid pyproject.toml" in capsys.readouterr().err


def test_this_repo_passes_its_own_check():
    assert main(["check", str(REPO_ROOT)]) == EXIT_OK


def test_flaky_runs_option_is_remembered_through_the_header(project):
    assert main(["init", str(project), "--force", "--flaky-runs", "4"]) == EXIT_OK
    assert check_workflow(project).status is Status.UP_TO_DATE
