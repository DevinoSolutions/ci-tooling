"""The fetch-uv action's download script, driven against a local server.

github.com answered 504 to the uv download for longer than setup-uv's three tries on 2026-11-08 and
2026-11-09, and every ci-lint run that hit it went red before the linter started. These tests pin
the replacement: a patient primary download, a fallback through the release-asset API, a sha256
check on every path, and nothing unverified left where the cache save would store it.

The script is real bash, curl, sha256sum and tar; only the two URLs are redirected (the script's
test-only overrides) to a server on 127.0.0.1 that this module controls.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTION_DIR = REPO_ROOT / ".github" / "actions" / "fetch-uv"
SCRIPT = ACTION_DIR / "fetch-uv.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "curl", "sha256sum", "tar")),
    reason="needs bash, curl, sha256sum and tar",
)

BASH = shutil.which("bash") or "bash"
TOKEN = "ghs_not-a-real-token-for-the-test"


def make_tarball() -> bytes:
    """A tarball laid out like uv's release: one top directory holding `uv` and `uvx`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        top = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu")
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name in ("uv", "uvx"):
            body = f'#!/bin/sh\necho "{name} 0.0.0-test"\n'.encode()
            info = tarfile.TarInfo(f"uv-x86_64-unknown-linux-gnu/{name}")
            info.size = len(body)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


GOOD = make_tarball()
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
BAD = b"<html>502 Bad Gateway</html>"


class Server:
    """Serves /release and /api; each answers with a status and a body the test sets."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {"/release": (504, b""), "/api": (504, b"")}
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                status, body = outer.routes.get(self.path, (404, b""))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def hits(self, path: str) -> list[dict[str, str]]:
        return [headers for p, headers in self.requests if p == path]


@pytest.fixture
def server():
    srv = Server()
    srv.thread.start()
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()


class Run:
    def __init__(self, tmp: Path, srv: Server, sha: str = GOOD_SHA) -> None:
        self.cache = tmp / "uv-cache"
        self.bin = tmp / "uv-bin"
        self.output = tmp / "github_output"
        self.path_file = tmp / "github_path"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FETCH_UV_")}
        self.env.update(
            FETCH_UV_VERSION="0.0.0-test",
            FETCH_UV_SHA256=sha,
            FETCH_UV_ASSET_ID="1",
            FETCH_UV_TOKEN=TOKEN,
            FETCH_UV_CACHE_DIR=self.cache.as_posix(),
            FETCH_UV_BIN_DIR=self.bin.as_posix(),
            FETCH_UV_PRIMARY_URL=f"{srv.base}/release",
            FETCH_UV_API_URL=f"{srv.base}/api",
            # Keep the failing cases to a few seconds: the production values are 5 s and 60 s.
            FETCH_UV_RETRY_DELAY="1",
            FETCH_UV_RETRY_MAX_TIME="2",
            FETCH_UV_API_RETRY_MAX_TIME="2",
            FETCH_UV_MAX_TIME="5",
            GITHUB_OUTPUT=self.output.as_posix(),
            GITHUB_PATH=self.path_file.as_posix(),
        )

    def go(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, SCRIPT.as_posix()], env=self.env, capture_output=True, text=True, timeout=120
        )

    def source(self) -> str:
        match = re.search(r"^source=(\w+)$", self.output.read_text(), re.M)
        assert match, "the script reported no source"
        return match.group(1)

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.cache.glob("*")) if self.cache.exists() else []


def test_release_download_is_verified_extracted_and_cached(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "release"
    assert "uv 0.0.0-test" in done.stdout
    assert (run.bin / "uv").is_file() and (run.bin / "uvx").is_file()
    assert run.leftovers() == ["uv-x86_64-unknown-linux-gnu.tar.gz"]
    assert run.bin.as_posix() in run.path_file.read_text()
    assert server.hits("/api") == [], "the API is only the fallback"


def test_a_504_from_github_falls_back_to_the_asset_api(tmp_path, server):
    server.routes["/release"] = (504, b"")
    server.routes["/api"] = (200, GOOD)
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "api"
    assert len(server.hits("/release")) > 1, "the primary download must be retried before giving up"
    (api_headers,) = server.hits("/api")
    assert api_headers["accept"] == "application/octet-stream"
    assert api_headers["authorization"] == f"Bearer {TOKEN}"
    assert all("authorization" not in h for h in server.hits("/release")), "no token to github.com"
    assert TOKEN not in done.stdout + done.stderr
    assert (run.bin / "uv").is_file()
    assert run.leftovers() == ["uv-x86_64-unknown-linux-gnu.tar.gz"]


def test_the_token_is_never_on_a_command_line():
    text = SCRIPT.read_text()
    assert "-K -" in text
    assert not re.search(r"curl[^\n]*\$\{?token", text), "the token must reach curl on stdin only"


def test_a_primary_body_that_misses_the_digest_falls_back_to_the_api(tmp_path, server):
    server.routes["/release"] = (200, BAD)
    server.routes["/api"] = (200, GOOD)
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "api"


def test_nothing_that_misses_the_digest_is_extracted_or_cached(tmp_path, server):
    server.routes["/release"] = (200, BAD)
    server.routes["/api"] = (200, BAD)
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode != 0
    assert "does not match the pinned sha256" in done.stdout + done.stderr
    assert not (run.bin / "uv").exists()
    assert run.leftovers() == [], "an unverified file must never sit where the cache save stores it"


def test_both_paths_down_fails_without_extracting(tmp_path, server):
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode != 0
    assert not (run.bin / "uv").exists()
    assert run.leftovers() == []


def test_a_verified_cached_tarball_needs_no_network(tmp_path, server):
    run = Run(tmp_path, server)
    run.cache.mkdir()
    (run.cache / "uv-x86_64-unknown-linux-gnu.tar.gz").write_bytes(GOOD)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "cache"
    assert server.requests == []
    assert (run.bin / "uv").is_file()


def test_a_cached_tarball_that_misses_the_digest_is_discarded_and_fetched_again(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    run.cache.mkdir()
    (run.cache / "uv-x86_64-unknown-linux-gnu.tar.gz").write_bytes(BAD)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "release"
    assert (run.cache / "uv-x86_64-unknown-linux-gnu.tar.gz").read_bytes() == GOOD


def test_the_pins_in_the_action_are_well_formed():
    text = (ACTION_DIR / "action.yml").read_text()
    sha = re.search(r'sha256:\s+description:[^\n]*\n\s+default: "([0-9a-f]{64})"', text)
    asset = re.search(r'asset-id:\s+description:[^\n]*\n\s+default: "(\d+)"', text)
    version = re.search(r'version:\s+description:[^\n]*\n\s+default: "(\d+\.\d+\.\d+)"', text)
    assert sha and asset and version, "version, sha256 and asset-id must all be pinned by default"


@pytest.mark.parametrize("workflow", ["ci-lint.yml", "selftest.yml"])
def test_the_workflows_do_not_go_back_to_setup_uv(workflow):
    text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text()
    uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.M)
    assert not any(u.startswith("astral-sh/setup-uv") for u in uses)
    assert any(u.endswith(".github/actions/fetch-uv") for u in uses)
