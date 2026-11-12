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
    RULE_PNPM_STORE_CACHE,
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


# -- R6: pnpm-store-cache --------------------------------------------------
#
# Every ARC runner pod on a node shares one pnpm store through the
# `/var/cache/ci` hostPath. `actions/setup-node` with `cache: pnpm` extracts a
# ~2 GB tarball over that store while sibling pods install out of it, and the
# siblings die with ERR_PNPM_ENOENT copyfile (Shorty dev, 2026-11-10). Hosted
# runners have a private store per job, so the same step is correct there.


def pnpm_job(runs_on: str, setup: str = "actions/setup-node@v4", with_block: str = "cache: pnpm") -> str:
    """A one-job workflow whose setup-node step carries `with_block` (indented under `with:`)."""
    with_lines = "".join(f"          {line}\n" for line in with_block.splitlines()) if with_block else ""
    return (
        "name: CI\n"
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  test:\n"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: pnpm/action-setup@v4\n"
        f"      - uses: {setup}\n"
        "        with:\n"
        "          node-version: 22\n"
        f"{with_lines}"
        "      - run: pnpm install --frozen-lockfile\n"
    )


def pnpm_package_json(root: Path, manager: str = "pnpm@10.18.0") -> None:
    (root / "package.json").write_text('{"name": "x", "packageManager": "%s"}\n' % manager, encoding="utf-8")


@pytest.mark.parametrize(
    "runs_on",
    [
        "ubuntu-devino",
        "ubuntu-devino-nightly",
        "[self-hosted, ubuntu-devino]",
        "{ group: arc, labels: ubuntu-devino }",
        "{ group: arc, labels: [ubuntu-devino-nightly] }",
        "${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}",
    ],
)
def test_r6_cache_pnpm_on_the_pool_is_reported_in_every_runs_on_form(tmp_path, runs_on):
    p = wf(tmp_path, "ci.yml", pnpm_job(runs_on))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    text = p.read_text(encoding="utf-8").splitlines()
    assert text[found[0].line - 1].strip() == "cache: pnpm"
    assert "ERR_PNPM_ENOENT" in found[0].message
    assert "Delete `cache: pnpm`" in found[0].fix


@pytest.mark.parametrize("spelling", ["pnpm", "'pnpm'", '"pnpm"', "PNPM"])
def test_r6_every_spelling_of_the_cache_value_is_reported(tmp_path, spelling):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", with_block=f"cache: {spelling}"))
    assert len(only(run([p]), RULE_PNPM_STORE_CACHE)) == 1


@pytest.mark.parametrize(
    "runs_on",
    [
        "ubuntu-latest",
        "ubuntu-24.04",
        "[ubuntu-latest]",
        "{ labels: ubuntu-latest }",
        "macos-latest",
        # an expression this lint cannot resolve is not guessed at
        "${{ matrix.os }}",
    ],
)
def test_r6_cache_pnpm_off_the_pool_passes(tmp_path, runs_on):
    """caramel, upup, sendly-js, statify-sdk and dokploy-community use it on hosted runners."""
    p = wf(tmp_path, "ci.yml", pnpm_job(runs_on))
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


@pytest.mark.parametrize("with_block", ["", "cache: yarn", "cache: ''"])
def test_r6_pool_job_without_a_pnpm_cache_passes(tmp_path, with_block):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", with_block=with_block))
    pnpm_package_json(tmp_path)
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_cache_npm_on_the_pool_is_reported(tmp_path):
    """`cache: npm` asks `npm config get cache`, which is /var/cache/ci/npm on the pool."""
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", with_block="cache: npm"))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "shares one npm cache" in found[0].message
    assert "/var/cache/ci/npm" in found[0].fix


def test_r6_cache_npm_off_the_pool_passes(tmp_path):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-latest", with_block="cache: npm"))
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def hand_cache_job(runs_on: str, path: str, pre: str = "") -> str:
    """A one-job workflow with an `actions/cache` step caching `path` (after `pre`, raw step YAML)."""
    path_lines = "".join(f"            {line}\n" for line in path.splitlines())
    return (
        "name: CI\n"
        "on:\n"
        "  pull_request:\n"
        "jobs:\n"
        "  test:\n"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{pre}"
        "      - uses: actions/cache@v4\n"
        "        with:\n"
        "          path: |\n"
        f"{path_lines}"
        "          key: store-${{ hashFiles('pnpm-lock.yaml') }}\n"
        "      - run: pnpm install --frozen-lockfile\n"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/var/cache/ci/pnpm-store",
        "/var/cache/ci/npm",
        "/var/cache/ci",
        "${{ env.npm_config_store_dir }}",
    ],
)
def test_r6_actions_cache_of_a_store_path_on_the_pool_is_reported(tmp_path, path):
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-devino", path))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "`actions/cache` step" in found[0].message
    assert "Delete this `actions/cache` step" in found[0].fix


def test_r6_actions_cache_of_the_pnpm_store_path_output_is_reported(tmp_path):
    pre = (
        "      - id: store\n"
        '        run: echo "path=$(pnpm store path --silent)" >> "$GITHUB_OUTPUT"\n'
    )
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-devino", "${{ steps.store.outputs.path }}", pre))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "pnpm store path" in found[0].message


