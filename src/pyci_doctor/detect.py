"""Inspect a Python project folder and describe what CI it needs.

Only reads files. Never imports or runs the project's code.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Python versions the generated CI can test, oldest to newest.
KNOWN_PYTHONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")

# Used when a project doesn't say which Python versions it supports.
DEFAULT_PYTHONS = ("3.12", "3.13", "3.14")


class DetectionError(Exception):
    """The project can't be inspected, e.g. the folder is missing or pyproject.toml is invalid."""


@dataclass
class ProjectProfile:
    name: str
    layout: str  # "src" or "flat"
    dependency_source: str  # "pyproject", "requirements" or "none"
    install_command: str
    test_runner: str  # "pytest", "unittest" or "none"
    test_command: str
    python_versions: list[str]
    has_ruff: bool
    has_coverage: bool
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def detect(project_dir: str | Path) -> ProjectProfile:
    root = Path(project_dir)
    if not root.is_dir():
        raise DetectionError(f"Not a folder: {root}")

    pyproject = _load_pyproject(root)
    project_table = pyproject.get("project", {})
    tool_table = pyproject.get("tool", {})
    warnings: list[str] = []

    name = project_table.get("name") or root.resolve().name

    dev_extra = _find_dev_extra(project_table)
    all_deps = _all_dependency_names(root, project_table)

    # pip can only install a project whose pyproject.toml describes a package:
    # a [project] table (standard) or a [build-system] table (e.g. Poetry, Hatch).
    # A pyproject.toml holding only tool settings, like [tool.pytest.ini_options], is not installable.
    installable = "project" in pyproject or "build-system" in pyproject

    install_args: list[str] = []
    if installable:
        dependency_source = "pyproject"
        install_args.append(f'-e ".[{dev_extra}]"' if dev_extra else "-e .")
    elif (root / "requirements.txt").is_file():
        dependency_source = "requirements"
        install_args.append("-r requirements.txt")
        if (root / "requirements-dev.txt").is_file():
            install_args.append("-r requirements-dev.txt")
    else:
        dependency_source = "none"
        if pyproject:
            warnings.append(
                "pyproject.toml has no [project] or [build-system] table, "
                "so the project can't be pip-installed."
            )
        else:
            warnings.append("No pyproject.toml or requirements.txt found.")

    test_runner = _detect_test_runner(root, tool_table, all_deps)
    if test_runner == "none":
        warnings.append("No tests found.")
    elif test_runner == "pytest" and "pytest" not in all_deps:
        install_args.append("pytest")
        warnings.append("pytest is used but not listed as a dependency; CI will install it.")

    install_command = "python -m pip install " + " ".join(install_args) if install_args else ""

    requires_python = project_table.get("requires-python")
    if requires_python:
        python_versions = supported_pythons(requires_python)
        if not python_versions:
            warnings.append(f"No known Python version matches requires-python '{requires_python}'.")
    else:
        python_versions = list(DEFAULT_PYTHONS)
        warnings.append("No requires-python found; defaulting to " + ", ".join(DEFAULT_PYTHONS) + ".")

    has_ruff = (
        "ruff" in tool_table
        or (root / "ruff.toml").is_file()
        or (root / ".ruff.toml").is_file()
        or "ruff" in all_deps
    )
    has_coverage = "coverage" in tool_table or "pytest-cov" in all_deps or "coverage" in all_deps

    return ProjectProfile(
        name=name,
        layout="src" if (root / "src").is_dir() else "flat",
        dependency_source=dependency_source,
        install_command=install_command,
        test_runner=test_runner,
        test_command=_test_command(root, test_runner),
        python_versions=python_versions,
        has_ruff=has_ruff,
        has_coverage=has_coverage,
        warnings=warnings,
    )


def _load_pyproject(root: Path) -> dict:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise DetectionError(f"Invalid pyproject.toml: {exc}") from exc


def _find_dev_extra(project_table: dict) -> str | None:
    extras = project_table.get("optional-dependencies", {})
    for candidate in ("dev", "test", "tests", "testing"):
        if candidate in extras:
            return candidate
    return None


_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirement_name(line: str) -> str | None:
    """'pytest-cov>=5 ; python_version>"3.10"' -> 'pytest-cov'. Comments and pip options -> None."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("-"):
        return None
    match = _NAME_RE.match(line)
    return match.group(1).lower().replace("_", "-") if match else None


def _all_dependency_names(root: Path, project_table: dict) -> set[str]:
    lines: list[str] = list(project_table.get("dependencies", []))
    for group in project_table.get("optional-dependencies", {}).values():
        lines.extend(group)
    for filename in ("requirements.txt", "requirements-dev.txt"):
        path = root / filename
        if path.is_file():
            lines.extend(path.read_text(encoding="utf-8").splitlines())
    return {name for name in map(_requirement_name, lines) if name}


def _has_test_files(root: Path) -> bool:
    tests_dir = root / "tests"
    search_root = tests_dir if tests_dir.is_dir() else root
    return any(search_root.rglob("test_*.py")) or any(search_root.rglob("*_test.py"))


def _detect_test_runner(root: Path, tool_table: dict, deps: set[str]) -> str:
    pytest_configured = (
        "pytest" in tool_table
        or (root / "pytest.ini").is_file()
        or (root / "conftest.py").is_file()
        or "pytest" in deps
    )
    if pytest_configured:
        return "pytest"
    if _has_test_files(root):
        return "unittest"
    return "none"


def _test_command(root: Path, test_runner: str) -> str:
    if test_runner == "pytest":
        return "python -m pytest -v"
    if test_runner == "unittest":
        # Bare `unittest discover` finds nothing in a tests/ folder without an __init__.py on
        # newer Pythons (3.13 runs 0 tests; 3.11 finds them). Pointing it at tests/ works on all.
        if (root / "tests").is_dir():
            return "python -m unittest discover -s tests -v"
        return "python -m unittest discover -v"
    return ""


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _matches(version: str, clause: str) -> bool:
    """Check one clause of a requires-python string, e.g. '>=3.10', for a version like '3.12'."""
    match = re.fullmatch(r"\s*(~=|==|!=|>=|<=|>|<)\s*([0-9]+(?:\.[0-9]+)*)(\.\*)?\s*", clause)
    if not match:
        raise DetectionError(f"Can't understand requires-python clause: '{clause.strip()}'")
    op, target, wildcard = match.groups()
    v = _version_tuple(version)
    t = _version_tuple(target)

    if wildcard or op == "~=":
        # '==3.11.*' means any 3.11.x; '~=3.10' means >=3.10 within 3.x.
        if op == "~=":
            return v >= t[:2] and v[:1] == t[:1]
        prefix_match = v[: len(t)] == t
        return prefix_match if op == "==" else not prefix_match

    # We compare minor versions only (3.12, not 3.12.4), so '>=3.10.2' counts 3.10 as supported.
    t = t[:2]
    return {
        "==": v == t,
        "!=": v != t,
        ">=": v >= t,
        "<=": v <= t,
        ">": v > t,
        "<": v < t,
    }[op]


def supported_pythons(requires_python: str) -> list[str]:
    clauses = [c for c in requires_python.split(",") if c.strip()]
    return [v for v in KNOWN_PYTHONS if all(_matches(v, c) for c in clauses)]
