"""Ask an AI model to suggest workflow improvements, then check every suggestion before using it.

The model is never trusted. The prompt tells it the rules, and validate() enforces them
with plain code. A suggestion is only accepted if every rule marked "error" passes.

Only the detected profile and the workflow text are sent to the model, never the project's code.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from pyci_doctor.detect import ProjectProfile
from pyci_doctor.render import OPERATING_SYSTEMS, matrix_pythons

Client = Callable[[str], str]  # takes a prompt, returns the model's reply text

DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"

# Problems worth retrying: the request may succeed a moment later.
RETRYABLE = (TimeoutError, ConnectionError)


class SuggestError(Exception):
    """The model couldn't be reached, or its reply couldn't be used."""


# --- Talking to the model ---


def build_prompt(profile: ProjectProfile, workflow: str) -> str:
    return f"""You improve GitHub Actions CI workflows for Python projects.

Project profile (detected from its files):
{json.dumps(profile.to_dict(), indent=2)}

Current workflow (.github/workflows/ci.yml):
```yaml
{workflow}```

Suggest a small number of concrete improvements, such as caching, a useful extra check,
or clearer step names. Keep everything that already works.

Rules (your answer is checked automatically and rejected if it breaks any):
- Keep the "test" job, its operating systems ({", ".join(OPERATING_SYSTEMS)}) and its Python versions.
- Keep running the tests with: {profile.test_command}
- Quote Python versions as strings, e.g. "3.10".
- Every job needs timeout-minutes.
- Keep permissions read-only. Never use pull_request_target.
- Never use continue-on-error or "|| true" to hide test failures.
- Never reference secrets.
- Never pipe downloaded scripts into a shell (curl ... | sh).
- Pin every action to a version, e.g. actions/checkout@v7.

