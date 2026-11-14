# pyci-doctor

[![CI](https://github.com/johnathan-clore/pyci-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/johnathan-clore/pyci-doctor/actions/workflows/ci.yml)

A command-line tool that sets up and maintains GitHub Actions CI for Python projects:

- **`init`** inspects a project and writes a cross-platform test workflow that fits it.
- **`check`** fails when that workflow has drifted from what the project now needs.
- **`flaky`** runs or reads repeated test runs and reports which tests are flaky, and whether
  their failures look like test bugs or infrastructure problems (timeouts, dropped connections).
- **`suggest`** (optional) asks an AI model for workflow improvements and accepts them only if
  they pass a set of automatic safety and correctness checks.

It has no runtime dependencies beyond the Python standard library, and it generates its own CI.

## Install

Requires Python 3.11 or newer.

```bash
python -m pip install "git+https://github.com/johnathan-clore/pyci-doctor@v0.2.0"
```

For `suggest`, install the `ai` extra (it adds PyYAML, used to check the model's answer):

```bash
python -m pip install "pyci-doctor[ai] @ git+https://github.com/johnathan-clore/pyci-doctor@v0.2.0"
```

## Quick start

```bash
cd your-project
python -m pyci_doctor detect          # see what it finds
python -m pyci_doctor init            # write .github/workflows/ci.yml
python -m pyci_doctor check           # later: is ci.yml still up to date?
python -m pyci_doctor flaky --runs 10 # run the tests 10 times and look for flaky ones
```

## Commands

### `detect [PATH] [--json]`

Reads the project's files (it never imports or runs its code) and reports:

| Field | Where it comes from |
|---|---|
| `install_command` | `pyproject.toml` with a `[project]` or `[build-system]` table → `pip install -e .` (plus a `dev`/`test` extra if one exists); otherwise `requirements.txt` (+ `requirements-dev.txt`) |
| `test_runner` / `test_command` | pytest if it appears in config or dependencies; otherwise unittest if test files exist |
| `python_versions` | `requires-python`, checked against 3.9–3.14; 3.12–3.14 with a warning if it's missing |
| `has_ruff` / `has_coverage` | their config sections, config files, or dependencies |

### `init [PATH] [--all-pythons] [--flaky-runs N] [--force] [--stdout]`

Writes `.github/workflows/ci.yml`:

- a **test** job on Ubuntu, Windows and macOS, for the oldest and newest supported Python
  (`--all-pythons` tests every supported version);
- a **lint** job if the project uses ruff;
- with `--flaky-runs N`, a **flaky** job that runs the suite N times, reports flaky tests and
  uploads the raw results.

It won't overwrite an existing file without `--force`. `--stdout` prints instead of writing.
The options used are recorded on the file's first line, so `check` can regenerate it the same way.

### `check [PATH] [--strict]`

Regenerates the workflow and compares it, byte for byte, with the one on disk.

| Result | Exit code |
|---|---|
| up to date | 0 |
| hand-written (no pyci-doctor header): skipped unless `--strict` | 0 |
| out of date (prints a unified diff) | 1 |
| same content but CRLF line endings | 1 |
| missing | 1 |
| project can't be inspected (e.g. invalid `pyproject.toml`) | 2 |

### `flaky [RESULTS ...] [--runs N] [--out DIR] [--json]`

Give it JUnit XML files (or folders of them), one per run, or let `--runs N` run pytest N times.
Each test is classified as **flaky**, **always failing**, **always passing** or **skipped**.
Every failure is labelled **infrastructure** or **test** by matching its message and traceback
against known environment problems (timeouts, connection errors, DNS failures, full disk,
out of memory, 502/503/504, rate limits). Tests missing from some runs are listed separately,
since that usually means a run crashed.

```text
7 tests across 3 runs: 3 flaky, 1 always failing, 2 always passing, 1 skipped

Flaky:
  tests.test_api::test_fetch_user  passed 2/3, failed 1/3, cause: infrastructure
      first failure: TimeoutError: The read operation timed out
  tests.test_math::test_rounding  passed 2/3, failed 1/3, cause: test
      first failure: AssertionError: assert 0.30000000000000004 == 0.3
```

Exit codes: 0 nothing found, 1 flaky or always-failing tests found, 2 results unreadable.

### `suggest [PATH] [--write] [--model NAME] [--json]`

Sends the detected profile and the current workflow (never the project's code) to an AI model
(Anthropic's API; set `ANTHROPIC_API_KEY`), asks for improvements as JSON, and checks the answer:

| Check | Rejects a suggestion that... |
|---|---|
| `valid-yaml` | isn't valid YAML |
| `test-job-kept`, `tests-still-run` | removes the test job or changes the test command |
| `all-platforms`, `python-versions-kept` | drops an operating system or the oldest/newest Python |
| `python-versions-are-strings` | unquotes versions (`3.10` would become `3.1`) |
| `failures-not-hidden` | adds `continue-on-error` or `\|\| true` |
| `job-timeouts` | leaves a job without `timeout-minutes` |
| `read-only-permissions`, `safe-triggers` | grants write access or uses `pull_request_target` |
| `no-secrets`, `no-remote-scripts` | references secrets or pipes `curl` into a shell |
| `actions-pinned` | uses an action without a version |
| `changes-described` | changes the workflow without saying what or why |

Third-party actions produce a warning, not a rejection. The command prints each change, every
check's result and a diff. With `--write`, an accepted suggestion is saved with a header marking
the file as hand-maintained, so `check` stops comparing it with generated output.
Timeouts and connection errors are retried with growing waits (2s, 4s).
Exit codes: 0 accepted (or no changes), 1 rejected, 2 the model couldn't be reached or answered unusably.

## Design decisions

- **Generated, not templated.** A fixed template gives every project the same CI. Reading the
  project first means a requirements-only project, a src-layout package and a unittest suite each
  get a workflow that actually installs and runs them.
- **Oldest and newest Python by default.** Most version-specific breakage shows up at one end of
  the supported range; testing both ends keeps the job count down.
- **`fail-fast: false`.** When one OS fails, you still want to know whether the others passed.
- **Timeouts on every job.** The default is 6 hours; a hung test should fail in minutes.
- **Versions are quoted strings.** Unquoted, YAML reads `3.10` as the number `3.1`.
- **Byte-for-byte output.** Generation is a pure function, files are always written with LF line
  endings (also on Windows), and `.gitattributes` keeps them LF in every checkout. That is what
  lets `check` compare exactly, and lets the golden-file tests run identically on all three OSes.
- **Hand-written workflows are respected.** `check` skips files it didn't generate unless asked.
- **Infrastructure detection is a heuristic.** Its patterns are deliberately narrow (a bare
  "timeout" would also match `assert cfg.timeout == 30`), and it suggests where to look first
  rather than deciding anything.
- **The model is never trusted.** The prompt states the rules, and plain code enforces them;
  the model's own claims (such as which changes it made) are checked against the actual text.
  The model client is a plain function (`prompt -> reply`), so every test uses canned replies.
- **Zero runtime dependencies.** `tomllib`, `xml.etree`, `difflib` and `argparse` are all in the
  standard library. PyYAML is used only in the tests, to check that generated YAML really parses.

## Known limitations

- Only GitHub Actions, and only pip-based installs (`pyproject.toml` or `requirements*.txt`).
- `requires-python` support covers `>=`, `<=`, `>`, `<`, `==`, `!=`, `~=` and `.*` wildcards,
  compared at the minor-version level.
- `ubuntu-latest`, `windows-latest` and `macos-latest` move to new OS versions over time.
- The lint job installs the latest ruff, so a new ruff release can add findings without a code change.
- `flaky --runs` supports pytest only; other runners can pass in their own JUnit XML files.
- Project-specific CI steps (like a report step) aren't generated; such workflows become
  hand-maintained, and `check` skips them.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest                    # full suite
python -m pytest --update-golden    # after an intended change to generated output; review with git diff
python -m pyci_doctor check         # this repo's own CI is generated by pyci-doctor
```

### Releasing

Bump `__version__` in `src/pyci_doctor/__init__.py`, regenerate (`python -m pyci_doctor init --force --flaky-runs 5`
and `python -m pytest --update-golden`), commit, then push a matching tag:

```bash
git tag -a v0.2.0 -m "pyci-doctor 0.2.0"
git push origin v0.2.0
```

`.github/workflows/release.yml` then checks that the tag matches `__version__`, runs the tests,
builds the package and creates the GitHub release with generated notes and the built files attached.

Tests are organised by module: `test_detect.py` (against small fake projects in
`tests/fixtures/`), `test_render.py` (golden files in `tests/golden/` plus YAML structure checks),
`test_check.py`, `test_ai.py` (canned model replies, safe and unsafe), `test_flaky.py` (hand-written JUnit XML plus an end-to-end run of a test that is
flaky on purpose, but predictably so) and `test_cli.py`.
