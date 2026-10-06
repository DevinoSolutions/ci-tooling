#!/usr/bin/env bash
#
# Add the ci-lint caller workflow to one or more repositories, one PR each.
#
# The interim enforcement path while the org is on the GitHub Free plan and a
# ruleset cannot be created. Each repo gets two files on branch
# ci/track1-w5-caller:
#
#   .github/workflows/ci-lint.yml   caller-template.yml, verbatim
#   .github/ci-lint-baseline        the default branch's head sha at adoption
#
# THE BASELINE IS NOT OPTIONAL (plan 4.3). Without it the lint runs in
# whole-changed-file mode, so the first PR to touch any pre-existing workflow
# inherits every finding already in that file. The marker allowlists what is
# there on the day the repo adopts the lint: from then on only added lines are
# linted, and the gate blocks regressions rather than the backlog. Writing it
# at adoption is the whole reason a sweep can be run against a fleet mid-flight
# instead of after every repo is already clean.
#
# NOT RUN BY ANY WORKFLOW. This is a hand-driven sweep; run it deliberately,
# ideally with --dry-run first, and read the PRs it opens.
#
#   ./apply-caller.sh --dry-run superbooks demofy
#   ./apply-caller.sh superbooks
#   ./apply-caller.sh --work-dir /tmp/sweep DevinoSolutions/uNotes
#
# A bare name is taken as DevinoSolutions/<name>.
#
# IDEMPOTENT. Each of the two files is written only when it is absent, so a
# repo that already has one (a W1/W4 migration PR gets there first for some)
# gets only the other, and a repo that has both is skipped without a commit. A
# repo whose ci/track1-w5-caller branch already exists on the remote is skipped
# outright. Nothing here overwrites a file, least of all an existing baseline
# sha, which would silently re-grandfather a different slice of history.
#
# Exit status is the number of repositories that failed, so a sweep that is
# partly blocked still tells you how much.

set -euo pipefail

BRANCH="ci/track1-w5-caller"
TARGET=".github/workflows/ci-lint.yml"
BASELINE=".github/ci-lint-baseline"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/caller-template.yml"

DRY_RUN=0
WORK_DIR=""

