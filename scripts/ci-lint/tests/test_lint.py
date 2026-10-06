"""Unit tests for the org CI lint (Track 1 / W5, spec sections 4.3 and 4.4).

Written before `lint.py` existed. Every rule has a positive and a negative
fixture, both trigger spellings (list and map) are covered, and the two
baseline modes (whole changed file / added lines only) are exercised against
a real throwaway git repository rather than a mocked `git diff`, because the
hunk parser is the part most likely to be wrong.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

import lint
from lint import (
    RULE_BUN_ENGINE_ORDER,
    RULE_CALLER_PERMISSIONS,
    RULE_CONCURRENCY,
    RULE_DOCKER_BUILD,
    RULE_NIGHTLY_RUNNER,
    RULE_UNPARSEABLE,
    lint_files,
    resolve_baseline,
)

# -- helpers ---------------------------------------------------------------


def wf(tmp_path: Path, name: str, body: str) -> Path:
    """Write a workflow file under .github/workflows and return its path.

    A body starting with a newline is an indented block literal and is dedented;
    anything else is written verbatim, which is what the trigger-form cases need
    (dedent cannot cope with an interpolated multi-line fragment).
    """
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    text = textwrap.dedent(body).lstrip("\n") if body.startswith("\n") else body
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def rules(findings) -> list[str]:
    return sorted(f.rule for f in findings)


def errors(findings):
    return [f for f in findings if f.severity == "error"]


def notes(findings):
    return [f for f in findings if f.severity == "notice"]


def only(findings, rule):
    return [f for f in findings if f.rule == rule]


def run(paths):
    return lint_files([str(p) for p in paths], added_lines_only=False, baseline_sha=None)


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.invalid")
    git(repo, "config", "user.name", "ci")
    git(repo, "config", "commit.gpgsign", "false")
    return repo


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


CLEAN = """
    name: ci
    on:
      pull_request:
        branches: [main]
    concurrency:
      group: ${{ github.workflow }}-${{ github.ref }}
      cancel-in-progress: true
    jobs:
      test:
        runs-on: ubuntu-devino
        steps:
          - uses: actions/checkout@v4
          - run: npm ci && npm test
"""


# -- a clean file ----------------------------------------------------------


def test_clean_workflow_has_no_findings(tmp_path):
    assert run([wf(tmp_path, "ci.yml", CLEAN)]) == []


def test_clean_nightly_workflow_has_no_findings(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          sweep:
            runs-on: ubuntu-devino-nightly
            timeout-minutes: 30
            steps:
              - run: npm run audit
        """,
    )
    assert run([p]) == []


# -- R1: docker build without an allow-build reason ------------------------


@pytest.mark.parametrize(
    "command",
    [
        "docker build -t app:ci .",
        "docker compose -f docker-compose.ci.yml build web",
        "docker-compose build web",
        "docker buildx build --load -t app:ci .",
        "docker buildx bake --load ci",
    ],
)
def test_r1_flags_every_build_spelling(tmp_path, command):
    p = wf(
        tmp_path,
        "build.yml",
        f"""
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: {command}
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert len(found) == 1
    assert found[0].line == 11


def test_r1_flags_bake_action_in_uses(tmp_path):
    p = wf(
        tmp_path,
        "bake.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - uses: docker/bake-action@v5
                with:
                  targets: ci
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert len(found) == 1
    assert found[0].line == 11


def test_r1_flags_build_push_action_in_uses(tmp_path):
    p = wf(
        tmp_path,
        "push.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - uses: docker/build-push-action@v6
                with:
                  push: true
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert len(found) == 1
    assert found[0].line == 11


def test_r1_allow_comment_silences_build_push_action(tmp_path):
    p = wf(
        tmp_path,
        "push.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              # ci-lint: allow-build the PR leg builds against the shared cache
              - uses: docker/build-push-action@v6
                with:
                  push: false
        """,
    )
    assert only(run([p]), RULE_DOCKER_BUILD) == []


def test_r1_message_links_the_wait_for_image_section(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build .
        """,
    )
    f = only(run([p]), RULE_DOCKER_BUILD)[0]
    assert "5.5" in f.message
    assert "wait-for-image" in f.message
    assert f.fix


def test_r1_allow_comment_on_the_same_line_silences_it(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build . # ci-lint: allow-build PR leg builds against the shared cache
        """,
    )
    assert only(run([p]), RULE_DOCKER_BUILD) == []


def test_r1_allow_comment_elsewhere_in_the_step_silences_it(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - name: build the fixture image
                # ci-lint: allow-build the fixture image has no registry copy
                run: |
                  docker build -t fixture:ci .
        """,
    )
    assert only(run([p]), RULE_DOCKER_BUILD) == []


def test_r1_allow_comment_needs_a_reason(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build . # ci-lint: allow-build
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert len(found) == 1
    assert "reason" in found[0].message.lower()


def test_r1_allow_comment_does_not_leak_into_the_next_step(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build -t first .
              - name: second
                # ci-lint: allow-build only this step is excused
                run: docker build -t second .
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert [f.line for f in found] == [11]


def test_r1_allow_comment_above_a_step_belongs_to_that_step_only(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build -t first .
              # ci-lint: allow-build only the second build is excused
              - run: docker build -t second .
        """,
    )
    found = only(run([p]), RULE_DOCKER_BUILD)
    assert [f.line for f in found] == [11]


