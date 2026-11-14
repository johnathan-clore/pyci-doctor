# pyci-doctor

[![CI](https://github.com/johnathan-clore/pyci-doctor/actions/workflows/ci.yml/badge.svg)](https://github.com/johnathan-clore/pyci-doctor/actions/workflows/ci.yml)

A command-line tool that sets up and maintains GitHub Actions CI for Python projects:

- **`init`** inspects a project (pip, uv or Poetry) and writes a cross-platform test workflow that fits it.
- **`check`** fails when that workflow has drifted from what the project now needs.
- **`flaky`** runs the tests repeatedly in shuffled order and reports which tests are flaky, whether
  their failures look like test bugs or infrastructure problems, and the seed that replays a failure.
- **`suggest`** (optional) asks an AI model for workflow improvements and accepts them only if
  they pass a set of automatic safety and correctness checks.

It has no runtime dependencies beyond the Python standard library, and it generates its own CI.

## Install

Requires Python 3.11 or newer.

```bash
python -m pip install "git+https://github.com/johnathan-clore/pyci-doctor@v0.4.0"
```

For `suggest`, install the `ai` extra (it adds PyYAML, used to check the model's answer):

```bash
python -m pip install "pyci-doctor[ai] @ git+https://github.com/johnathan-clore/pyci-doctor@v0.4.0"
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
| `package_manager` | `uv.lock` → uv; `poetry.lock` or Poetry dependencies in `pyproject.toml` → Poetry; otherwise pip |
| `install_command` | uv: `uv sync --all-extras`; Poetry: `poetry install --all-extras`; pip: `pip install -e .` (plus a `dev`/`test` extra) for a `pyproject.toml` with a `[project]` or `[build-system]` table, otherwise `requirements.txt` (+ `requirements-dev.txt`) |
| `test_runner` / `test_command` | pytest if it appears in config or dependencies; otherwise unittest if test files exist |
| `python_versions` | `requires-python` (or Poetry's `python = "^3.11"`), checked against 3.9–3.15; the three newest (3.13–3.15) with a warning if it's missing |
| `has_ruff` / `has_coverage` | their config sections, config files, or dependencies |

### `init [PATH] [--all-pythons] [--flaky-runs N] [--coverage] [--force] [--stdout]`

Writes `.github/workflows/ci.yml`:

- a **test** job on Ubuntu, Windows and macOS, for the oldest and newest supported Python
  (`--all-pythons` tests every supported version);
- a **lint** job if the project uses ruff (with a pinned ruff version);
- with `--coverage`, pytest-cov in the test job and a coverage table on the run page;
- with `--flaky-runs N`, a **flaky** job that runs the suite N times in shuffled order, shows flaky
  tests as annotations and a summary table, and uploads the raw results.

It won't overwrite an existing file without `--force`. `--stdout` prints instead of writing.
The options used are recorded on the file's first line, so `check` can regenerate it the same way.

#### Adding your own steps and jobs

Every generated job ends with a marked section, and the file ends with one for whole jobs.
Anything you put between the markers is kept by `init --force` and ignored by `check`:

```yaml
      - name: Run tests
        run: python -m pytest -v

      # >>> custom steps (test): pyci-doctor keeps anything between these lines
      - name: Build the scorecard
        run: python -m evals.report
      # <<< custom steps (test)
```

Everything outside the markers is still checked for drift. A missing end marker, or custom steps
for a job that would no longer be generated, is an error, never silent data loss.

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

### `flaky [RESULTS ...] [--runs N] [--out DIR] [--seed N] [--no-shuffle] [--command CMD] [--github] [--json]`

Give it JUnit XML files (or folders of them), one per run, or let `--runs N` run pytest N times.

With `--runs`, every run uses a different **test order** (test files shuffled, then the tests in each
file), because many flaky tests only fail when another test ran first. Run *i* uses seed *start+i*;
the seed is in each result file's name and in the report, and replays that exact order:

```bash
PYCI_DOCTOR_SEED=1234 python -m pytest -p pyci_doctor.pytest_shuffle
```

`--seed` fixes the starting seed, `--no-shuffle` keeps the normal order, and `--command` starts pytest
another way (e.g. `"uv run pytest"`). Inside GitHub Actions (or with `--github`) flaky and failing
tests are also printed as annotations and added to the run's summary page.

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
- **Custom sections, not hand-maintained files.** Projects nearly always need a step or two of
  their own; marked sections let them have that and keep drift checking for everything else.
- **Shuffled, reproducible runs.** Repeating the same order can't find order-dependent tests;
  random order without a seed can't be replayed. Each run gets a recorded seed. The shuffle plugin
  is copied into its own folder and put on `PYTHONPATH`, so it also loads inside uv and Poetry
  environments without exposing pyci-doctor's other packages to them.
- **Staleness is tested, not remembered.** A weekly scheduled run (`maintenance.yml`) checks the
  pinned action versions, the newest Python and the pinned ruff against what has been released.
- **Zero runtime dependencies.** `tomllib`, `xml.etree`, `difflib` and `argparse` are all in the
  standard library. PyYAML is used only in the tests, to check that generated YAML really parses.

## Known limitations

- Only GitHub Actions. Supported installers: pip (`pyproject.toml`, `requirements*.txt`), uv and Poetry.
- `requires-python` support covers `>=`, `<=`, `>`, `<`, `==`, `!=`, `~=` and `.*` wildcards,
  compared at the minor-version level.
- `ubuntu-latest`, `windows-latest` and `macos-latest` move to new OS versions over time.
- `flaky --runs` supports pytest only; other runners can pass in their own JUnit XML files.
- Shuffling keeps tests grouped by file, so it finds dependencies between files and between tests
  in one file, but every run still executes each file's tests together.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest                    # full suite
python -m pytest --update-golden    # after an intended change to generated output; review with git diff
python -m pyci_doctor check         # this repo's own CI is generated by pyci-doctor
python -m ruff check .              # the same lint the CI's lint job runs
python -m pytest -m network         # are action versions, Python and ruff current? (needs internet)
```

### Releasing

Bump `__version__` in `src/pyci_doctor/__init__.py`, regenerate (run the command on the first line of
`.github/workflows/ci.yml`, then `python -m pytest --update-golden`), commit, then push a matching tag:

```bash
git tag -a v0.4.0 -m "pyci-doctor 0.4.0"
git push origin v0.4.0
```

`.github/workflows/release.yml` then checks that the tag matches `__version__`, runs the tests,
builds the package and creates the GitHub release with generated notes and the built files attached.

### Publishing to PyPI

The release workflow can also publish to PyPI with *trusted publishing* (PyPI verifies the
workflow itself; no password or token is stored). One-time setup:

1. On pypi.org, under *Your account → Publishing*, add a pending publisher: project `pyci-doctor`,
   owner `johnathan-clore`, repository `pyci-doctor`, workflow `release.yml`, environment `pypi`.
2. In the GitHub repo, *Settings → Environments → New environment* named `pypi`.
3. In *Settings → Secrets and variables → Actions → Variables*, add `PUBLISH_TO_PYPI` = `true`.

From the next tag on, the `pypi` job publishes the files the release job built and tested.

**Every October** a new Python is released. Add it to `KNOWN_PYTHONS` in `src/pyci_doctor/detect.py`,
regenerate, and release. Users then see the change through `pyci-doctor check`. As a reminder,
`test_known_pythons_include_the_running_python` fails when the tests run on a Python newer than the list.

Tests are organised by module: `test_detect.py` (small fake projects in `tests/fixtures/`, including
uv and Poetry ones), `test_render.py` (golden files in `tests/golden/` plus YAML structure checks),
`test_check.py`, `test_custom.py` (custom sections), `test_ai.py` (canned model replies, safe and unsafe),
`test_flaky.py` (hand-written JUnit XML, plus real pytest runs of tests that are flaky on purpose:
one by counting its runs, one by depending on test order), `test_cli.py` and `test_freshness.py`
(network checks, skipped unless `-m network`).
