# ci-lint — the org workflow-hygiene gate

Four rules over the files a pull request touches under `.github/workflows/`.
They are the regression guard for Track 1: W1–W4 take duplicate runs, dead
workflows and repeated container builds off the `ubuntu-devino` pool, and this
lint is what stops the next pull request from putting them back.

Stdlib plus PyYAML. No packaging, no lockfile: `lint.py` is a single script.

## Rules

### `docker-build`

Fails a step whose `run:` or `uses:` invokes:

| pattern | example |
|---|---|
| `docker build` | `docker build -t app:ci .` |
| `docker compose … build` (and the legacy `docker-compose` spelling) | `docker compose -f ci.yml build web`, `docker compose up -d --build` |
| `docker buildx build` | `docker buildx build --load .` |
| `docker buildx bake` | `docker buildx bake ci` |
| `docker/bake-action` | `uses: docker/bake-action@v5` |
| `docker/build-push-action` | `uses: docker/build-push-action@v6` |

Dokploy is the only builder on `main` and `dev`. CI waits for the image and
pulls it by digest instead, through the `wait-for-image` composite action —
[spec §5.5][waitforimage].

Only `run:` and `uses:` values are scanned, and the `#` comment on a line is
stripped before matching, so `name: no docker build here` and a commented-out
command do not trip the rule.

### `missing-concurrency`

Fails a workflow that triggers on `push` or `pull_request` and has no
top-level `concurrency:` block, in any spelling of `on:` — `on: push`,
`on: [push, pull_request]`, or the mapping form. The fix the message prints:

```yaml
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ !contains(fromJSON('["refs/heads/main","refs/heads/dev"]'), github.ref) }}
```

The expression keeps `cancel-in-progress: false` on `main` and `dev`, so a
deploy-path run is never cancelled halfway, and cancels superseded runs
everywhere else.

**The `allow-missing-concurrency` marker.** For some workflows no top-level
group is the correct answer, and adding one makes things worse. If the jobs
serialise on external state — a single-use credential, a shared sandbox, a
write-back store that a half-finished run leaves inconsistent — then:

- `cancel-in-progress: true` kills a superseded run mid-suite, which is how a
  rotated single-use token gets consumed with its replacement never written
  back; and
- `cancel-in-progress: false` does not save you either, because GitHub keeps at
  most one *pending* run per group and a newly queued run replaces the one
  already waiting. A third concurrent pull request loses its run, and an
  `if: always()` status job reads that cancellation as a failure.

There is no queue-depth knob that fixes the second case. (`queue: max` is not a
GitHub Actions key; Actions ignores it, so a workflow carrying it has no
serialisation guarantee at all and `actionlint` reports `unexpected key "queue"`.)
The answer for such a workflow is a wait-based mutex inside the job — a step
that polls until the earlier holder is done and never cancels anyone — plus this
marker in the workflow header:

```yaml
name: E2E
# ci-lint: allow-missing-concurrency the accounting lane holds a single-use Xero/QuickBooks
# refresh token through a shared write-back store; a cancelled or replaced run strands it
on:
  push:
```

Scope is the workflow header — everything above the first job — because the rule
is about the whole file, not one job. A marker inside a job does not exempt it.
As everywhere else, the reason is required: `# ci-lint: allow-missing-concurrency`
with nothing after it is not an exemption, and the finding says so.

### `nightly-runner`

Fails a job in a workflow with a `schedule:` trigger whose `runs-on` is not
`ubuntu-devino-nightly` (a list naming that label is fine). Nightlies live on
a scale set capped at four runners with a low PriorityClass and `nice 10`, so
a sweep can never take pods from a push or pull-request run — [spec §4.4][pool].

A job that calls a reusable workflow has no `runs-on`; it passes the label as
an input instead, and the rule accepts that:

```yaml
jobs:
  evals:
    uses: DevinoSolutions/.github/.github/workflows/ai-evals.yml@main
    with:
      runner: ubuntu-devino-nightly
```

Two further forms count as compliant.

