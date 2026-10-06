# ci-tooling

Public, secret-free tooling for the DevinoSolutions org CI lint.

This repository is public on purpose. A reusable workflow, and anything a
workflow checks out at run time, has to be readable by every caller. Private
callers can read a private repo's workflow (org Actions access setting), but
public callers cannot, and the linter fetch used to need an org-wide read token
that never existed. Everything here is configs and scripts: no secrets, no
internal hostnames, no credentials. Keep it that way.

| Path | What |
|---|---|
| `.github/workflows/ci-lint.yml` | The reusable `workflow_call` that every repo calls |
| `scripts/ci-lint/` | The linter (`lint.py`), its tests, the caller template and the adoption script. Start with its README |

## Calling it

```yaml
jobs:
  ci-lint:
    uses: DevinoSolutions/ci-tooling/.github/workflows/ci-lint.yml@main
```

No `secrets:` block is needed. The job picks its runner from the caller's
visibility: `ubuntu-devino` (self-hosted) for private repos, `ubuntu-latest`
(GitHub-hosted) for public ones. See `scripts/ci-lint/caller-template.yml`.
