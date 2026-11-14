import json
from pathlib import Path

import pytest

from pyci_doctor.cli import main
from pyci_doctor.detect import (
    DEFAULT_PYTHONS,
    DetectionError,
    detect,
    supported_pythons,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --- Realistic fake projects (tests/fixtures/) ---


def test_src_layout_pytest_project():
    profile = detect(FIXTURES / "src_pytest")
    assert profile.name == "demo"
    assert profile.layout == "src"
    assert profile.dependency_source == "pyproject"
    assert profile.install_command == 'python -m pip install -e ".[dev]"'
    assert profile.test_runner == "pytest"
    assert profile.python_versions == ["3.11", "3.12", "3.13", "3.14"]
    assert profile.has_ruff is True
    assert profile.has_coverage is True
    assert profile.warnings == []


def test_flat_requirements_project():
    profile = detect(FIXTURES / "flat_requirements")
    assert profile.name == "flat_requirements"  # no pyproject, so the folder name is used
    assert profile.layout == "flat"
    assert profile.dependency_source == "requirements"
    assert profile.install_command == (
        "python -m pip install -r requirements.txt -r requirements-dev.txt"
    )
    assert profile.test_runner == "pytest"
    assert profile.python_versions == list(DEFAULT_PYTHONS)
    assert any("requires-python" in w for w in profile.warnings)


def test_unittest_project_with_upper_python_bound():
    profile = detect(FIXTURES / "unittest_only")
    assert profile.test_runner == "unittest"
    assert profile.install_command == "python -m pip install -e ."
    assert profile.python_versions == ["3.10", "3.11", "3.12"]
    assert profile.has_ruff is False
    assert profile.has_coverage is False


def test_project_with_nothing_to_install_or_test():
    profile = detect(FIXTURES / "no_tests")
    assert profile.dependency_source == "none"
    assert profile.install_command == ""
    assert profile.test_runner == "none"
    assert "No tests found." in profile.warnings


def test_invalid_pyproject_raises_clear_error():
    with pytest.raises(DetectionError, match="Invalid pyproject.toml"):
        detect(FIXTURES / "broken_pyproject")


def test_missing_folder_raises_clear_error(tmp_path):
    with pytest.raises(DetectionError, match="Not a folder"):
        detect(tmp_path / "does-not-exist")


# --- Small edge cases built on the fly in a temporary folder ---


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_test_extra_is_used_when_there_is_no_dev_extra(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "x"\nversion = "1"\n'
        '[project.optional-dependencies]\ntest = ["pytest"]\n',
    )
    assert detect(tmp_path).install_command == 'python -m pip install -e ".[test]"'


def test_pytest_config_without_pytest_dependency_warns(tmp_path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "x"\nversion = "1"\n[tool.pytest.ini_options]\n')
    profile = detect(tmp_path)
    assert profile.test_runner == "pytest"
    assert any("not listed as a dependency" in w for w in profile.warnings)


def test_ruff_toml_file_enables_ruff(tmp_path):
    write(tmp_path / "ruff.toml", "line-length = 88\n")
    assert detect(tmp_path).has_ruff is True


def test_requirement_names_are_normalised(tmp_path):
    # Underscore vs hyphen and upper case shouldn't matter; markers and comments are ignored.
    write(tmp_path / "requirements.txt", 'Pytest_Cov>=5 ; python_version > "3.10"  # coverage\n')
    assert detect(tmp_path).has_coverage is True


def test_test_files_named_with_test_suffix_are_found(tmp_path):
    write(tmp_path / "pkg" / "math_test.py", "def test_x():\n    pass\n")
    assert detect(tmp_path).test_runner == "unittest"


# --- requires-python parsing ---


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (">=3.11", ["3.11", "3.12", "3.13", "3.14"]),
        (">=3.10,<3.13", ["3.10", "3.11", "3.12"]),
        (">3.12", ["3.13", "3.14"]),
        ("<=3.10", ["3.9", "3.10"]),
        ("==3.12.*", ["3.12"]),
        ("!=3.12.*, >=3.11", ["3.11", "3.13", "3.14"]),
        ("~=3.13", ["3.13", "3.14"]),
        (">=3.10.2", ["3.10", "3.11", "3.12", "3.13", "3.14"]),
        (" >= 3.13 ", ["3.13", "3.14"]),
        (">=4.0", []),
    ],
)
def test_supported_pythons(spec, expected):
    assert supported_pythons(spec) == expected


def test_unsupported_python_range_warns(tmp_path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "x"\nversion = "1"\nrequires-python = ">=4.0"\n')
    profile = detect(tmp_path)
    assert profile.python_versions == []
    assert any("No known Python version" in w for w in profile.warnings)


def test_unreadable_requires_python_raises_clear_error():
    with pytest.raises(DetectionError, match="Can't understand"):
        supported_pythons("python3 please")


# --- The `detect` command ---


def test_detect_command_prints_json(capsys):
    assert main(["detect", str(FIXTURES / "src_pytest"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "demo"
    assert data["python_versions"] == ["3.11", "3.12", "3.13", "3.14"]


def test_detect_command_prints_readable_text(capsys):
    assert main(["detect", str(FIXTURES / "no_tests")]) == 0
    out = capsys.readouterr().out
    assert "test_runner" in out
    assert "warning: No tests found." in out


def test_detect_command_reports_errors_on_stderr_with_exit_code_1(capsys):
    assert main(["detect", str(FIXTURES / "broken_pyproject")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: Invalid pyproject.toml")