**An event-conditional `runs-on`.** A workflow that carries both a schedule
and an event trigger can keep its pull-request leg on the main pool and put
only its nightly leg on the deprioritised set:

```yaml
runs-on: ${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}
```

`A && B || C` is GitHub's ternary, so the rule reads the branch taken when the
event is `schedule` and passes when that branch is the nightly label. The
inverted spelling (`github.event_name != 'schedule' && 'ubuntu-devino' || 'ubuntu-devino-nightly'`)
works too, as does the same expression folded over several lines with `>-`.

Only that one shape is resolved. Any other expression — `${{ matrix.runner }}`,
`${{ inputs.runner }}` — is reported, and the message says the expression could
not be resolved rather than pretending it was. A lint that quietly accepts an
expression it did not understand is worse than one that asks.

**The `allow-schedule-main` marker.** Some scheduled work belongs on the main
pool: a short-interval liveness probe whose latency is the whole point would be
made useless by queueing behind a nightly sweep. Exempt it with a reason, on
the job or in the workflow header:

```yaml
jobs:
  probe:
    # ci-lint: allow-schedule-main a 10-minute liveness probe; queueing behind a nightly hides an outage
    runs-on: ubuntu-devino
```

A marker in the header — anywhere above the first job — exempts every job in
the workflow. A comment block directly above a job belongs to that job, not to
the header, so it exempts that job alone. As with `allow-build`, the reason is
required: a bare marker is still a finding, and the message says so.

superbooks `uptime-probe.yml` is the intended first user. It does not carry
the marker yet — that lands with the W1 nightly changes. Nothing in the fleet
uses either of these two forms today.

### `caller-permissions`

Fails a job whose `uses:` points at a reusable workflow that declares a
permission the caller does not hold.

A called workflow can only ever receive permissions its caller already has.
Ask for one the caller lacks and the run does not fail a step — it fails at
**startup**, before any job exists, with no annotation and no log. There is
nothing in the UI to read, which is what makes it worth a lint.

This is not hypothetical. On 2026-09-10 two self-tests in this repository died
exactly that way: they granted `contents: read` and called workflows declaring
`id-token: write`, and the runs came back `startup_failure` with no jobs.

How the comparison is made:

| side | what counts |
|---|---|
| callee | every scope it declares, at workflow level and on any of its jobs, at the highest level asked |
| caller | the calling job's `permissions:` block if it has one, otherwise the workflow-level block |

A job-level block **replaces** the workflow-level one rather than adding to it,
which is how a caller that looks correct at the top can still be wrong, so the
rule follows the same replacement.

A caller with no block at all runs with the default token, whose level for most
scopes depends on an org setting the linter cannot read. So that case is judged
on `id-token` alone: GitHub never puts an OIDC token in the default token, it
has to be requested. Everything else is left alone rather than guessed at.

Which callees are checked:

- `./.github/workflows/x.yml` — resolved against the repository being linted.
  If the file is not there, that is a **failure**, not a skip. A local `uses:`
  either resolves inside the checkout or the run cannot start, so there is
  nothing to be uncertain about, and being charitable about it is expensive:
  `caly`'s `pr.yml` called `./.github/workflows/nextjs-bundle-analysis.yml`,
  which that repository does not have, and all 47 runs between 2026-06-04 and
  2026-09-05 were startup failures with zero jobs. It went unnoticed for three
  months, which is the whole reason this rule exists.
- `DevinoSolutions/.github/.github/workflows/x.yml@<ref>` — resolved only when
  that path exists in the repository being linted, which in practice means the
  `.github` repo checking its own reusable workflows. Owner and repo are both
  compared, not just the filename: a call into someone else's repository must
  never be judged against a local file that happens to share a name. A caller
  never resolves to itself either, so a repo whose `ci-lint.yml` calls the
  org's `ci-lint.yml` is not compared against its own copy.
- A chain is followed all the way down. If A calls B and B's job calls C, C's
  declared permissions are required of A, because that is how GitHub passes
  them. A cycle terminates rather than recursing forever.
