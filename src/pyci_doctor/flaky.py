"""Find flaky tests by comparing the results of several runs of the same test suite.

Input is JUnit XML, the format pytest writes with --junitxml and most CI tools understand.
"""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pyci_doctor.pytest_shuffle import SEED_VARIABLE


class FlakyError(Exception):
    """Results can't be read, e.g. a missing file or invalid XML."""


# --- Reading results ---


@dataclass(frozen=True)
class Outcome:
    test_id: str
    result: str  # "passed", "failed", "error" or "skipped"
    message: str = ""  # short, one-line summary (JUnit's message attribute)
    details: str = ""  # full text, usually the traceback


def parse_junit(path: str | Path) -> list[Outcome]:
    path = Path(path)
    try:
        root = ET.parse(path).getroot()
    except FileNotFoundError as exc:
        raise FlakyError(f"File not found: {path}") from exc
    except ET.ParseError as exc:
        raise FlakyError(f"Invalid JUnit XML in {path}: {exc}") from exc

    if root.tag not in ("testsuites", "testsuite"):
        raise FlakyError(f"{path} is not JUnit XML (root element is <{root.tag}>)")

    outcomes = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name

        result, message, details = "passed", "", ""
        for tag in ("failure", "error", "skipped"):
            element = case.find(tag)
            if element is not None:
                result = "failed" if tag == "failure" else tag
                details = (element.text or "").strip()
                message = element.get("message", "").strip() or details.split("\n", 1)[0]
                break
        outcomes.append(Outcome(test_id, result, message, details))
    return outcomes


def collect_result_files(paths: Sequence[str | Path]) -> list[Path]:
    """Accept files and folders. A folder means every *.xml file in it.

    Folders matter on Windows: PowerShell doesn't expand `results/*.xml` for programs
    the way bash does, so `pyci-doctor flaky results` is the portable way to pass many files.
    """
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.xml")))
        else:
            files.append(p)
    if not files:
        raise FlakyError("No result files found.")
    return files


# --- Telling infrastructure failures from test failures ---

# Messages that point at the environment (network, disk, memory, rate limits) rather than
# at the code under test. Matching is case-insensitive. Patterns are deliberately specific:
# a bare "timeout" or "429" would also match ordinary assertions like `assert cfg.timeout == 30`.
# This is a heuristic that points you where to look first, not a verdict.
INFRASTRUCTURE_PATTERNS = [
    r"\bTimeoutError\b",
    r"\btimed out\b",
    r"\bConnection(Reset|Refused|Aborted)?Error\b",
    r"\bconnection (reset|refused|aborted)\b",
    r"\bECONNRESET\b|\bECONNREFUSED\b|\bETIMEDOUT\b",
    r"Temporary failure in name resolution|Name or service not known|getaddrinfo failed",
    r"\bNo space left on device\b",
    r"\bMemoryError\b",
    r"\b(HTTP )?(502|503|504)\b.*(Bad Gateway|Service Unavailable|Gateway Time-?out)",
    r"Too Many Requests|\brate[- ]limit(ed)?\b",
]
_INFRA_RE = re.compile("|".join(f"(?:{p})" for p in INFRASTRUCTURE_PATTERNS), re.IGNORECASE)


def failure_kind(message: str) -> str:
    """'infrastructure' if the message matches a known environment problem, else 'test'."""
    return "infrastructure" if _INFRA_RE.search(message) else "test"


# --- Classifying tests across runs ---


@dataclass
class History:
    """One test's results across all runs. (Not named TestHistory: pytest would try to collect it.)"""

    test_id: str
    results: list[str] = field(default_factory=list)  # one entry per run; "missing" if absent
    failure_kinds: list[str] = field(default_factory=list)
    first_failure: str = ""

    @property
    def passed(self) -> int:
        return self.results.count("passed")

    @property
    def failed(self) -> int:
        return self.results.count("failed") + self.results.count("error")

    @property
    def category(self) -> str:
        if self.passed and self.failed:
            return "flaky"
        if self.failed:
            return "stable-fail"
        if self.passed:
            return "stable-pass"
        return "skipped"

    @property
    def suspected_cause(self) -> str:
        """For tests that failed at least once: what the failures have in common."""
        kinds = set(self.failure_kinds)
        if not kinds:
            return ""
        return kinds.pop() if len(kinds) == 1 else "mixed"