def test_r6_actions_cache_of_an_env_var_set_from_pnpm_store_path_is_reported(tmp_path):
    pre = '      - run: echo "STORE_PATH=$(pnpm store path --silent)" >> "$GITHUB_ENV"\n'
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-devino", "${{ env.STORE_PATH }}", pre))
    assert len(only(run([p]), RULE_PNPM_STORE_CACHE)) == 1


@pytest.mark.parametrize("uses", ["actions/cache/restore@v4", "actions/cache/save@v4"])
def test_r6_actions_cache_restore_and_save_forms_are_reported(tmp_path, uses):
    body = hand_cache_job("ubuntu-devino", "/var/cache/ci/pnpm-store").replace("actions/cache@v4", uses)
    assert len(only(run([wf(tmp_path, "ci.yml", body)]), RULE_PNPM_STORE_CACHE)) == 1


def test_r6_actions_cache_of_a_store_path_on_a_hosted_runner_passes(tmp_path):
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-latest", "/var/cache/ci/pnpm-store"))
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


@pytest.mark.parametrize(
    "path",
    ["node_modules", "~/.cache/ms-playwright", "dist\\n.next/cache", ".pnpm-store", "~/.local/share/pnpm/store"],
)
def test_r6_actions_cache_of_an_unrelated_path_on_the_pool_passes(tmp_path, path):
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-devino", path))
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_actions_cache_store_path_honours_the_allow_marker(tmp_path):
    body = hand_cache_job("ubuntu-devino", "/var/cache/ci/pnpm-store").replace(
        "  test:\n", "  test:\n    # ci-lint: allow-pnpm-cache per-pod emptyDir store on this set\n"
    )
    assert only(run([wf(tmp_path, "ci.yml", body)]), RULE_PNPM_STORE_CACHE) == []


def test_r6_only_the_pool_job_is_reported_when_a_file_mixes_runners(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        """
        name: CI
        on:
          pull_request:
        jobs:
          hosted:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/setup-node@v4
                with:
                  cache: pnpm
          pool:
            runs-on: ubuntu-devino
            steps:
              - uses: actions/setup-node@v4
                with:
                  # cache: pnpm  (a comment is not an input)
                  node-version: 22
              - uses: actions/setup-node@v4
                with:
                  cache: pnpm
                  cache-dependency-path: pnpm-lock.yaml
        """,
    )
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "`pool`" in found[0].message
    assert found[0].line == 20


def test_r6_a_cache_expression_with_a_pnpm_branch_is_reported(tmp_path):
    p = wf(
        tmp_path,
        "ci.yml",
        pnpm_job("ubuntu-devino", with_block="cache: ${{ github.event_name == 'push' && 'pnpm' || '' }}"),
    )
    assert len(only(run([p]), RULE_PNPM_STORE_CACHE)) == 1


def test_r6_reusable_workflow_defaulting_to_the_pool_is_reported(tmp_path):
    p = wf(
        tmp_path,
        "lane.yml",
        """
        name: lane
        on:
          workflow_call:
            inputs:
              runner:
                type: string
                default: ubuntu-devino
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: actions/setup-node@v4
                with:
                  cache: pnpm
        """,
    )
    assert len(only(run([p]), RULE_PNPM_STORE_CACHE)) == 1


def test_r6_reusable_workflow_defaulting_to_hosted_passes(tmp_path):
    p = wf(
        tmp_path,
        "lane.yml",
        """
        name: lane
        on:
          workflow_call:
            inputs:
              runner:
                type: string
                default: ubuntu-latest
        jobs:
          gate:
            runs-on: ${{ inputs.runner }}
            steps:
              - uses: actions/setup-node@v4
                with:
                  cache: pnpm
        """,
    )
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


# setup-node v5 auto-caches whatever package.json's `packageManager` names
# (npm, yarn or pnpm) unless `package-manager-cache: false`. v6.0.0 narrowed it
# to npm only (actions/setup-node#1374), and v7 kept that. v4 had no auto-cache.


@pytest.mark.parametrize(
    "setup",
    [
        "actions/setup-node@v5",
        "actions/setup-node@v5.0.0",
        "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444 # v5.0.0",
    ],
)
def test_r6_setup_node_v5_auto_caching_pnpm_is_reported(tmp_path, setup):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup=setup, with_block=""))
    pnpm_package_json(tmp_path)
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "setup-node v5" in found[0].message
    assert "package-manager-cache: false" in found[0].fix
    assert "actions/setup-node@" in p.read_text(encoding="utf-8").splitlines()[found[0].line - 1]