- Any other `<owner>/<repo>/…@<ref>`, and a target that resolves but is not a
  `workflow_call` workflow, is **skipped with a notice, not failed**. The
  notice names the `uses:` and says which of the two it was. Notices do not
  affect the exit status.

**What this rule does not cover.** A cross-repo callee is never read, so in a
fleet repository — where `ci-lint.yml` runs in the *target* repo's context and
this repository is on disk only at `.ci-lint-tooling/` — every call
into the org's reusable workflows is a notice. Today that is 14 of the 14
notices the fleet corpus produces, 13 of them the org `python-ci.yml`. Inside
this repository the rule is a real check; outside it, it currently guards only
local `./` calls. Passing `.ci-lint-tooling/` as a second resolution root,
gated on the owner/repo check above, would convert those 13 into real checks
with no new network access. That is the obvious next step and is not done here.

Under a `.github/ci-lint-baseline`, a finding survives the added-lines filter
when the call *or the permissions block it depends on* was touched, deletions
included. Narrowing `permissions:` is the shape that breaks a caller, and it
never touches the `uses:` line.

Exempt a job with `# ci-lint: allow-permissions <reason>`, on the job or in the
workflow header, with the same rules as the other markers. The marker is read
before the callee is resolved, so it silences the skip notice as well as the
failure — an exempted job is not checked at all.

### `bun-engine-order`

A job on the self-hosted pool that runs `bun` or `bunx` **before**
`actions/setup-node` — or without it entirely — is reported.

Bun resolves `node` to itself for the child processes a script spawns under
`bun run` when no real `node` is on `PATH`. `ubuntu-devino` shipped no system
node until the v1 runner image landed on **2026-09-11 at 14:56Z**; it now ships
`/usr/local/bin/node`. So every such child ran on JavaScriptCore before that
moment and runs on V8 after it, and nothing in any repository changed. V8-only
flags, heap limits and engine behaviour all moved at once: `superbooks`'
type-aware lint began failing on five unrelated branches with

```
FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - JavaScript heap out of memory
 1: 0xe46bbe node::OOMErrorHandler(...) [/usr/local/bin/node]
```

at a `--max-old-space-size=6144` cap that JavaScriptCore had silently ignored
for the cap's entire life. The job was green four times out of four on the old
image, including on the commit that is still `main`.

`actions/setup-node` installs a real node on **either** image, so a job that
runs it before its first bun command never changed engine and never will.

**The rule is about order, not presence.** `superbooks` does call
`actions/setup-node` in the job that broke — seven lines after the step that
broke. Checking only that the action appears somewhere in the job would have
passed the one job in the fleet that actually failed, which is why the check is
worth having as code rather than as a convention.

Scope is deliberately narrow:

- **only the self-hosted pool.** GitHub-hosted images have always shipped a
  node, so the same workflow on `ubuntu-latest` is not at risk and is not
  flagged.
- **"runs bun" means command position, not the three letters.** A `run:` body is
  shell, so it is split on command separators and only the first word of each
  segment counts.  in a path filter, , and the Bun is a fast JavaScript runtime, package manager, bundler, and test runner. (1.3.13+bf2e2cecf)

Usage: bun <command> [...flags] [...args]

Commands:
  run       ./my-script.ts       Execute a file with Bun
            lint                 Run a package.json script
  test                           Run unit tests with Bun
  x         nuxi                 Execute a package binary (CLI), installing if needed (bunx)
  repl                           Start a REPL session with Bun
  exec                           Run a shell script directly with Bun

  install                        Install dependencies for a package.json (bun i)
  add       @evan/duckdb         Add a dependency to package.json (bun a)
  remove    moment               Remove a dependency from package.json (bun rm)
  update    @zarfjs/zarf         Update outdated dependencies
  audit                          Check installed packages for vulnerabilities
  outdated                       Display latest versions of outdated dependencies
  link      [<package>]          Register or link a local npm package
  unlink                         Unregister a local npm package
  publish                        Publish a package to the npm registry
  patch <pkg>                    Prepare a package for patching
  pm <subcommand>                Additional package management utilities
  info      zod                  Display package metadata from the registry
  why       tailwindcss          Explain why a package is installed

  build     ./a.ts ./b.jsx       Bundle TypeScript & JavaScript into a single file

  init                           Start an empty Bun project from a built-in template
  create    vite                 Create a new project from a template (bun c)
  upgrade                        Upgrade to latest version of Bun.
  feedback  ./file1 ./file2      Provide feedback to the Bun team.

  <command> --help               Print help text for command.

