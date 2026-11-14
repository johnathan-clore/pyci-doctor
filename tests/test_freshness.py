"""Is anything pyci-doctor generates out of date? Needs the internet, so it's skipped by default.

Run with: python -m pytest -m network
.github/workflows/maintenance.yml runs it every week, so staleness shows up as a red run
(and a GitHub email) instead of being discovered by users.
"""

import re
import subprocess
import sys

import pytest

from pyci_doctor.detect import KNOWN_PYTHONS
from pyci_doctor.render import (
    CHECKOUT_ACTION,
    RUFF_VERSION,
    SETUP_PYTHON_ACTION,
    UPLOAD_ARTIFACT_ACTION,
)

pytestmark = pytest.mark.network


def remote_tags(repo: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", f"https://github.com/{repo}.git"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return [line.split("refs/tags/")[1] for line in result.stdout.splitlines() if "refs/tags/" in line]


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


@pytest.mark.parametrize("action", [CHECKOUT_ACTION, SETUP_PYTHON_ACTION, UPLOAD_ARTIFACT_ACTION])
def test_generated_actions_use_the_latest_major_version(action):
    repo, pinned = action.split("@")
    majors = [t for t in remote_tags(repo) if re.fullmatch(r"v\d+", t)]
    latest = max(majors, key=lambda t: int(t[1:]))
    assert pinned == latest, f"{repo} is at {latest}; update the constant in src/pyci_doctor/render.py"


def test_newest_known_python_is_the_newest_release():
    # actions/python-versions holds the builds setup-python installs; stable ones look like 3.15.2-123.
    stable = {
        m.group(1)
        for t in remote_tags("actions/python-versions")
        if (m := re.fullmatch(r"(\d+\.\d+)\.\d+-\d+", t))
    }
    newest = max(stable, key=version_key)
    assert KNOWN_PYTHONS[-1] == newest, f"Python {newest} is out; add it to KNOWN_PYTHONS in detect.py"


def test_pinned_ruff_is_the_latest_release():
    result = subprocess.run(
        [sys.executable, "-m", "pip", "index", "versions", "ruff"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    latest = re.search(r"ruff \(([^)]+)\)", result.stdout).group(1)
    assert latest == RUFF_VERSION, f"ruff {latest} is out; update RUFF_VERSION in render.py"