def classify(runs: Sequence[Sequence[Outcome]]) -> list[History]:
    histories: dict[str, History] = {}
    for run_index, run in enumerate(runs):
        seen = set()
        for outcome in run:
            history = histories.setdefault(
                outcome.test_id, History(outcome.test_id, ["missing"] * run_index)
            )
            history.results.append(outcome.result)
            seen.add(outcome.test_id)
            if outcome.result in ("failed", "error"):
                # Classify on the full traceback too: the real cause is often deep inside it.
                history.failure_kinds.append(failure_kind(f"{outcome.message}\n{outcome.details}"))
                if not history.first_failure:
                    history.first_failure = outcome.message
        # A test that disappears from a run (e.g. the run crashed) is recorded as missing.
        for test_id, history in histories.items():
            if test_id not in seen:
                history.results.append("missing")

    order = {"flaky": 0, "stable-fail": 1, "skipped": 2, "stable-pass": 3}
    return sorted(histories.values(), key=lambda h: (order[h.category], h.test_id))


# --- Running the suite several times ---

# runner(command, env) -> exit code. Injectable so tests can fake the subprocess.
Runner = Callable[[list[str], dict[str, str]], int]

SEED_RE = re.compile(r"-seed-(\d+)\.xml$")


def _default_runner(command: list[str], env: dict[str, str]) -> int:
    # Capture pytest's own output so it can't mix into ours (e.g. break --json on stdout).
    return subprocess.run(command, capture_output=True, text=True, env=env).returncode


