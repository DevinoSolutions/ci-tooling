"""Drives check.py against a real local HTTP server, one fake site per case."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import check

FUTURE = (datetime.now(timezone.utc) + timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%SZ")
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

HOME_HTML = '<html><head><link rel="alternate" type="text/markdown" href="/index.md"></head><body>Hi</body></html>'
ROBOTS_OK = "User-agent: *\nAllow: /\n\nSitemap: https://example.com/sitemap.xml\n"


def good_site():
    """Every surface correct. A case copies it and breaks one thing."""
    return {
        "/llms.txt": (200, {"Content-Type": "text/plain; charset=utf-8"}, "# Example\n" + "x" * 3000),
        "/index.md": (200, {"Content-Type": "text/markdown; charset=utf-8"}, "# Example home\n"),
        "/": "negotiate",
        "/.well-known/security.txt": (
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
            f"Contact: mailto:security@example.com\nExpires: {FUTURE}\n",
        ),
        "/.well-known/api-catalog": (
            200,
            {"Content-Type": "application/linkset+json"},
            json.dumps({"linkset": [{"anchor": "https://example.com/api"}]}),
        ),
        "/robots.txt": (200, {"Content-Type": "text/plain"}, ROBOTS_OK),
    }


class Site:
    def __init__(self, routes, home_markdown=None, home_html=None):
        self.routes = routes
        self.home_markdown = home_markdown or (
            200,
            [("Content-Type", "text/markdown; charset=utf-8"), ("Vary", "Accept")],
            "# Example home\n",
        )
        self.home_html = home_html or (200, [("Content-Type", "text/html; charset=utf-8")], HOME_HTML)


@pytest.fixture
def serve():
    servers = []

    def start(site: Site) -> str:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def do_GET(self):  # noqa: N802 - http.server API
                route = site.routes.get(self.path.split("?")[0])
                if route == "negotiate":
                    accept = self.headers.get("Accept", "")
                    status, headers, body = site.home_markdown if "text/markdown" in accept and "text/html" not in accept else site.home_html
                elif route is None:
                    status, headers, body = 404, [("Content-Type", "text/html")], "not found"
                else:
                    status, header_map, body = route
                    headers = list(header_map.items())
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body.encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()


def outcomes(origin, skips=None):
    return {r.name: r for r in check.run(origin, skips or {})}


def test_a_site_that_serves_every_surface_passes_every_check(serve):
    results = outcomes(serve(Site(good_site())))
    assert {name: r.outcome for name, r in results.items()} == {name: "PASS" for name in check.CHECKS}


def test_a_short_llms_txt_fails_with_its_byte_count(serve):
    routes = good_site()
    routes["/llms.txt"] = (200, {"Content-Type": "text/plain"}, "# tiny\n")
    result = outcomes(serve(Site(routes)))["llms-txt"]
    assert result.outcome == "FAIL" and "7 bytes" in result.detail


def test_markdown_for_the_home_page_without_vary_accept_fails(serve):
    site = Site(good_site(), home_markdown=(200, [("Content-Type", "text/markdown")], "# Home\n"))
    result = outcomes(serve(site))["accept-markdown"]
    assert result.outcome == "FAIL" and "Vary: Accept" in result.detail


def test_vary_accept_on_its_own_header_line_counts(serve):
    """Next sends its router Vary and the app's Accept on separate lines; both must be read."""
    site = Site(
        good_site(),
        home_markdown=(
            200,
            [("Content-Type", "text/markdown"), ("Vary", "rsc, next-router-state-tree"), ("Vary", "Accept")],
            "# Home\n",
        ),
    )
    assert outcomes(serve(site))["accept-markdown"].outcome == "PASS"


def test_a_home_page_that_ignores_accept_markdown_fails(serve):
    site = Site(good_site(), home_markdown=(200, [("Content-Type", "text/html")], HOME_HTML))
    result = outcomes(serve(site))["accept-markdown"]
    assert result.outcome == "FAIL" and "text/html" in result.detail


def test_negotiation_that_breaks_browsers_fails_browser_html(serve):
    site = Site(good_site(), home_html=(200, [("Content-Type", "text/markdown"), ("Vary", "Accept")], "# Home\n"))
    assert outcomes(serve(site))["browser-html"].outcome == "FAIL"


def test_a_link_header_satisfies_the_alternate_check(serve):
    site = Site(
        good_site(),
        home_html=(
            200,
            [("Content-Type", "text/html"), ("Link", '</index.md>; rel="alternate"; type="text/markdown"')],
            "<html><head></head></html>",
        ),
    )
    assert outcomes(serve(site))["alternate-link"].outcome == "PASS"