@pytest.mark.parametrize(
    "setup, with_block, manager",
    [
        # explicitly off, in both spellings setup-node accepts
        ("actions/setup-node@v5", "package-manager-cache: false", "pnpm@10.18.0"),
        ("actions/setup-node@v5", "package-manager-cache: 'false'", "pnpm@10.18.0"),
        # v6+ auto-caches npm only; v4 has no auto-cache at all
        ("actions/setup-node@v6", "", "pnpm@10.18.0"),
        ("actions/setup-node@v7.1.0", "", "pnpm@10.18.0"),
        ("actions/setup-node@v4", "", "pnpm@10.18.0"),
        # yarn has no shared directory on the pool
        ("actions/setup-node@v5", "", "yarn@4.9.0"),
        # a sha pin with no version comment is not guessed at
        ("actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444", "", "pnpm@10.18.0"),
    ],
)
def test_r6_setup_node_without_pnpm_auto_caching_passes(tmp_path, setup, with_block, manager):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup=setup, with_block=with_block))
    pnpm_package_json(tmp_path, manager)
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_setup_node_v5_with_no_package_json_passes(tmp_path):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup="actions/setup-node@v5", with_block=""))
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_setup_node_v5_auto_caching_on_a_hosted_runner_passes(tmp_path):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-latest", setup="actions/setup-node@v5", with_block=""))
    pnpm_package_json(tmp_path)
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_allow_pnpm_cache_on_the_job_exempts_it(tmp_path):
    body = pnpm_job("ubuntu-devino").replace(
        "  test:\n", "  test:\n    # ci-lint: allow-pnpm-cache this scale set has a per-pod emptyDir store\n"
    )
    p = wf(tmp_path, "ci.yml", body)
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_allow_pnpm_cache_in_the_header_covers_every_job(tmp_path):
    body = "# ci-lint: allow-pnpm-cache per-pod store on this pool\n" + pnpm_job("ubuntu-devino")
    p = wf(tmp_path, "ci.yml", body)
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_allow_pnpm_cache_without_a_reason_is_not_an_exemption(tmp_path):
    body = pnpm_job("ubuntu-devino").replace("  test:\n", "  test:\n    # ci-lint: allow-pnpm-cache\n")
    p = wf(tmp_path, "ci.yml", body)
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "no reason" in found[0].message


def test_r6_fix_prints_a_marker_the_linter_reads(tmp_path):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino"))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    marker_line = next(line for line in found[0].fix.splitlines() if lint.ALLOW_PNPM_CACHE_MARKER in line)
    assert lint._ALLOW_PNPM_CACHE_RE.search(marker_line) is not None


def test_r6_baseline_reports_a_newly_added_cache_line_and_not_an_old_one(tmp_path):
    repo = init_repo(tmp_path)
    rel = ".github/workflows/ci.yml"
    old = pnpm_job("ubuntu-devino")
    wf(repo, "ci.yml", old)
    base = commit(repo, "base")
    # untouched: the existing finding is filtered out
    added = lint_files([rel], added_lines_only=True, baseline_sha=base, base_ref=base, repo_root=repo)
    assert only(added, RULE_PNPM_STORE_CACHE) == []
    # a second pool job adding `cache: pnpm` is reported, and only that one
    new = old + (
        "  e2e:\n"
        "    runs-on: ubuntu-devino\n"
        "    steps:\n"
        "      - uses: actions/setup-node@v4\n"
        "        with:\n"
        "          cache: pnpm\n"
    )
    wf(repo, "ci.yml", new)
    commit(repo, "add e2e")
    added = lint_files([rel], added_lines_only=True, baseline_sha=base, base_ref=base, repo_root=repo)
    found = only(added, RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "`e2e`" in found[0].message


# -- R6, second round (review of ci-tooling#6): more ways to restore the shared store ------


def steps_job(runs_on: str, steps: str, job_extra: str = "", top_extra: str = "") -> str:
    """A one-job workflow around raw step YAML (steps are written at 6 spaces of indent)."""
    body = "".join(f"      {line}\n" if line else "\n" for line in textwrap.dedent(steps).strip("\n").splitlines())
    return (
        "name: CI\n"
        "on:\n"
        "  pull_request:\n"
        f"{top_extra}"
        "jobs:\n"
        "  test:\n"
        f"    runs-on: {runs_on}\n"
        f"{job_extra}"
        "    steps:\n"
        f"{body}"
    )


def r6(tmp_path, body, name="ci.yml"):
    return only(run([wf(tmp_path, name, body)]), RULE_PNPM_STORE_CACHE)


@pytest.mark.parametrize("uses", ["pnpm/action-setup@v4", "pnpm/action-setup@v5", "pnpm/action-setup@v6", "PNPM/action-setup@v4"])
@pytest.mark.parametrize("value", ["true", "'true'", "True"])
def test_r6_pnpm_action_setup_with_cache_on_the_pool_is_reported(tmp_path, uses, value):
    found = r6(tmp_path, steps_job("ubuntu-devino", f"- uses: {uses}\n  with:\n    cache: {value}\n"))
    assert len(found) == 1
    assert "`pnpm/action-setup` step" in found[0].message
    assert "ERR_PNPM_ENOENT" in found[0].message


@pytest.mark.parametrize("with_block", ["", "  with:\n    version: 10\n", "  with:\n    cache: false\n", "  with:\n    cache: 'false'\n"])
def test_r6_pnpm_action_setup_without_cache_passes(tmp_path, with_block):
    assert r6(tmp_path, steps_job("ubuntu-devino", "- uses: pnpm/action-setup@v4\n" + with_block)) == []


def test_r6_pnpm_action_setup_cache_on_a_hosted_runner_passes(tmp_path):
    assert r6(tmp_path, steps_job("ubuntu-latest", "- uses: pnpm/action-setup@v4\n  with:\n    cache: true\n")) == []


@pytest.mark.parametrize("major", ["v5", "v6", "v7"])
@pytest.mark.parametrize("manager", ["npm@10.9.0", "^npm@10.9.0", "npm"])
def test_r6_setup_node_auto_caching_npm_is_reported_on_every_major_that_has_it(tmp_path, major, manager):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup=f"actions/setup-node@{major}", with_block=""))
    pnpm_package_json(tmp_path, manager)
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    if major == "v5" and manager == "npm":
        assert found == []  # v5 only matches `npm@...`; bare `npm` is a v6+ spelling
        return
    assert len(found) == 1
    assert f"setup-node {major[0]}{major[1:]}" in found[0].message
    assert "auto-caches npm" in found[0].message
    assert "pnpm" not in found[0].message.split("Every runner pod")[0].replace("pnpm/action-setup", "")