def _install_shuffle_plugin(out: Path) -> Path:
    """Copy the shuffle plugin into its own folder, as a standalone module.

    Only that folder goes on PYTHONPATH. Putting pyci-doctor's whole site-packages there
    instead could shadow the project's own packages inside a uv or Poetry virtualenv.
    """
    plugin_dir = out / "_plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("pytest_shuffle.py")
    (plugin_dir / "pyci_doctor_shuffle.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return plugin_dir


def run_repeatedly(
    base_command: Sequence[str],
    runs: int,
    out_dir: str | Path,
    runner: Runner = _default_runner,
    progress: Callable[[str], None] = lambda message: None,
    seed: int | None = None,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Run pytest `runs` times, writing one JUnit XML file per run.

    With a seed, run i uses seed + i to shuffle the test order, and the seed is part of the
    result file's name (run-002-seed-1235.xml), so any failing order can be replayed.
    A failing run is expected (that's what we're looking for); only a run that writes no
    results file at all is an error, because then the test command itself is broken.
    """
    if runs < 2:
        raise FlakyError("Need at least 2 runs to compare.")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("run-*.xml"):
        old.unlink()  # results of an earlier invocation would otherwise be mixed in

    env = dict(os.environ if environ is None else environ)
    command = list(base_command)
    if seed is not None:
        plugin_dir = _install_shuffle_plugin(out)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin_dir), env.get("PYTHONPATH", "")]))
        command += ["-p", "pyci_doctor_shuffle"]

    files = []
    for i in range(1, runs + 1):
        if seed is None:
            result_file = out / f"run-{i:03d}.xml"
        else:
            env[SEED_VARIABLE] = str(seed + i)
            result_file = out / f"run-{i:03d}-seed-{seed + i}.xml"
        exit_code = runner([*command, f"--junitxml={result_file}"], env)
        progress(f"run {i}/{runs}: pytest exit code {exit_code}")
        if not result_file.is_file():
            raise FlakyError(f"Run {i} produced no results file; the test command itself is failing.")
        files.append(result_file)
    return files


def seeds_from_files(files: Sequence[Path]) -> list[int | None]:
    """The shuffle seed of each run, read from its file name (None if it wasn't shuffled)."""
    return [int(m.group(1)) if (m := SEED_RE.search(Path(f).name)) else None for f in files]


def failing_seeds(history: History, seeds: Sequence[int | None]) -> list[int]:
    return [
        seed
        for result, seed in zip(history.results, seeds, strict=True)
        if result in ("failed", "error") and seed is not None
    ]


# --- Reporting ---


def format_report(
    histories: Sequence[History], run_count: int, seeds: Sequence[int | None] | None = None
) -> str:
    flaky = [h for h in histories if h.category == "flaky"]
    failing = [h for h in histories if h.category == "stable-fail"]
    lines = [
        f"{len(histories)} tests across {run_count} runs: "
        f"{len(flaky)} flaky, {len(failing)} always failing, "
        f"{sum(h.category == 'stable-pass' for h in histories)} always passing, "
        f"{sum(h.category == 'skipped' for h in histories)} skipped",
    ]
    for title, group in (("Flaky", flaky), ("Always failing", failing)):
        if not group:
            continue
        lines += ["", f"{title}:"]
        for h in group:
            lines.append(
                f"  {h.test_id}  passed {h.passed}/{run_count}, "
                f"failed {h.failed}/{run_count}, cause: {h.suspected_cause}"
            )
            if h.first_failure:
                # First line only: assertion messages can run over many lines.
                first_line = h.first_failure.splitlines()[0] if h.first_failure.strip() else ""
                lines.append(f"      first failure: {first_line[:200]}")
            failed_with = failing_seeds(h, seeds) if seeds else []
            if failed_with:
                lines.append(f"      failed with shuffle seed(s): {', '.join(map(str, failed_with))}")
    if seeds and any(failing_seeds(h, seeds) for h in flaky):
        lines += [
            "",
            "To replay a failing order (pyci-doctor installed in the same environment):",
            f"  {SEED_VARIABLE}=<seed> python -m pytest -p pyci_doctor.pytest_shuffle",
        ]
    missing = [h for h in histories if "missing" in h.results]
    if missing:
        lines += ["", "Missing from some runs (a run may have crashed):"]
        lines += [f"  {h.test_id}" for h in missing]
    return "\n".join(lines) + "\n"


def to_dict(histories: Sequence[History], run_count: int, seeds: Sequence[int | None] | None = None) -> dict:
    seeds = list(seeds) if seeds else [None] * run_count
    return {
        "runs": run_count,
        "seeds": seeds,
        "tests": [
            {
                "test_id": h.test_id,
                "category": h.category,
                "passed": h.passed,
                "failed": h.failed,
                "results": h.results,
                "suspected_cause": h.suspected_cause,
                "first_failure": h.first_failure,
                "failing_seeds": failing_seeds(h, seeds),
            }
            for h in histories
        ],
    }


# --- GitHub Actions output ---


def _escape(text: str, for_property: bool = False) -> str:
    """Escape text for a GitHub workflow command, so newlines or ':' can't break it."""
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if for_property:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


def github_annotations(histories: Sequence[History], run_count: int) -> list[str]:
    """Workflow commands that make GitHub show flaky and failing tests on the run and the PR."""
    lines = []
    for h in histories:
        if h.category == "flaky":
            level, title = "warning", f"Flaky test: {h.test_id}"
        elif h.category == "stable-fail":
            level, title = "error", f"Always failing: {h.test_id}"
        else:
            continue
        message = f"passed {h.passed}/{run_count}, failed {h.failed}/{run_count}, cause: {h.suspected_cause}"
        if h.first_failure:
            message += f"\nfirst failure: {h.first_failure[:500]}"
        lines.append(f"::{level} title={_escape(title, for_property=True)}::{_escape(message)}")
    return lines


def markdown_summary(histories: Sequence[History], run_count: int, seeds: Sequence[int | None] | None = None) -> str:
    """A table for the run's summary page (written to $GITHUB_STEP_SUMMARY)."""
    problems = [h for h in histories if h.category in ("flaky", "stable-fail")]
    lines = [f"### Flaky test check: {run_count} runs", ""]
    if not problems:
        lines.append(f"No flaky or failing tests in {len(histories)} tests.")
        return "\n".join(lines) + "\n"
    lines += ["| Test | Result | Passed | Cause | Failing seeds |", "|---|---|---|---|---|"]
    for h in problems:
        seed_text = ", ".join(map(str, failing_seeds(h, seeds))) if seeds else ""
        test_id = h.test_id.replace("|", "\\|")
        lines.append(
            f"| `{test_id}` | {h.category} | {h.passed}/{run_count} | {h.suspected_cause} | {seed_text} |"
        )
    return "\n".join(lines) + "\n"
