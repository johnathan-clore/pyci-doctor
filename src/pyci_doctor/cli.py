"""Command-line entry point for pyci-doctor."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from pyci_doctor import __version__
from pyci_doctor.detect import DetectionError, detect


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyci-doctor",
        description="Generate GitHub Actions CI for a Python project and detect flaky tests.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subcommands = parser.add_subparsers(dest="command")

    detect_cmd = subcommands.add_parser("detect", help="Show what pyci-doctor finds in a project.")
    detect_cmd.add_argument("path", nargs="?", default=".", help="Project folder (default: current folder).")
    detect_cmd.add_argument("--json", action="store_true", help="Print the result as JSON.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "detect":
        return _run_detect(args.path, as_json=args.json)

    parser.print_help()
    return 0


def _run_detect(path: str, as_json: bool) -> int:
    try:
        profile = detect(path)
    except DetectionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(profile.to_dict(), indent=2))
        return 0

    for key, value in profile.to_dict().items():
        if key == "warnings":
            continue
        if isinstance(value, list):
            value = ", ".join(value)
        print(f"{key:18} {value}")
    for warning in profile.warnings:
        print(f"warning: {warning}")
    return 0
