"""Check that the fetch-uv action's three pins describe one real uv release asset.

The API fallback downloads by asset id, the primary download by version, and both are checked
against one sha256. A bump that changes two of the three would only break the fallback, and only
while github.com is already down, so this reads the pins from action.yml and asks GitHub whether
they agree: the asset must be the linux x86_64 tarball, its digest must be the pinned sha256, and
its download URL must belong to the pinned version. Run by selftest.yml.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import yaml

ACTION = Path(__file__).resolve().parents[2] / ".github" / "actions" / "fetch-uv" / "action.yml"
TARBALL = "uv-x86_64-unknown-linux-gnu.tar.gz"


def main() -> int:
    inputs = yaml.safe_load(ACTION.read_text())["inputs"]
    version = inputs["version"]["default"]
    sha256 = inputs["sha256"]["default"]
    asset_id = inputs["asset-id"]["default"]

    request = urllib.request.Request(
        f"https://api.github.com/repos/astral-sh/uv/releases/assets/{asset_id}",
        headers={"Accept": "application/vnd.github+json"},
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        asset = json.load(response)

    problems = []
    if asset.get("name") != TARBALL:
        problems.append(f"asset {asset_id} is {asset.get('name')!r}, not {TARBALL!r}")
    if asset.get("digest") != f"sha256:{sha256}":
        problems.append(f"asset {asset_id} has digest {asset.get('digest')!r}, the pin is sha256:{sha256}")
    if f"/{version}/" not in asset.get("browser_download_url", ""):
        problems.append(f"asset {asset_id} is not part of release {version}: {asset.get('browser_download_url')}")
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"uv {version}: asset {asset_id} is {TARBALL}, digest and release match the pins")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
