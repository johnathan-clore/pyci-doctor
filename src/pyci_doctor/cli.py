"""Command-line entry point for pyci-doctor."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pyci_doctor import __version__
from pyci_doctor.detect import DetectionError, detect
from pyci_doctor.render import RenderError, render_workflow

WORKFLOW_PATH = Path(".github") / "workflows" / "ci.yml"


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

    init_cmd = subcommands.add_parser("init", help="Write .github/workflows/ci.yml for a project.")
    init_cmd.add_argument("path", nargs="?", default=".", help="Project folder (default: current folder).")
    init_cmd.add_argument(
        "--all-pythons",
        action="store_true",
        help="Test every supported Python version, not just the oldest and newest.",
    )
    init_cmd.add_argument("--force", action="store_true", help="Overwrite an existing ci.yml.")
    init_cmd.add_argument(
        "--stdout", action="store_true", help="Print the workflow instead of writing the file."
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "detect":
        return _run_detect(args.path, as_json=args.json)
    if args.command == "init":
        return _run_init(args.path, all_pythons=args.all_pythons, force=args.force, to_stdout=args.stdout)

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


def _run_init(path: str, all_pythons: bool, force: bool, to_stdout: bool) -> int:
    try:
        profile = detect(path)
        workflow = render_workflow(profile, all_pythons=all_pythons)
    except (DetectionError, RenderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for warning in profile.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if to_stdout:
        sys.stdout.write(workflow)
        return 0

    target = Path(path) / WORKFLOW_PATH
    if target.exists() and not force:
        print(f"error: {target} already exists; use --force to overwrite it.", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" stops Windows from writing CRLF line endings, so the file is identical on every OS.
    target.write_text(workflow, encoding="utf-8", newline="\n")
    print(f"Wrote {target}")
    return 0