def test_a_home_page_without_any_markdown_alternate_fails(serve):
    site = Site(good_site(), home_html=(200, [("Content-Type", "text/html")], '<link rel="alternate" hreflang="fr" href="/fr">'))
    assert outcomes(serve(site))["alternate-link"].outcome == "FAIL"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (f"Expires: {FUTURE}\n", "no Contact"),
        ("Contact: mailto:a@example.com\n", "no Expires"),
        (f"Contact: mailto:a@example.com\nExpires: {PAST}\n", "expired"),
        ("Contact: mailto:a@example.com\nExpires: next year\n", "not an ISO 8601"),
    ],
)
def test_security_txt_needs_contact_and_a_future_expires(serve, body, fragment):
    routes = good_site()
    routes["/.well-known/security.txt"] = (200, {"Content-Type": "text/plain"}, body)
    result = outcomes(serve(Site(routes)))["security-txt"]
    assert result.outcome == "FAIL" and fragment in result.detail


def test_a_missing_security_txt_reports_the_status_it_got(serve):
    routes = good_site()
    del routes["/.well-known/security.txt"]
    result = outcomes(serve(Site(routes)))["security-txt"]
    assert result.outcome == "FAIL" and "404" in result.detail


def test_an_api_catalog_without_a_linkset_fails(serve):
    routes = good_site()
    routes["/.well-known/api-catalog"] = (200, {"Content-Type": "application/json"}, json.dumps({"linkset": []}))
    assert outcomes(serve(Site(routes)))["api-catalog"].outcome == "FAIL"


def test_robots_that_blocks_an_assistant_crawler_names_it(serve):
    routes = good_site()
    routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, "User-agent: ClaudeBot\nDisallow: /\n\n" + ROBOTS_OK)
    result = outcomes(serve(Site(routes)))["robots-ai"]
    assert result.outcome == "FAIL" and "ClaudeBot" in result.detail and "Bingbot" not in result.detail


def test_blocking_gptbot_is_an_owner_call_and_does_not_fail_the_contract(serve):
    routes = good_site()
    routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, "User-agent: GPTBot\nDisallow: /\n\n" + ROBOTS_OK)
    assert outcomes(serve(Site(routes)))["robots-ai"].outcome == "PASS"


def test_robots_without_a_sitemap_line_fails(serve):
    routes = good_site()
    routes["/robots.txt"] = (200, {"Content-Type": "text/plain"}, "User-agent: *\nAllow: /\n")
    result = outcomes(serve(Site(routes)))["robots-ai"]
    assert result.outcome == "FAIL" and "Sitemap" in result.detail


class SoftNotFound(dict):
    """Routes where every unknown path answers 200 HTML instead of 404."""

    def get(self, key, default=None):
        return super().get(key, (200, {"Content-Type": "text/html"}, "<html>soft</html>"))


def test_a_soft_200_for_an_unknown_twin_fails(serve):
    result = outcomes(serve(Site(SoftNotFound(good_site()))))["unknown-md-404"]
    assert result.outcome == "FAIL" and "soft 200" in result.detail


def test_a_skipped_check_is_not_fetched_and_carries_its_reason(serve):
    routes = good_site()
    del routes["/.well-known/api-catalog"]
    result = outcomes(serve(Site(routes)), {"api-catalog": "no public API or MCP"})["api-catalog"]
    assert (result.outcome, result.detail) == ("SKIP", "no public API or MCP")


def test_skips_parse_from_commas_and_newlines():
    assert check.parse_skips(["api-catalog: no API, security-txt: owner item\nindex-md: static export"]) == {
        "api-catalog": "no API",
        "security-txt": "owner item",
        "index-md": "static export",
    }


@pytest.mark.parametrize("value", ["api-catalog", "api-catalog:   ", "not-a-check: reason"])
def test_a_skip_without_a_reason_or_for_an_unknown_check_is_refused(value):
    with pytest.raises(ValueError):
        check.parse_skips([value])


@pytest.mark.parametrize("value", ["example.com", "https://example.com/docs", "ftp://example.com"])
def test_origin_must_be_a_bare_http_origin(value):
    with pytest.raises(ValueError):
        check.normalise_origin(value)


def test_main_exits_1_and_writes_a_summary_when_a_check_fails(serve, tmp_path, capsys):
    routes = good_site()
    del routes["/.well-known/security.txt"]
    summary = tmp_path / "summary.md"
    code = check.main(["--origin", serve(Site(routes)), "--summary-file", str(summary)])
    assert code == 1
    assert "❌ | `security-txt`" in summary.read_text(encoding="utf-8")
    assert "1 check(s) failed" in capsys.readouterr().out


def test_main_exits_0_when_the_only_gap_is_skipped(serve):
    routes = good_site()
    del routes["/.well-known/api-catalog"]
    assert check.main(["--origin", serve(Site(routes)), "--skip", "api-catalog: no public API"]) == 0


def test_main_exits_2_on_a_skip_without_a_reason(capsys):
    assert check.main(["--origin", "https://example.com", "--skip", "api-catalog"]) == 2
    assert "has no reason" in capsys.readouterr().err
