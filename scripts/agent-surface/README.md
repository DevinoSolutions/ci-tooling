# agent-surface — the production contract check (T-5785)

`check.py` probes one production origin with plain GETs and fails when a
surface that AI assistants and agents read is missing or malformed. Stdlib
only, read-only, no secrets.

```sh
python scripts/agent-surface/check.py --origin https://example.com
python scripts/agent-surface/check.py --origin https://example.com --skip "api-catalog: no public API"
```

Exit 0 when every check passes or is skipped, 1 when one fails, 2 on a usage
error. `--summary-file` appends a Markdown table (the workflow points it at the
step summary); `--json` prints machine-readable results.

## Checks

| check | passes when |
|---|---|
| `llms-txt` | `/llms.txt` is 200 `text/plain` or `text/markdown` and at least 2048 bytes (`--llms-min-bytes`) |
| `index-md` | `/index.md`, the home page's Markdown twin, is 200 `text/markdown` (or `text/plain`) with a body |
| `accept-markdown` | `/` with `Accept: text/markdown` is 200 `text/markdown` **and** carries `Vary: Accept`; without it a shared cache can hand the Markdown to browsers |
| `browser-html` | `/` with a browser `Accept` is still 200 `text/html` (negotiation must never break the page) |
| `alternate-link` | the home page declares `<link rel="alternate" type="text/markdown">`, in the HTML or a `Link` header |
| `security-txt` | `/.well-known/security.txt` is 200 `text/plain` with `Contact:` and a future ISO 8601 `Expires:` (RFC 9116) |
| `api-catalog` | `/.well-known/api-catalog` is 200 JSON with a non-empty `linkset` (RFC 9727 / RFC 9264) |
| `robots-ai` | `robots.txt` lets OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-User, Claude-SearchBot, PerplexityBot, Perplexity-User, Google-Extended, Applebot and Bingbot read `/`, and names a `Sitemap:` |
| `unknown-md-404` | a random `/<name>.md` answers 404 or 410, not a soft 200 |

GPTBot and CCBot are deliberately not in `robots-ai`: letting training crawlers
in is an owner decision per app (T-5789), not part of the contract.

Headers sent on several lines (Next sends its router `Vary` and the app's
`Accept` separately) are joined before they are read. Every request retries
once on a network error or a 5xx.

## Skips

A skip is `check: reason`. The reason is required, so the caller's workflow
file explains every exemption to the next reader. Unknown names are refused.

## Calling it

Copy `caller-template.yml` to `.github/workflows/agent-surface.yml` and set
the origin. It runs every morning and by hand, never on a pull request.

## Tests

```sh
uv run --no-project --with pytest python -m pytest scripts/agent-surface -q
```

Each case serves a fake site from a real local HTTP server and breaks one
surface.
