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
    assert profile.test_command == "python -m pytest -v"
    assert profile.python_versions == ["3.11", "3.12", "3.13", "3.14", "3.15"]
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
    assert profile.test_command == "python -m unittest discover -s tests -v"
    assert profile.install_command == "python -m pip install -e ."
    assert profile.python_versions == ["3.10", "3.11", "3.12"]
    assert profile.has_ruff is False
    assert profile.has_coverage is False


def test_project_with_nothing_to_install_or_test():
    profile = detect(FIXTURES / "no_tests")
    assert profile.dependency_source == "none"
    assert profile.install_command == ""
    assert profile.test_runner == "none"
    assert profile.test_command == ""
    assert "No tests found." in profile.warnings


def test_pyproject_with_only_tool_settings_is_not_installed():
    # Regression test: modelled on llm-output-eval, whose pyproject.toml holds only pytest settings.
    # Running `pip install -e .` there fails, so the detector must not suggest it.
    profile = detect(FIXTURES / "config_only_pyproject")
    assert profile.dependency_source == "none"
    assert profile.install_command == "python -m pip install pytest"
    assert profile.test_runner == "pytest"
    assert any("can't be pip-installed" in w for w in profile.warnings)


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


def test_pytest_config_without_pytest_dependency_adds_pytest_to_install(tmp_path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "x"\nversion = "1"\n[tool.pytest.ini_options]\n')
    profile = detect(tmp_path)
    assert profile.test_runner == "pytest"
    assert profile.install_command == "python -m pip install -e . pytest"
    assert any("not listed as a dependency" in w for w in profile.warnings)


def test_build_system_without_project_table_is_installable(tmp_path):
    # Poetry projects describe the package under [tool.poetry] and [build-system], not [project].
    write(
        tmp_path / "pyproject.toml",
        '[build-system]\nrequires = ["poetry-core"]\nbuild-backend = "poetry.core.masonry.api"\n',
    )
    profile = detect(tmp_path)
    assert profile.dependency_source == "pyproject"
    assert profile.install_command == "python -m pip install -e ."


def test_config_only_pyproject_falls_back_to_requirements_txt(tmp_path):
    write(tmp_path / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    write(tmp_path / "requirements.txt", "pytest\n")
    profile = detect(tmp_path)
    assert profile.dependency_source == "requirements"
    assert profile.install_command == "python -m pip install -r requirements.txt"
    assert profile.has_ruff is True


def test_ruff_toml_file_enables_ruff(tmp_path):
    write(tmp_path / "ruff.toml", "line-length = 88\n")
    assert detect(tmp_path).has_ruff is True


def test_requirement_names_are_normalised(tmp_path):
    # Underscore vs hyphen and upper case shouldn't matter; markers and comments are ignored.
    write(tmp_path / "requirements.txt", 'Pytest_Cov>=5 ; python_version > "3.10"  # coverage\n')
    assert detect(tmp_path).has_coverage is True


def test_test_files_named_with_test_suffix_are_found(tmp_path):
    write(tmp_path / "pkg" / "math_test.py", "def test_x():\n    pass\n")
    profile = detect(tmp_path)
    assert profile.test_runner == "unittest"
    assert profile.test_command == "python -m unittest discover -v"  # no tests/ folder


# --- requires-python parsing ---


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (">=3.11", ["3.11", "3.12", "3.13", "3.14", "3.15"]),
        (">=3.10,<3.13", ["3.10", "3.11", "3.12"]),
        (">3.12", ["3.13", "3.14", "3.15"]),
        ("<=3.10", ["3.9", "3.10"]),
        ("==3.12.*", ["3.12"]),
        ("!=3.12.*, >=3.11", ["3.11", "3.13", "3.14", "3.15"]),
        ("~=3.13", ["3.13", "3.14", "3.15"]),
        (">=3.10.2", ["3.10", "3.11", "3.12", "3.13", "3.14", "3.15"]),
        (" >= 3.13 ", ["3.13", "3.14", "3.15"]),
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
    assert data["python_versions"] == ["3.11", "3.12", "3.13", "3.14", "3.15"]


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


def test_known_pythons_include_the_running_python():
    # Staleness alarm: running the tests on a Python newer than KNOWN_PYTHONS fails here,
    # a reminder to add it (a new Python is released every October).
    import sys

    from pyci_doctor.detect import KNOWN_PYTHONS

    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert running in KNOWN_PYTHONS, f"Add {running} to KNOWN_PYTHONS in detect.py"



# --- uv and Poetry projects ---


def test_uv_project():
    profile = detect(FIXTURES / "uv_project")
    assert profile.package_manager == "uv"
    assert profile.dependency_source == "uv.lock"
    assert profile.install_command == "python -m pip install uv && uv sync --all-extras"
    assert profile.test_command == "uv run pytest -v"
    assert profile.python_versions == ["3.12", "3.13", "3.14", "3.15"]
    assert profile.has_ruff is True  # found in the "lint" dependency group
    assert profile.warnings == []


def test_poetry_project():
    profile = detect(FIXTURES / "poetry_project")
    assert profile.name == "poetry_project"  # Poetry keeps the name in [tool.poetry], not [project]
    assert profile.package_manager == "poetry"
    assert profile.dependency_source == "poetry.lock"
    assert profile.install_command == "python -m pip install poetry && poetry install --all-extras"
    assert profile.test_command == "poetry run pytest -v"
    assert profile.python_versions == ["3.12", "3.13", "3.14", "3.15"]  # from python = "^3.12"
    assert profile.has_coverage is True  # pytest-cov in the dev group
    assert profile.warnings == []


def test_poetry_without_lock_file_is_still_poetry(tmp_path):
    write(
        tmp_path / "pyproject.toml",
        '[tool.poetry]\nname = "x"\n[tool.poetry.dependencies]\npython = "~3.13"\n'
        '[tool.poetry.group.dev.dependencies]\npytest = "*"\n',
    )
    profile = detect(tmp_path)
    assert profile.package_manager == "poetry"
    assert profile.dependency_source == "pyproject"
    assert profile.python_versions == ["3.13"]  # ~3.13 means >=3.13,<3.14


def test_pip_projects_are_unchanged():
    assert detect(FIXTURES / "src_pytest").package_manager == "pip"


def test_uv_project_without_pytest_dependency_warns_instead_of_pip_installing(tmp_path):
    write(tmp_path / "pyproject.toml", '[project]\nname = "x"\nversion = "1"\n[tool.pytest.ini_options]\n')
    write(tmp_path / "uv.lock", "version = 1\n")
    profile = detect(tmp_path)
    assert "pytest" not in profile.install_command.split("&&")[-1]
    assert any("add it to your uv dev group" in w for w in profile.warnings)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("^3.11", ">=3.11,<4.0"),
        ("~3.12", ">=3.12,<3.13"),
        ("~=3.12", "~=3.12"),
        (">=3.11,<3.14", ">=3.11,<3.14"),
        ("^3.11.4", ">=3.11,<4.0"),
    ],
)
def test_poetry_python_constraints_are_translated(spec, expected):
    from pyci_doctor.detect import _poetry_python_spec

    assert _poetry_python_spec({"poetry": {"dependencies": {"python": spec}}}) == expected