@pytest.mark.parametrize(
    "dev_engines",
    [
        '{"packageManager": {"name": "npm", "version": "10.9.0"}}',
        '{"packageManager": [{"name": "yarn"}, {"name": "npm"}]}',
    ],
)
def test_r6_setup_node_v6_auto_caching_from_dev_engines_is_reported(tmp_path, dev_engines):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup="actions/setup-node@v6", with_block=""))
    (tmp_path / "package.json").write_text('{"name": "x", "devEngines": %s}\n' % dev_engines, encoding="utf-8")
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "devEngines.packageManager" in found[0].message


@pytest.mark.parametrize(
    "dev_engines",
    ['{"packageManager": {"name": "pnpm"}}', '{"packageManager": [{"name": "yarn"}]}', '{"runtime": {"name": "node"}}'],
)
def test_r6_setup_node_v6_dev_engines_naming_another_manager_passes(tmp_path, dev_engines):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup="actions/setup-node@v6", with_block=""))
    (tmp_path / "package.json").write_text('{"name": "x", "devEngines": %s}\n' % dev_engines, encoding="utf-8")
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_setup_node_v6_auto_cache_switched_off_passes(tmp_path):
    p = wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", setup="actions/setup-node@v6", with_block="package-manager-cache: false"))
    pnpm_package_json(tmp_path, "npm@10.9.0")
    assert only(run([p]), RULE_PNPM_STORE_CACHE) == []


def test_r6_cache_npm_message_does_not_cite_pnpm(tmp_path):
    found = only(run([wf(tmp_path, "ci.yml", pnpm_job("ubuntu-devino", with_block="cache: npm"))]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "pnpm" not in found[0].message.lower().replace("pnpm/action-setup", "")
    assert "pnpm" not in found[0].fix.split("If this job")[0].lower()


@pytest.mark.parametrize(
    "ref",
    [
        "${{ steps.store.outputs['path'] }}",
        "${{ steps['store'].outputs.path }}",
        '${{ steps["store"].outputs["path"] }}',
    ],
)
def test_r6_bracket_syntax_for_a_step_output_is_followed(tmp_path, ref):
    pre = '      - id: store\n        run: echo "path=$(pnpm store path --silent)" >> "$GITHUB_OUTPUT"\n'
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", ref, pre))) == 1


@pytest.mark.parametrize("ref", ["${{ env['STORE_PATH'] }}", '${{ env["STORE_PATH"] }}'])
def test_r6_bracket_syntax_for_an_env_var_is_followed(tmp_path, ref):
    pre = '      - run: echo "STORE_PATH=$(pnpm store path --silent)" >> "$GITHUB_ENV"\n'
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", ref, pre))) == 1


@pytest.mark.parametrize(
    "query",
    [
        "pnpm store path",
        "pnpm -s store path",
        "pnpm --silent store path",
        "pnpm store path --silent",
        "pnpm config get store-dir",
        "npm config get cache",
        "npm get cache",
        "npm -s config get cache",
    ],
)
def test_r6_every_spelling_of_the_store_query_is_followed(tmp_path, query):
    pre = f'      - id: s\n        run: echo "dir=$({query})" >> "$GITHUB_OUTPUT"\n'
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre))) == 1


def test_r6_an_env_var_set_from_a_step_output_is_followed(tmp_path):
    steps = """
        - id: s
          run: echo "dir=$(pnpm store path)" >> "$GITHUB_OUTPUT"
        - uses: actions/cache@v4
          env:
            STORE: ${{ steps.s.outputs.dir }}
          with:
            path: ${{ env.STORE }}
            key: k
    """
    assert len(r6(tmp_path, steps_job("ubuntu-devino", steps))) == 1


def test_r6_github_env_handed_on_through_github_output_is_followed(tmp_path):
    steps = """
        - run: echo "STORE=$(pnpm store path)" >> "$GITHUB_ENV"
        - id: t
          run: echo "dir=$STORE" >> "$GITHUB_OUTPUT"
        - uses: actions/cache@v4
          with:
            path: ${{ steps.t.outputs.dir }}
            key: k
    """
    assert len(r6(tmp_path, steps_job("ubuntu-devino", steps))) == 1


def test_r6_a_shell_variable_chain_inside_one_step_is_followed(tmp_path):
    steps = """
        - id: s
          run: |
            DIR=$(pnpm store path)
            echo "dir=$DIR" >> "$GITHUB_OUTPUT"
        - uses: actions/cache@v4
          with:
            path: ${{ steps.s.outputs.dir }}
            key: k
    """
    assert len(r6(tmp_path, steps_job("ubuntu-devino", steps))) == 1


