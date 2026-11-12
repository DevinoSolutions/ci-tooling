"""DevinoSolutions org CI lint — the Track 1 hygiene rules (spec 4.3).

Enforced org-wide through `.github/workflows/ci-lint.yml` and a repository
ruleset, so that the savings W1-W4 land are not undone by the next PR.

    R1  docker-build          a step that builds a container image in CI
    R2  missing-concurrency   a push/pull_request workflow with no top-level
                              `concurrency:` block, so pushes stack instead of
                              superseding each other. Exempt a workflow whose
                              jobs serialise on external state with
                              `# ci-lint: allow-missing-concurrency <reason>`
                              in the workflow header
    R3  nightly-runner        a scheduled workflow whose jobs sit on the main
                              pool instead of the deprioritised nightly set
    R4  caller-permissions    a job calling a reusable workflow without holding
                              the permissions that workflow declares, which
                              fails the run at startup with nothing to read
    R6  pnpm-store-cache      a self-hosted job whose `actions/setup-node` or
                              `pnpm/action-setup` step restores the pnpm or npm
                              cache (`cache: pnpm|npm`, or setup-node's automatic
                              caching), or whose `actions/cache` (or fork) step
                              caches the shared store path (`/var/cache/ci`,
                              `pnpm store path`, ...), directly or through a local
                              composite action, over the store every pod on the
                              node shares

WHY LINE NUMBERS, NOT JUST FILES. Adoption is incremental: a repo drops a
`.github/ci-lint-baseline` file naming the sha at which it was migrated, and
from then on only *added* lines are linted, so the backlog inside a large
workflow file does not block an unrelated one-line PR. That only works if
every finding is anchored to a line, and if a finding survives the added-lines
filter when any of the lines that *cause* it were touched — adding `push:` to an
existing trigger block must trip R2 even though the `on:` line is untouched, and
deleting `id-token: write` from a caller's `permissions:` block must trip R4
even though it leaves `uses:` alone and adds no line at all. Hence
`Finding.anchors`, and hence `added_lines` counting the gap a deletion leaves.

Stdlib plus PyYAML only, so the workflow needs one `pip install`.

Local use:

    python scripts/ci-lint/lint.py .github/workflows/ci.yml
    python scripts/ci-lint/lint.py --base origin/main $(git diff --name-only origin/main...HEAD -- .github/workflows)

See scripts/ci-lint/README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

# ── rule ids ───────────────────────────────────────────────────────────────

RULE_DOCKER_BUILD = "docker-build"
RULE_CONCURRENCY = "missing-concurrency"
RULE_NIGHTLY_RUNNER = "nightly-runner"
RULE_CALLER_PERMISSIONS = "caller-permissions"
RULE_BUN_ENGINE_ORDER = "bun-engine-order"
RULE_UNPARSEABLE = "unparseable-workflow"

SEVERITY_ERROR = "error"
SEVERITY_NOTICE = "notice"

NIGHTLY_LABEL = "ubuntu-devino-nightly"
BASELINE_FILE = ".github/ci-lint-baseline"
ALLOW_MARKER = "ci-lint: allow-build"

SPEC_URL = (
    "https://github.com/DevinoSolutions/runner-infra/blob/main/docs/superpowers/specs/"
    "2026-09-10-ci-build-once-and-pool-design.md"
)
WAIT_FOR_IMAGE_URL = f"{SPEC_URL}#55-ci-side-wait-for-image"

CONCURRENCY_SNIPPET = (
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.ref }}\n"
    "  cancel-in-progress: ${{ !contains(fromJSON('[\"refs/heads/main\",\"refs/heads/dev\"]'),"
    " github.ref) }}"
)

# ── findings ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One rule violation, anchored to the line a reviewer should look at."""

    file: str
    line: int
    rule: str
    message: str
    fix: str
    anchors: tuple[int, ...] = field(default=())
    severity: str = SEVERITY_ERROR

    @property
    def anchor_lines(self) -> tuple[int, ...]:
        return self.anchors or (self.line,)

    def as_annotation(self) -> str:
        body = f"[{self.rule}] {self.message}"
        if self.fix:
            body += f"\n\nFix: {self.fix}"
        return f"::{self.severity} file={self.file},line={self.line},title=ci-lint {self.rule}::{_escape(body)}"


def _escape(text: str) -> str:
    """GitHub workflow-command escaping for the message payload."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# ── YAML loading with line marks ───────────────────────────────────────────
#
# PyYAML throws away positions once a document is constructed, so the loader
# below stashes them back on every mapping under reserved keys. `_KEY_MARKS`
# maps a key to (line of the key, last line of its value), which is what the
# rules need to point at `runs-on:` rather than at the whole job.

_KEY_MARKS = "__ci_lint_key_marks__"
_NODE_START = "__ci_lint_start__"
_NODE_END = "__ci_lint_end__"
_META = (_KEY_MARKS, _NODE_START, _NODE_END)


class _MarkedLoader(yaml.SafeLoader):
    """SafeLoader that records source positions on every mapping."""


def _construct_marked_mapping(loader: _MarkedLoader, node: yaml.MappingNode) -> dict:
    loader.flatten_mapping(node)
    data: dict = {}
    marks: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, (str, int, float, bool, type(None))):
            key = str(key)
        data[key] = loader.construct_object(value_node, deep=True)
        marks[key] = (key_node.start_mark.line + 1, value_node.end_mark.line + 1)
    data[_KEY_MARKS] = marks
    data[_NODE_START] = node.start_mark.line + 1
    data[_NODE_END] = node.end_mark.line + 1
    return data


_MarkedLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_marked_mapping)


def _keys(mapping: dict) -> list:
    return [k for k in mapping if k not in _META]


def _key_line(mapping: dict, key) -> int | None:
    marks = mapping.get(_KEY_MARKS) or {}
    return marks[key][0] if key in marks else None


def _value_range(mapping: dict, key) -> tuple[int, int] | None:
    marks = mapping.get(_KEY_MARKS) or {}
    return marks.get(key)


def _block_lines(mapping: dict, key) -> set[int]:
    """Every source line a key's block occupies, key line included. Empty if absent."""
    span = _value_range(mapping, key)
    return set(range(span[0], span[1] + 1)) if span else set()


def _on_key(doc: dict):
    """The `on:` key, tolerating YAML 1.1 resolving a bare `on` to the boolean True."""
    for candidate in ("on", True):
        if candidate in doc:
            return candidate
    return None


# ── rule 1: docker build ───────────────────────────────────────────────────

_BUILD_PATTERNS = (
    (re.compile(r"\bdocker\s+buildx\s+build\b"), "docker buildx build"),
    (re.compile(r"\bdocker\s+buildx\s+bake\b"), "docker buildx bake"),
    (re.compile(r"\bdocker[-\s]+compose\b.*\bbuild\b"), "docker compose build"),
    (re.compile(r"\bdocker\s+build\b"), "docker build"),
    (re.compile(r"docker/bake-action"), "docker/bake-action"),
    (re.compile(r"docker/build-push-action"), "docker/build-push-action"),
)


def _marker_re(name: str) -> re.Pattern[str]:
    return re.compile(r"#\s*ci-lint:\s*" + re.escape(name) + r"\b(?P<reason>.*)$")


ALLOW_BUILD_MARKER = "allow-build"
ALLOW_SCHEDULE_MARKER = "allow-schedule-main"
ALLOW_PERMISSIONS_MARKER = "allow-permissions"
ALLOW_CONCURRENCY_MARKER = "allow-missing-concurrency"
ALLOW_BUN_ENGINE_MARKER = "allow-bun-engine"

# A marker as it must be WRITTEN in a workflow file. `_marker_re` matches only
# with the `ci-lint:` prefix, so a message that prints the bare name sends the
# reader to a comment this linter silently ignores — which is exactly what the
# nightly-runner remediation used to do, and it cost a round trip in
# `runner-parity` before anyone noticed the exemption was doing nothing.
# `ALLOW_MARKER` above is the same rendering for allow-build.
WRITTEN_SCHEDULE_MARKER = f"ci-lint: {ALLOW_SCHEDULE_MARKER}"
WRITTEN_PERMISSIONS_MARKER = f"ci-lint: {ALLOW_PERMISSIONS_MARKER}"
WRITTEN_CONCURRENCY_MARKER = f"ci-lint: {ALLOW_CONCURRENCY_MARKER}"
WRITTEN_BUN_ENGINE_MARKER = f"ci-lint: {ALLOW_BUN_ENGINE_MARKER}"
_ALLOW_BUILD_RE = _marker_re(ALLOW_BUILD_MARKER)
_ALLOW_SCHEDULE_RE = _marker_re(ALLOW_SCHEDULE_MARKER)
_ALLOW_PERMISSIONS_RE = _marker_re(ALLOW_PERMISSIONS_MARKER)
_ALLOW_CONCURRENCY_RE = _marker_re(ALLOW_CONCURRENCY_MARKER)
_ALLOW_BUN_ENGINE_RE = _marker_re(ALLOW_BUN_ENGINE_MARKER)


def _strip_comment(line: str) -> str:
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def _build_command(line: str) -> str | None:
    payload = _strip_comment(line)
    for pattern, label in _BUILD_PATTERNS:
        if pattern.search(payload):
            return label
    return None


def _attach_leading_comments(
    lines: list[str], spans: list[tuple[int, int]], floor: int
) -> list[tuple[int, int]]:
    """Give each span the comment lines sitting directly above it.

    A marker written above a list item belongs to that item — that is how people
    write comments — but the naive span for the *previous* item runs right up to
    the next item's first line and would swallow it, excusing the wrong step. So
    the comment block moves: the span below gains it, the span above loses it.
    Only comment lines move; a blank line ends the block, and `floor` stops the
    first span climbing out of its job.
    """
    out = [[start, end] for start, end in spans]
    for index, span in enumerate(out):
        lower = out[index - 1][0] + 1 if index else floor
        start = span[0]
        while start - 1 >= lower and lines[start - 2].strip().startswith("#"):
            start -= 1
        span[0] = start
        if index:
            out[index - 1][1] = min(out[index - 1][1], start - 1)
    return [(start, end) for start, end in out]


def _marker_state(lines: list[str], spans: list[tuple[int, int]], marker: re.Pattern[str]) -> str:
    """'allowed' / 'no-reason' / 'absent' for a `# ci-lint: <marker> <reason>` comment.

    `spans` are the 1-based inclusive line ranges the marker may live in — the
    step for allow-build, the job plus the workflow header for
    allow-schedule-main. A marker with no reason after it is deliberately NOT
    an exemption: the reason is what a reviewer reads.
    """
    state = "absent"
    for start, end in spans:
        for raw in lines[max(start, 1) - 1 : min(end, len(lines))]:
            match = marker.search(raw)
            if match is None:
                continue
            if match.group("reason").strip():
                return "allowed"
            state = "no-reason"
    return state


