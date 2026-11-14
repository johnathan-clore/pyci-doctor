"""Shared pytest setup for this test suite."""


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        help="Rewrite tests/golden/*.yml from the current generator output instead of comparing.",
    )
