import subprocess
import sys

import pytest

from pyci_doctor import __version__
from pyci_doctor.cli import main


def test_version_flag_prints_version_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"pyci-doctor {__version__}"


def test_no_arguments_prints_help_and_returns_zero(capsys):
    assert main([]) == 0
    assert "usage: pyci-doctor" in capsys.readouterr().out


def test_unknown_option_exits_with_error_code_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--no-such-option"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_runs_as_module_in_a_real_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "pyci_doctor", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"pyci-doctor {__version__}"


# --- The release workflow (.github/workflows/release.yml) ---


def load_release_workflow():
    from pathlib import Path

    import yaml

    path = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_release_workflow_runs_on_version_tags_and_checks_the_version():
    data = load_release_workflow()
    assert data[True] == {"push": {"tags": ["v*"]}}  # PyYAML reads the `on` key as True
    runs = [step.get("run", "") for step in data["jobs"]["release"]["steps"]]
    version_check = next(r for r in runs if "__version__" in r)
    assert '"v$version" != "$GITHUB_REF_NAME"' in version_check
    # Tests must run before anything is published.
    assert runs.index("python -m pytest -q") < next(i for i, r in enumerate(runs) if "gh release create" in r)


def test_release_permissions_are_per_job_and_minimal():
    data = load_release_workflow()
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["release"]["permissions"] == {"contents": "write"}
    assert data["jobs"]["pypi"]["permissions"] == {"id-token": "write"}


def test_pypi_publishing_is_opt_in_and_publishes_the_tested_build():
    pypi = load_release_workflow()["jobs"]["pypi"]
    assert pypi["if"] == "vars.PUBLISH_TO_PYPI == 'true'"
    assert pypi["needs"] == "release"  # only after tests passed and the GitHub release exists
    assert pypi["environment"] == "pypi"
    assert pypi["steps"][-1]["uses"].startswith("pypa/gh-action-pypi-publish@")