Learn more about Bun:            https://bun.com/docs
Join our Discord community:      https://bun.com/discord inside
   are not invocations. Running an earlier draft over all 212
  repositories reported three  jobs for exactly that reason, which is
  what the tokeniser and its parametrised test exist to prevent.
- **`oven-sh/setup-bun` alone is not enough to fire.** A job that installs Bun
  and never runs it has no engine to get wrong.
- **an unresolvable `runs-on:` expression is not reported.** The linter resolves
  literals, lists, `labels:` maps, the one event-conditional ternary it already
  understands, and `${{ inputs.<name> }}` (below). Anything else it stays quiet
  about — guessing at an expression is worse than saying nothing.

#### Reusable workflows

A reusable workflow's `runs-on` is almost always `${{ inputs.runner }}`, and
reading that file alone cannot resolve it. Skipping those would leave the hole
exactly where the org's shared lanes live: every caller of a `.github` reusable
inherits its step order, so one bad ordering there is a fleet-wide exposure that
no per-repo check would ever see.

Two things resolve it, and either naming a `ubuntu-devino*` label makes the job
pool-capable:

1. **the input's own `default:`**, read from `on.workflow_call.inputs`. This
   covers all ten org reusables, whose `runner` input defaults to
   `${{ github.event_name == 'schedule' && 'ubuntu-devino-nightly' || 'ubuntu-devino' }}`;
2. **what callers actually pass**, supplied as `caller_values` — a
   `{input name: [values]}` mapping — because that is cross-repository knowledge
   this process cannot discover on its own. `lint_text(..., caller_values=...)`
   takes it; it defaults to `None`, so nothing changes for a normal single-repo
   run. A sweep that has read the whole org can pass it and catch a reusable
   whose default is GitHub-hosted but whose callers put it on the pool.

An input with a hosted default and no pool-passing caller is not reported.

Fix:

```yaml
      - uses: actions/setup-node@v4
        with:
          node-version: 22
```

placed before the first `bun`/`bunx` step. `oven-sh/setup-bun` can stay where
it is.

Exempt with `# ci-lint: allow-bun-engine <reason>` on the job or in the
workflow header — a `bun test` suite, or code that genuinely wants
JavaScriptCore. If that is the intent, also pin it with `[run] bun = true` in
`bunfig.toml` so the choice survives a runner-image change instead of depending
on one. At the time this rule was written **no repository in the org set that
key**, which is why every repository that came through the promotion intact did
so by `setup-node` ordering rather than by intent.

Run over all 212 default branches — 480 workflow files across the 132
repositories that have any — the rule reports three jobs in two repositories,
and the three bun-capable org reusables (`ai-evals`, `lighthouse-gate`,
`schema-drift`) all pass on their existing ordering: each already runs
`actions/setup-node` before any bun step, which is what makes every caller of
the shared lane immune.

### `unparseable-workflow`

A file that is not valid YAML is reported as a finding rather than crashing
the run, pointing at the line PyYAML choked on.

## The escape-hatch comments

All five rules have one. They take the same shape — `# ci-lint: <marker> <reason>`
— and in all four the reason is required, because the reason is what a reviewer
reads and a bare marker is just a mute button.

| marker | rule | scope |
|---|---|---|
| `allow-build` | `docker-build` | the step it sits in |
| `allow-schedule-main` | `nightly-runner` | the job it sits in, or every job when it sits in the workflow header |
| `allow-permissions` | `caller-permissions` | the job it sits in, or every job when it sits in the workflow header |
| `allow-missing-concurrency` | `missing-concurrency` | the workflow header only — the rule is workflow-scoped |
| `allow-bun-engine` | `bun-engine-order` | the job it sits in, or every job when it sits in the workflow header |

