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