@pytest.mark.parametrize(
    "where",
    ["workflow", "job", "step"],
)
def test_r6_a_store_path_set_in_any_env_scope_is_followed(tmp_path, where):
    cache = "- uses: actions/cache@v4\n  with:\n    path: ${{ env.STORE }}\n    key: k\n"
    env_line = "  STORE: /var/cache/ci/pnpm-store\n"
    if where == "workflow":
        body = steps_job("ubuntu-devino", cache, top_extra="").replace("jobs:\n", "env:\n" + env_line + "jobs:\n", 1)
    elif where == "job":
        body = steps_job("ubuntu-devino", cache, job_extra="    env:\n      STORE: /var/cache/ci/pnpm-store\n")
    else:
        body = steps_job("ubuntu-devino", cache.replace("  with:", "  env:\n    STORE: /var/cache/ci/pnpm-store\n  with:"))
    assert len(r6(tmp_path, body)) == 1


def test_r6_a_store_query_after_the_cache_step_does_not_count(tmp_path):
    """The step output is read before it is written: the cache step cannot be caching it."""
    steps = """
        - uses: actions/cache@v4
          with:
            path: ${{ steps.s.outputs.dir }}
            key: k
        - id: s
          run: echo "dir=$(pnpm store path)" >> "$GITHUB_OUTPUT"
    """
    assert r6(tmp_path, steps_job("ubuntu-devino", steps)) == []


@pytest.mark.parametrize("name", ["npm_config_cache", "npm_config_store_dir"])
def test_r6_the_pool_provided_store_env_vars_are_both_the_shared_store(tmp_path, name):
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ env.%s }}" % name))) == 1


def test_r6_npm_config_get_cache_output_is_the_shared_npm_cache(tmp_path):
    pre = '      - id: s\n        run: echo "dir=$(npm config get cache)" >> "$GITHUB_OUTPUT"\n'
    found = r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre))
    assert len(found) == 1
    assert "npm config get cache" in found[0].message


@pytest.mark.parametrize(
    "uses",
    ["Actions/Cache@v4", "ACTIONS/CACHE/RESTORE@v4", "useblacksmith/cache@v5", "runs-on/cache@v4", "buildjet/cache/save@v4"],
)
def test_r6_cache_actions_match_case_insensitively_and_by_the_cache_name(tmp_path, uses):
    body = hand_cache_job("ubuntu-devino", "/var/cache/ci/pnpm-store").replace("actions/cache@v4", uses)
    assert len(r6(tmp_path, body)) == 1


@pytest.mark.parametrize("uses", ["actions/checkout@v4", "someone/cache-helper@v1", "x/cached@v1"])
def test_r6_other_actions_are_not_cache_actions(tmp_path, uses):
    body = hand_cache_job("ubuntu-devino", "/var/cache/ci/pnpm-store").replace("actions/cache@v4", uses)
    assert r6(tmp_path, body) == []


def test_r6_setup_node_matches_case_insensitively(tmp_path):
    body = pnpm_job("ubuntu-devino", setup="Actions/Setup-Node@v4")
    assert len(r6(tmp_path, body)) == 1


def test_r6_a_multi_line_path_reports_the_offending_line(tmp_path):
    p = wf(tmp_path, "ci.yml", hand_cache_job("ubuntu-devino", "~/.cache/Cypress\n/var/cache/ci/pnpm-store\nnode_modules"))
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert p.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == "/var/cache/ci/pnpm-store"
    assert "path: /var/cache/ci/pnpm-store" in found[0].message


# False positives the review found.


@pytest.mark.parametrize(
    "env_value",
    ["${{ github.workspace }}/.pnpm-store", "$GITHUB_WORKSPACE/.pnpm-store", "/home/runner/.local/share/pnpm"],
)
@pytest.mark.parametrize("scope", ["job", "step"])
def test_r6_a_store_env_var_pointing_outside_the_shared_root_is_not_the_shared_store(tmp_path, env_value, scope):
    if scope == "job":
        body = hand_cache_job("ubuntu-devino", "${{ env.npm_config_store_dir }}").replace(
            "    steps:\n", f"    env:\n      npm_config_store_dir: {env_value}\n    steps:\n", 1
        )
    else:
        body = hand_cache_job("ubuntu-devino", "${{ env.npm_config_store_dir }}").replace(
            "        with:\n          path: |", f"        env:\n          npm_config_store_dir: {env_value}\n        with:\n          path: |"
        )
    assert r6(tmp_path, body) == []


def test_r6_a_store_env_var_overridden_to_the_shared_root_is_still_the_shared_store(tmp_path):
    body = hand_cache_job("ubuntu-devino", "${{ env.npm_config_store_dir }}").replace(
        "    steps:\n", "    env:\n      npm_config_store_dir: /var/cache/ci/pnpm-store\n    steps:\n", 1
    )
    assert len(r6(tmp_path, body)) == 1


def test_r6_only_the_variable_assigned_from_the_store_path_is_the_store(tmp_path):
    pre = (
        "      - run: |\n"
        "          pnpm store path\n"
        '          echo "CYPRESS=$HOME/.cache/Cypress" >> "$GITHUB_ENV"\n'
    )
    assert r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ env.CYPRESS }}", pre)) == []


