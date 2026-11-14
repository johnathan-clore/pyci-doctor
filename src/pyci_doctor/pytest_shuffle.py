"""pytest plugin: run tests in a shuffled order that a seed makes reproducible.

Many flaky tests only fail when another test runs first (shared state, leftover files,
module-level caches). Running the suite in one fixed order never finds them.

Enable it with the environment variable PYCI_DOCTOR_SEED and pytest's -p option:

    PYCI_DOCTOR_SEED=1234 python -m pytest -p pyci_doctor.pytest_shuffle

Without PYCI_DOCTOR_SEED it does nothing. It uses only the standard library, so
`flaky --runs` can copy this file into any environment (venv, uv, Poetry) and load it there.
"""

from __future__ import annotations

import os
import random

SEED_VARIABLE = "PYCI_DOCTOR_SEED"


def shuffled_order(node_ids: list[str], seed: int) -> list[str]:
    """Shuffle test files, then the tests inside each file.

    Tests stay grouped by file, so module-level fixtures are still set up once per file.
    Pure function: the same ids and seed always give the same order.
    """
    rng = random.Random(seed)
    groups: dict[str, list[str]] = {}
    for node_id in node_ids:
        groups.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    files = list(groups.values())
    rng.shuffle(files)
    for tests in files:
        rng.shuffle(tests)
    return [node_id for tests in files for node_id in tests]


def pytest_collection_modifyitems(session, config, items):
    seed = os.environ.get(SEED_VARIABLE)
    if seed is None:
        return
    by_id = {item.nodeid: item for item in items}
    items[:] = [by_id[node_id] for node_id in shuffled_order([item.nodeid for item in items], int(seed))]


def pytest_report_header(config):
    seed = os.environ.get(SEED_VARIABLE)
    if seed is not None:
        return f"test order shuffled by pyci-doctor, seed {seed}"