def _docker_build_findings(path: str, lines: list[str], doc: dict) -> list[Finding]:
    findings: list[Finding] = []
    for _job_name, job, job_start, job_end in _jobs(doc):
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        blocks = [s for s in steps if isinstance(s, dict)]
        if not blocks:
            continue
        starts = [s[_NODE_START] for s in blocks]
        naive = [
            (start, max(start, min(starts[i + 1] - 1 if i + 1 < len(starts) else job_end, len(lines))))
            for i, start in enumerate(starts)
        ]
        spans = _attach_leading_comments(lines, naive, job_start + 1)
        for index, step in enumerate(blocks):
            step_start, step_end = spans[index]
            allow = _marker_state(lines, [(step_start, step_end)], _ALLOW_BUILD_RE)
            if allow == "allowed":
                continue
            for key in ("run", "uses"):
                span = _value_range(step, key)
                if span is None:
                    continue
                first, last = span[0], max(span[0], min(span[1], step_end))
                for line_no in range(first, last + 1):
                    command = _build_command(lines[line_no - 1])
                    if command is None:
                        continue
                    findings.append(_docker_finding(path, line_no, command, allow))
                    break
    return findings


def _docker_finding(path: str, line_no: int, command: str, allow: str) -> Finding:
    if allow == "no-reason":
        message = (
            f"`{command}` is marked with `# {ALLOW_MARKER}` but no reason follows it. "
            "The reason is the whole point of the marker: it is what a reviewer reads. "
            f"See {WAIT_FOR_IMAGE_URL} (spec 5.5, wait-for-image)."
        )
    else:
        message = (
            f"`{command}` builds a container image in CI. Dokploy is the only builder on "
            "`main` and `dev`; CI waits for the image and pulls it by digest instead. "
            f"See {WAIT_FOR_IMAGE_URL} (spec 5.5, wait-for-image)."
        )
    fix = (
        "Replace the build with the `DevinoSolutions/.github/actions/wait-for-image` step, "
        "or, when this build genuinely has no registry copy (a PR-only leg, a test fixture "
        f"image), keep it and add a reason on the step:  # {ALLOW_MARKER} <reason>"
    )
    return Finding(file=path, line=line_no, rule=RULE_DOCKER_BUILD, message=message, fix=fix)


# ── rule 2: concurrency ────────────────────────────────────────────────────


def _triggers(doc: dict) -> tuple[dict[str, int], int | None]:
    """Map trigger name -> line, for every `on:` spelling. Second value is the `on:` line."""
    key = _on_key(doc)
    if key is None:
        return {}, None
    on_line = _key_line(doc, key)
    value = doc[key]
    found: dict[str, int] = {}
    if isinstance(value, str):
        found[value] = on_line or 1
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                found[item] = on_line or 1
    elif isinstance(value, dict):
        for name in _keys(value):
            if isinstance(name, str):
                found[name] = _key_line(value, name) or on_line or 1
    return found, on_line


def _concurrency_header_span(lines: list[str], doc: dict) -> tuple[int, int]:
    """Where a `# ci-lint: allow-missing-concurrency` comment may live.

    This rule is about the WHOLE workflow, not one job, so the marker belongs in
    the workflow header — everything above the first job. A file with no `jobs:`
    key never reaches this rule in practice, but the span is defined for it
    anyway rather than letting `_marker_spans` index an empty list.
    """
    jobs = _jobs(doc)
    if not jobs:
        return (1, len(lines))
    header_span, _job_spans = _marker_spans(lines, doc, jobs)
    return header_span


def _concurrency_findings(path: str, lines: list[str], doc: dict) -> list[Finding]:
    triggers, on_line = _triggers(doc)
    offending = [name for name in ("push", "pull_request") if name in triggers]
    if not offending or "concurrency" in doc:
        return []

    # The exemption exists because "no concurrency block" is sometimes the
    # CORRECT answer, not an oversight. A workflow whose jobs serialise on
    # external state can be actively harmed by a top-level group: with
    # `cancel-in-progress` a superseded run is killed mid-suite, and with it
    # false GitHub still keeps at most one PENDING run per group and lets a
    # newly queued run replace the one already waiting — so a third concurrent
    # PR loses its run and any `always()` status job reads that cancellation as
    # a failure. `superbooks/e2e.yml` is the case this was written for.
    #
    # Header-scoped, like `allow-schedule-main`'s workflow-level half, and a
    # marker with no reason after it is not an exemption.
    exemption = _marker_state(lines, [_concurrency_header_span(lines, doc)], _ALLOW_CONCURRENCY_RE)
    if exemption == "allowed":
        return []

    line = triggers[offending[0]]
    anchors = tuple(sorted({line, *(triggers[n] for n in offending), *( (on_line,) if on_line else () )}))
    no_reason = (
        f"This workflow is marked with `# {WRITTEN_CONCURRENCY_MARKER}` but no reason follows it, "
        "so the exemption does not apply. "
        if exemption == "no-reason"
        else ""
    )
    message = (
        no_reason
        + f"This workflow triggers on `{'` and `'.join(offending)}` but has no top-level "
        "`concurrency:` block, so a second push to the same branch queues behind the first "
        "instead of superseding it. On a saturated self-hosted pool that is the single "
        "largest source of wasted job-minutes. Add:\n\n" + CONCURRENCY_SNIPPET
    )
    fix = (
        "Add the block above at the top level of the workflow. Deploy-path workflows on "
        "`main`/`dev` keep `cancel-in-progress: false`; the expression above already does "
        "that for those two branches and cancels everywhere else.\n\n"
        "If this workflow's jobs serialise on external state — a single-use credential, a "
        "shared sandbox, anything a killed or replaced run can leave half-written — then NO "
        "top-level group is the right answer and a wait-based mutex belongs in the job "
        "instead. Say so in the workflow header:  "
        f"# {WRITTEN_CONCURRENCY_MARKER} <reason>"
    )
    return [Finding(file=path, line=line, rule=RULE_CONCURRENCY, message=message, fix=fix, anchors=anchors)]


# ── rule 3: nightly label ──────────────────────────────────────────────────


