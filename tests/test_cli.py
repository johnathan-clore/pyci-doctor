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


def test_release_workflow_runs_on_version_tags_and_checks_the_version():
    from pathlib import Path

    import yaml

    path = Path(__file__).parent.parent / ".github" / "workflows" / "release.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data[True]  # PyYAML reads the `on` key as True
    assert triggers == {"push": {"tags": ["v*"]}}
    assert data["permissions"] == {"contents": "write"}

    runs = [step.get("run", "") for step in data["jobs"]["release"]["steps"]]
    version_check = next(r for r in runs if "__version__" in r)
    assert '"v$version" != "$GITHUB_REF_NAME"' in version_check
    # Tests must run before anything is published.
    assert runs.index("python -m pytest -q") < next(i for i, r in enumerate(runs) if "gh release create" in r)
