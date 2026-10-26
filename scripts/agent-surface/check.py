"""Agent-surface contract check (GetItDone T-5785).

Probes ONE production origin over HTTP and fails when a surface that AI
assistants and agents read is missing or malformed. Stdlib only, read-only, no
secrets: it sends plain GETs, the same requests any crawler sends.

    python check.py --origin https://example.com
    python check.py --origin https://example.com --skip "api-catalog: no public API or MCP"

Exit code 0 when every check passes or is skipped, 1 when one fails, 2 on a
usage error. A skip needs a reason after the colon; a bare name is refused, so
every exemption explains itself in the caller's workflow file.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

USER_AGENT = "Mozilla/5.0 (compatible; DevinoAgentSurfaceCheck/1.0; +https://github.com/DevinoSolutions/ci-tooling)"
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"

# The assistants' crawlers and fetchers the contract (T-5785 item 6) says robots.txt must let in.
# GPTBot and CCBot are left out on purpose: whether training crawlers get in is
# an owner call per app (T-5789), not part of the contract.
AI_USER_AGENTS = (
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-User",
    "Claude-SearchBot",
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",
    "Applebot",
    "Bingbot",
)

TEXT_TYPES = ("text/plain", "text/markdown")


@dataclass
class Response:
    status: int | str
    content_type: str
    headers: dict[str, str]
    body: str
    url: str


@dataclass
class Result:
    name: str
    outcome: str  # PASS | FAIL | SKIP
    detail: str


def fetch(url: str, accept: str = "*/*", timeout: float = 25.0, max_bytes: int = 512_000) -> Response:
    """GET with redirects followed; one retry on a network error or a 5xx."""
    last: Response | None = None
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                headers = _headers(reply.headers)
                body = reply.read(max_bytes).decode("utf-8", "replace")
                return Response(reply.status, _media_type(headers), headers, body, reply.geturl())
        except urllib.error.HTTPError as error:
            headers = _headers(error.headers)
            last = Response(error.code, _media_type(headers), headers, "", url)
            if error.code < 500:
                return last
        except Exception as error:  # noqa: BLE001 - any transport failure is a FAIL with its name
            last = Response(f"error: {type(error).__name__}", "", {}, "", url)
        if attempt == 0:
            time.sleep(3)
    assert last is not None
    return last


def _headers(message) -> dict[str, str]:
    """Lower-cased names; a header sent on several lines (Vary, Link) is joined with commas, as RFC 9110 allows."""
    joined: dict[str, str] = {}
    for key, value in (message.items() if message is not None else []):
        key = key.lower()
        joined[key] = f"{joined[key]}, {value}" if key in joined else value
    return joined


def _media_type(headers: dict[str, str]) -> str:
    return (headers.get("content-type") or "").split(";")[0].strip().lower()


def _vary_has_accept(headers: dict[str, str]) -> bool:
    fields = [f.strip().lower() for f in (headers.get("vary") or "").split(",")]
    return "accept" in fields or "*" in fields


def _seen(response: Response) -> str:
    return f"{response.status} {response.content_type or '-'}"


# ── the checks ──────────────────────────────────────────────────────────────
# Each takes the origin and returns (ok, detail). The detail is what a reader of
# a red run needs to fix it, so it names the URL and what came back.


def check_llms_txt(origin: str, min_bytes: int) -> tuple[bool, str]:
    r = fetch(origin + "/llms.txt")
    size = len(r.body.encode("utf-8"))
    if r.status != 200 or r.content_type not in TEXT_TYPES:
        return False, f"/llms.txt answered {_seen(r)}; want 200 text/plain or text/markdown"
    if size < min_bytes:
        return False, f"/llms.txt is {size} bytes; the contract asks for at least {min_bytes}"
    return True, f"/llms.txt 200 {r.content_type}, {size} bytes"


def check_index_md(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/index.md")
    if r.status != 200 or r.content_type not in TEXT_TYPES or not r.body.strip():
        return False, f"/index.md answered {_seen(r)}; want 200 text/markdown with the home page's text"
    return True, f"/index.md 200 {r.content_type}"


def check_accept_markdown(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/", accept="text/markdown")
    if r.status != 200 or r.content_type != "text/markdown":
        return False, f"/ with Accept: text/markdown answered {_seen(r)}; want 200 text/markdown"
    if not _vary_has_accept(r.headers):
        return False, "the Markdown answer for / has no Vary: Accept, so a cache can serve it to browsers"
    return True, "/ with Accept: text/markdown is 200 text/markdown with Vary: Accept"


def check_browser_html(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/", accept=BROWSER_ACCEPT)
    if r.status != 200 or r.content_type != "text/html":
        return False, f"/ with a browser Accept answered {_seen(r)}; want 200 text/html"
    return True, "/ with a browser Accept is 200 text/html"


_LINK_TAG = re.compile(r"<link\b[^>]*>", re.I)


def check_alternate_link(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/", accept=BROWSER_ACCEPT)
    header = r.headers.get("link", "")
    if re.search(r'rel="?alternate"?[^,]*type="?text/markdown', header, re.I) or re.search(
        r'type="?text/markdown"?[^,]*rel="?alternate', header, re.I
    ):
        return True, "Link header advertises the Markdown alternate"
    for tag in _LINK_TAG.findall(r.body):
        if re.search(r'rel=["\']?alternate', tag, re.I) and re.search(r'type=["\']?text/markdown', tag, re.I):
            return True, '<link rel="alternate" type="text/markdown"> is in the home page HTML'
    return False, 'the home page has no <link rel="alternate" type="text/markdown"> and no Link header for it'


def check_security_txt(origin: str, now: datetime | None = None) -> tuple[bool, str]:
    r = fetch(origin + "/.well-known/security.txt")
    if r.status != 200 or r.content_type != "text/plain":
        return False, f"/.well-known/security.txt answered {_seen(r)}; want 200 text/plain (RFC 9116)"
    fields = {}
    for line in r.body.splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, value = line.split(":", 1)
            fields.setdefault(key.strip().lower(), value.strip())
    if "contact" not in fields:
        return False, "security.txt has no Contact: field (RFC 9116 requires one)"
    expires = fields.get("expires")
    if not expires:
        return False, "security.txt has no Expires: field (RFC 9116 requires one)"
    try:
        when = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return False, f"security.txt Expires: {expires!r} is not an ISO 8601 date-time"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= (now or datetime.now(timezone.utc)):
        return False, f"security.txt expired on {expires}"
    return True, f"security.txt 200, Contact present, Expires {expires}"


def check_api_catalog(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/.well-known/api-catalog")
    if r.status != 200:
        return False, f"/.well-known/api-catalog answered {_seen(r)}; want 200 (RFC 9727)"
    try:
        doc = json.loads(r.body)
    except ValueError:
        return False, "/.well-known/api-catalog is not JSON"
    if not isinstance(doc, dict) or not isinstance(doc.get("linkset"), list) or not doc["linkset"]:
        return False, "/.well-known/api-catalog has no non-empty linkset array (RFC 9264)"
    return True, f"api-catalog 200 {r.content_type}, {len(doc['linkset'])} linkset entr{'y' if len(doc['linkset']) == 1 else 'ies'}"


def check_robots_ai(origin: str) -> tuple[bool, str]:
    r = fetch(origin + "/robots.txt")
    if r.status != 200:
        return False, f"/robots.txt answered {_seen(r)}; want 200"
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(r.body.splitlines())
    blocked = [ua for ua in AI_USER_AGENTS if not parser.can_fetch(ua, origin + "/")]
    if blocked:
        return False, "robots.txt keeps these crawlers off /: " + ", ".join(blocked)
    if not re.search(r"^\s*sitemap\s*:", r.body, re.I | re.M):
        return False, "robots.txt has no Sitemap: line"
    return True, f"robots.txt lets all {len(AI_USER_AGENTS)} AI crawlers read / and names a sitemap"


def check_unknown_md(origin: str) -> tuple[bool, str]:
    path = f"/agent-surface-check-{secrets.token_hex(6)}.md"
    r = fetch(origin + path)
    if r.status in (404, 410):
        return True, f"an unknown .md path answers {r.status}"
    return False, f"{path} answered {_seen(r)}; an unknown twin must be 404, not a soft 200"


CHECKS = (
    "llms-txt",
    "index-md",
    "accept-markdown",
    "browser-html",
    "alternate-link",
    "security-txt",
    "api-catalog",
    "robots-ai",
    "unknown-md-404",
)


def run(origin: str, skips: dict[str, str], llms_min_bytes: int = 2048) -> list[Result]:
    table = {
        "llms-txt": lambda: check_llms_txt(origin, llms_min_bytes),
        "index-md": lambda: check_index_md(origin),
        "accept-markdown": lambda: check_accept_markdown(origin),
        "browser-html": lambda: check_browser_html(origin),
        "alternate-link": lambda: check_alternate_link(origin),
        "security-txt": lambda: check_security_txt(origin),
        "api-catalog": lambda: check_api_catalog(origin),
        "robots-ai": lambda: check_robots_ai(origin),
        "unknown-md-404": lambda: check_unknown_md(origin),
    }
    results = []
    for name in CHECKS:
        if name in skips:
            results.append(Result(name, "SKIP", skips[name]))
            continue
        ok, detail = table[name]()
        results.append(Result(name, "PASS" if ok else "FAIL", detail))
    return results


def parse_skips(values: list[str]) -> dict[str, str]:
    """`name: reason` items, comma- or newline-separated. A missing reason or an unknown name is a usage error."""
    skips: dict[str, str] = {}
    for value in values:
        for item in re.split(r"[\n,]", value):
            item = item.strip()
            if not item:
                continue
            name, _, reason = item.partition(":")
            name, reason = name.strip(), reason.strip()
            if name not in CHECKS:
                raise ValueError(f"unknown check {name!r}; the checks are: {', '.join(CHECKS)}")
            if not reason:
                raise ValueError(f"skip {name!r} has no reason; write it as '{name}: <why this app is exempt>'")
            skips[name] = reason
    return skips


def normalise_origin(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError(f"--origin must be a bare origin like https://example.com, got {value!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def summary_markdown(origin: str, results: list[Result]) -> str:
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️"}
    lines = [f"### Agent-surface contract: {origin}", "", "| | check | detail |", "|---|---|---|"]
    lines += [f"| {icon[r.outcome]} | `{r.name}` | {r.detail.replace('|', '/')} |" for r in results]
    failed = sum(r.outcome == "FAIL" for r in results)
    lines += ["", f"{failed} failed, {sum(r.outcome == 'PASS' for r in results)} passed, {sum(r.outcome == 'SKIP' for r in results)} skipped."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--origin", required=True, help="production origin, e.g. https://example.com")
    parser.add_argument("--skip", action="append", default=[], help="'check: reason' (repeatable, or comma/newline-separated)")
    parser.add_argument("--llms-min-bytes", type=int, default=2048)
    parser.add_argument("--summary-file", help="append a Markdown summary here (GITHUB_STEP_SUMMARY)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON instead of lines")
    args = parser.parse_args(argv)
    try:
        origin = normalise_origin(args.origin)
        skips = parse_skips(args.skip)
    except ValueError as error:
        print(f"usage error: {error}", file=sys.stderr)
        return 2
    results = run(origin, skips, args.llms_min_bytes)
    if args.json:
        print(json.dumps({"origin": origin, "results": [r.__dict__ for r in results]}, indent=2))
    else:
        for r in results:
            print(f"{r.outcome:4}  {r.name:15}  {r.detail}")
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(summary_markdown(origin, results))
    failed = [r for r in results if r.outcome == "FAIL"]
    if failed and not args.json:
        print(f"\n{len(failed)} check(s) failed on {origin}. Each line above names the URL and what it answered.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