def _jobs(doc: dict) -> list[tuple[str, dict, int, int]]:
    """(name, job, first line, last line) for each job, bounded by the next job."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    names = [n for n in _keys(jobs) if isinstance(jobs.get(n), dict)]
    starts = [_key_line(jobs, n) or jobs[n][_NODE_START] for n in names]
    doc_end = doc.get(_NODE_END, 10**9)
    out = []
    for index, name in enumerate(names):
        end = starts[index + 1] - 1 if index + 1 < len(names) else doc_end
        out.append((name, jobs[name], starts[index], end))
    return out


# An event-conditional runs-on is compliant when the branch taken on a
# schedule event is the nightly label:
#
#   runs-on: ${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
#
# That shape is how a workflow with both a schedule and an event trigger keeps
# its PR leg on the main pool while its nightly leg lands on the deprioritised
# set. `A && B || C` is GitHub's ternary: A true yields B, otherwise C. Only
# this one shape is resolved — anything else is reported rather than guessed
# at, because a lint that quietly accepts an expression it did not understand
# is worse than one that asks.

_EXPRESSION_RE = re.compile(r"^\$\{\{(?P<body>.*)\}\}$", re.DOTALL)
_TERNARY_RE = re.compile(r"^(?P<cond>.+?)\s*&&\s*(?P<yes>'[^']*'|\"[^\"]*\")\s*\|\|\s*(?P<no>'[^']*'|\"[^\"]*\")$")
_EVENT_NAME_RE = re.compile(
    r"""github\.event_name\s*(?P<op>==|!=)\s*(?P<quote>['"])(?P<value>[^'"]*)(?P=quote)"""
)
_EVENT_NAME_REVERSED_RE = re.compile(
    r"""(?P<quote>['"])(?P<value>[^'"]*)(?P=quote)\s*(?P<op>==|!=)\s*github\.event_name"""
)


def is_expression(value) -> bool:
    return isinstance(value, str) and _EXPRESSION_RE.match(value.strip()) is not None


def schedule_branch_label(value: str) -> str | None:
    """The label an event-conditional `runs-on` picks on a schedule event, if resolvable."""
    match = _EXPRESSION_RE.match(value.strip())
    if match is None:
        return None
    body = " ".join(match.group("body").split())
    ternary = _TERNARY_RE.match(body)
    if ternary is None:
        return None
    condition = ternary.group("cond")
    event = _EVENT_NAME_RE.search(condition) or _EVENT_NAME_REVERSED_RE.search(condition)
    if event is None or event.group("value") != "schedule":
        return None
    chosen = ternary.group("yes") if event.group("op") == "==" else ternary.group("no")
    return chosen[1:-1]


def _marker_spans(lines: list[str], doc: dict, jobs: list) -> tuple[tuple[int, int], list[tuple[int, int]]]:
    """Where a job-scoped marker may live.

    A comment block directly above a job belongs to that job; everything left
    above the first job is the workflow header, where a marker covers every job
    in the file.
    """
    job_spans = _attach_leading_comments(
        lines,
        [(start, min(end, len(lines))) for _, _, start, end in jobs],
        (_key_line(doc, "jobs") or 0) + 1,
    )
    return (1, job_spans[0][0] - 1), job_spans


def _runs_on_ok(runs_on) -> bool:
    if isinstance(runs_on, str):
        if is_expression(runs_on):
            return schedule_branch_label(runs_on) == NIGHTLY_LABEL
        return runs_on.strip() == NIGHTLY_LABEL
    if isinstance(runs_on, list):
        return any(isinstance(item, str) and item.strip() == NIGHTLY_LABEL for item in runs_on)
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            return labels.strip() == NIGHTLY_LABEL
        if isinstance(labels, list):
            return any(isinstance(item, str) and item.strip() == NIGHTLY_LABEL for item in labels)
    return False


_NIGHTLY_EXEMPTION_HINT = (
    "If this job genuinely has to stay on the main pool — a short-interval liveness probe "
    "whose latency is the point, say — exempt it with a comment on the job or in the "
    f"workflow header:  # {WRITTEN_SCHEDULE_MARKER} <reason>"
)


def _nightly_findings(path: str, lines: list[str], doc: dict) -> list[Finding]:
    triggers, _ = _triggers(doc)
    if "schedule" not in triggers:
        return []
    schedule_line = triggers["schedule"]
    jobs = _jobs(doc)
    if not jobs:
        return []
    header_span, job_spans = _marker_spans(lines, doc, jobs)
    findings: list[Finding] = []
    for index, (name, job, job_start, _job_end) in enumerate(jobs):
        exemption = _marker_state(lines, [header_span, job_spans[index]], _ALLOW_SCHEDULE_RE)
        if exemption == "allowed":
            continue
        uses = job.get("uses")
        if isinstance(uses, str):
            with_block = job.get("with")
            runner = with_block.get("runner") if isinstance(with_block, dict) else None
            if isinstance(runner, str) and runner.strip() == NIGHTLY_LABEL:
                continue
            line = (
                _key_line(with_block, "runner")
                if isinstance(with_block, dict) and "runner" in with_block
                else _key_line(job, "uses")
            ) or job_start
            message = (
                f"Job `{name}` runs on a schedule through a reusable workflow but does not pass "
                f"`runner: {NIGHTLY_LABEL}`. Nightlies run on the deprioritised scale set so a "
                "sweep can never take pods away from a push or PR run (spec 4.4)."
            )
            fix = (
                f"In this job's `with:` block, set  runner: {NIGHTLY_LABEL}\n\n"
                + _NIGHTLY_EXEMPTION_HINT
            )
        else:
            if "runs-on" not in job:
                continue
            if _runs_on_ok(job.get("runs-on")):
                continue
            runs_on = job.get("runs-on")
            line = _key_line(job, "runs-on") or job_start
            message = (
                f"Job `{name}` belongs to a scheduled workflow but runs on "
                f"`{_render(runs_on)}` instead of `{NIGHTLY_LABEL}`. {_why_not(runs_on)}The "
                "nightly scale set is capped at four runners with a low PriorityClass, so a "
                "nightly can never starve an event-triggered run (spec 4.4)."
            )
            fix = (
                f"runs-on: {NIGHTLY_LABEL}\n\n"
                "or, when the same workflow also serves an event trigger, the event-conditional "
                f"form:  runs-on: ${{{{ github.event_name == 'schedule' && '{NIGHTLY_LABEL}' || "
                "'ubuntu-devino' }}\n\n" + _NIGHTLY_EXEMPTION_HINT
            )
        if exemption == "no-reason":
            message = (
                f"Job `{name}` is marked with `# {WRITTEN_SCHEDULE_MARKER}` but no reason follows "
                "it, so the exemption does not apply. The reason is the whole point of the "
                f"marker: it is what a reviewer reads. {message}"
            )
        findings.append(
            Finding(
                file=path,
                line=line,
                rule=RULE_NIGHTLY_RUNNER,
                message=message,
                fix=fix,
                anchors=tuple(sorted({line, job_start, schedule_line})),
            )
        )
    return findings

# ── bun engine order ───────────────────────────────────────────────────────
#
# Bun resolves `node` to itself for the child processes a script spawns under
# `bun run` when no real `node` is on PATH. The self-hosted pool shipped NO node
# until the v1 runner image landed on 2026-09-11 at 14:56Z, so on that pool every
# such child ran on JavaScriptCore before that moment and runs on V8 after it —
# an engine change underneath the job with no commit to point at. V8-only flags,
# heap limits and engine behaviour all moved with it: `superbooks`' type-aware
# lint started OOM-ing at a `--max-old-space-size=6144` cap that JSC had silently
# ignored for the cap's whole life, on five unrelated branches at once.
#
# `actions/setup-node` installs a real node on either image, so a job that runs it
# BEFORE its first bun command never changed engine and never will. That is the
# whole rule — and it is about ORDER, not presence. `superbooks`' casualty does
# call `actions/setup-node`; it calls it seven lines AFTER the step that broke,
# which is worth more than any amount of "we remembered to add setup-node".
#
# Scope is deliberately narrow: only jobs on the self-hosted pool, because only
# that pool ever lacked a system node. GitHub-hosted images have always shipped
# one, so the same workflow on `ubuntu-latest` is not at risk and is not flagged.

RULE_BUN_ENGINE_ORDER = "bun-engine-order"
DEVINO_POOL_PREFIX = "ubuntu-devino"

_SETUP_NODE_RE = re.compile(r"^actions/setup-node@")
_SETUP_BUN_RE = re.compile(r"^oven-sh/setup-bun@")
# A `run:` block is shell, so "does this job run bun" is a question about COMMAND
# POSITION, not about whether the three letters appear. Matching the word alone
# reports `ubuntu-devino`, `bundle exec`, `echo "use bun"` — and, found by running
# this rule over the whole org before shipping it, three superbooks jobs whose
# path-filter strings contain `bun\.lock`. So the body is split on shell command
# separators and only the first word of each segment counts, after stepping over
# leading `VAR=value` assignments and the handful of words that can precede a
# command (`sudo`, `then`, `do`, ...).
# `)` is deliberately NOT a separator: it closes a subshell, and in a `case` it
# terminates a pattern label. Treating it as one turns the label in
# `bun) DEF_INSTALL="bun install ..."` into a bare `bun` and reports a job that
# only ASSIGNS a string. `(` still separates, so `$(bun --version)` still counts.
_SHELL_SPLIT_RE = re.compile(r"[\n;&|(`]+")
_COMMAND_PREFIXES = frozenset({"sudo", "then", "else", "elif", "do", "time", "exec", "nohup", "!"})
_BUN_COMMANDS = frozenset({"bun", "bunx"})
# Quoted text is data. Stripped BEFORE the split, because a separator inside a
# message ("must be pnpm|bun|npm") otherwise manufactures a segment whose only
# word is `bun`. Stripped before comments too, so a `#` inside a string cannot
# truncate the line.
_QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.DOTALL)


def _runs_bun(run: str) -> bool:
    """True when a `run:` body invokes bun as a command."""
    unquoted = _QUOTED_RE.sub(" ", run)
    payload = "\n".join(_strip_comment(line) for line in unquoted.splitlines())
    for segment in _SHELL_SPLIT_RE.split(payload):
        words = segment.split()
        index = 0
        # a `case` pattern label — `bun)`, `pnpm|bun)` — is not a command, but the
        # command for that branch follows it on the same line.
        if index < len(words) and words[index].endswith(")"):
            index += 1
        while index < len(words) and (words[index] in _COMMAND_PREFIXES or "=" in words[index]):
            index += 1
        if index < len(words) and words[index] in _BUN_COMMANDS:
            return True
    return False

_BUN_ENGINE_EXEMPTION_HINT = (
    "If this job wants Bun's own runtime for its children — a `bun test` suite, or code that "
    "depends on JavaScriptCore — say so on the job or in the workflow header:  "
    f"# {WRITTEN_BUN_ENGINE_MARKER} <reason>  and pin it properly with `[run] bun = true` in "
    "bunfig.toml, so the choice survives a runner-image change instead of depending on one."
)


def _expression_labels(value: str) -> list[str]:
    """Both branches of the one expression shape this linter resolves, else []."""
    outer = _EXPRESSION_RE.match(value.strip())
    if outer is None:
        return []
    ternary = _TERNARY_RE.match(outer.group("body").strip())
    if ternary is None:
        return []
    return [ternary.group("yes").strip("'\""), ternary.group("no").strip("'\"")]


def _runner_labels(runs_on) -> list[str]:
    """Every literal label a `runs-on:` can resolve to; [] when it cannot be resolved.

    An empty list means "unknown", and an unknown runner is NOT reported. A lint
    that guesses at an expression it did not parse is worse than one that stays
    quiet — the same reasoning `_runs_on_ok` applies to the nightly rule.
    """
    if isinstance(runs_on, str):
        if is_expression(runs_on):
            return _expression_labels(runs_on)
        return [runs_on.strip()]
    if isinstance(runs_on, list):
        return [item.strip() for item in runs_on if isinstance(item, str)]
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels")
        if isinstance(labels, str):
            return [labels.strip()]
        if isinstance(labels, list):
            return [item.strip() for item in labels if isinstance(item, str)]
    return []


_INPUT_REF_RE = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")


def _workflow_call_inputs(doc: dict) -> dict:
    """`on.workflow_call.inputs` as a plain mapping, or {}."""
    on_key = _on_key(doc)
    if on_key is None:
        return {}
    on_block = doc.get(on_key)
    if not isinstance(on_block, dict):
        return {}
    call = on_block.get("workflow_call")
    if not isinstance(call, dict):
        return {}
    inputs = call.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _input_runner_name(runs_on) -> str | None:
    """The input name behind `runs-on: ${{ inputs.<name> }}`, else None."""
    if not isinstance(runs_on, str):
        return None
    match = _INPUT_REF_RE.match(runs_on.strip())
    return match.group(1) if match else None


def _pool_capable(runs_on, doc: dict, caller_values: dict | None) -> bool:
    """Can this job land on the self-hosted pool?

    A reusable workflow's `runs-on` is usually `${{ inputs.runner }}`, which no
    amount of reading THIS file resolves — and a rule that stays quiet there has
    a hole exactly where the org's shared lanes live, since every caller of a
    `.github` reusable inherits its step order. Two things close it:

      1. the input's own `default:`, read from `on.workflow_call.inputs`; and
      2. what callers actually pass, supplied by the caller as `caller_values`
         ({input name: [values seen]}) because it is cross-repository knowledge
         this process cannot discover on its own.

    Either one naming a `ubuntu-devino*` label makes the job pool-capable. An
    input with a GitHub-hosted default and no pool-passing caller is not.
    """
    if any(label.startswith(DEVINO_POOL_PREFIX) for label in _runner_labels(runs_on)):
        return True
    name = _input_runner_name(runs_on)
    if name is None:
        return False
    spec = _workflow_call_inputs(doc).get(name)
    if isinstance(spec, dict):
        default = spec.get("default")
        if any(label.startswith(DEVINO_POOL_PREFIX) for label in _runner_labels(default)):
            return True
    for value in (caller_values or {}).get(name, ()):
        if isinstance(value, str) and any(
            label.startswith(DEVINO_POOL_PREFIX) for label in _runner_labels(value)
        ):
            return True
    return False


def _first_bun_and_node(steps: list) -> tuple[int | None, int | None, bool]:
    """(first bun-command line, first setup-node line, saw oven-sh/setup-bun)."""
    first_bun: int | None = None
    first_node: int | None = None
    saw_setup_bun = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        line = step.get(_NODE_START)
        uses = step.get("uses")
        if isinstance(uses, str):
            spelled = uses.strip()
            if _SETUP_NODE_RE.match(spelled) and first_node is None:
                first_node = line
            if _SETUP_BUN_RE.match(spelled):
                saw_setup_bun = True
        run = step.get("run")
        if isinstance(run, str) and first_bun is None and _runs_bun(run):
            first_bun = line
    return first_bun, first_node, saw_setup_bun