Reply with JSON only, no other text, in exactly this shape:
{{"changes": [{{"summary": "...", "reason": "..."}}], "workflow": "<the complete new ci.yml>"}}
"""


def call_with_retries(
    client: Client,
    prompt: str,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] | None = None,
) -> str:
    """Retry timeouts and connection errors, waiting longer each time (2s, 4s, ...)."""
    # Look up time.sleep now, not when the module loads. A default of `sleep=time.sleep`
    # would capture the original function once, so patching time.sleep later would have no effect.
    sleep = sleep or time.sleep
    for attempt in range(1, attempts + 1):
        try:
            reply = client(prompt)
        except RETRYABLE as exc:
            if attempt == attempts:
                raise SuggestError(f"Model call failed after {attempts} attempts: {exc}") from exc
            sleep(base_delay * 2 ** (attempt - 1))
            continue
        if not reply or not reply.strip():
            raise SuggestError("Model returned an empty reply.")
        return reply
    raise AssertionError("unreachable")


def anthropic_client(api_key: str, model: str = DEFAULT_MODEL, timeout: float = 120) -> Client:
    """A Client that calls the Anthropic Messages API using only the standard library."""

    def client(prompt: str) -> str:
        body = json.dumps(
            {"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
        ).encode("utf-8")
        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            # Rate limits, server errors and "overloaded" (529) are worth retrying.
            if exc.code in (429, 500, 502, 503, 504, 529):
                raise ConnectionError(f"HTTP {exc.code} from model API") from exc
            raise SuggestError(f"Model API returned HTTP {exc.code}; check the API key and model name.") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Can't reach model API: {exc.reason}") from exc
        return "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )

    return client


# --- Reading the reply ---


@dataclass
class Suggestion:
    workflow: str
    changes: list[dict]


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*)\n```\s*$", re.DOTALL)


def parse_reply(reply: str) -> Suggestion:
    text = reply.strip()
    # Models often wrap JSON in a ```json block despite being asked not to.
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SuggestError(f"Model reply isn't valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise SuggestError("Model reply must be a JSON object.")
    workflow, changes = data.get("workflow"), data.get("changes")
    if not isinstance(workflow, str) or not workflow.strip():
        raise SuggestError("Model reply has no 'workflow' text.")
    if not isinstance(changes, list) or not all(
        isinstance(c, dict) and isinstance(c.get("summary"), str) for c in changes
    ):
        raise SuggestError("Model reply's 'changes' must be a list of {summary, reason} objects.")
    if not workflow.endswith("\n"):
        workflow += "\n"
    return Suggestion(workflow, changes)


# --- Checking the suggestion ---


@dataclass
class RuleResult:
    rule: str
    passed: bool
    message: str
    severity: str = "error"  # "error" blocks the suggestion; "warning" is only reported


def _load_yaml(text: str):
    try:
        import yaml
    except ImportError as exc:
        raise SuggestError(
            "AI suggestions need PyYAML to check the reply: "
            'python -m pip install "pyci-doctor[ai] @ git+https://github.com/johnathan-clore/pyci-doctor"'
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, str(exc).splitlines()[0]
    if isinstance(data, dict) and True in data:
        data["on"] = data.pop(True)  # PyYAML reads the key `on` as True (YAML 1.1)
    return data, ""


def _all_steps(jobs: dict) -> list[tuple[str, dict]]:
    return [
        (name, step)
        for name, job in jobs.items()
        if isinstance(job, dict)
        for step in job.get("steps", []) or []
        if isinstance(step, dict)
    ]


def validate(workflow: str, profile: ProjectProfile) -> list[RuleResult]:
    data, yaml_error = _load_yaml(workflow)
    if not isinstance(data, dict):
        return [RuleResult("valid-yaml", False, yaml_error or "Not a YAML mapping.")]

    results = [RuleResult("valid-yaml", True, "Parses as YAML.")]

    def check(rule: str, passed: bool, ok: str, bad: str, severity: str = "error") -> None:
        results.append(RuleResult(rule, passed, ok if passed else bad, severity))

    jobs = data.get("jobs") if isinstance(data.get("jobs"), dict) else {}
    test_job = jobs.get("test") if isinstance(jobs.get("test"), dict) else {}
    matrix = (test_job.get("strategy") or {}).get("matrix") or {}
    steps = _all_steps(jobs)
    runs = [str(step.get("run", "")) for _, step in steps]

    triggers = data.get("on")
    trigger_names = set(triggers) if isinstance(triggers, (dict, list)) else {triggers}
    check(
        "safe-triggers",
        "pull_request_target" not in trigger_names,
        "No pull_request_target trigger.",
        "Uses pull_request_target, which runs untrusted PR code with repository secrets.",
    )

    permission_values = [data.get("permissions")] + [
        job.get("permissions") for job in jobs.values() if isinstance(job, dict)
    ]
    write_access = any("write" in json.dumps(p) for p in permission_values if p is not None)
    check("read-only-permissions", not write_access, "Permissions stay read-only.", "Grants write permissions.")

    check("test-job-kept", bool(test_job), "Test job present.", "The 'test' job was removed.")

    os_list = matrix.get("os") or []
    missing_os = [os for os in OPERATING_SYSTEMS if os not in os_list]
    check(
        "all-platforms",
        not missing_os,
        "Still tests on Linux, Windows and macOS.",
        f"Drops operating systems: {', '.join(missing_os)}.",
    )

    versions = matrix.get("python-version") or []
    required = matrix_pythons(profile) if profile.python_versions else []
    check(
        "python-versions-are-strings",
        bool(versions) and all(isinstance(v, str) for v in versions),
        "Python versions are quoted strings.",
        f"Python versions must be quoted strings, got {versions!r}.",
    )
    missing_versions = [v for v in required if v not in [str(x) for x in versions]]
    check(
        "python-versions-kept",
        not missing_versions,
        "Keeps the oldest and newest supported Python.",
        f"Drops Python versions: {', '.join(missing_versions)}.",
    )

    test_runs = [r for r in runs if profile.test_command and profile.test_command in r]
    check(
        "tests-still-run",
        bool(test_runs),
        f"Still runs: {profile.test_command}",
        f"No step runs the test command '{profile.test_command}'.",
    )

    hides_failures = any(
        isinstance(job, dict) and job.get("continue-on-error") for job in jobs.values()
    ) or any(step.get("continue-on-error") for _, step in steps) or any("|| true" in r for r in test_runs)
    check(
        "failures-not-hidden",
        not hides_failures,
        "Failures still fail the build.",
        "Uses continue-on-error or '|| true', which would hide failures.",
    )

    no_timeout = [name for name, job in jobs.items() if isinstance(job, dict) and "timeout-minutes" not in job]
    check(
        "job-timeouts",
        not no_timeout,
        "Every job has a timeout.",
        f"Jobs without timeout-minutes: {', '.join(no_timeout)}.",
    )

    check(
        "no-secrets",
        "secrets." not in workflow,
        "Uses no secrets.",
        "References secrets; add those by hand after reviewing why they're needed.",
    )

    remote_script = re.search(r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b", workflow)
    check(
        "no-remote-scripts",
        remote_script is None,
        "Doesn't pipe downloaded scripts into a shell.",
        "Pipes a downloaded script into a shell.",
    )

    uses = [str(step["uses"]) for _, step in steps if "uses" in step]
    unpinned = [u for u in uses if "@" not in u]
    check(
        "actions-pinned",
        not unpinned,
        "Every action is pinned to a version.",
        f"Actions without a version: {', '.join(unpinned)}.",
    )

    third_party = sorted({u.split("@")[0] for u in uses if not u.startswith("actions/")})
    check(
        "only-official-actions",
        not third_party,
        "Only uses actions published by GitHub.",
        f"Uses third-party actions, review them before trusting: {', '.join(third_party)}.",
        severity="warning",
    )

    return results


# --- Putting it together ---


@dataclass
class SuggestResult:
    suggestion: Suggestion | None
    rules: list[RuleResult] = field(default_factory=list)
    error: str = ""
    unchanged: bool = False  # the model returned the workflow as it was

    @property
    def accepted(self) -> bool:
        return (
            self.suggestion is not None
            and not self.error
            and not self.unchanged
            and all(r.passed for r in self.rules if r.severity == "error")
        )


def suggest(
    profile: ProjectProfile,
    current_workflow: str,
    client: Client,
    sleep: Callable[[float], None] | None = None,
) -> SuggestResult:
    """Never raises for a bad model reply: the problem is reported in the result instead."""
    prompt = build_prompt(profile, current_workflow)
    try:
        suggestion = parse_reply(call_with_retries(client, prompt, sleep=sleep))
    except SuggestError as exc:
        return SuggestResult(None, error=str(exc))

    if suggestion.workflow == current_workflow:
        # Identical text: nothing to accept, whatever the model claims it changed.
        return SuggestResult(suggestion, unchanged=True)

    rules = validate(suggestion.workflow, profile)
    described = bool(suggestion.changes)
    rules.append(
        RuleResult(
            rule="changes-described",
            passed=described,
            message=f"{len(suggestion.changes)} change(s) described."
            if described
            else "The workflow changed but the reply doesn't say what or why.",
        )
    )
    return SuggestResult(suggestion, rules)