`allow-schedule-main`, `allow-permissions`, `allow-missing-concurrency` and
`allow-bun-engine` are covered under their rules above.

### `allow-build`

`docker-build` needs an escape hatch because some builds have no registry copy
to wait for: a pull-request-only leg building against the shared cache, or a
test fixture image. Add the marker to the offending step:

```yaml
      - name: build the fixture image
        # ci-lint: allow-build the fixture image has no registry copy
        run: docker build -t fixture:ci .
```

- The reason is required. `# ci-lint: allow-build` with nothing after it is
  still a finding, with a message saying so — the reason is what a reviewer
  reads, and a bare marker is just a mute button.
- The marker counts when it sits anywhere inside the step, at the end of the
  offending line, or in the comment block directly above the step's `-`. A
  comment block belongs to the item below it, so it excuses that step and not
  the one above.
- It does not carry into the next step. Step boundaries come from the parsed
  YAML, not from guessing at indentation.

## The baseline file

A repository adopts the lint by committing `.github/ci-lint-baseline`
containing one sha — the sha of its W1–W4 migration commit. From then on:

| state | mode |
|---|---|
| the file holds a sha that is an ancestor of the pull request's base | added lines only |
| no file, unreadable file, not a sha, or a sha that is not an ancestor | whole changed files |

Added-lines mode keeps a finding when **any** of the lines that cause it were
added by the pull request, not merely the line the annotation points at. So
adding `push:` to an existing trigger block trips `missing-concurrency` even
though the `on:` line was not touched, while an unrelated one-line edit
elsewhere in the same file stays quiet. Whole-file mode is the strict default:
a repository that has not migrated gets no grandfathering.

The diff is `git diff <base>...HEAD -U0`, so a full-history checkout is
required (`fetch-depth: 0`).

## Running it locally

```bash
# one file, whole-file mode
python scripts/ci-lint/lint.py .github/workflows/ci.yml

# exactly what CI does, against your pull request's base
git fetch origin main
python scripts/ci-lint/lint.py --base origin/main \
  $(git diff --name-only --diff-filter=ACMR origin/main...HEAD -- .github/workflows)

# every workflow in the repository
git ls-files '.github/workflows/*.y*ml' > /tmp/wf.txt
python scripts/ci-lint/lint.py --paths-from /tmp/wf.txt
```

Flags: `--base` (enables baseline resolution), `--repo-root` (default `.`),
`--paths-from FILE`, `--summary-file FILE` (appends the markdown job summary).
Exit status is `1` when there are findings, `0` otherwise. Findings print as
GitHub `::error` annotations, which is also perfectly readable in a terminal.

PyYAML is the only dependency:

```bash
uv run --no-project --with pyyaml python scripts/ci-lint/lint.py ...
```

## Tests

```bash
python -m pytest scripts/ci-lint -q
```

163 tests. Every rule has a positive and a negative fixture, including both
accepted forms of a scheduled job's runner and the three escape-hatch markers;
`caller-permissions` is tested against the exact shape of the two self-tests
that hit `startup_failure` on 2026-09-10, plus the job-level override, a
callee that cannot be resolved, and a caller with no block at all;
both baseline modes run against throwaway git repositories built by the test,
so the hunk parser and the ancestry check are exercised rather than mocked,
and a malformed, unknown or abbreviated baseline sha each has a case. The
caller template is linted by the linter it installs; the two concurrency
groups are asserted to be distinguishable without evaluating an expression;
and `apply-caller.sh` is syntax-checked, made to refuse a run with no
repository named, and checked for the baseline write and the idempotency
guards.
`.github/workflows/selftest.yml` runs the same suite on every pull request on
a GitHub-hosted runner (this repository is public).

## How it runs org-wide