def test_r1_ignores_docker_words_outside_run_and_uses(tmp_path):
    p = wf(
        tmp_path,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - name: no docker build happens here
                run: docker compose up -d
              # docker build is what this file deliberately avoids
              - run: docker compose down -v
        """,
    )
    assert only(run([p]), RULE_DOCKER_BUILD) == []


# -- R2: push / pull_request without top-level concurrency -----------------


@pytest.mark.parametrize(
    "trigger",
    [
        "on: push",
        "on: [push]",
        "on: [pull_request, workflow_dispatch]",
        "on:\n  push:\n    branches: [main]",
        "on:\n  pull_request:\n",
        "on:\n  push:\n  workflow_dispatch:\n",
    ],
)
def test_r2_flags_every_trigger_form(tmp_path, trigger):
    tail = "jobs:\n  b:\n    runs-on: ubuntu-devino\n    steps:\n      - run: echo hi\n"
    p = wf(tmp_path, "t.yml", f"name: t\n{trigger}\n{tail}")
    assert len(only(run([p]), RULE_CONCURRENCY)) == 1


def test_r2_accepts_a_top_level_concurrency_block(tmp_path):
    assert only(run([wf(tmp_path, "ci.yml", CLEAN)]), RULE_CONCURRENCY) == []


# The exemption. "No concurrency block" is sometimes the CORRECT answer: a
# workflow whose jobs serialise on external state is actively harmed by a
# top-level group, because a superseded run is either killed mid-suite or, with
# cancel-in-progress false, replaced while merely PENDING - and an `always()`
# status job reads that cancellation as a failure. superbooks/e2e.yml is the
# case this was written for; see its sandbox-e2e-accounting-token-store lane.

_R2_PROBE = """
name: e2e
on:
  push:
    branches: [main]
  pull_request:
jobs:
  standard:
    runs-on: ubuntu-devino
    steps:
      - run: echo hi
"""


def test_r2_flags_the_probe_without_a_marker(tmp_path):
    assert len(only(run([wf(tmp_path, "e2e.yml", _R2_PROBE)]), RULE_CONCURRENCY)) == 1


def test_r2_a_header_marker_with_a_reason_exempts_the_workflow(tmp_path):
    marker = f"# ci-lint: {lint.ALLOW_CONCURRENCY_MARKER}"
    reason = "the accounting lane holds a single-use token; a replaced pending run strands it"
    exempted = _R2_PROBE.lstrip("\n").replace(
        "name: e2e\n", f"name: e2e\n{marker} {reason}\n"
    )
    assert only(run([wf(tmp_path, "e2e.yml", exempted)]), RULE_CONCURRENCY) == []


def test_r2_the_no_reason_message_names_the_prefixed_marker(tmp_path):
    bare = _R2_PROBE.lstrip("\n").replace(
        "name: e2e\n", f"name: e2e\n# ci-lint: {lint.ALLOW_CONCURRENCY_MARKER}\n"
    )
    found = only(run([wf(tmp_path, "e2e.yml", bare)]), RULE_CONCURRENCY)
    assert len(found) == 1
    assert f"`# ci-lint: {lint.ALLOW_CONCURRENCY_MARKER}`" in found[0].message


def test_r2_the_bare_marker_name_without_the_prefix_is_ignored(tmp_path):
    """The trap `WRITTEN_SCHEDULE_MARKER` exists to stop: a comment that reads
    like an exemption but does not carry the `ci-lint:` prefix is not one."""
    unprefixed = _R2_PROBE.lstrip("\n").replace(
        "name: e2e\n",
        f"name: e2e\n# {lint.ALLOW_CONCURRENCY_MARKER} looks official, is not\n",
    )
    assert len(only(run([wf(tmp_path, "e2e.yml", unprefixed)]), RULE_CONCURRENCY)) == 1


def test_r2_a_marker_inside_a_job_does_not_exempt_the_workflow(tmp_path):
    """This rule is workflow-scoped, so the marker has to live in the header."""
    marker = f"# ci-lint: {lint.ALLOW_CONCURRENCY_MARKER}"
    in_job = _R2_PROBE.lstrip("\n").replace(
        "  standard:\n", f"  standard:\n    {marker} wrong place, real reason\n"
    )
    assert len(only(run([wf(tmp_path, "e2e.yml", in_job)]), RULE_CONCURRENCY)) == 1


def test_r2_the_fix_text_points_at_the_marker(tmp_path):
    found = only(run([wf(tmp_path, "e2e.yml", _R2_PROBE)]), RULE_CONCURRENCY)
    assert len(found) == 1
    assert f"# ci-lint: {lint.ALLOW_CONCURRENCY_MARKER} <reason>" in found[0].fix


def test_r2_ignores_workflows_without_push_or_pull_request(tmp_path):
    p = wf(
        tmp_path,
        "dispatch.yml",
        """
        name: manual
        on:
          workflow_dispatch:
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: echo hi
        """,
    )
    assert only(run([p]), RULE_CONCURRENCY) == []


def test_r2_message_carries_the_exact_snippet(tmp_path):
    p = wf(
        tmp_path,
        "t.yml",
        """
        name: t
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: echo hi
        """,
    )
    f = only(run([p]), RULE_CONCURRENCY)[0]
    snippet = f"{f.message}\n{f.fix}"
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in snippet
    assert (
        "cancel-in-progress: ${{ !contains(fromJSON('[\"refs/heads/main\",\"refs/heads/dev\"]'), github.ref) }}"
        in snippet
    )


# -- R3: schedule workflows must run on ubuntu-devino-nightly --------------


def test_r3_flags_a_scheduled_job_on_the_main_pool(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          sweep:
            runs-on: ubuntu-devino
            steps:
              - run: npm run audit
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert found[0].line == 7
    assert "ubuntu-devino-nightly" in found[0].message


def test_r3_flags_only_the_offending_jobs(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '0 6 * * *'
          workflow_dispatch:
        jobs:
          good:
            runs-on: ubuntu-devino-nightly
            steps:
              - run: echo ok
          bad:
            runs-on: ubuntu-latest
            steps:
              - run: echo no
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert found[0].line == 12


def test_r3_accepts_a_reusable_job_passing_the_runner_input(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '20 6 * * *'
        jobs:
          evals:
            uses: DevinoSolutions/.github/.github/workflows/ai-evals.yml@main
            with:
              runner: ubuntu-devino-nightly
              eval-command: npm run evals
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_flags_a_reusable_job_that_omits_the_runner_input(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '20 6 * * *'
        jobs:
          evals:
            uses: DevinoSolutions/.github/.github/workflows/ai-evals.yml@main
            with:
              eval-command: npm run evals
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert "runner: ubuntu-devino-nightly" in found[0].fix


def test_r3_accepts_a_runs_on_list_naming_the_nightly_label(tmp_path):
    p = wf(
        tmp_path,
        "nightly.yml",
        """
        name: nightly
        on:
          schedule:
            - cron: '0 7 * * *'
        jobs:
          sweep:
            runs-on: [self-hosted, ubuntu-devino-nightly]
            steps:
              - run: echo ok
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_ignores_workflows_without_a_schedule(tmp_path):
    assert only(run([wf(tmp_path, "ci.yml", CLEAN)]), RULE_NIGHTLY_RUNNER) == []


# -- R3, form (a): an event-conditional runs-on expression -----------------


def test_r3_accepts_an_event_conditional_resolving_to_the_nightly_label(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          probe:
            runs-on: ${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
            steps:
              - run: echo ok
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_accepts_the_inverted_event_conditional(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            runs-on: ${{ github.event_name != 'schedule' && 'ubuntu-devino' || 'ubuntu-devino-nightly' }}
            steps:
              - run: echo ok
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_accepts_an_event_conditional_folded_over_two_lines(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            runs-on: >-
              ${{ github.event_name == 'schedule'
              && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
            steps:
              - run: echo ok
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_flags_an_event_conditional_whose_schedule_branch_is_the_main_pool(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            runs-on: ${{ github.event_name == 'schedule' && 'ubuntu-devino' || 'ubuntu-devino-nightly' }}
            steps:
              - run: echo ok
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert "schedule" in found[0].message


def test_r3_flags_an_expression_it_cannot_resolve(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            runs-on: ${{ matrix.runner }}
            steps:
              - run: echo ok
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert "expression" in found[0].message.lower()
    assert "allow-schedule-main" in found[0].fix


# -- R3, form (b): the allow-schedule-main marker --------------------------


def test_r3_allow_schedule_main_on_the_job_exempts_it(tmp_path):
    p = wf(
        tmp_path,
        "uptime-probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '3,13,23,33,43,53 * * * *'
        jobs:
          probe:
            # ci-lint: allow-schedule-main a 10-minute liveness probe; nightly queueing hides an outage
            runs-on: ubuntu-devino
            steps:
              - run: curl -sf https://superbooks.io/api/status/
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_allow_schedule_main_in_the_header_exempts_the_whole_workflow(tmp_path):
    p = wf(
        tmp_path,
        "uptime-probe.yml",
        """
        # ci-lint: allow-schedule-main latency matters more than pool priority here
        name: probe
        on:
          schedule:
            - cron: '3,13,23,33,43,53 * * * *'
        jobs:
          probe:
            runs-on: ubuntu-devino
            steps:
              - run: curl -sf https://superbooks.io/
          alert:
            runs-on: ubuntu-devino
            steps:
              - run: echo alert
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_allow_schedule_main_needs_a_reason(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            # ci-lint: allow-schedule-main
            runs-on: ubuntu-devino
            steps:
              - run: echo ok
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert "reason" in found[0].message.lower()


def test_r3_allow_schedule_main_does_not_leak_into_the_next_job(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            # ci-lint: allow-schedule-main latency matters here
            runs-on: ubuntu-devino
            steps:
              - run: echo ok
          sweep:
            runs-on: ubuntu-devino
            steps:
              - run: echo sweep
        """,
    )
    found = only(run([p]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    # line 12 is `sweep`'s runs-on; the exempt `probe` job is above it
    assert found[0].line == 12


def test_r3_allow_schedule_main_exempts_a_reusable_job_too(tmp_path):
    p = wf(
        tmp_path,
        "probe.yml",
        """
        name: probe
        on:
          schedule:
            - cron: '0 6 * * *'
        jobs:
          probe:
            # ci-lint: allow-schedule-main the probe measures latency from the main pool
            uses: DevinoSolutions/.github/.github/workflows/pwa-prod-probe.yml@main
            with:
              base-url: https://superbooks.io
        """,
    )
    assert only(run([p]), RULE_NIGHTLY_RUNNER) == []


def test_r3_superbooks_uptime_probe_shape_is_clean(tmp_path):
    p = wf(
        tmp_path,
        "uptime-probe.yml",
        """
        name: Uptime probe

        # Cron is offset off the hour so it never queues behind deploy-verify.
        #
        # ci-lint: allow-schedule-main a 10-minute liveness probe: moving it to the
        # nightly set would let it queue behind a sweep, and a probe that reports an
        # outage 40 minutes late is not a probe.

        on:
          schedule:
            - cron: "3,13,23,33,43,53 * * * *"
          workflow_dispatch:

        permissions:
          contents: read
          issues: write

        concurrency:
          group: uptime-probe
          cancel-in-progress: false

        jobs:
          probe:
            runs-on: ubuntu-devino
            timeout-minutes: 10
            steps:
              - run: curl -sf https://superbooks.io/api/status/
        """,
    )
    assert run([p]) == []


# -- R3: the marker the rule PRINTS must be the marker the rule MATCHES ----
#
# The remediation used to print `# allow-schedule-main <reason>` while
# `_marker_re` matched `#\s*ci-lint:\s*allow-schedule-main\b`. Anyone who typed
# what the linter told them to type got an exemption that was silently ignored,
# which is worse than no escape hatch at all: the comment reads as a decision
# and does nothing. It cost a wasted round trip in `runner-parity` before the
# missing prefix was spotted. These two tests close the loop rather than pinning
# a string, so the same drift cannot come back through a reworded message.

_MARKER_IN_HINT = re.compile(r"#\s*(?P<marker>\S.*?)\s*<reason>")

_R3_PROBE = """
name: probe
on:
  schedule:
    - cron: '0 6 * * *'
jobs:
  probe:
    runs-on: ubuntu-devino
    steps:
      - run: echo ok
"""


def test_r3_the_marker_the_fix_prints_is_the_marker_the_rule_accepts(tmp_path):
    found = only(run([wf(tmp_path, "probe.yml", _R3_PROBE.lstrip("\n"))]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1

    printed = _MARKER_IN_HINT.search(found[0].fix)
    assert printed is not None, f"the fix names no marker to write:\n{found[0].fix}"
    marker = printed.group("marker")
    assert marker == f"ci-lint: {lint.ALLOW_SCHEDULE_MARKER}"

    # Write back exactly what the linter printed and the finding must clear.
    exempted = _R3_PROBE.lstrip("\n").replace(
        "  probe:\n",
        f"  probe:\n    # {marker} a liveness probe; queueing behind a sweep hides an outage\n",
    )
    assert only(run([wf(tmp_path, "probe.yml", exempted)]), RULE_NIGHTLY_RUNNER) == []


def test_r3_the_no_reason_message_names_the_prefixed_marker(tmp_path):
    bare = _R3_PROBE.lstrip("\n").replace(
        "  probe:\n", f"  probe:\n    # ci-lint: {lint.ALLOW_SCHEDULE_MARKER}\n"
    )
    found = only(run([wf(tmp_path, "probe.yml", bare)]), RULE_NIGHTLY_RUNNER)
    assert len(found) == 1
    assert f"`# ci-lint: {lint.ALLOW_SCHEDULE_MARKER}`" in found[0].message


# -- R4: a caller's permissions must cover the workflow it calls -----------
#
# From a real startup_failure on 2026-09-10: selftest-pwa-shell-build.yml and
# selftest-pwa-emulator-smoke.yml granted `contents: read` and called workflows
# declaring `id-token: write`. A called workflow can only ever receive what its
# caller holds, so the run died before any job existed — no annotation, no log,
# nothing to read. That is the failure mode this rule exists to make legible.

CALLEE_ID_TOKEN = """
    name: shell build (reusable)
    on:
      workflow_call:
    permissions:
      contents: read
      id-token: write
    jobs:
      build:
        runs-on: ubuntu-devino
        steps:
          - run: echo build
    """


def two(tmp_path, caller_body, callee_body=CALLEE_ID_TOKEN, callee="pwa-shell-build.yml"):
    """Write a callee and its caller; return the caller's path."""
    wf(tmp_path, callee, callee_body)
    return wf(tmp_path, "selftest.yml", caller_body)


def test_r4_flags_the_real_startup_failure_shape(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "id-token" in found[0].message
    assert "pwa-shell-build.yml" in found[0].message


def test_r4_message_names_the_startup_failure_symptom(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    f = only(errors(run([p])), RULE_CALLER_PERMISSIONS)[0]
    assert "startup" in f.message.lower()
    assert "id-token: write" in f.fix


def test_r4_accepts_a_caller_that_grants_what_the_callee_declares(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
          id-token: write
        jobs:
          selftest:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert only(errors(run([p])), RULE_CALLER_PERMISSIONS) == []


def test_r4_job_level_permissions_can_supply_what_the_workflow_lacks(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            permissions:
              contents: read
              id-token: write
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert only(errors(run([p])), RULE_CALLER_PERMISSIONS) == []


def test_r4_job_level_permissions_replace_rather_than_merge(tmp_path):
    """GitHub replaces the workflow block wholesale; a narrower job block loses the scope."""
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
          id-token: write
        jobs:
          selftest:
            permissions:
              contents: read
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert len(only(errors(run([p])), RULE_CALLER_PERMISSIONS)) == 1


def test_r4_no_permissions_block_at_all_still_fails_on_id_token(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        jobs:
          selftest:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert len(only(errors(run([p])), RULE_CALLER_PERMISSIONS)) == 1


def test_r4_no_permissions_block_is_not_flagged_for_ordinary_scopes(tmp_path):
    """The default token's contents/packages level depends on an org setting we cannot read."""
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        jobs:
          selftest:
            uses: ./.github/workflows/plain.yml
        """,
        callee_body="""
        name: plain (reusable)
        on:
          workflow_call:
        permissions:
          contents: read
        jobs:
          a:
            runs-on: ubuntu-devino
            steps:
              - run: echo ok
        """,
        callee="plain.yml",
    )
    assert only(errors(run([p])), RULE_CALLER_PERMISSIONS) == []


def test_r4_read_does_not_satisfy_write(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            uses: ./.github/workflows/writer.yml
        """,
        callee_body="""
        name: writer (reusable)
        on:
          workflow_call:
        permissions:
          contents: write
        jobs:
          a:
            runs-on: ubuntu-devino
            steps:
              - run: echo ok
        """,
        callee="writer.yml",
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "contents" in found[0].message


def test_r4_write_all_satisfies_everything(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions: write-all
        jobs:
          selftest:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert only(errors(run([p])), RULE_CALLER_PERMISSIONS) == []


def test_r4_write_all_callee_gets_a_usable_fix_block(tmp_path):
    # F7. "(all scopes)" was filtered out of the fix list, so the whole fix
    # rendered as a heading with nothing under it.
    p = two(
        tmp_path,
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
        """
        name: everything (reusable)
        on:
          workflow_call:
        permissions: write-all
        jobs:
          build:
            runs-on: ubuntu-devino
            steps:
              - run: echo build
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "permissions: write-all" in found[0].fix
    assert not found[0].fix.rstrip().endswith("permissions:")


def test_r4_write_all_callee_fix_block_survives_a_job_level_caller_block(tmp_path):
    p = two(
        tmp_path,
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: write
        jobs:
          a:
            permissions:
              contents: read
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
        """
        name: everything (reusable)
        on:
          workflow_call:
        permissions: write-all
        jobs:
          build:
            runs-on: ubuntu-devino
            steps:
              - run: echo build
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "permissions: write-all" in found[0].fix
    assert "REPLACES" in found[0].fix


def test_r4_counts_permissions_declared_on_a_callee_job(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            uses: ./.github/workflows/jobperms.yml
        """,
        callee_body="""
        name: job perms (reusable)
        on:
          workflow_call:
        jobs:
          a:
            runs-on: ubuntu-devino
            permissions:
              contents: read
              id-token: write
            steps:
              - run: echo ok
        """,
        callee="jobperms.yml",
    )
    assert len(only(errors(run([p])), RULE_CALLER_PERMISSIONS)) == 1


def test_r4_resolves_the_org_qualified_form_when_the_file_is_here(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            uses: DevinoSolutions/.github/.github/workflows/pwa-shell-build.yml@main
        """,
    )
    assert len(only(errors(run([p])), RULE_CALLER_PERMISSIONS)) == 1


def test_r4_org_qualified_form_ignores_a_third_party_owner(tmp_path):
    # F3. `_ORG_USES_RE` captured only the path, so a call into someone else's
    # repository was compared against whatever local file happened to share the
    # filename. sentry-selfhost calls
    # getsentry/craft/.github/workflows/changelog-preview.yml@v2; the day it
    # grows a local changelog-preview.yml, R4 would start reporting that file's
    # permissions as if they were craft's -- a fabricated failure, or worse a
    # real one masked by a permissive local namesake.
    p = two(
        tmp_path,
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: some-other-org/thing/.github/workflows/pwa-shell-build.yml@v1
        """,
    )
    findings = run([p])
    assert errors(findings) == []
    skipped = notes(findings)
    assert len(skipped) == 1
    assert "some-other-org/thing" in skipped[0].message


def test_r4_org_qualified_form_resolves_the_org_reusable_repo(tmp_path):
    # The complement: the shape R4 is actually for keeps working.
    p = two(
        tmp_path,
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: DevinoSolutions/.github/.github/workflows/pwa-shell-build.yml@main
        """,
    )
    assert len(only(errors(run([p])), RULE_CALLER_PERMISSIONS)) == 1


def test_r4_unresolvable_callee_is_a_note_not_a_failure(tmp_path):
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: some-other-org/repo/.github/workflows/thing.yml@v1
        """,
    )
    findings = run([p])
    assert errors(findings) == []
    skipped = notes(findings)
    assert len(skipped) == 1
    assert skipped[0].rule == RULE_CALLER_PERMISSIONS
    assert "some-other-org/repo" in skipped[0].message


def test_r4_missing_local_callee_is_a_failure_not_a_note(tmp_path):
    # caly's `pr.yml` called ./.github/workflows/nextjs-bundle-analysis.yml, a
    # file that does not exist in that repository. Every run of it between
    # 2026-06-04 and 2026-09-05 -- 47 of them -- ended in startup_failure with
    # zero jobs, which is why caly had no PR CI at all for three months. A
    # `./` path either resolves inside the checkout or the run cannot start;
    # there is nothing to be charitable about, so this is an error.
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: ./.github/workflows/does-not-exist.yml
        """,
    )
    findings = run([p])
    assert notes(findings) == []
    found = only(errors(findings), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "does-not-exist.yml" in found[0].message
    assert "startup" in found[0].message.lower()
    assert found[0].fix


def test_r4_missing_local_callee_anchors_to_the_uses_line(tmp_path):
    # Unlike a permissions narrowing, this one is caused by the `uses:` line
    # itself, so added-lines mode catches it the moment the call is written.
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        jobs:
          a:
            uses: ./.github/workflows/gone.yml
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert found[0].line == 6
    assert 6 in found[0].anchor_lines


def test_r4_missing_local_callee_still_honours_the_marker(tmp_path):
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        jobs:
          a:
            # ci-lint: allow-permissions the callee lands in a follow-up PR
            uses: ./.github/workflows/not-yet.yml
        """,
    )
    assert run([p]) == []


def test_r4_a_callee_that_is_not_reusable_says_so(tmp_path):
    # F6: "resolved, but has no workflow_call trigger" is not the same as
    # "could not be resolved here", and a caller pointing at a workflow that
    # lost its workflow_call: trigger is itself a broken call worth naming.
    p = two(
        tmp_path,
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
        """
        name: not reusable
        on:
          push:
        permissions:
          id-token: write
        jobs:
          build:
            runs-on: ubuntu-devino
            steps:
              - run: echo build
        """,
    )
    findings = run([p])
    assert errors(findings) == []
    skipped = notes(findings)
    assert len(skipped) == 1
    assert "workflow_call" in skipped[0].message
    assert "not resolvable in this checkout" not in skipped[0].message


def test_r4_remote_callee_keeps_the_unresolvable_wording(tmp_path):
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        jobs:
          a:
            uses: some-other-org/repo/.github/workflows/thing.yml@v1
        """,
    )
    skipped = notes(run([p]))
    assert len(skipped) == 1
    assert "not resolvable in this checkout" in skipped[0].message


def test_r4_follows_a_transitive_callee(tmp_path):
    # F5. A reusable workflow that declares nothing itself but whose job calls
    # a second reusable workflow needing `id-token: write` still requires the
    # ROOT caller to hold it -- permissions are passed down the whole chain,
    # not one hop. The org's reusable workflows are one level deep today, but
    # pwa-release.yml is exactly the file that grows a nested call.
    wf(
        tmp_path,
        "inner.yml",
        """
        name: inner
        on:
          workflow_call:
        permissions:
          id-token: write
        jobs:
          sign:
            runs-on: ubuntu-devino
            steps:
              - run: echo sign
        """,
    )
    wf(
        tmp_path,
        "middle.yml",
        """
        name: middle
        on:
          workflow_call:
        jobs:
          relay:
            uses: ./.github/workflows/inner.yml
        """,
    )
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: ./.github/workflows/middle.yml
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "id-token" in found[0].message


def test_r4_transitive_chain_is_satisfied_by_the_root_caller(tmp_path):
    wf(
        tmp_path,
        "inner.yml",
        """
        name: inner
        on:
          workflow_call:
        permissions:
          id-token: write
        jobs:
          sign:
            runs-on: ubuntu-devino
            steps:
              - run: echo sign
        """,
    )
    wf(
        tmp_path,
        "middle.yml",
        """
        name: middle
        on:
          workflow_call:
        jobs:
          relay:
            uses: ./.github/workflows/inner.yml
        """,
    )
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
          id-token: write
        jobs:
          a:
            uses: ./.github/workflows/middle.yml
        """,
    )
    assert run([p]) == []


def test_r4_survives_a_cycle_between_two_reusable_workflows(tmp_path):
    # A malformed pair that calls each other must not hang the linter.
    wf(
        tmp_path,
        "ping.yml",
        """
        name: ping
        on:
          workflow_call:
        jobs:
          a:
            uses: ./.github/workflows/pong.yml
        """,
    )
    wf(
        tmp_path,
        "pong.yml",
        """
        name: pong
        on:
          workflow_call:
        permissions:
          id-token: write
        jobs:
          b:
            uses: ./.github/workflows/ping.yml
        """,
    )
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            uses: ./.github/workflows/ping.yml
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "id-token" in found[0].message


def test_r4_marker_on_the_job_exempts_it(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            # ci-lint: allow-permissions the OIDC upload is disabled for this fixture run
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    assert only(errors(run([p])), RULE_CALLER_PERMISSIONS) == []


def test_r4_marker_silences_the_skip_note_too(tmp_path):
    # The marker means "do not check this job", so an exempted job whose callee
    # is unresolvable produces neither a failure nor a note about not checking it.
    p = wf(
        tmp_path,
        "caller.yml",
        """
        name: caller
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            # ci-lint: allow-permissions the org workflow is reviewed in its own repo
            uses: some-other-org/repo/.github/workflows/thing.yml@v1
        """,
    )
    findings = run([p])
    assert errors(findings) == []
    assert notes(findings) == []


def test_r4_marker_needs_a_reason(tmp_path):
    p = two(
        tmp_path,
        """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            # ci-lint: allow-permissions
            uses: ./.github/workflows/pwa-shell-build.yml
        """,
    )
    found = only(errors(run([p])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert "reason" in found[0].message.lower()


def test_r4_the_no_reason_message_names_the_prefixed_marker(tmp_path):
    """Same defect class as R3, on the caller-permissions rule.

    `_no_reason_prefix` printed the bare `# allow-permissions` while
    `_marker_re` only ever matches `#\\s*ci-lint:\\s*allow-permissions\\b`. The
    reader of that sentence is already holding a marker they wrote; quoting it
    back to them WITHOUT the prefix says the prefixed form they used is not the
    one the linter wants, and the obvious "fix" — dropping the prefix — turns a
    working exemption into a silently ignored comment. Asserting through
    `lint.ALLOW_PERMISSIONS_MARKER` rather than a literal keeps this honest if
    the marker is ever renamed.
    """
    marker = f"ci-lint: {lint.ALLOW_PERMISSIONS_MARKER}"
    caller = """
        name: selftest
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          selftest:
            # {marker}
            uses: ./.github/workflows/pwa-shell-build.yml
        """.replace("{marker}", marker)

    found = only(errors(run([two(tmp_path, caller)])), RULE_CALLER_PERMISSIONS)
    assert len(found) == 1
    assert f"`# {marker}`" in found[0].message, (
        f"the no-reason message must quote the marker WITH its `ci-lint:` prefix, "
        f"got:\n{found[0].message}"
    )

    # Close the loop: the marker as quoted, plus a reason, must actually exempt.
    # A message naming a marker the rule cannot match is the bug being fixed.
    with_reason = caller.replace(
        f"# {marker}\n",
        f"# {marker} the callee is reviewed in its own repo\n",
    )
    assert only(errors(run([two(tmp_path, with_reason)])), RULE_CALLER_PERMISSIONS) == []


def test_r4_ignores_a_job_that_is_not_a_workflow_call(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: ci
        on:
          workflow_dispatch:
        permissions:
          contents: read
        jobs:
          a:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - run: echo ok
        """,
    )
    assert run([p]) == []


# -- unparseable input -----------------------------------------------------


def test_unparseable_workflow_is_reported_not_raised(tmp_path):
    p = wf(tmp_path, "broken.yml", "name: x\non: [push\njobs: {\n")
    found = run([p])
    assert rules(found) == [RULE_UNPARSEABLE]
    assert found[0].line >= 1


def test_missing_file_is_skipped(tmp_path):
    assert lint_files([str(tmp_path / "nope.yml")], added_lines_only=False, baseline_sha=None) == []


# -- findings shape --------------------------------------------------------


def test_findings_carry_file_line_rule_message_and_fix(tmp_path):
    p = wf(
        tmp_path,
        "t.yml",
        """
        name: t
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build .
        """,
    )
    findings = run([p])
    assert len(findings) == 2
    for f in findings:
        assert f.file == str(p)
        assert isinstance(f.line, int)
        assert f.line >= 1
        assert f.rule
        assert f.message
        assert f.fix


# -- baseline semantics ----------------------------------------------------


def test_resolve_baseline_off_when_the_marker_file_is_absent(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
    base = commit(repo, "init")
    assert resolve_baseline(repo, base) == (False, None)


def test_resolve_baseline_on_when_the_marker_sha_is_an_ancestor(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
    marker = commit(repo, "init")
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".github" / "ci-lint-baseline").write_text(marker + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "baseline")
    assert resolve_baseline(repo, base) == (True, marker)


def test_resolve_baseline_off_when_the_marker_sha_is_not_an_ancestor(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
    base = commit(repo, "init")
    git(repo, "checkout", "-q", "-b", "side", base)
    (repo / "side.txt").write_text("y\n", encoding="utf-8", newline="\n")
    side = commit(repo, "side")
    git(repo, "checkout", "-q", "main")
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".github" / "ci-lint-baseline").write_text(side + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "baseline naming a non-ancestor")
    assert resolve_baseline(repo, base) == (False, None)


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "\n",
        "not-a-sha\n",
        "zzzzzzz\n",
        "12345\n",
        "# the sha of the migration commit goes here\n",
        "0123456789abcdef0123456789abcdef0123456789abcdef\n",
        "deadbeef cafe\n",
    ],
)
def test_resolve_baseline_off_when_the_marker_is_malformed(tmp_path, contents):
    repo = init_repo(tmp_path)
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "ci-lint-baseline").write_text(contents, encoding="utf-8", newline="\n")
    base = commit(repo, "malformed baseline")
    assert resolve_baseline(repo, base) == (False, None)


def test_resolve_baseline_off_when_the_marker_sha_is_unknown_to_the_repo(tmp_path):
    repo = init_repo(tmp_path)
    (repo / ".github").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "ci-lint-baseline").write_text("0" * 40 + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "baseline naming a commit that does not exist")
    assert resolve_baseline(repo, base) == (False, None)


def test_resolve_baseline_accepts_an_abbreviated_sha(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
    marker = commit(repo, "init")
    (repo / ".github").mkdir(exist_ok=True)
    (repo / ".github" / "ci-lint-baseline").write_text(marker[:10] + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "baseline")
    assert resolve_baseline(repo, base) == (True, marker[:10])


def test_added_lines_only_reports_the_new_violation_and_grandfathers_the_old(tmp_path):
    repo = init_repo(tmp_path)
    path = wf(
        repo,
        "build.yml",
        """
        name: build
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build -t old .
        """,
    )
    marker = commit(repo, "pre-existing build step")
    (repo / ".github" / "ci-lint-baseline").write_text(marker + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "adopt the lint baseline")
    path.write_text(
        path.read_text(encoding="utf-8") + "      - run: docker build -t new .\n",
        encoding="utf-8",
        newline="\n",
    )
    commit(repo, "add a second build step")

    rel = ".github/workflows/build.yml"
    whole = lint_files([rel], added_lines_only=False, baseline_sha=None, repo_root=repo)
    assert len(only(whole, RULE_DOCKER_BUILD)) == 2

    added = lint_files([rel], added_lines_only=True, baseline_sha=marker, base_ref=base, repo_root=repo)
    found = only(added, RULE_DOCKER_BUILD)
    assert len(found) == 1
    assert found[0].line == 12


def test_added_lines_only_still_catches_a_newly_added_trigger(tmp_path):
    repo = init_repo(tmp_path)
    path = wf(
        repo,
        "t.yml",
        """
        name: t
        on:
          workflow_dispatch:
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: echo hi
        """,
    )
    marker = commit(repo, "manual only")
    (repo / ".github" / "ci-lint-baseline").write_text(marker + "\n", encoding="utf-8", newline="\n")
    base = commit(repo, "adopt the lint baseline")
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  workflow_dispatch:\n", "  workflow_dispatch:\n  push:\n    branches: [main]\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    commit(repo, "also run on push")

    added = lint_files(
        [".github/workflows/t.yml"], added_lines_only=True, baseline_sha=marker, base_ref=base, repo_root=repo
    )
    assert len(only(added, RULE_CONCURRENCY)) == 1


def test_added_lines_only_stays_quiet_on_an_untouched_file(tmp_path):
    repo = init_repo(tmp_path)
    wf(
        repo,
        "build.yml",
        """
        name: build
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build .
        """,
    )
    marker = commit(repo, "pre-existing violations")
    (repo / "README.md").write_text("x\n", encoding="utf-8", newline="\n")
    base = commit(repo, "unrelated")
    (repo / "README.md").write_text("y\n", encoding="utf-8", newline="\n")
    commit(repo, "unrelated change")

    added = lint_files(
        [".github/workflows/build.yml"], added_lines_only=True, baseline_sha=marker, base_ref=base, repo_root=repo
    )
    assert added == []


# -- R4 under the added-lines filter ---------------------------------------
#
# Added-lines mode is the fleet default: `apply-caller.sh` stamps a baseline at
# adoption. A PR that *narrows* a caller's `permissions:` block does not touch
# the job's `uses:` line, so R4 has to anchor to the permissions block itself,
# and the filter has to notice a line that was removed as well as one added --
# deleting `id-token: write` adds no line at all.

R4_CALLEE = """
    name: reusable
    on:
      workflow_call:
    permissions:
      id-token: write
    jobs:
      build:
        runs-on: ubuntu-devino
        steps:
          - run: echo build
    """

R4_CALLER_OK = """
    name: caller
    on:
      pull_request:
    concurrency:
      group: g
      cancel-in-progress: true
    permissions:
      contents: read
      id-token: write
    jobs:
      call:
        uses: ./.github/workflows/reusable.yml
    """


def write_baseline(repo: Path, sha: str) -> None:
    (repo / ".github" / "ci-lint-baseline").write_text(sha + "\n", encoding="utf-8", newline="\n")


def _r4_baseline_repo(tmp_path):
    """A repo whose caller is clean and adopted, ready for a permissions edit."""
    repo = init_repo(tmp_path)
    wf(repo, "reusable.yml", R4_CALLEE)
    caller = wf(repo, "caller.yml", R4_CALLER_OK)
    marker = commit(repo, "clean caller")
    write_baseline(repo, marker)
    base = commit(repo, "adopt the lint baseline")
    return repo, caller, marker, base


def _r4_modes(repo, marker, base):
    rel = ".github/workflows/caller.yml"
    whole = lint_files([rel], added_lines_only=False, baseline_sha=None, repo_root=repo)
    added = lint_files([rel], added_lines_only=True, baseline_sha=marker, base_ref=base, repo_root=repo)
    return only(errors(whole), RULE_CALLER_PERMISSIONS), only(errors(added), RULE_CALLER_PERMISSIONS)


def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def test_r4_added_lines_catches_a_permission_the_pr_deleted(tmp_path):
    repo, caller, marker, base = _r4_baseline_repo(tmp_path)
    _rewrite(caller, "  id-token: write\n", "")
    commit(repo, "tighten the caller permissions")

    whole, added = _r4_modes(repo, marker, base)
    assert len(whole) == 1
    assert len(added) == 1, "a PR that deletes the scope the callee needs must not be grandfathered"


def test_r4_added_lines_catches_a_permission_the_pr_downgraded(tmp_path):
    repo, caller, marker, base = _r4_baseline_repo(tmp_path)
    _rewrite(caller, "  id-token: write\n", "  id-token: read\n")
    commit(repo, "downgrade id-token to read")

    whole, added = _r4_modes(repo, marker, base)
    assert len(whole) == 1
    assert len(added) == 1


def test_r4_added_lines_catches_a_job_level_block_that_replaces_a_good_one(tmp_path):
    # The workflow-level block still grants id-token; the PR adds a job-level
    # block, which REPLACES it rather than adding to it. Only the two new job
    # lines were added, so the finding has to anchor to them.
    repo, caller, marker, base = _r4_baseline_repo(tmp_path)
    _rewrite(
        caller,
        "    uses: ./.github/workflows/reusable.yml\n",
        "    permissions:\n      contents: read\n    uses: ./.github/workflows/reusable.yml\n",
    )
    commit(repo, "add a job-level permissions block")

    whole, added = _r4_modes(repo, marker, base)
    assert len(whole) == 1
    assert len(added) == 1


def test_r4_added_lines_still_grandfathers_an_untouched_caller(tmp_path):
    # The complement of the three above: R4's wider anchors must not make an
    # existing violation resurface on a PR that edits something unrelated.
    repo = init_repo(tmp_path)
    wf(repo, "reusable.yml", R4_CALLEE)
    caller = wf(
        repo,
        "caller.yml",
        """
        name: caller
        on:
          pull_request:
        concurrency:
          group: g
          cancel-in-progress: true
        permissions:
          contents: read
        jobs:
          call:
            uses: ./.github/workflows/reusable.yml
        """,
    )
    marker = commit(repo, "pre-existing R4 violation")
    write_baseline(repo, marker)
    base = commit(repo, "adopt the lint baseline")
    _rewrite(caller, "name: caller\n", "name: caller renamed\n")
    commit(repo, "rename the workflow")

    whole, added = _r4_modes(repo, marker, base)
    assert len(whole) == 1
    assert added == []


def test_added_lines_only_counts_a_deletion_as_touching_its_neighbours(tmp_path):
    # The filter primitive behind the R4 cases above, tested on its own: a hunk
    # that only removes lines still marks the gap the removal left behind, so a
    # rule whose cause is a line that is no longer there can still fire.
    repo = init_repo(tmp_path)
    path = repo / "f.txt"
    path.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8", newline="\n")
    base = commit(repo, "five lines")
    path.write_text("l1\nl2\nl4\nl5\n", encoding="utf-8", newline="\n")
    commit(repo, "drop l3")

    assert lint.added_lines(repo, base, ["f.txt"])["f.txt"] == {2, 3}


# -- output formatting and CLI ---------------------------------------------


def test_annotations_are_github_error_commands(tmp_path):
    p = wf(
        tmp_path,
        "t.yml",
        """
        name: t
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: echo hi
        """,
    )
    text = lint.format_annotations(run([p]))
    assert text.startswith("::error file=")
    assert ",line=" in text
    assert "\n" not in text.rstrip("\n")


def test_summary_lists_every_rule_that_fired(tmp_path):
    p = wf(
        tmp_path,
        "t.yml",
        """
        name: t
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: docker build .
        """,
    )
    summary = lint.format_summary(run([p]), [str(p)])
    assert RULE_CONCURRENCY in summary
    assert RULE_DOCKER_BUILD in summary


def test_cli_exits_non_zero_and_annotates(tmp_path, capsys):
    p = wf(
        tmp_path,
        "t.yml",
        """
        name: t
        on: push
        jobs:
          b:
            runs-on: ubuntu-devino
            steps:
              - run: echo hi
        """,
    )
    summary = tmp_path / "summary.md"
    code = lint.main([str(p), "--summary-file", str(summary)])
    assert code == 1
    assert "::error file=" in capsys.readouterr().out
    assert RULE_CONCURRENCY in summary.read_text(encoding="utf-8")


def test_cli_exits_zero_on_a_clean_file(tmp_path, capsys):
    p = wf(tmp_path, "ci.yml", CLEAN)
    assert lint.main([str(p)]) == 0
    assert "::error" not in capsys.readouterr().out


def test_cli_with_no_paths_is_a_no_op(tmp_path):
    assert lint.main([]) == 0


# -- the interim per-repo caller sweep -------------------------------------
#
# Until the org is off the Free plan the ruleset cannot exist, so repos opt in
# by committing a caller workflow. The template has to satisfy the very lint it
# invokes, and the sweep script has to be syntactically sound before anyone
# points it at 60 repositories.

CI_LINT_DIR = Path(__file__).resolve().parents[1]


def test_caller_template_exists_and_is_clean_under_the_lint():
    template = CI_LINT_DIR / "caller-template.yml"
    assert template.is_file()
    found = lint.lint_text(str(template), template.read_text(encoding="utf-8"))
    # a note for the org-qualified callee is expected; nothing may be an error
    assert errors(found) == []


def test_caller_template_calls_the_public_workflow_and_passes_no_secrets():
    text = (CI_LINT_DIR / "caller-template.yml").read_text(encoding="utf-8")
    assert "uses: DevinoSolutions/ci-tooling/.github/workflows/ci-lint.yml@main" in text
    # the workflow lives in a public repo and needs no token: nothing is passed
    assert "secrets:" not in text
    assert "CI_LINT_READ_TOKEN" not in text
    assert "\r" not in text


def _concurrency_group(text: str) -> str:
    match = re.search(r"^concurrency:\n\s+group:\s*(?P<group>.+)$", text, re.MULTILINE)
    assert match is not None, "no top-level concurrency group found"
    return match.group("group").strip()


def test_caller_and_reusable_cannot_share_a_concurrency_group():
    """A caller and the workflow it calls must not land in the same group.

    `github.workflow` resolves to the CALLER's workflow name on both sides, so
    two blocks written `ci-lint-${{ github.workflow }}-...` collide and the run
    cancels itself. The groups must be distinguishable without evaluating any
    expression, hence: no `github.workflow` on either side, and different text.
    """
    caller = _concurrency_group((CI_LINT_DIR / "caller-template.yml").read_text(encoding="utf-8"))
    reusable = _concurrency_group(
        (CI_LINT_DIR.parents[1] / ".github" / "workflows" / "ci-lint.yml").read_text(encoding="utf-8")
    )
    assert "github.workflow" not in caller
    assert "github.workflow" not in reusable
    assert caller != reusable


def test_both_workflows_pin_pyyaml():
    root = CI_LINT_DIR.parents[1] / ".github" / "workflows"
    for name in ("ci-lint.yml", "selftest.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "--with pyyaml==" in text, name
        assert "--with pyyaml " not in text, name


def test_reusable_workflow_needs_no_secret_or_token():
    """The linter is fetched anonymously from the public ci-tooling repo.

    The old design read the private `.github` repo with an org secret that was
    never created, so every caller failed with "Repository not found". Nothing
    in the workflow may name a secret or pass a `token:` again.
    """
    text = (CI_LINT_DIR.parents[1] / ".github" / "workflows" / "ci-lint.yml").read_text(encoding="utf-8")
    assert "CI_LINT_READ_TOKEN" not in text
    assert "secrets." not in text
    assert "token:" not in text
    block = text.split("Check out the linter from DevinoSolutions/ci-tooling", 1)[1]
    block = block.split("- name: Locate the linter", 1)[0]
    assert "repository: DevinoSolutions/ci-tooling" in block


def test_reusable_workflow_runner_follows_the_callers_visibility():
    """Private callers use the self-hosted pool; public callers must use GitHub-hosted."""
    text = (CI_LINT_DIR.parents[1] / ".github" / "workflows" / "ci-lint.yml").read_text(encoding="utf-8")
    assert (
        "runs-on: ${{ github.event.repository.private && 'ubuntu-devino' || 'ubuntu-latest' }}" in text
    )


def test_apply_caller_script_is_valid_bash():
    script = CI_LINT_DIR / "apply-caller.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "ci/track1-w5-caller" in text
    assert "\r" not in text
    # The script goes in on stdin rather than by path: on a Windows dev box the
    # `bash` on PATH may be WSL's, which cannot see a C:\ path at all. Bytes,
    # not text=True, or Python's pipe rewrites every \n as \r\n and bash chokes.
    checked = subprocess.run(["bash", "-n"], input=text.encode(), capture_output=True, check=False)
    assert checked.returncode == 0, checked.stderr.decode()


def test_apply_caller_script_also_writes_the_baseline_marker():
    """Adoption has to allowlist what is already there, or the sweep opens 60 red PRs."""
    text = (CI_LINT_DIR / "apply-caller.sh").read_text(encoding="utf-8")
    assert ".github/ci-lint-baseline" in text
    assert "rev-parse HEAD" in text


def test_apply_caller_script_is_idempotent_about_both_files():
    text = (CI_LINT_DIR / "apply-caller.sh").read_text(encoding="utf-8")
    # each file is written only when absent, and a repo with both is skipped
    assert text.count('if [ -f "${dir}/${TARGET}" ]') == 1
    assert text.count('if [ -f "${dir}/${BASELINE}" ]') == 1
    assert "nothing to do" in text
    assert "--exit-code --heads origin" in text


def test_apply_caller_script_refuses_to_run_without_a_repo():
    text = (CI_LINT_DIR / "apply-caller.sh").read_text(encoding="utf-8")
    result = subprocess.run(["bash", "-s"], input=text.encode(), capture_output=True, check=False)
    assert result.returncode != 0
    assert b"usage" in (result.stdout + result.stderr).lower()


# -- R5: bun-engine-order --------------------------------------------------
#
# The pool shipped no system node until the v1 runner image (2026-09-11 14:56Z).
# Bun aliases `node` to itself for the children a script spawns when no node is
# on PATH, so a pool job that runs bun BEFORE `actions/setup-node` had its engine
# chosen by the image, and that choice flipped from JavaScriptCore to V8 with no
# commit behind it. The rule is about ORDER; `superbooks` had a setup-node all
# along, seven lines too late.


def test_r5_pool_job_running_bun_with_no_setup_node_is_reported(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          frontend-quality:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - uses: oven-sh/setup-bun@v2
              - run: bun install --frozen-lockfile
              - run: bun run typecheck
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert "frontend-quality" in found[0].message
    assert "never calls `actions/setup-node`" in found[0].message
    assert "actions/setup-node@v4" in found[0].fix


def test_r5_setup_node_after_the_first_bun_step_is_reported(tmp_path):
    """The superbooks shape: setup-node present, but too late to matter."""
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          type-aware-lint:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - uses: oven-sh/setup-bun@v2
              - run: bun install --frozen-lockfile
              - run: bun run lint:type-aware
              - uses: actions/setup-node@v4
                with:
                  node-version: 22
              - run: node scripts/ratchet.mjs
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert "only at line" in found[0].message


def test_r5_setup_node_before_the_first_bun_step_passes(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          frontend-quality:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - uses: oven-sh/setup-bun@v2
              - uses: actions/setup-node@v4
                with:
                  node-version: 22
              - run: bun install --frozen-lockfile
              - run: bun run typecheck
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_github_hosted_runner_is_not_reported(tmp_path):
    """Only the self-hosted pool ever lacked a system node."""
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          frontend-quality:
            runs-on: ubuntu-latest
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_setup_bun_with_no_bun_command_is_not_reported(tmp_path):
    """Nothing runs, so there is no engine to get wrong."""
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          prep:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: echo done
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_the_word_ubuntu_devino_is_not_a_bun_command(tmp_path):
    """`ubuntu-devino` contains the substring `bun`; `bundle` starts with it.

    A naive substring match flags the entire fleet on the runner label alone.
    """
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          echo-runner:
            runs-on: ubuntu-devino
            steps:
              - run: echo "this job runs on ubuntu-devino and bundles nothing"
              - run: bundle exec rails test
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


@pytest.mark.parametrize(
    "body, runs_bun",
    [
        ("bun install --frozen-lockfile", True),
        ("cd web && bun run build", True),
        ("VITEST_MAX_WORKERS=4 bun test", True),
        ("if [ -f x ]; then bun install; fi", True),
        ("bunx prettier --check .", True),
        ("sudo bun run build", True),
        ("$(bun --version)", True),
        # Command position, not mere presence. Every False case below is a real
        # string from the org's workflows or one line away from one.
        (r"watch='^(apps/web/|packages/|package\.json$|bun\.lock$)'", False),
        (r'grep -E "^bunfig\.toml$" changed.txt', False),
        ('echo "this job runs on ubuntu-devino"', False),
        ("bundle exec rails test", False),
        ('echo "we use bun for this repo"', False),
        ("# bun install", False),
        ("npm run build", False),
    ],
)
def test_r5_runs_bun_is_about_command_position(body, runs_bun):
    """`bun.lock` in a path filter is not an invocation of bun.

    Three superbooks jobs were reported by an earlier draft of this rule for
    exactly that reason, found by running it over all 212 repositories before
    shipping it. A lint that cries wolf on `bun.lock` gets switched off.
    """
    assert lint._runs_bun(body) is runs_bun


def test_r5_a_path_filter_naming_bun_lock_is_not_a_bun_step(tmp_path):
    """End-to-end form of the case above, as a whole workflow."""
    p = wf(
        tmp_path,
        "deploy-verify.yml",
        """
        name: Deploy verify
        on:
          push:
            branches: [main]
        jobs:
          post-deploy-verify:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - name: Decide whether the diff can affect the deploy
                run: |
                  web_watch='^(apps/web/|packages/|package\.json$|bun\.lock$|turbo\.json$)'
                  echo "$web_watch"
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_bunx_counts_as_a_bun_command(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          format:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bunx prettier --check .
        """,
    )
    assert len(only(run([p]), RULE_BUN_ENGINE_ORDER)) == 1


def test_r5_a_bun_mention_in_a_run_comment_does_not_count(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          note:
            runs-on: ubuntu-devino
            steps:
              - run: |
                  # we used to run bun here, now we do not
                  echo ok
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_list_runs_on_containing_the_pool_is_reported(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          gate:
            runs-on: [self-hosted, ubuntu-devino]
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run build
        """,
    )
    assert len(only(run([p]), RULE_BUN_ENGINE_ORDER)) == 1


def test_r5_event_conditional_runner_resolves_both_branches(tmp_path):
    """The nightly ternary puts the job on the pool on both legs."""
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          schedule:
            - cron: '0 3 * * *'
          pull_request:
        jobs:
          gate:
            # ci-lint: allow-schedule-main not what this test is about
            runs-on: ${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run build
        """,
    )
    assert len(only(run([p]), RULE_BUN_ENGINE_ORDER)) == 1


def test_r5_unresolvable_runner_expression_is_not_guessed_at(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run build
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_allow_bun_engine_on_the_job_exempts_it(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          worker-tests:
            # ci-lint: allow-bun-engine the iii suite IS a bun:test suite; bunfig pins [run] bun = true
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun test
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_allow_bun_engine_in_the_header_covers_every_job(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        # ci-lint: allow-bun-engine this whole workflow is the bun-runtime conformance suite
        name: CI
        on:
          pull_request:
        jobs:
          one:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run a
          two:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run b
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5_allow_bun_engine_without_a_reason_is_not_an_exemption(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          worker-tests:
            # ci-lint: allow-bun-engine
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun test
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert "no reason follows it" in found[0].message


def test_r5_finding_is_anchored_to_the_bun_step(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          gate:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/checkout@v4
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert p.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == (
        "- run: bun run typecheck"
    )


def test_r5_only_the_offending_job_in_a_mixed_workflow_is_reported(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          good:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/setup-node@v4
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
          bad:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert "`bad`" in found[0].message


def test_r5_written_marker_carries_the_ci_lint_prefix(tmp_path):
    """The remediation must name the marker this linter actually matches.

    `allow-schedule-main` and `allow-permissions` both shipped printing a bare
    name that `_marker_re` never matches, so the exemption a reader copied did
    nothing. This closes the loop: the hint is parsed back out.
    """
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          gate:
            runs-on: ubuntu-devino
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    marker_line = next(
        line for line in found[0].fix.splitlines() if lint.ALLOW_BUN_ENGINE_MARKER in line
    )
    assert lint._ALLOW_BUN_ENGINE_RE.search(marker_line) is not None


# -- R5b: reusable workflows -----------------------------------------------
#
# A reusable workflow's `runs-on` is almost always `${{ inputs.runner }}`, which
# reading that file alone cannot resolve — and that is exactly where the org's
# shared lanes live, so every caller inherits the step order. Two things resolve
# it: the input's own `default:`, and what callers actually pass.


def test_r5b_input_default_on_the_pool_is_linted(tmp_path):
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                description: 'Runner label.'
                required: false
                type: string
                default: ubuntu-devino
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    found = only(run([p]), RULE_BUN_ENGINE_ORDER)
    assert len(found) == 1
    assert "inputs.runner" in found[0].message


def test_r5b_event_conditional_input_default_is_resolved(tmp_path):
    """The org's ten reusables default to the nightly/main ternary."""
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                required: false
                type: string
                default: ${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    assert len(only(run([p]), RULE_BUN_ENGINE_ORDER)) == 1


def test_r5b_hosted_input_default_is_not_linted(tmp_path):
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                required: false
                type: string
                default: ubuntu-latest
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5b_a_caller_passing_a_pool_label_makes_it_linted(tmp_path):
    """Hosted default, but a caller in the corpus passes the pool label."""
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                required: false
                type: string
                default: ubuntu-latest
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    text = p.read_text(encoding="utf-8")
    assert lint.lint_text(str(p), text) == []
    found = [
        f
        for f in lint.lint_text(str(p), text, caller_values={"runner": ["ubuntu-devino"]})
        if f.rule == RULE_BUN_ENGINE_ORDER
    ]
    assert len(found) == 1


def test_r5b_a_caller_passing_a_hosted_label_does_not(tmp_path):
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                required: false
                type: string
                default: ubuntu-latest
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    text = p.read_text(encoding="utf-8")
    found = [
        f
        for f in lint.lint_text(
            str(p), text, caller_values={"runner": ["ubuntu-latest", "macos-15"]}
        )
        if f.rule == RULE_BUN_ENGINE_ORDER
    ]
    assert found == []


def test_r5b_a_pool_input_with_setup_node_first_passes(tmp_path):
    """The shape all three bun-capable org reusables already have."""
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          workflow_call:
            inputs:
              runner:
                required: false
                type: string
                default: ubuntu-devino
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - uses: actions/setup-node@v4
                with:
                  node-version: 22
              - run: bun install --frozen-lockfile
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


def test_r5b_an_unknown_input_is_still_not_guessed_at(tmp_path):
    """No workflow_call block at all, so the input resolves to nothing."""
    p = wf(
        tmp_path,
        "reusable.yml",
        """
        name: Reusable
        on:
          pull_request:
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: oven-sh/setup-bun@v2
              - run: bun run typecheck
        """,
    )
    assert only(run([p]), RULE_BUN_ENGINE_ORDER) == []


@pytest.mark.parametrize(
    "body, runs_bun",
    [
        # A `case` label is not a command; the command after it is.
        ("bun)  bun install --frozen-lockfile ;;", True),
        ('bun) DEF_INSTALL="bun install"; DEF_PRISMA="bunx prisma" ;;', False),
        ("pnpm) pnpm install --frozen-lockfile ;;", False),
        # Quoted text is data. Both of these are real lines from
        # .github/workflows/schema-drift.yml, and both reported before the fix.
        ('*) echo "::error::package-manager must be pnpm|bun|npm, got x"; exit 1 ;;', False),
        ("$(bun --version)", True),
        ("(cd web && bun run build)", True),
    ],
)
def test_r5b_case_labels_and_quoted_text(body, runs_bun):
    assert lint._runs_bun(body) is runs_bun


def test_reusable_workflow_passes_the_base_to_the_linter():
    """Without `--base` the linter ignores `.github/ci-lint-baseline` and lints whole files."""
    text = (CI_LINT_DIR.parents[1] / ".github" / "workflows" / "ci-lint.yml").read_text(encoding="utf-8")
    assert '--base "$BASE_SHA"' in text


def test_reusable_workflow_passes_the_base_to_the_linter():
    """Without `--base` the linter ignores `.github/ci-lint-baseline` and lints whole files."""
    text = (CI_LINT_DIR.parents[1] / ".github" / "workflows" / "ci-lint.yml").read_text(encoding="utf-8")
    assert '--base "$BASE_SHA"' in text