def _bun_engine_findings(
    path: str, lines: list[str], doc: dict, caller_values: dict | None = None
) -> list[Finding]:
    jobs = _jobs(doc)
    if not jobs:
        return []
    header_span, job_spans = _marker_spans(lines, doc, jobs)
    findings: list[Finding] = []
    for index, (name, job, job_start, _job_end) in enumerate(jobs):
        if not _pool_capable(job.get("runs-on"), doc, caller_values):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        first_bun, first_node, saw_setup_bun = _first_bun_and_node(steps)
        if first_bun is None:
            # `oven-sh/setup-bun` with no bun command runs nothing, so there is no
            # engine to get wrong and nothing to order. Reported by no rule.
            continue
        if first_node is not None and first_node < first_bun:
            continue
        exemption = _marker_state(lines, [header_span, job_spans[index]], _ALLOW_BUN_ENGINE_RE)
        if exemption == "allowed":
            continue
        where = (
            f"calls `actions/setup-node` only at line {first_node}, after it"
            if first_node is not None
            else "never calls `actions/setup-node`"
        )
        via_input = _input_runner_name(job.get("runs-on"))
        how = (
            f"can land on the self-hosted pool (`runs-on: ${{{{ inputs.{via_input} }}}}`) and"
            if via_input
            else "runs on the self-hosted pool and"
        )
        message = (
            f"Job `{name}` {how} runs bun at line {first_bun}, but "
            f"{where}. The pool had no system node before the v1 image (2026-09-11 14:56Z) and "
            "has one now, and Bun aliases `node` to itself for the children a script spawns when "
            "no node is on PATH — so this job's engine is decided by the runner image rather "
            "than by this file, and it changed once already without a commit."
        )
        fix = (
            "Put a real node on PATH before the first bun step:\n\n"
            "      - uses: actions/setup-node@v4\n"
            "        with:\n"
            "          node-version: 22\n\n"
            "It goes before the first `bun`/`bunx` step; `oven-sh/setup-bun` may stay where it "
            "is.\n\n" + _BUN_ENGINE_EXEMPTION_HINT
        )
        if exemption == "no-reason":
            message = (
                f"Job `{name}` is marked with `# {WRITTEN_BUN_ENGINE_MARKER}` but no reason "
                "follows it, so the exemption does not apply. The reason is the whole point of "
                f"the marker: it is what a reviewer reads. {message}"
            )
        anchor = _key_line(job, "runs-on") or job_start
        findings.append(
            Finding(
                file=path,
                line=first_bun,
                rule=RULE_BUN_ENGINE_ORDER,
                message=message,
                fix=fix,
                anchors=tuple(sorted({first_bun, job_start, anchor})),
            )
        )
    return findings


# ── pnpm store cache ───────────────────────────────────────────────────────
#
# Every ARC scale set mounts the node's hostPath `/var/cache/ci` into its runner
# pods and points pnpm at it (`npm_config_store_dir=/var/cache/ci/pnpm-store`).
# So every concurrent job on a node shares ONE pnpm store, and it is already warm.
#
# `actions/setup-node` with `cache: pnpm` asks `pnpm store path`, gets that shared
# directory, and on a hit extracts a ~2 GB tarball over it while sibling pods are
# hard-linking out of the same files. A concurrent `pnpm install` then dies with
#
#   ERR_PNPM_ENOENT  ENOENT: no such file or directory, copyfile
#   '/var/cache/ci/pnpm-store/v10/files/...'
#
# Diagnosed 2026-11-10 on Shorty `dev` (fixed in Shorty #1173). Nothing else in
# the cluster writes to the store; the only writer that races is this restore.
# On the pool the cache buys nothing — the store it restores into is already
# there — so the fix is to drop it, not to tune it.
#
# GitHub-hosted runners have a private, empty store per job, so `cache: pnpm` is
# correct there and is not flagged.
#
# The implicit form. setup-node v5 turned caching ON by default
# (`package-manager-cache`, default true) for whichever of npm/yarn/pnpm the root
# package.json's `packageManager` names. v6.0.0 narrowed that to npm only
# (actions/setup-node#1374, "Limit automatic caching to npm"), and v7 keeps the
# npm-only rule; v6+ also reads `devEngines.packageManager`. So a v5 step with no
# `cache:` input still restores the pnpm store when `packageManager` is `pnpm@...`
# (and the npm cache when it is `npm@...`), v6 and v7 restore the npm cache for npm,
# and v4 and below do nothing. All of it read from the action's own source.
#
# What this rule can NOT do. It runs on pull requests, against the files and lines
# the PR touches. It does not stop a stale branch or a default branch that already
# has the pattern, and it cannot stop the corrupting job from running. Detection of
# the damage is the host watchdog's job (ci-store-watchdog on each node).

RULE_PNPM_STORE_CACHE = "pnpm-store-cache"
ALLOW_PNPM_CACHE_MARKER = "allow-pnpm-cache"
WRITTEN_PNPM_CACHE_MARKER = f"ci-lint: {ALLOW_PNPM_CACHE_MARKER}"
_ALLOW_PNPM_CACHE_RE = _marker_re(ALLOW_PNPM_CACHE_MARKER)

_SETUP_NODE_REF_RE = re.compile(r"^actions/setup-node@(?P<ref>\S+)$", re.I)
_MAJOR_RE = re.compile(r"^v?(?P<major>\d+)(?:\.\d+)*$")
_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# A sha-pinned `uses:` carries its version as a trailing comment, which YAML drops,
# so it is read from the raw line: `uses: actions/setup-node@<sha> # v5.0.0`.
_PIN_COMMENT_RE = re.compile(r"#\s*v?(?P<major>\d+)(?:\.\d+)*\b")


def _setup_node_major(uses: str, raw_line: str) -> int | None:
    """setup-node's major version, from the ref or a sha pin's comment; None if unknown."""
    match = _SETUP_NODE_REF_RE.match(uses.strip())
    if match is None:
        return None
    ref = match.group("ref")
    version = _MAJOR_RE.match(ref)
    if version is not None:
        return int(version.group("major"))
    if _FULL_SHA_RE.match(ref):
        comment = raw_line.find("#")
        pinned = _PIN_COMMENT_RE.match(raw_line[comment:]) if comment >= 0 else None
        if pinned is not None:
            return int(pinned.group("major"))
    return None


def _cache_names_pnpm(value) -> bool:
    """True when a `cache:` input is pnpm, literally or on a branch of the one resolvable ternary."""
    if not isinstance(value, str):
        return False
    if is_expression(value):
        return any(label.strip().lower() == "pnpm" for label in _expression_labels(value))
    return value.strip().lower() == "pnpm"


def _cache_names_npm(value) -> bool:
    """True when a `cache:` input is npm: `npm config get cache` answers /var/cache/ci/npm on the pool."""
    if not isinstance(value, str):
        return False
    if is_expression(value):
        return any(label.strip().lower() == "npm" for label in _expression_labels(value))
    return value.strip().lower() == "npm"


