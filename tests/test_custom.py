"""Custom sections: users' own steps and jobs survive regeneration and don't count as drift."""

import shutil
from pathlib import Path

import pytest
import yaml

from pyci_doctor.check import WORKFLOW_PATH, Status, check_workflow
from pyci_doctor.cli import EXIT_CANNOT_CHECK, EXIT_OK, EXIT_PROBLEM_FOUND, main
from pyci_doctor.render import RenderError, extract_custom_blocks

FIXTURES = Path(__file__).parent / "fixtures"

REPORT_STEP = """\
      - name: Build the scorecard
        run: python -m evals.report"""

DEPLOY_JOB = """\
  docs:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - run: echo "build docs here\""""


@pytest.fixture
def project(tmp_path):
    """A copy of the fake project shaped like llm-output-eval, with a generated workflow."""
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "config_only_pyproject", root)
    assert main(["init", str(root)]) == EXIT_OK
    return root


def workflow(root: Path) -> Path:
    return root / WORKFLOW_PATH


def add_to_section(root: Path, section: str, text: str) -> None:
    """Insert text just before a section's end marker, as a user editing the file would."""
    path = workflow(root)
    content = path.read_text(encoding="utf-8")
    end_marker = f"# <<< {'custom jobs' if section == 'jobs' else f'custom steps ({section})'}"
    head, sep, tail = content.partition(end_marker)
    assert sep, f"no end marker for {section}"
    indent = head[head.rfind("\n") + 1 :]  # the end marker's own indentation
    path.write_text(head[: len(head) - len(indent)] + text + "\n" + indent + sep + tail, encoding="utf-8", newline="\n")


# --- The motivating case: llm-output-eval's extra report step ---


def test_custom_step_is_not_drift(project):
    add_to_section(project, "test", REPORT_STEP)
    assert check_workflow(project).status is Status.UP_TO_DATE


def test_custom_step_runs_after_the_tests_in_valid_yaml(project):
    add_to_section(project, "test", REPORT_STEP)
    data = yaml.safe_load(workflow(project).read_text(encoding="utf-8"))
    steps = data["jobs"]["test"]["steps"]
    assert steps[-2]["name"] == "Run tests"
    assert steps[-1] == {"name": "Build the scorecard", "run": "python -m evals.report"}


def test_custom_job_is_a_real_job(project):
    add_to_section(project, "jobs", DEPLOY_JOB)
    data = yaml.safe_load(workflow(project).read_text(encoding="utf-8"))
    assert set(data["jobs"]) == {"test", "docs"}
    assert check_workflow(project).status is Status.UP_TO_DATE


def test_regenerating_keeps_custom_sections(project):
    add_to_section(project, "test", REPORT_STEP)
    add_to_section(project, "jobs", DEPLOY_JOB)
    (project / "ruff.toml").write_text("line-length = 100\n", encoding="utf-8")  # the project changes

    assert check_workflow(project).status is Status.OUT_OF_DATE  # new lint job expected
    assert main(["init", str(project), "--force"]) == EXIT_OK

    content = workflow(project).read_text(encoding="utf-8")
    assert "python -m evals.report" in content
    assert '  docs:\n' in content
    assert "  lint:\n" in content
    assert check_workflow(project).status is Status.UP_TO_DATE


def test_edit_outside_a_custom_section_is_still_drift(project):
    add_to_section(project, "test", REPORT_STEP)
    path = workflow(project)
    path.write_text(
        path.read_text(encoding="utf-8").replace("timeout-minutes: 15", "timeout-minutes: 60"),
        encoding="utf-8",
        newline="\n",
    )
    result = check_workflow(project)
    assert result.status is Status.OUT_OF_DATE
    assert "evals.report" not in result.diff, "the custom step itself must not show up as a difference"


# --- Mistakes in the markers ---


def test_unclosed_section_is_an_error_and_init_leaves_the_file_alone(project, capsys):
    path = workflow(project)
    broken = path.read_text(encoding="utf-8").replace("      # <<< custom steps (test)\n", "")
    path.write_text(broken, encoding="utf-8", newline="\n")

    assert main(["check", str(project)]) == EXIT_CANNOT_CHECK
    assert main(["init", str(project), "--force"]) == 1
    assert "end marker for 'test' missing" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == broken


def test_steps_for_a_job_that_disappears_are_not_dropped_silently(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1"\n', encoding="utf-8")
    (tmp_path / "ruff.toml").write_text("", encoding="utf-8")  # lint only, no tests
    assert main(["init", str(tmp_path)]) == EXIT_OK
    add_to_section(tmp_path, "lint", "      - run: echo extra lint")
    before = workflow(tmp_path).read_text(encoding="utf-8")

    (tmp_path / "ruff.toml").unlink()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    assert main(["init", str(tmp_path), "--force"]) == 1
    assert "no job for the custom steps in: lint" in capsys.readouterr().err
    assert workflow(tmp_path).read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# >>> custom steps (test): x\n# >>> custom jobs: x\n", "starts before 'test' has ended"),
        ("# <<< custom steps (test)\n", "doesn't match an open section"),
        ("# >>> custom jobs: x\n", "never closed"),
        ("# >>> custom jobs: x\n# <<< custom steps (test)\n", "doesn't match"),
        ("# >>> custom jobs: x\n# <<< custom jobs\n# >>> custom jobs: x\n# <<< custom jobs\n", "two custom sections"),
    ],
)
def test_malformed_markers(text, message):
    with pytest.raises(RenderError, match=message):
        extract_custom_blocks(text)


def test_extract_keeps_lines_exactly():
    text = "  # >>> custom jobs: keep\n  extra:\n    x: 1   \n  # <<< custom jobs\n"
    assert extract_custom_blocks(text) == {"jobs": ["  extra:", "    x: 1   "]}


def test_crlf_file_with_custom_steps_reports_line_endings(project):
    add_to_section(project, "test", REPORT_STEP)
    path = workflow(project)
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert check_workflow(project).status is Status.LINE_ENDINGS
    assert main(["check", str(project)]) == EXIT_PROBLEM_FOUND
