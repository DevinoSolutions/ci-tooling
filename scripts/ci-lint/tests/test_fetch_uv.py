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
    """Serves /release, /api and /obj; each answers with a status, a body and headers the test sets."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes] | tuple[int, bytes, dict[str, str]]] = {
            "/release": (504, b""),
            "/api": (504, b""),
        }
        self.requests: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                status, body, *rest = outer.routes.get(self.path, (404, b""))
                self.send_response(status)
                for name, value in (rest[0] if rest else {}).items():
                    self.send_header(name, value)
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


def path_without_python312() -> str:
    """The ambient PATH minus every directory holding a `python3.12`, so the script's "is one on
    PATH already?" answer comes from the test and not from the machine it runs on. (selftest.yml's
    own earlier step links one into a directory on PATH.)"""
    kept = [d for d in os.environ["PATH"].split(os.pathsep) if not shutil.which("python3.12", path=d)]
    path = os.pathsep.join(kept)
    if not all(shutil.which(tool, path=path) for tool in ("bash", "curl", "sha256sum", "tar", "sort", "tail")):
        pytest.skip("the system python3.12 lives in a directory the script's tools also need")
    return path


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
            PATH=path_without_python312(),
            RUNNER_TOOL_CACHE=(tmp / "no-toolcache").as_posix(),
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


def real_curl() -> str:
    path = shutil.which("curl")
    assert path
    return Path(path).as_posix()


def curl_shim(tmp: Path) -> tuple[Path, Path]:
    """A `curl` that records its argv and its environment, then runs the real one."""
    shim_dir = tmp / "shim"
    shim_dir.mkdir()
    log = tmp / "curl.log"
    shim = shim_dir / "curl"
    script = "\n".join(
        [
            "#!/bin/bash",
            "{ printf 'ARG %s\\n' \"$@\"; env | sed 's/^/ENV /'; } >> \"" + log.as_posix() + '"',
            'exec "' + real_curl() + '" "$@"',
            "",
        ]
    )
    shim.write_bytes(script.encode())
    shim.chmod(0o755)
    return shim_dir, log


def test_the_token_reaches_curl_on_stdin_only(tmp_path, server):
    server.routes["/api"] = (200, GOOD)
    run = Run(tmp_path, server)
    shim_dir, log = curl_shim(tmp_path)
    run.env["PATH"] = f"{shim_dir}{os.pathsep}{run.env['PATH']}"
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert run.source() == "api"
    recorded = log.read_text()
    assert f"ARG {server.base}/api" in recorded, "the shim must have seen the API call"
    assert TOKEN not in recorded, "the token was on a curl command line or in its environment"
    assert server.hits("/api")[0]["authorization"] == f"Bearer {TOKEN}", "it must still arrive"


def test_the_token_is_not_sent_to_a_url_that_is_not_githubs_api(tmp_path, server):
    server.routes["/api"] = (200, GOOD)
    run = Run(tmp_path, server)
    run.env["FETCH_UV_API_URL"] = f"http://localhost:{server.httpd.server_address[1]}/api"
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    (headers,) = server.hits("/api")
    assert "authorization" not in headers


def test_the_token_is_not_sent_through_a_userinfo_url(tmp_path, server):
    # curl reads `127.0.0.1:1` here as user:password and connects to `localhost`, so a prefix match
    # on `http://127.0.0.1:` alone would hand the token to another host.
    server.routes["/api"] = (200, GOOD)
    run = Run(tmp_path, server)
    run.env["FETCH_UV_API_URL"] = f"http://127.0.0.1:1@localhost:{server.httpd.server_address[1]}/api"
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    (headers,) = server.hits("/api")
    assert TOKEN not in str(headers)
    assert not headers.get("authorization", "").startswith("Bearer")


def test_the_token_does_not_follow_a_redirect_to_another_host(tmp_path, server):
    # The real API answers 302 to a CDN host. `localhost` and `127.0.0.1` are different hosts to curl.
    other = Server()
    other.routes["/obj"] = (200, GOOD)
    other.thread.start()
    try:
        server.routes["/api"] = (302, b"", {"Location": f"http://localhost:{other.httpd.server_address[1]}/obj"})
        run = Run(tmp_path, server)
        done = run.go()
        assert done.returncode == 0, done.stdout + done.stderr
        assert run.source() == "api"
        assert server.hits("/api")[0]["authorization"] == f"Bearer {TOKEN}"
        (redirected,) = other.hits("/obj")
        assert "authorization" not in redirected
    finally:
        other.httpd.shutdown()
        other.httpd.server_close()


def tool_cache_python(tool_cache: Path, version: str, complete: bool = True) -> Path:
    bin_dir = tool_cache / "Python" / version / "x64" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python3", "python3.12", "pip"):
        (bin_dir / name).write_bytes(b"#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)
    if complete:
        (tool_cache / "Python" / version / "x64.complete").write_bytes(b"")
    return bin_dir


def linked_python(run: Run) -> Path:
    return run.bin / "python3.12"


def test_the_tool_cache_python_312_is_linked_by_name_only(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    tool_cache = tmp_path / "toolcache"
    tool_cache_python(tool_cache, "3.12.3")
    newest = tool_cache_python(tool_cache, "3.12.16")
    tool_cache_python(tool_cache, "3.13.1")
    run.env["RUNNER_TOOL_CACHE"] = tool_cache.as_posix()
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert linked_python(run).exists()
    assert f"python 3.12 from the runner tool cache: {newest.as_posix()}/python3.12" in done.stdout
    if linked_python(run).is_symlink():
        assert Path(os.readlink(linked_python(run))) == newest / "python3.12"
    # Nothing else of the job's PATH changes: python, python3 and pip stay what they were.
    assert run.path_file.read_text().splitlines() == [run.bin.as_posix()]
    assert not (run.bin / "python3").exists() and not (run.bin / "pip").exists()


def test_an_unfinished_tool_cache_install_is_ignored(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    tool_cache = tmp_path / "toolcache"
    finished = tool_cache_python(tool_cache, "3.12.4")
    tool_cache_python(tool_cache, "3.12.99", complete=False)
    run.env["RUNNER_TOOL_CACHE"] = tool_cache.as_posix()
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"{finished.as_posix()}/python3.12" in done.stdout
    assert "3.12.99" not in done.stdout


def test_a_python_312_already_on_path_is_left_alone(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    tool_cache = tmp_path / "toolcache"
    tool_cache_python(tool_cache, "3.12.16")
    run.env["RUNNER_TOOL_CACHE"] = tool_cache.as_posix()
    system = tmp_path / "system-bin"
    system.mkdir()
    (system / "python3.12").write_bytes(b"#!/bin/sh\n")
    (system / "python3.12").chmod(0o755)
    run.env["PATH"] = f"{system}{os.pathsep}{run.env['PATH']}"
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert not linked_python(run).exists()
    assert "runner tool cache" not in done.stdout


def test_without_a_tool_cache_python_312_nothing_is_linked(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert not linked_python(run).exists()
    assert run.path_file.read_text().splitlines() == [run.bin.as_posix()]


def test_a_bad_cached_tarball_warning_names_the_key_to_delete(tmp_path, server):
    server.routes["/release"] = (200, GOOD)
    run = Run(tmp_path, server)
    run.env["FETCH_UV_CACHE_KEY"] = "uv-linux-x86_64-0.0.0-test-abc"
    run.cache.mkdir()
    (run.cache / "uv-x86_64-unknown-linux-gnu.tar.gz").write_bytes(BAD)
    done = run.go()
    assert done.returncode == 0, done.stdout + done.stderr
    assert "gh cache delete 'uv-linux-x86_64-0.0.0-test-abc'" in done.stdout + done.stderr


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