def _truthy_input(value) -> bool:
    """A boolean action input spelled the ways GitHub accepts; an unresolved expression is not guessed at."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str) and not is_expression(value):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


# An `actions/cache` step whose `path:` is the node's shared package store. Same hazard as
# setup-node's `cache:`, spelled by hand: a save uploads the whole shared store, a restore
# extracts it over files sibling pods are hard-linking out of. On 2026-11-11 such a restore
# rewrote ~340k files in the forth store and left six CAS names holding another file's bytes.
# GitHub resolves `owner/repo` case-insensitively, and the cache forks people reach for
# (useblacksmith/cache, runs-on/cache, buildjet/cache) are all `<owner>/cache[/restore|/save]`.
_ACTIONS_CACHE_RE = re.compile(r"^[^@\s]+/cache(?:/(?:restore|save))?@", re.I)
_SETUP_NODE_CACHE_RE = re.compile(r"^actions/setup-node@", re.I)
_PNPM_ACTION_SETUP_RE = re.compile(r"^pnpm/action-setup@", re.I)
# The shared hostPath. A workspace-local `.pnpm-store` or `~/.local/share/pnpm` is not shared on the pool.
_SHARED_ROOT_RE = re.compile(r"/var/cache/ci", re.I)
# The env vars pnpm/npm read their store from. The pool's pods set them to the shared hostPath,
# so a workflow that does not override them reads the shared store through them.
_STORE_ENV_NAMES = frozenset({"npm_config_store_dir", "npm_config_cache"})
# A command whose answer IS the shared store on the pool (the env points pnpm/npm at it). Flags may
# sit between the tool and the subcommand (`pnpm -s store path`, `npm --silent get cache`).
_STORE_QUERY_RE = re.compile(
    r"\bpnpm\b(?:\s+-\S+)*\s+(?:store\s+path|(?:config\s+)?get\s+store-dir)\b"
    r"|\bnpm\b(?:\s+-\S+)*\s+(?:config\s+get|get)\s+cache\b"
)
# A command that points pnpm/npm at some other directory for the rest of the job.
_STORE_RELOCATE_RE = re.compile(
    r"\bpnpm\b(?:\s+-\S+)*\s+config\s+set\s+store-dir[ =]+(?P<a>\S+)"
    r"|\bnpm\b(?:\s+-\S+)*\s+config\s+set\s+cache[ =]+(?P<b>\S+)"
    r"|\bnpm_config_(?:store_dir|cache)=(?P<c>\S+)",
    re.I,
)
# steps.s.outputs.x, steps.s.outputs['x'], steps['s'].outputs.x
_STEP_OUTPUT_REF_RE = re.compile(
    r"""steps(?:\.(?P<dot>[A-Za-z0-9_-]+)|\[\s*['"](?P<br>[A-Za-z0-9_-]+)['"]\s*\])\s*\.\s*outputs\b"""
)
# env.X, env['X'], $X, ${X}
_ENV_NAME_REF_RE = re.compile(
    r"""(?:\benv\.|\$\{?)(?P<dot>[A-Za-z_][A-Za-z0-9_]*)|\benv\[\s*['"](?P<br>[A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""
)
_ASSIGNED_NAME_RE = re.compile(r"""(?m)(?:^|[\s;"'])([A-Za-z_][A-Za-z0-9_]*)=""")
_OUTPUT_SINK_RE = re.compile(r"GITHUB_OUTPUT|::set-output")
_INPUT_EXPR_RE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}")
_BLOCK_SCALAR_RE = re.compile(r"[|>][+-]?\d*\s*(?:#.*)?$")


def _refs(pattern: re.Pattern[str], text: str) -> list[str]:
    return [m.group("dot") or m.group("br") for m in pattern.finditer(text)]


def _run_lines(run: str) -> list[str]:
    """A `run:` body as logical lines: backslash continuations joined."""
    return run.replace("\\\n", " ").splitlines()


def _env_scope_values(*scopes) -> dict[str, str]:
    """Env name -> value across workflow/job/step scopes, the most specific scope winning."""
    values: dict[str, str] = {}
    for scope in scopes:
        if isinstance(scope, dict):
            for key, value in scope.items():
                if key in _META:
                    continue
                values[str(key)] = value if isinstance(value, str) else str(value)
    return values


class _StoreFlow:
    """Which names and step ids carry the shared store path, as of the step being judged."""

    def __init__(self, earlier: list, scopes: tuple) -> None:
        self.names: set[str] = set()
        self.ids: set[str] = set()
        self.relocated = False
        env = _env_scope_values(*scopes)
        # A pod-provided store var overridden to something outside /var/cache/ci is not the shared store.
        self.local_vars = {
            key.lower()
            for key, value in env.items()
            if key.lower() in _STORE_ENV_NAMES and not _SHARED_ROOT_RE.search(value)
        }
        self.relocated = bool(self.local_vars)
        self._settle_env(env)
        for prior in earlier:
            if isinstance(prior, dict):
                self._take_run(prior)
        self._settle_env(env)

    def _settle_env(self, env: dict[str, str]) -> None:
        """Env values can name a shared literal, a tainted step output or another tainted name;
        a few passes settle chains like STORE -> CACHE_DIR."""
        for _ in range(3):
            before = len(self.names)
            for key, value in env.items():
                if key.lower() not in self.local_vars and self._value_is_shared(value):
                    self.names.add(key)
            if before == len(self.names):
                break

    def _value_is_shared(self, text: str) -> bool:
        if _SHARED_ROOT_RE.search(text):
            return True
        if any(ref in self.ids for ref in _refs(_STEP_OUTPUT_REF_RE, text)):
            return True
        for ref in _refs(_ENV_NAME_REF_RE, text):
            if ref in self.names:
                return True
            if ref.lower() in _STORE_ENV_NAMES and ref.lower() not in self.local_vars:
                return True
        return False

    def _take_run(self, prior: dict) -> None:
        run = prior.get("run")
        if not isinstance(run, str):
            return
        for line in _run_lines(run):
            for move in _STORE_RELOCATE_RE.finditer(line):
                if not _SHARED_ROOT_RE.search(move.group("a") or move.group("b") or move.group("c") or ""):
                    self.relocated = True
            queried = _STORE_QUERY_RE.search(line) is not None and not self.relocated
            carried = self._value_is_shared(line)
            if not (queried or carried):
                continue
            # Only what this very line assigns from the store path carries it; a variable set on
            # another line of the same step (CYPRESS_CACHE=...) does not.
            self.names.update(_ASSIGNED_NAME_RE.findall(line))
            if isinstance(prior.get("id"), str) and _OUTPUT_SINK_RE.search(line):
                self.ids.add(prior["id"])

    def line_cause(self, text: str) -> str | None:
        """Why one line of an `actions/cache` path is the shared store, or None."""
        literal = _SHARED_ROOT_RE.search(text)
        if literal is not None:
            return f"names the shared store (`{literal.group(0)}`)"
        for ref in _refs(_STEP_OUTPUT_REF_RE, text):
            if ref in self.ids:
                return f"is the output of step `{ref}`, which reads the store path (`pnpm store path` / `npm config get cache`)"
        for ref in _refs(_ENV_NAME_REF_RE, text):
            if ref in self.names:
                return f"is `${ref}`, which is set from the shared store path"
            if ref.lower() in _STORE_ENV_NAMES and ref.lower() not in self.local_vars:
                return f"is `${ref}`, which the pool points at the shared store"
        return None


def _store_cache_step_cause(
    step: dict, earlier: list, doc: dict, job: dict, lines: list[str] | None = None
) -> tuple[int | None, str] | None:
    """(line, why) when this `actions/cache` step's `path:` is the shared store, else None.

    `line` is the offending line of a multi-line `path:`, not the first one."""
    inputs = step.get("with") if isinstance(step.get("with"), dict) else {}
    path = inputs.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    flow = _StoreFlow(earlier, (doc.get("env"), job.get("env"), step.get("env")))
    key_line = _key_line(inputs, "path")
    block = (
        key_line is not None
        and lines is not None
        and key_line <= len(lines)
        and _BLOCK_SCALAR_RE.search(lines[key_line - 1].strip()) is not None
    )
    for index, raw in enumerate(path.splitlines()):
        text = raw.strip()
        if not text or text.startswith(("!", "#")):
            continue
        why = flow.line_cause(text)
        if why is not None:
            line = None if key_line is None else key_line + (1 + index if block else 0)
            return line, f"caches `path: {text}`, which {why}"
    return None


def _auto_cache_enabled(value) -> bool:
    """setup-node reads `(input || 'true').toUpperCase() === 'TRUE'`; an expression is unknown."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if is_expression(text):
        return False
    return text.upper() in ("", "TRUE")


def _root_package_json(repo_root: Path | None) -> dict | None:
    """The root package.json, which is all setup-node's auto-caching reads."""
    if repo_root is None:
        return None
    try:
        data = json.loads((Path(repo_root) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_NPM_MANAGER_RE = re.compile(r"^\^?npm(?:@.*)?$")


def _auto_cached_manager(major: int, package: dict | None) -> str | None:
    """'npm' or 'pnpm' when setup-node `major` auto-caches it for this package.json, else None.

    Read from the action's own source. v5 matched `^\\^?(npm|yarn|pnpm)@` on `packageManager`. v6
    (actions/setup-node#1374) and v7 narrowed it to npm: `packageManager` of `npm`, `npm@...` or
    `^npm@...`, or a `devEngines.packageManager` entry (object or array) named the same way.
    """
    if not isinstance(package, dict) or major < 5:
        return None
    top = package.get("packageManager")
    if major == 5:
        match = re.match(r"^\^?(npm|yarn|pnpm)@", top) if isinstance(top, str) else None
        return match.group(1) if match and match.group(1) != "yarn" else None
    dev = package.get("devEngines")
    dev = dev.get("packageManager") if isinstance(dev, dict) else None
    entries = dev if isinstance(dev, list) else ([dev] if dev else [])
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and _NPM_MANAGER_RE.match(entry["name"]):
            return "npm"
    return "npm" if isinstance(top, str) and _NPM_MANAGER_RE.match(top) else None


_PNPM_CACHE_EXEMPTION_HINT = (
    "If this job really needs its own restore — a runner whose store is NOT the shared "
    "hostPath — say so on the job or in the workflow header:  "
    f"# {WRITTEN_PNPM_CACHE_MARKER} <reason>"
)

_FIX_PNPM = (
    "Delete `cache: pnpm` (and its `cache-dependency-path:`) from this step. The "
    "pool's pnpm store is already warm at `/var/cache/ci/pnpm-store`; "
    "`pnpm install --frozen-lockfile` reads it directly."
)
_FIX_NPM = (
    "Delete `cache: npm` (and its `cache-dependency-path:`) from this step. The "
    "pool's npm cache is already warm at `/var/cache/ci/npm` "
    "(`npm_config_cache`); `npm ci` reads it directly."
)
_FIX_HAND = (
    "Delete this `actions/cache` step. The pool's store is already warm at "
    "`/var/cache/ci/pnpm-store` (npm: `/var/cache/ci/npm`); `pnpm install "
    "--frozen-lockfile` reads it directly."
)
_FIX_AUTO = "Add `package-manager-cache: false` under this step's `with:`."


class _Hit(NamedTuple):
    line: int | None  # in the numbering of the file the step lives in
    owner: str
    cause: str
    fix: str
    shared: str  # "pnpm store" | "npm cache" | "package store"


class _R6Context:
    """What the step judge needs beyond the step: package.json (read once) and the repo root."""

    def __init__(self, repo_root: Path | None) -> None:
        self.repo_root = repo_root
        self._package: dict | None = None
        self._read = False

    def package(self) -> dict | None:
        if not self._read:
            self._package = _root_package_json(self.repo_root)
            self._read = True
        return self._package


def _step_hit(
    step: dict, earlier: list, doc: dict, job: dict, lines: list[str], ctx: _R6Context, *, direct: bool = False
) -> _Hit | None:
    """Does this one step put the shared store through a cache restore/save?

    `direct` is a composite action judged on its own: nothing is known about the runner, so only
    what is wrong on ANY runner counts (a literal /var/cache/ci path, or a `cache:` forwarded from
    an input whose default names pnpm/npm)."""
    uses = step.get("uses")
    if not isinstance(uses, str):
        return None
    uses = uses.strip()
    inputs = step.get("with") if isinstance(step.get("with"), dict) else {}
    uses_line = _key_line(step, "uses") or step.get(_NODE_START)
    if _ACTIONS_CACHE_RE.match(uses):
        found = _store_cache_step_cause(step, earlier, doc, job, lines)
        if found is None:
            return None
        line, cause = found
        if direct and not _SHARED_ROOT_RE.search(cause):
            return None
        return _Hit(line or _key_line(inputs, "path") or uses_line, "`actions/cache` step", cause, _FIX_HAND, "package store")
    if direct:
        return None
    if _PNPM_ACTION_SETUP_RE.match(uses):
        if "cache" not in inputs or not _truthy_input(inputs.get("cache")):
            return None
        return _Hit(
            _key_line(inputs, "cache") or uses_line,
            "`pnpm/action-setup` step",
            f"sets `cache: {inputs.get('cache')}`",
            "Delete `cache:` from this `pnpm/action-setup` step (and its `cache_dependency_path`). "
            "The pool's pnpm store is already warm at `/var/cache/ci/pnpm-store`; "
            "`pnpm install --frozen-lockfile` reads it directly.",
            "pnpm store",
        )
    if not _SETUP_NODE_CACHE_RE.match(uses):
        return None
    owner = "`actions/setup-node` step"
    cache = inputs.get("cache")
    if _cache_names_pnpm(cache):
        return _Hit(_key_line(inputs, "cache") or uses_line, owner, f"sets `cache: {cache}`", _FIX_PNPM, "pnpm store")
    if _cache_names_npm(cache):
        return _Hit(_key_line(inputs, "cache") or uses_line, owner, f"sets `cache: {cache}`", _FIX_NPM, "npm cache")
    if isinstance(cache, str) and cache.strip():
        return None
    raw = lines[uses_line - 1] if uses_line and uses_line <= len(lines) else ""
    major = _setup_node_major(uses, raw)
    if major is None or major < 5 or not _auto_cache_enabled(inputs.get("package-manager-cache")):
        return None
    manager = _auto_cached_manager(major, ctx.package())
    if manager is None:
        return None
    declared = (ctx.package() or {}).get("packageManager")
    source = f"`packageManager: {declared}`" if isinstance(declared, str) else "`devEngines.packageManager`"
    cause = (
        f"uses setup-node v{major} with no `cache:` and `package-manager-cache` left on, and "
        f"package.json says {source} — v{major} auto-caches {manager} in that case"
    )
    fix = _FIX_AUTO
    if major == 5 and manager == "pnpm":
        cause += " (v6 narrowed auto-caching to npm)"
        fix = "Add `package-manager-cache: false` under this step's `with:`, or move to `actions/setup-node@v6` or later, which only auto-caches npm."
    return _Hit(uses_line, owner, cause, fix, "pnpm store" if manager == "pnpm" else "npm cache")


# ── composite actions ─────────────────────────────────────────────────────
#
# A pool job often reaches setup-node through a local composite
# (`uses: ./.github/actions/setup`), where `cache:` is written in action.yml and the
# workflow file the PR touches shows only the call. The call is therefore followed: the
# composite's steps are judged as if they sat in the job, with `${{ inputs.x }}` replaced by
# what the caller passes (or the input's default).

_MAX_COMPOSITE_DEPTH = 3
_LOCAL_ACTION_USES_RE = re.compile(r"^\./(?P<dir>(?!\.github/workflows/)[^@\s]+?)/?$")


def _load_composite(repo_root: Path | None, rel_dir: str) -> tuple[dict, list[str], str] | None:
    """(doc, source lines, repo-relative path) of a local composite action, or None."""
    if repo_root is None:
        return None
    root = Path(repo_root).resolve()
    for name in ("action.yml", "action.yaml"):
        candidate = (root / rel_dir / name).resolve()
        try:
            relative = candidate.relative_to(root)
            text = candidate.read_text(encoding="utf-8")
            doc = yaml.load(text, Loader=_MarkedLoader)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        runs = doc.get("runs") if isinstance(doc, dict) else None
        if isinstance(runs, dict) and runs.get("using") == "composite" and isinstance(runs.get("steps"), list):
            return doc, text.splitlines(), relative.as_posix()
    return None


def _subst_inputs(node, values: dict):
    """`${{ inputs.x }}` replaced throughout a step; a whole-value expression keeps its type."""
    if isinstance(node, str):
        whole = _INPUT_EXPR_RE.fullmatch(node.strip())
        if whole is not None and whole.group(1) in values:
            return values[whole.group(1)]
        return _INPUT_EXPR_RE.sub(
            lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), node
        )
    if isinstance(node, list):
        return [_subst_inputs(item, values) for item in node]
    if isinstance(node, dict):
        return {k: (v if k in _META else _subst_inputs(v, values)) for k, v in node.items()}
    return node


def _composite_inputs(action: dict, passed: dict) -> dict:
    declared = action.get("inputs") if isinstance(action.get("inputs"), dict) else {}
    values: dict = {}
    for name, spec in declared.items():
        if name in _META:
            continue
        default = spec.get("default") if isinstance(spec, dict) else None
        values[name] = default if default is not None else ""
    for name, value in passed.items():
        if name not in _META:
            values[name] = value
    return values


def _step_hits(
    step: dict,
    earlier: list,
    doc: dict,
    job: dict,
    lines: list[str],
    ctx: _R6Context,
    depth: int = 0,
    seen: tuple = (),
) -> list[_Hit]:
    """Hits for one step, following a local composite action into its own steps.

    Lines of a hit that came from inside a composite are None: they number another file, and the
    finding anchors on the caller's `uses:` line instead (the action path and line are in `cause`)."""
    direct = _step_hit(step, earlier, doc, job, lines, ctx)
    if direct is not None:
        return [direct]
    uses = step.get("uses")
    match = _LOCAL_ACTION_USES_RE.match(uses.strip()) if isinstance(uses, str) else None
    if match is None or depth >= _MAX_COMPOSITE_DEPTH or match.group("dir") in seen:
        return []
    loaded = _load_composite(ctx.repo_root, match.group("dir"))
    if loaded is None:
        return []
    action, action_lines, action_path = loaded
    values = _composite_inputs(action, step.get("with") if isinstance(step.get("with"), dict) else {})
    inner = [_subst_inputs(s, values) for s in action["runs"]["steps"] if isinstance(s, dict)]
    hits: list[_Hit] = []
    for position, inner_step in enumerate(inner):
        for hit in _step_hits(
            inner_step, list(earlier) + inner[:position], doc, job, action_lines, ctx, depth + 1, seen + (match.group("dir"),)
        ):
            where = f"{action_path}:{hit.line}" if hit.line else action_path
            hits.append(
                hit._replace(
                    line=None,
                    owner=f"`{uses.strip()}` composite action, whose {hit.owner} ({where})",
                )
            )
    return hits


def _r6_message(name: str, hit: _Hit) -> str:
    base = (
        f"Job `{name}` runs on the self-hosted pool and its {hit.owner} {hit.cause}. "
        f"Every runner pod on a node shares one {hit.shared} through the `/var/cache/ci` "
        "hostPath, so restoring the ~2 GB cache tarball over it races the installs of concurrent jobs"
    )
    if hit.shared == "npm cache":
        return (
            base
            + " and can leave cached packages missing or half-written under them. The cache is "
            "already warm, so the restore buys nothing on this pool."
        )
    if hit.shared == "pnpm store":
        return (
            base
            + " and fails them with `ERR_PNPM_ENOENT ... copyfile /var/cache/ci/pnpm-store/v10/files/...`. "
            "The store is already warm, so the restore buys nothing on this pool."
        )
    return (
        base
        + " and fails them (pnpm: `ERR_PNPM_ENOENT ... copyfile`). The store is already warm, so the "
        "restore buys nothing on this pool. A save also uploads the whole shared directory, and a "
        "restore of one can leave CAS names hard-linked to another file's bytes (a whole-store "
        "restore did exactly that on 2026-11-11)."
    )


def _pnpm_cache_findings(
    path: str,
    lines: list[str],
    doc: dict,
    repo_root: Path | None = None,
    caller_values: dict | None = None,
) -> list[Finding]:
    jobs = _jobs(doc)
    if not jobs:
        return []
    header_span, job_spans = _marker_spans(lines, doc, jobs)
    ctx = _R6Context(repo_root or _infer_repo_root(path))
    findings: list[Finding] = []
    for index, (name, job, job_start, _job_end) in enumerate(jobs):
        if not _pool_capable(job.get("runs-on"), doc, caller_values):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for position, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            uses_line = _key_line(step, "uses") or step.get(_NODE_START)
            for hit in _step_hits(step, steps[:position], doc, job, lines, ctx):
                found = _r6_finding(path, lines, name, job, job_start, hit, uses_line, header_span, job_spans[index])
                if found is not None:
                    findings.append(found)
    return findings


def _r6_finding(
    path: str,
    lines: list[str],
    name: str,
    job: dict,
    job_start: int,
    hit: _Hit,
    uses_line: int | None,
    header_span: tuple[int, int],
    job_span: tuple[int, int],
) -> Finding | None:
    line = hit.line or uses_line
    exemption = _marker_state(lines, [header_span, job_span], _ALLOW_PNPM_CACHE_RE)
    if exemption == "allowed":
        return None
    message = _r6_message(name, hit)
    if exemption == "no-reason":
        message = (
            f"Job `{name}` is marked with `# {WRITTEN_PNPM_CACHE_MARKER}` but no reason "
            "follows it, so the exemption does not apply. The reason is the whole point "
            f"of the marker: it is what a reviewer reads. {message}"
        )
    anchor = _key_line(job, "runs-on") or job_start
    return Finding(
        file=path,
        line=line,
        rule=RULE_PNPM_STORE_CACHE,
        message=message,
        fix=f"{hit.fix}\n\n{_PNPM_CACHE_EXEMPTION_HINT}",
        anchors=tuple(sorted({line, uses_line, job_start, anchor} - {None})),
    )


def _composite_action_findings(path: str, lines: list[str], doc: dict) -> list[Finding]:
    """A composite action.yml linted on its own: only what is wrong whatever the caller's runner is.

    That is a literal `/var/cache/ci` path in an `actions/cache` step. A `cache:` forwarded from an
    input is judged against the input's default (a caller that passes nothing gets it); a literal
    `cache: pnpm` is NOT reported here, because hosted callers may use the action correctly: it is
    reported on the pool job that calls the action, by `_pnpm_cache_findings`."""
    runs = doc.get("runs")
    if not isinstance(runs, dict) or runs.get("using") != "composite" or not isinstance(runs.get("steps"), list):
        return []
    values = _composite_inputs(doc, {})
    ctx = _R6Context(None)
    findings: list[Finding] = []
    steps = [s for s in runs["steps"] if isinstance(s, dict)]
    for position, step in enumerate(steps):
        resolved = _subst_inputs(step, values)
        hit = _step_hit(resolved, steps[:position], doc, {}, lines, ctx, direct=True)
        if hit is None:
            forwarded = step.get("with", {}).get("cache") if isinstance(step.get("with"), dict) else None
            if not (isinstance(forwarded, str) and _INPUT_EXPR_RE.search(forwarded)):
                continue
            hit = _step_hit(resolved, steps[:position], doc, {}, lines, ctx)
            if hit is None or hit.shared == "package store":
                continue
            hit = hit._replace(cause=f"forwards `cache: {forwarded}` and the input's default makes that `{(resolved.get('with') or {}).get('cache')}`")
        if _marker_state(lines, [(1, len(lines))], _ALLOW_PNPM_CACHE_RE) == "allowed":
            continue
        uses_line = _key_line(step, "uses") or step.get(_NODE_START)
        line = hit.line or uses_line
        findings.append(
            Finding(
                file=path,
                line=line,
                rule=RULE_PNPM_STORE_CACHE,
                message=(
                    f"This composite action's {hit.owner} {hit.cause}. A job on the self-hosted pool "
                    "that calls it restores or saves a cache over the `/var/cache/ci` store that every "
                    "runner pod on the node shares, which races concurrent installs and can leave "
                    "CAS names holding another file's bytes (a whole-store restore did exactly that "
                    "on 2026-11-11)."
                ),
                fix=f"{hit.fix}\n\n{_PNPM_CACHE_EXEMPTION_HINT}",
                anchors=tuple(sorted({line, uses_line} - {None})),
            )
        )
    return findings


def _render(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


# ── rule 4: a caller's permissions must cover the workflow it calls ────────
#
# A called workflow can only ever receive permissions its caller already holds.
# Ask for one the caller lacks and the run does not fail a step — it fails at
# STARTUP, before any job exists, with no annotation and no log. On 2026-09-10
# two self-tests in this repo died exactly that way: they granted
# `contents: read` and called workflows declaring `id-token: write`. There is
# nothing in the UI to read, so the rule exists to say it in the PR instead.

_PERM_LEVELS = {"none": 0, "read": 1, "write": 2}
_PERM_DEFAULT = "__default__"
# GitHub never puts an OIDC token in the default GITHUB_TOKEN; it must be asked
# for. Every other scope's default level depends on an org/repo setting the
# linter cannot read, so a caller with no block at all is only judged on these.
NEVER_IN_DEFAULT_TOKEN = frozenset({"id-token"})

# Stands in for "every scope", which is what a `read-all` / `write-all` callee
# asks for; it is a label in a message, never a real GitHub permission name.
BLANKET_SCOPE = "(all scopes)"

_LOCAL_USES_RE = re.compile(r"^\./(?P<path>\.github/workflows/[^@\s]+)$")
_ORG_USES_RE = re.compile(
    r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/(?P<path>\.github/workflows/[^@\s]+)@\S+$"
)

# The one repository whose org-qualified `uses:` may be resolved against the
# checkout on disk, which happens when that repository is linting itself. Any
# other owner/repo names a workflow this run cannot read, and matching it on
# filename alone would compare a caller against a local namesake: the day
# sentry-selfhost grows its own `changelog-preview.yml`, a call to
# `getsentry/craft/.github/workflows/changelog-preview.yml@v2` would be judged
# against the wrong file. Owner and repo are therefore checked, not just path.
ORG_WORKFLOW_REPO = "DevinoSolutions/.github"


def _parse_permissions(block) -> dict[str, int] | None:
    """A permissions block as scope -> level, with a default level for unlisted scopes.

    `None` means no block at all, which is not the same as `permissions: {}`:
    the first inherits the default token, the second grants nothing.
    """
    if block is None:
        return None
    if isinstance(block, str):
        text = block.strip()
        if text == "read-all":
            return {_PERM_DEFAULT: 1}
        if text == "write-all":
            return {_PERM_DEFAULT: 2}
        return {_PERM_DEFAULT: 0}
    if isinstance(block, dict):
        parsed = {_PERM_DEFAULT: 0}
        for scope in _keys(block):
            value = block[scope]
            if isinstance(value, str):
                parsed[str(scope)] = _PERM_LEVELS.get(value.strip(), 0)
        return parsed
    return None


def _declared_permissions(doc: dict) -> dict[str, int]:
    """Everything the callee asks for, workflow level and job level, at the highest level asked."""
    merged = {_PERM_DEFAULT: 0}
    blocks = [_parse_permissions(doc.get("permissions"))]
    blocks += [_parse_permissions(job.get("permissions")) for _n, job, _s, _e in _jobs(doc)]
    for block in blocks:
        if block is None:
            continue
        for scope, level in block.items():
            merged[scope] = max(merged.get(scope, 0), level)
    return merged


def _missing_scopes(caller: dict[str, int] | None, callee: dict[str, int]) -> list[tuple[str, int, int]]:
    """(scope, needed, held) for every scope the caller cannot pass on."""
    wanted = [(s, lvl) for s, lvl in callee.items() if s != _PERM_DEFAULT and lvl > 0]
    if caller is None:
        return [(s, lvl, 0) for s, lvl in wanted if s in NEVER_IN_DEFAULT_TOKEN]
    missing = [(s, lvl, caller.get(s, caller[_PERM_DEFAULT])) for s, lvl in wanted]
    missing = [entry for entry in missing if entry[2] < entry[1]]
    if callee[_PERM_DEFAULT] > caller[_PERM_DEFAULT]:
        missing.append((BLANKET_SCOPE, callee[_PERM_DEFAULT], caller[_PERM_DEFAULT]))
    return missing


def _level_name(level: int) -> str:
    for name, value in _PERM_LEVELS.items():
        if value == level:
            return name
    return str(level)


def _infer_repo_root(path: str) -> Path | None:
    parts = Path(path).resolve().parts
    if len(parts) >= 3 and parts[-2] == "workflows" and parts[-3] == ".github":
        return Path(*parts[:-3])
    # a composite action: <root>/.github/actions/<name>/action.yml
    anchors = [i for i, part in enumerate(parts[:-1]) if part == ".github"]
    if anchors:
        return Path(*parts[: anchors[-1]])
    return None


CALLEE_LOCAL = "local"
CALLEE_MISSING = "missing"
CALLEE_REMOTE = "remote"


def _resolve_callee(uses: str, caller_path: str, repo_root: Path | None) -> tuple[str, Path | None]:
    """Where a job's `uses:` points, and whether this checkout can see it.

    Three answers, and the difference between the last two is the whole point:

      (CALLEE_LOCAL, path)    resolved to a file in this repository
      (CALLEE_MISSING, None)  a `./…` path with no such file here. Not an
                              uncertainty — a local `uses:` either resolves in
                              the checkout or the run cannot start, so this is
                              reported as a failure rather than skipped.
      (CALLEE_REMOTE, None)   another repository's workflow, unreadable here

    Two shapes can resolve: `./.github/workflows/x.yml`, and the org-qualified
    `<owner>/<repo>/.github/workflows/x.yml@<ref>` when `<owner>/<repo>` is the
    org's own reusable-workflow repository and that path exists here, which in
    practice means the `.github` repo linting its own reusable workflows.
    """
    text = uses.strip()
    local = _LOCAL_USES_RE.match(text)
    match = local or _ORG_USES_RE.match(text)
    if match is None:
        return CALLEE_REMOTE, None
    if local is None:
        named = f"{match.group('owner')}/{match.group('repo')}"
        if named.casefold() != ORG_WORKFLOW_REPO.casefold():
            return CALLEE_REMOTE, None
    relative = match.group("path")
    roots = [root for root in (repo_root, _infer_repo_root(caller_path)) if root is not None]
    if not roots:
        return CALLEE_REMOTE, None
    caller_resolved = Path(caller_path).resolve()
    self_reference = False
    for root in roots:
        candidate = Path(root) / relative
        if not candidate.is_file():
            continue
        # A repo that calls the org's copy of a workflow it also has locally must
        # not be checked against its own file — and a caller must never resolve
        # to itself, which is what `ci-lint.yml` calling `ci-lint.yml` would do.
        if candidate.resolve() == caller_resolved:
            self_reference = True
            continue
        return CALLEE_LOCAL, candidate
    if local is not None and not self_reference:
        return CALLEE_MISSING, None
    return CALLEE_REMOTE, None


CALLEE_REUSABLE = "reusable"
CALLEE_NOT_REUSABLE = "not-reusable"
CALLEE_UNREADABLE = "unreadable"


# A chain of reusable workflows passes permissions all the way down: if A calls
# B and B's job calls C, C's `id-token: write` has to be held by A, not by B.
# Reading one hop would pass a caller that cannot possibly work. The org's
# reusable workflows are one level deep today, so the depth cap is generous
# and only there to bound a pathological tree; a cycle is caught separately.
_MAX_CALLEE_DEPTH = 8


def _callee_permissions(
    path: Path, repo_root: Path | None = None, seen: tuple[str, ...] = ()
) -> tuple[str, dict[str, int] | None]:
    """What a resolved callee asks for, following the workflows it calls in turn.

    `None` for two different reasons, and the caller words the skip note from
    which one it was: unparseable, or resolvable but not a reusable workflow at
    all. Conflating either with "could not be resolved" tells a reviewer to go
    looking in the wrong repository.
    """
    try:
        text = path.read_text(encoding="utf-8")
        doc = yaml.load(text, Loader=_MarkedLoader)
    except (OSError, yaml.YAMLError):
        return CALLEE_UNREADABLE, None
    if not isinstance(doc, dict):
        return CALLEE_UNREADABLE, None
    triggers, _ = _triggers(doc)
    if "workflow_call" not in triggers:
        return CALLEE_NOT_REUSABLE, None

    merged = _declared_permissions(doc)
    seen = (*seen, str(Path(path).resolve()))
    if len(seen) < _MAX_CALLEE_DEPTH:
        for _name, job, _start, _end in _jobs(doc):
            nested = job.get("uses")
            if not isinstance(nested, str):
                continue
            where, nested_path = _resolve_callee(nested, str(path), repo_root)
            if where != CALLEE_LOCAL or str(nested_path.resolve()) in seen:
                continue
            _reason, deeper = _callee_permissions(nested_path, repo_root, seen)
            for scope, level in (deeper or {}).items():
                merged[scope] = max(merged.get(scope, 0), level)
    return CALLEE_REUSABLE, merged


def _skip_reason(reason: str) -> str:
    """Why a resolved-or-not callee could not be compared, in the reviewer's words."""
    if reason == CALLEE_NOT_REUSABLE:
        return (
            "resolves in this checkout but declares no `workflow_call:` trigger, so it has no "
            "permissions to compare — and a workflow that is not reusable cannot be called at "
            "all, which is worth a look on its own."
        )
    if reason == CALLEE_UNREADABLE:
        return "resolves in this checkout but could not be parsed, so its permissions could not be read."
    return "is not resolvable in this checkout, so its permissions could not be compared with this workflow's."


def _no_reason_prefix(name: str, exemption: str) -> str:
    """The sentence a bare `allow-permissions` with no reason after it earns."""
    if exemption != "no-reason":
        return ""
    return (
        f"Job `{name}` is marked with `# {WRITTEN_PERMISSIONS_MARKER}` but no reason follows it, "
        "so the exemption does not apply. "
    )


def _caller_permission_findings(
    path: str, lines: list[str], doc: dict, repo_root: Path | None
) -> list[Finding]:
    jobs = _jobs(doc)
    if not jobs:
        return []
    header_span, job_spans = _marker_spans(lines, doc, jobs)
    workflow_perms = _parse_permissions(doc.get("permissions"))
    workflow_perm_lines = _block_lines(doc, "permissions")
    findings: list[Finding] = []

    for index, (name, job, job_start, _end) in enumerate(jobs):
        uses = job.get("uses")
        if not isinstance(uses, str):
            continue
        line = _key_line(job, "uses") or job_start
        anchors = tuple(sorted({line, job_start}))
        # What the caller grants is as much a cause of this finding as the call
        # itself, and it is the half a PR usually edits: narrowing
        # `permissions:` leaves `uses:` untouched, so anchoring only there means
        # added-lines mode — the fleet default — never sees the regression.
        perm_anchors = tuple(sorted(set(anchors) | workflow_perm_lines | _block_lines(job, "permissions")))

        # The marker is read before the callee is resolved: it means "do not check
        # this job", so it silences the skip notice as well as the failure. A
        # marker with no reason is not an exemption and falls through to both.
        exemption = _marker_state(lines, [header_span, job_spans[index]], _ALLOW_PERMISSIONS_RE)
        if exemption == "allowed":
            continue

        where, callee_path = _resolve_callee(uses, path, repo_root)
        if where == CALLEE_MISSING:
            findings.append(
                Finding(
                    file=path,
                    line=line,
                    rule=RULE_CALLER_PERMISSIONS,
                    message=_no_reason_prefix(name, exemption)
                    + (
                        f"Job `{name}` calls `{uses}`, which does not exist in this repository. "
                        "A `uses:` pointing at a missing local file is a workflow-level error, so "
                        "it does not fail a step — the whole run fails at STARTUP, before any job "
                        "exists, with no annotation and no log to read. `caly` shipped exactly this "
                        "shape: 47 consecutive runs between 2026-06-04 and 2026-09-05, every one a "
                        "startup failure with zero jobs, and nobody noticed for three months."
                    ),
                    fix=(
                        f"Add `{_LOCAL_USES_RE.match(uses.strip()).group('path')}` to this "
                        "repository, point `uses:` at a workflow that exists, or drop the job."
                    ),
                    anchors=anchors,
                )
            )
            continue

        reason, callee = (
            _callee_permissions(callee_path, repo_root)
            if where == CALLEE_LOCAL
            else (CALLEE_REMOTE, None)
        )
        if callee is None:
            findings.append(
                Finding(
                    file=path,
                    line=line,
                    rule=RULE_CALLER_PERMISSIONS,
                    message=f"Job `{name}` calls `{uses}`, which {_skip_reason(reason)} Skipped, not failed.",
                    fix="",
                    anchors=anchors,
                    severity=SEVERITY_NOTICE,
                )
            )
            continue

        job_perms = _parse_permissions(job.get("permissions"))
        effective = job_perms if job_perms is not None else workflow_perms
        missing = _missing_scopes(effective, callee)
        if not missing:
            continue

        needed = ", ".join(f"`{scope}: {_level_name(need)}`" for scope, need, _have in missing)
        held = (
            "this workflow declares no `permissions:` block, so the job runs with the default "
            "token, which never carries an OIDC token"
            if effective is None
            else "the caller grants "
            + ", ".join(f"`{scope}: {_level_name(have)}`" for scope, _need, have in missing)
        )
        message = (
            f"Job `{name}` calls `{Path(callee_path).name}`, which declares {needed}, but "
            f"{held}. A called workflow can only receive permissions its caller already holds, "
            "so this does not fail a step — the whole run fails at STARTUP, before any job "
            "exists, with no annotation and no log to read."
        )
        # A callee whose whole block is the string `write-all` (or `read-all`)
        # asks for every scope at once and names none, so there is nothing to
        # list under a `permissions:` heading — say the blanket form instead of
        # rendering a heading with nothing under it. Only a string block can
        # produce the blanket entry, so it never coexists with named scopes.
        blanket = next((need for scope, need, _have in missing if scope == BLANKET_SCOPE), None)
        body = (
            f"permissions: {_level_name(blanket)}-all"
            if blanket is not None
            else "permissions:\n"
            + "\n".join(
                f"  {scope}: {_level_name(need)}"
                for scope, need, _have in missing
                if scope != BLANKET_SCOPE
            )
        )
        fix = f"Add to the caller (workflow level, or on this job):\n\n{body}"
        if job_perms is not None:
            fix += (
                "\n\nNote this job has its own `permissions:` block, which REPLACES the "
                "workflow-level one rather than adding to it, so the scope has to go there."
            )
        message = _no_reason_prefix(name, exemption) + message
        findings.append(
            Finding(
                file=path,
                line=line,
                rule=RULE_CALLER_PERMISSIONS,
                message=message,
                fix=fix,
                anchors=perm_anchors,
            )
        )
    return findings


def _why_not(runs_on) -> str:
    """The extra sentence a `runs-on` expression earns, so the reader knows what was read."""
    if not is_expression(runs_on):
        return ""
    label = schedule_branch_label(runs_on)
    if label is None:
        return (
            "That expression could not be resolved: the only conditional form this lint reads is "
            "`${{ github.event_name == 'schedule' && '<label>' || '<label>' }}`. "
        )
    return f"On a schedule event that expression resolves to `{label}`. "


# ── git plumbing ───────────────────────────────────────────────────────────


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def resolve_baseline(repo_root: Path | str, base_ref: str) -> tuple[bool, str | None]:
    """Decide the lint mode for this PR.

    Returns (added_lines_only, baseline_sha). Added-lines mode is on only when
    `.github/ci-lint-baseline` exists, holds a sha, and that sha is an ancestor
    of the PR base — i.e. the repo really did go through the W1-W4 migration on
    this line of history. Anything else falls back to linting whole files, which
    is the strict mode.
    """
    root = Path(repo_root)
    marker = root / BASELINE_FILE
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False, None
    sha = raw.splitlines()[0].strip() if raw else ""
    if not _SHA_RE.match(sha):
        return False, None
    if _git(root, "merge-base", "--is-ancestor", sha, base_ref).returncode != 0:
        return False, None
    return True, sha


def added_lines(repo_root: Path | str, base_ref: str, paths: list[str]) -> dict[str, set[int]]:
    """Lines `base_ref...HEAD` touched, keyed by repo-relative posix path.

    Added lines are the obvious half. The other half is a hunk that only
    *removes* lines: it adds nothing, so a filter built on added lines alone is
    blind to a change whose whole effect is a deletion — removing `id-token:
    write` from a caller's `permissions:` block, or dropping a `concurrency:`
    block. Neither is backlog to grandfather; both are regressions the PR
    introduced. A pure deletion is therefore recorded as touching the two
    surviving lines the gap now sits between (`@@ -3 +2,0 @@` -> {2, 3}), which
    is what lets a rule anchored to the surrounding block still fire.
    """
    root = Path(repo_root)
    result = _git(root, "diff", "--no-color", "-U0", f"{base_ref}...HEAD", "--", *paths)
    if result.returncode != 0:
        return {}
    added: dict[str, set[int]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if current is None or not line.startswith("@@"):
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        touched = range(start, start + count) if count else (max(start, 1), start + 1)
        added.setdefault(current, set()).update(touched)
    return added


def _repo_relative(path: str, repo_root: Path | None) -> str:
    if repo_root is None:
        return Path(path).as_posix()
    try:
        return Path(path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


# ── entry points ───────────────────────────────────────────────────────────


def lint_text(
    path: str,
    text: str,
    repo_root: Path | str | None = None,
    caller_values: dict | None = None,
) -> list[Finding]:
    """Lint one workflow's source. Exposed for callers that already hold the bytes."""
    lines = text.splitlines()
    try:
        # _MarkedLoader is a SafeLoader subclass; it only overrides the mapping
        # constructor to record line marks, so no arbitrary tags are constructible.
        doc = yaml.load(text, Loader=_MarkedLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 1) if mark is not None else 1
        return [
            Finding(
                file=path,
                line=line,
                rule=RULE_UNPARSEABLE,
                message=f"This workflow is not valid YAML, so it cannot be linted or run: {exc.__class__.__name__}.",
                fix="Run actionlint (or `yq .`) on the file and fix the syntax error.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    findings = _docker_build_findings(path, lines, doc)
    findings += _concurrency_findings(path, lines, doc)
    findings += _nightly_findings(path, lines, doc)
    findings += _caller_permission_findings(
        path, lines, doc, Path(repo_root) if repo_root is not None else None
    )
    findings += _bun_engine_findings(path, lines, doc, caller_values)
    findings += _pnpm_cache_findings(
        path, lines, doc, Path(repo_root) if repo_root is not None else None, caller_values
    )
    findings += _composite_action_findings(path, lines, doc)
    return findings


def lint_files(
    paths: list[str],
    added_lines_only: bool = False,
    baseline_sha: str | None = None,
    *,
    base_ref: str | None = None,
    repo_root: Path | str | None = None,
) -> list[Finding]:
    """Lint the given workflow files.

    `added_lines_only` keeps only findings anchored to a line the PR added,
    using `git diff <base>...HEAD -U0`; the diff base is `base_ref` when given
    and otherwise `baseline_sha`. `repo_root` resolves relative paths and is the
    repository git runs in.
    """
    root = Path(repo_root) if repo_root is not None else None
    findings: list[Finding] = []
    for path in paths:
        target = Path(path)
        if root is not None and not target.is_absolute():
            target = root / target
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            continue
        findings.extend(lint_text(path, text, repo_root=root))

    if added_lines_only:
        base = base_ref or baseline_sha
        if base:
            relative = {path: _repo_relative(path, root) for path in paths}
            diff_root = root if root is not None else Path(".")
            touched = added_lines(diff_root, base, sorted(set(relative.values())))
            findings = [
                f
                for f in findings
                if any(line in touched.get(relative.get(f.file, f.file), set()) for line in f.anchor_lines)
            ]

    findings.sort(key=lambda f: (f.file, f.line, f.rule))
    return findings


def format_annotations(findings: list[Finding]) -> str:
    return "\n".join(f.as_annotation() for f in findings)


def format_summary(findings: list[Finding], files: list[str]) -> str:
    header = "## ci-lint\n\n"
    failures = [f for f in findings if f.severity == SEVERITY_ERROR]
    skipped = [f for f in findings if f.severity == SEVERITY_NOTICE]
    rules_line = (
        "Rules: `docker-build`, `missing-concurrency`, `nightly-runner`, `caller-permissions` "
        f"([spec 4.3]({SPEC_URL}#43-org-lint-required-workflow)).\n"
    )
    if not failures:
        out = [header, f"No findings across {len(files)} changed workflow file(s).\n\n", rules_line]
        out.append(_skipped_block(skipped))
        return "".join(out)
    out = [header, f"{len(failures)} finding(s) across {len(files)} changed workflow file(s).\n\n"]
    out.append("| file | line | rule | what to do |\n|---|---|---|---|\n")
    for f in failures:
        fix = f.fix.replace("\n", " ").replace("|", "\\|")
        out.append(f"| `{f.file}` | {f.line} | `{f.rule}` | {fix} |\n")
    out.append("\n<details><summary>Full messages</summary>\n\n")
    for f in failures:
        out.append(f"**`{f.file}`:{f.line} — `{f.rule}`**\n\n{f.message}\n\n")
    out.append("</details>\n")
    out.append(_skipped_block(skipped))
    return "".join(out)


def _skipped_block(skipped: list[Finding]) -> str:
    if not skipped:
        return ""
    out = [f"\n<details><summary>{len(skipped)} check(s) skipped, not failed</summary>\n\n"]
    for f in skipped:
        out.append(f"- `{f.file}`:{f.line} — {f.message}\n")
    out.append("\n</details>\n")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci-lint", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="workflow files to lint")
    parser.add_argument("--paths-from", help="file holding one workflow path per line")
    parser.add_argument("--base", help="PR base ref or sha; enables baseline resolution")
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument("--summary-file", help="markdown job summary is written here")
    args = parser.parse_args(argv)

    paths = list(args.paths)
    if args.paths_from:
        listed = Path(args.paths_from).read_text(encoding="utf-8").splitlines()
        paths += [line.strip() for line in listed if line.strip()]

    root = Path(args.repo_root)
    mode = "whole changed files"
    added_only, baseline_sha = False, None
    if args.base:
        added_only, baseline_sha = resolve_baseline(root, args.base)
        if added_only:
            mode = f"added lines only (baseline {baseline_sha[:12]})"

    findings = lint_files(
        paths,
        added_lines_only=added_only,
        baseline_sha=baseline_sha,
        base_ref=args.base,
        repo_root=root,
    )

    failures = [f for f in findings if f.severity == SEVERITY_ERROR]
    skipped = len(findings) - len(failures)
    if findings:
        sys.stdout.write(format_annotations(findings) + "\n")
    tail = f", {skipped} skipped" if skipped else ""
    sys.stdout.write(
        f"ci-lint: {len(failures)} finding(s){tail} in {len(paths)} file(s); mode: {mode}\n"
    )

    if args.summary_file:
        summary = format_summary(findings, paths)
        with Path(args.summary_file).open("a", encoding="utf-8") as handle:
            handle.write(summary)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