usage() {
  cat >&2 <<'USAGE'
usage: apply-caller.sh [--dry-run] [--work-dir DIR] <repo> [<repo>...]

  <repo>        "name" (implies DevinoSolutions/name) or "owner/name"
  --dry-run     clone and stage, print what would happen, push nothing
  --work-dir    where clones go (default: a mktemp -d, removed on exit)

Adds, on branch ci/track1-w5-caller, and opens a PR:
  .github/workflows/ci-lint.yml   from scripts/ci-lint/caller-template.yml
  .github/ci-lint-baseline        the default branch's head sha at adoption

Each file is written only when absent; a repo with both is skipped.
USAGE
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --work-dir) [ $# -ge 2 ] || usage; WORK_DIR="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; break ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) break ;;
  esac
done

[ $# -ge 1 ] || usage
[ -f "$TEMPLATE" ] || { echo "template missing: $TEMPLATE" >&2; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "gh CLI not on PATH" >&2; exit 1; }

CLEANUP=0
if [ -z "$WORK_DIR" ]; then
  WORK_DIR="$(mktemp -d)"
  CLEANUP=1
fi
mkdir -p "$WORK_DIR"
# Must return 0. An EXIT trap whose last command fails takes the script's exit
# status with it, and this one's status is the count of failed repositories.
cleanup() {
  if [ "$CLEANUP" -eq 1 ]; then
    rm -rf "$WORK_DIR"
  fi
  return 0
}
trap cleanup EXIT

failed=0

for arg in "$@"; do
  case "$arg" in
    */*) repo="$arg" ;;
    *) repo="DevinoSolutions/${arg}" ;;
  esac
  name="${repo##*/}"
  dir="${WORK_DIR}/${name}"

  echo "=== ${repo}"

  if ! gh repo clone "$repo" "$dir" -- --quiet; then
    echo "  clone failed" >&2
    failed=$((failed + 1))
    continue
  fi

  base="$(git -C "$dir" symbolic-ref --short HEAD)"
  head_sha="$(git -C "$dir" rev-parse HEAD)"

  if git -C "$dir" ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "  skip: branch ${BRANCH} already exists on the remote"
    continue
  fi

  want_caller=1
  want_baseline=1
  if [ -f "${dir}/${TARGET}" ]; then
    echo "  ${TARGET} already present on ${base}, leaving it alone"
    want_caller=0
  fi
  if [ -f "${dir}/${BASELINE}" ]; then
    echo "  ${BASELINE} already present on ${base}, leaving it alone"
    want_baseline=0
  fi
  if [ "$want_caller" -eq 0 ] && [ "$want_baseline" -eq 0 ]; then
    echo "  skip: nothing to do"
    continue
  fi

  git -C "$dir" checkout -q -b "$BRANCH"
  mkdir -p "${dir}/.github/workflows"

  if [ "$want_caller" -eq 1 ]; then
    # LF regardless of the local core.autocrlf.
    tr -d '\r' < "$TEMPLATE" > "${dir}/${TARGET}"
    git -C "$dir" add "$TARGET"
  fi
  if [ "$want_baseline" -eq 1 ]; then
    printf '%s\n' "$head_sha" > "${dir}/${BASELINE}"
    git -C "$dir" add "$BASELINE"
  fi

  if git -C "$dir" diff --cached --quiet; then
    echo "  skip: nothing staged"
    continue
  fi

  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  dry run: would commit on ${BRANCH} and open a PR against ${base}"
    echo "  baseline would be ${head_sha}"
    git -C "$dir" --no-pager diff --cached --stat
    continue
  fi

  git -C "$dir" commit -q -F - <<MSG
ci(track1/W5): run the org ci-lint on pull requests

Calls DevinoSolutions/ci-tooling/.github/workflows/ci-lint.yml, which checks the
workflow files this PR touches for three things: a step that builds a
container image in CI, a push/pull_request workflow with no top-level
concurrency block, and a scheduled workflow whose jobs are not on
ubuntu-devino-nightly.

.github/ci-lint-baseline records ${head_sha}, the head of ${base} at adoption.
Everything already in this repo's workflows on that commit is grandfathered:
from here the lint reads only the lines a PR adds, so it blocks regressions
without holding an unrelated one-line change hostage to a backlog.

This caller is interim. The org-wide way to run the lint is a repository
ruleset requiring the workflow, which needs a paid GitHub plan; until then the
check runs and fails on findings but cannot be marked required. The caller can
be deleted once the ruleset exists.
MSG

  if ! git -C "$dir" push -q -u origin "$BRANCH"; then
    echo "  push failed" >&2
    failed=$((failed + 1))
    continue
  fi

  if ! gh pr create --repo "$repo" --base "$base" --head "$BRANCH" \
    --title "ci(track1/W5): run the org ci-lint on pull requests" \
    --body "Adds the caller for \`DevinoSolutions/ci-tooling/.github/workflows/ci-lint.yml\`, plus the baseline marker that grandfathers this repository's existing workflows.

The lint checks the workflow files a PR touches for a container build in CI, a \`push\`/\`pull_request\` workflow without top-level \`concurrency\`, and a scheduled workflow off \`ubuntu-devino-nightly\`. Rules, escape-hatch comments and the baseline file are documented in \`scripts/ci-lint/README.md\` in \`DevinoSolutions/.github\`.

\`.github/ci-lint-baseline\` records \`${head_sha}\`, the head of \`${base}\` at adoption. Everything already in this repository's workflows on that commit is allowlisted: from here the lint reads only the lines a PR adds, so it blocks regressions without holding an unrelated one-line change hostage to a backlog.

Interim: the org-wide path is a ruleset requiring this workflow, which needs a paid GitHub plan. Until then the check runs and fails on findings but cannot be marked required, and the caller can be deleted once the ruleset exists.

Nothing else in this repository changes."; then
    echo "  pr create failed" >&2
    failed=$((failed + 1))
    continue
  fi
done

if [ "$failed" -gt 0 ]; then
  echo "${failed} repository/repositories failed" >&2
fi
exit "$failed"