def test_r6_the_variable_assigned_on_the_same_line_as_the_store_path_is_the_store(tmp_path):
    pre = (
        "      - run: |\n"
        '          echo "CYPRESS=$HOME/.cache/Cypress" >> "$GITHUB_ENV"\n'
        '          echo "STORE=$(pnpm store path)" >> "$GITHUB_ENV"\n'
    )
    assert r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ env.CYPRESS }}", pre)) == []
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ env.STORE }}", pre), name="b.yml")) == 1


@pytest.mark.parametrize(
    "relocate",
    [
        "pnpm config set store-dir $GITHUB_WORKSPACE/.pnpm-store",
        "pnpm config set store-dir ${{ github.workspace }}/.pnpm-store",
        'echo "npm_config_store_dir=$GITHUB_WORKSPACE/.pnpm-store" >> "$GITHUB_ENV"',
    ],
)
def test_r6_a_store_moved_into_the_workspace_before_the_query_is_not_the_shared_store(tmp_path, relocate):
    pre = (
        f"      - run: {relocate}\n"
        "      - id: s\n"
        '        run: echo "dir=$(pnpm store path)" >> "$GITHUB_OUTPUT"\n'
    )
    assert r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre)) == []


def test_r6_a_store_set_to_the_shared_root_before_the_query_is_still_shared(tmp_path):
    pre = (
        "      - run: pnpm config set store-dir /var/cache/ci/pnpm-store\n"
        "      - id: s\n"
        '        run: echo "dir=$(pnpm store path)" >> "$GITHUB_OUTPUT"\n'
    )
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre))) == 1


# Composite actions.


def write_action(root: Path, name: str, body: str) -> None:
    d = root / ".github" / "actions" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "action.yml").write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8", newline="\n")


SETUP_ACTION = """
    name: setup
    inputs:
      cache:
        default: ''
    runs:
      using: composite
      steps:
        - uses: actions/setup-node@v4
          with:
            node-version: 22
            cache: ${{ inputs.cache }}
"""


def test_r6_a_pool_job_passing_cache_pnpm_into_a_composite_is_reported_at_the_call(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION)
    body = steps_job("ubuntu-devino", "- uses: ./.github/actions/setup\n  with:\n    cache: pnpm\n")
    p = wf(tmp_path, "ci.yml", body)
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert p.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == "- uses: ./.github/actions/setup"
    assert ".github/actions/setup/action.yml" in found[0].message
    assert "`actions/setup-node` step" in found[0].message


def test_r6_a_composite_whose_input_default_is_pnpm_is_reported_when_the_caller_passes_nothing(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION.replace("default: ''", "default: pnpm"))
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/setup\n"))) == 1


def test_r6_a_composite_called_with_no_cache_passes(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION)
    assert r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/setup\n")) == []


def test_r6_a_composite_called_from_a_hosted_job_passes(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION)
    assert r6(tmp_path, steps_job("ubuntu-latest", "- uses: ./.github/actions/setup\n  with:\n    cache: pnpm\n")) == []


def test_r6_a_composite_with_a_literal_cache_pnpm_is_reported_for_the_pool_caller(tmp_path):
    write_action(
        tmp_path,
        "setup",
        """
        runs:
          using: composite
          steps:
            - uses: actions/setup-node@v4
              with:
                cache: pnpm
        """,
    )
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/setup\n"))) == 1
    assert r6(tmp_path, steps_job("ubuntu-latest", "- uses: ./.github/actions/setup\n"), name="b.yml") == []


def test_r6_a_composite_calling_a_composite_is_followed(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION)
    write_action(
        tmp_path,
        "outer",
        """
        inputs:
          mgr:
            default: pnpm
        runs:
          using: composite
          steps:
            - uses: ./.github/actions/setup
              with:
                cache: ${{ inputs.mgr }}
        """,
    )
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/outer\n"))) == 1


def test_r6_a_composite_that_calls_itself_does_not_loop(tmp_path):
    write_action(
        tmp_path,
        "loop",
        """
        runs:
          using: composite
          steps:
            - uses: ./.github/actions/loop
        """,
    )
    assert r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/loop\n")) == []


def test_r6_a_composite_path_cannot_leave_the_repository(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/setup-node@v4\n      with:\n        cache: pnpm\n",
        encoding="utf-8",
    )
    assert r6(repo, steps_job("ubuntu-devino", "- uses: ./../outside\n")) == []


def test_r6_excluded_path_lines_are_not_the_cache(tmp_path):
    assert r6(tmp_path, hand_cache_job("ubuntu-devino", "node_modules\n!/var/cache/ci/pnpm-store")) == []


def test_r6_a_composite_hand_cache_of_the_shared_store_is_reported_for_the_pool_caller(tmp_path):
    write_action(
        tmp_path,
        "warm",
        """
        runs:
          using: composite
          steps:
            - uses: actions/cache@v4
              with:
                path: /var/cache/ci/pnpm-store
                key: k
        """,
    )
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/warm\n"))) == 1


