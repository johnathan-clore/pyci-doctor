"""Command-line entry point for pyci-doctor."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from pyci_doctor import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyci-doctor",
        description="Generate GitHub Actions CI for a Python project and detect flaky tests.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