`.github/workflows/ci-lint.yml` in this repository is the reusable workflow
(`workflow_call`). It lives in a **public** repository so that every caller can
use it: a public repo cannot call a workflow in a private repo at all, and the
old arrangement (workflow and linter in the private `DevinoSolutions/.github`,
linter fetched with an org read token) failed in every caller because that
token never existed and a caller's `GITHUB_TOKEN` cannot read another private
repo. The workflow checks the caller out, then does a second, anonymous
checkout of this repository to fetch `lint.py`. No secret or token is involved.
Inside this repository the second checkout is skipped and the pull request's
own copy of the linter is used, so a pull request that changes a rule is
checked by the changed rule.

`runs-on` follows the caller's visibility
(`github.event.repository.private && 'ubuntu-devino' || 'ubuntu-latest'`):
private repos run on the self-hosted pool, public repos on GitHub-hosted
runners. `DevinoSolutions/.github/.github/workflows/ci-lint.yml` remains as a
thin wrapper that calls this workflow, so callers still pointing at the old path
keep working.

When a required-workflow ruleset eventually exists, point its `workflows` rule
at this repository's `ci-lint.yml`.

**The ruleset does not exist yet, and it waits on a plan upgrade.** Rulesets
are a paid GitHub feature. `DevinoSolutions` is on the Free plan, so
`POST /orgs/DevinoSolutions/rulesets` returns 403 "Upgrade to GitHub Team",
and the per-repository fallback returns 403 "Upgrade to GitHub Pro or make
this repository public". Neither is a token-scope problem: the calls were made
with `admin:org`. Branch protection on private repos is gated the same way, so
on the current plan the lint can run and fail a check but cannot be *required*
by any mechanism. The ready-to-POST payload and the command to run once the
org is on GitHub Team or higher are recorded in the W5 pull request body.

### Interim: the per-repo caller sweep

Until then a repository opts in by committing a caller workflow that calls
this one. Two files here do that:

| file | what |
|---|---|
| `caller-template.yml` | the caller, copied verbatim into a repo as `.github/workflows/ci-lint.yml` |
| `apply-caller.sh` | clones a repo, adds the caller **and the baseline marker** on branch `ci/track1-w5-caller`, opens a PR |

```bash
./scripts/ci-lint/apply-caller.sh --dry-run superbooks demofy
./scripts/ci-lint/apply-caller.sh superbooks
./scripts/ci-lint/apply-caller.sh --work-dir /tmp/sweep DevinoSolutions/uNotes
```

A bare name means `DevinoSolutions/<name>`.

The baseline is written at the same time, holding the default branch's head
sha at adoption, and it is not optional. Without it the lint runs in
whole-changed-file mode, so the first PR to touch any pre-existing workflow
inherits every finding already in that file — the sweep would open a wave of
red PRs about code nobody in them wrote. With it, everything present on the
day of adoption is allowlisted and the gate reads only added lines, which is
what makes a mid-flight sweep possible at all.

The script is idempotent. Each of the two files is written only when absent,
so a repo that already has one gets the other and a repo that has both is
skipped without a commit; a repo whose branch is already on the remote is
skipped outright. Nothing is overwritten, least of all an existing baseline
sha, which would silently re-grandfather a different slice of history. Exit
status is the number of repositories that failed, so a partly blocked sweep
still reports how much landed. It is hand-driven: no workflow invokes it, and
nothing happens until you run it.

The concurrency group in the caller and the group in the reusable workflow are
deliberately different, and neither uses `github.workflow`. That expression
resolves to the caller's workflow name on both sides of a `uses:` call, so
identical blocks would put a run in a group with itself and cancel it.

The template passes no secrets: the workflow is in a public repository and
needs none. The same caller works unchanged in private and public repos.

[waitforimage]: https://github.com/DevinoSolutions/runner-infra/blob/main/docs/superpowers/specs/2026-09-10-ci-build-once-and-pool-design.md#55-ci-side-wait-for-image
[pool]: https://github.com/DevinoSolutions/runner-infra/blob/main/docs/superpowers/specs/2026-09-10-ci-build-once-and-pool-design.md#44-nightlies-on-a-deprioritised-scale-set