def test_r6_a_composite_action_file_with_a_literal_shared_store_cache_is_reported_on_its_own(tmp_path):
    write_action(
        tmp_path,
        "warm",
        """
        runs:
          using: composite
          steps:
            - uses: actions/cache@v4
              with:
                path: |
                  ~/.cache/Cypress
                  /var/cache/ci/pnpm-store
                key: k
        """,
    )
    p = tmp_path / ".github" / "actions" / "warm" / "action.yml"
    found = only(run([p]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert p.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == "/var/cache/ci/pnpm-store"
    assert "composite action" in found[0].message


def test_r6_a_composite_action_file_forwarding_a_cache_input_defaulting_to_pnpm_is_reported(tmp_path):
    write_action(tmp_path, "setup", SETUP_ACTION.replace("default: ''", "default: pnpm"))
    found = only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert "forwards" in found[0].message


@pytest.mark.parametrize("default", ["''", "yarn"])
def test_r6_a_composite_action_file_forwarding_a_harmless_default_passes(tmp_path, default):
    write_action(tmp_path, "setup", SETUP_ACTION.replace("default: ''", f"default: {default}"))
    assert only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_a_composite_action_file_with_a_literal_cache_pnpm_is_left_to_the_calling_job(tmp_path):
    """A hosted caller may use it correctly; the pool job that calls it is the one reported."""
    write_action(
        tmp_path,
        "setup",
        """
        runs:
          using: composite
          steps:
            - uses: actions/setup-node@v4
              with:
                cache: pnpm
        """,
    )
    assert only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_a_non_composite_action_file_is_not_a_finding(tmp_path):
    write_action(tmp_path, "js", "name: js\nruns:\n  using: node20\n  main: index.js\n")
    assert run([tmp_path / ".github" / "actions" / "js" / "action.yml"]) == []


def test_r6_a_composite_calling_itself_is_reported_once_not_once_per_level(tmp_path):
    write_action(
        tmp_path,
        "loop",
        """
        runs:
          using: composite
          steps:
            - uses: actions/setup-node@v4
              with:
                cache: pnpm
            - uses: ./.github/actions/loop
        """,
    )
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/loop\n"))) == 1


def test_r6_a_job_env_store_path_handed_to_a_step_output_is_followed(tmp_path):
    steps = """
        - id: t
          run: echo "dir=$STORE" >> "$GITHUB_OUTPUT"
        - uses: actions/cache@v4
          with:
            path: ${{ steps.t.outputs.dir }}
            key: k
    """
    body = steps_job("ubuntu-devino", steps, job_extra="    env:\n      STORE: /var/cache/ci/pnpm-store\n")
    assert len(r6(tmp_path, body)) == 1


# -- R6, third round: depth, relocation per tool, a PR that edits only a composite action -----


def _chain(tmp_path, levels: int, leaf: str) -> None:
    """action `c1` calls `c2` ... calls `c<levels>`, whose steps are `leaf`."""
    for level in range(1, levels):
        write_action(
            tmp_path,
            f"c{level}",
            f"runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/c{level + 1}\n",
        )
    write_action(tmp_path, f"c{levels}", leaf)


LEAF_CACHE_PNPM = """
    runs:
      using: composite
      steps:
        - uses: actions/setup-node@v4
          with:
            cache: pnpm
"""


def test_r6_a_cache_step_exactly_three_composite_levels_deep_is_reported(tmp_path):
    _chain(tmp_path, 3, LEAF_CACHE_PNPM)
    assert len(r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/c1\n"))) == 1


def test_r6_a_cache_step_four_composite_levels_deep_is_beyond_the_limit(tmp_path):
    """Documented limit: three levels are followed, a fourth is not."""
    _chain(tmp_path, 4, LEAF_CACHE_PNPM)
    assert r6(tmp_path, steps_job("ubuntu-devino", "- uses: ./.github/actions/c1\n")) == []


def _shell(tmp_path, command: str, query: str, cache_path: str):
    pre = f"      - run: {command}\n      - id: s\n        run: echo \"dir=$({query})\" >> \"$GITHUB_OUTPUT\"\n"
    return r6(tmp_path, hand_cache_job("ubuntu-devino", cache_path, pre))


def test_r6_moving_npm_does_not_move_pnpm(tmp_path):
    found = _shell(tmp_path, "npm config set cache $GITHUB_WORKSPACE/.npm", "pnpm store path", "${{ steps.s.outputs.dir }}")
    assert len(found) == 1


def test_r6_moving_pnpm_does_not_move_npm(tmp_path):
    found = _shell(tmp_path, "pnpm config set store-dir $GITHUB_WORKSPACE/.pnpm-store", "npm config get cache", "${{ steps.s.outputs.dir }}")
    assert len(found) == 1


def test_r6_moving_npm_moves_the_npm_query(tmp_path):
    assert _shell(tmp_path, "npm config set cache $GITHUB_WORKSPACE/.npm", "npm config get cache", "${{ steps.s.outputs.dir }}") == []


@pytest.mark.parametrize(
    "scope_name, query, expect",
    [
        ("npm_config_cache", "pnpm store path", 1),
        ("npm_config_store_dir", "npm config get cache", 1),
        ("npm_config_cache", "npm config get cache", 0),
        ("npm_config_store_dir", "pnpm store path", 0),
    ],
)
def test_r6_a_job_env_override_moves_only_its_own_tool(tmp_path, scope_name, query, expect):
    body = hand_cache_job(
        "ubuntu-devino",
        "${{ steps.s.outputs.dir }}",
        f"      - id: s\n        run: echo \"dir=$({query})\" >> \"$GITHUB_OUTPUT\"\n",
    ).replace("    steps:\n", f"    env:\n      {scope_name}: ${{{{ github.workspace }}}}/local\n    steps:\n", 1)
    assert len(r6(tmp_path, body)) == expect


def test_r6_a_one_command_env_prefix_does_not_relocate_later_queries(tmp_path):
    """`VAR=x cmd` applies to that command alone; the query two steps later still reads the shared store."""
    found = _shell(
        tmp_path,
        "npm_config_store_dir=$GITHUB_WORKSPACE/.pnpm-store pnpm install --frozen-lockfile",
        "pnpm store path",
        "${{ steps.s.outputs.dir }}",
    )
    assert len(found) == 1


def test_r6_a_one_command_env_prefix_on_the_query_itself_is_not_the_shared_store(tmp_path):
    pre = '      - id: s\n        run: dir=$(npm_config_store_dir=$GITHUB_WORKSPACE/.s pnpm store path) && echo "dir=$dir" >> "$GITHUB_OUTPUT"\n'
    assert r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre)) == []


@pytest.mark.parametrize(
    "command",
    [
        'export npm_config_store_dir=$GITHUB_WORKSPACE/.s',
        'echo "npm_config_store_dir=$GITHUB_WORKSPACE/.s" >> "$GITHUB_ENV"',
        "export npm_config_store_dir=$GITHUB_WORKSPACE/.s && true",
    ],
)
def test_r6_exported_or_persisted_env_assignments_still_relocate(tmp_path, command):
    assert _shell(tmp_path, command, "pnpm store path", "${{ steps.s.outputs.dir }}") == []


# A PR that edits only the composite action.


POOL_CALLER = "- uses: ./.github/actions/setup\n"


def test_r6_editing_only_a_composite_to_add_cache_pnpm_is_reported_when_a_pool_job_calls_it(tmp_path):
    write_action(tmp_path, "setup", LEAF_CACHE_PNPM)
    wf(tmp_path, "ci.yml", steps_job("ubuntu-devino", POOL_CALLER))
    action = tmp_path / ".github" / "actions" / "setup" / "action.yml"
    found = only(run([action]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert action.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == "cache: pnpm"
    assert "`test`" in found[0].message and "ci.yml" in found[0].message


def test_r6_editing_only_a_composite_to_add_pnpm_action_setup_cache_is_reported(tmp_path):
    write_action(
        tmp_path,
        "setup",
        """
        runs:
          using: composite
          steps:
            - uses: pnpm/action-setup@v4
              with:
                cache: true
        """,
    )
    wf(tmp_path, "ci.yml", steps_job("ubuntu-devino", POOL_CALLER))
    action = tmp_path / ".github" / "actions" / "setup" / "action.yml"
    found = only(run([action]), RULE_PNPM_STORE_CACHE)
    assert len(found) == 1
    assert action.read_text(encoding="utf-8").splitlines()[found[0].line - 1].strip() == "cache: true"


def test_r6_editing_only_a_composite_called_only_from_hosted_jobs_passes(tmp_path):
    write_action(tmp_path, "setup", LEAF_CACHE_PNPM)
    wf(tmp_path, "ci.yml", steps_job("ubuntu-latest", POOL_CALLER))
    assert only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_editing_only_a_composite_nobody_calls_passes(tmp_path):
    write_action(tmp_path, "setup", LEAF_CACHE_PNPM)
    wf(tmp_path, "ci.yml", steps_job("ubuntu-devino", "- run: echo hi\n"))
    assert only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_editing_only_an_inner_composite_is_reported_through_the_outer_one(tmp_path):
    write_action(tmp_path, "setup", LEAF_CACHE_PNPM)
    write_action(tmp_path, "outer", "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/setup\n")
    wf(tmp_path, "ci.yml", steps_job("ubuntu-devino", "- uses: ./.github/actions/outer\n"))
    action = tmp_path / ".github" / "actions" / "setup" / "action.yml"
    assert len(only(run([action]), RULE_PNPM_STORE_CACHE)) == 1
    assert only(run([tmp_path / ".github" / "actions" / "outer" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_a_pool_job_exempted_with_the_marker_does_not_make_the_composite_a_finding(tmp_path):
    write_action(tmp_path, "setup", LEAF_CACHE_PNPM)
    body = steps_job("ubuntu-devino", POOL_CALLER).replace(
        "  test:\n", "  test:\n    # ci-lint: allow-pnpm-cache per-pod emptyDir store on this set\n"
    )
    wf(tmp_path, "ci.yml", body)
    assert only(run([tmp_path / ".github" / "actions" / "setup" / "action.yml"]), RULE_PNPM_STORE_CACHE) == []


def test_r6_a_one_command_prefix_inside_a_substitution_does_not_relocate_a_later_query(tmp_path):
    pre = (
        "      - run: probe=$(npm_config_store_dir=$GITHUB_WORKSPACE/.s pnpm store path)\n"
        "      - id: s\n"
        '        run: echo "dir=$(pnpm store path)" >> "$GITHUB_OUTPUT"\n'
    )
    assert len(r6(tmp_path, hand_cache_job("ubuntu-devino", "${{ steps.s.outputs.dir }}", pre))) == 1
