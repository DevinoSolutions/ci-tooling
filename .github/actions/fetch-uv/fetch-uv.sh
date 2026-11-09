#!/usr/bin/env bash
# Put the pinned uv release binary in $FETCH_UV_BIN_DIR, from the cache directory when a verified
# tarball is already there, from github.com when not, and from the GitHub asset API when
# github.com keeps failing. The tarball is checked against a pinned sha256 on every path, and an
# unverified file is never extracted and never left where the cache save would store it.
#
# Why this exists: astral-sh/setup-uv downloads uv with @actions/tool-cache, which gives up after
# three tries (about 40 seconds). github.com/<owner>/<repo>/releases/download/... answered 504 for
# well over that on 2026-11-08 and 2026-11-09, and every ci-lint run that hit it went red before
# the linter started. The API asset URL 302s to the same CDN object but skips the github.com
# redirect hop that failed, so it is the fallback.
#
# Inputs (environment, so nothing secret is ever on a command line):
#   FETCH_UV_VERSION    uv release, no leading v
#   FETCH_UV_SHA256     sha256 of uv-x86_64-unknown-linux-gnu.tar.gz of that release
#   FETCH_UV_ASSET_ID   GitHub release asset id of that tarball
#   FETCH_UV_TOKEN      token for the asset API (optional; unauthenticated calls share 60/hour per IP).
#                       Read once and unset, so no child process inherits it, and sent only to
#                       https://api.github.com/ (the test server on 127.0.0.1 is the other exception)
#   FETCH_UV_CACHE_KEY  the actions cache key, named in the warning when a cached tarball is bad
#   FETCH_UV_CACHE_DIR  directory the actions cache restores and saves
#   FETCH_UV_BIN_DIR    directory the binaries are extracted to, and added to GITHUB_PATH
# Test-only overrides (the digest check still runs on whatever they serve):
#   FETCH_UV_PRIMARY_URL, FETCH_UV_API_URL, FETCH_UV_RETRY_DELAY, FETCH_UV_RETRY_MAX_TIME,
#   FETCH_UV_MAX_TIME, FETCH_UV_API_RETRY_MAX_TIME
set -euo pipefail

version="${FETCH_UV_VERSION:?}"
sha256="${FETCH_UV_SHA256:?}"
asset_id="${FETCH_UV_ASSET_ID:?}"
token="${FETCH_UV_TOKEN:-}"
unset FETCH_UV_TOKEN
cache_key="${FETCH_UV_CACHE_KEY:-}"
cache_dir="${FETCH_UV_CACHE_DIR:?}"
bin_dir="${FETCH_UV_BIN_DIR:?}"

asset="uv-x86_64-unknown-linux-gnu.tar.gz"
primary_url="${FETCH_UV_PRIMARY_URL:-https://github.com/astral-sh/uv/releases/download/${version}/${asset}}"
api_url="${FETCH_UV_API_URL:-https://api.github.com/repos/astral-sh/uv/releases/assets/${asset_id}}"
retry_delay="${FETCH_UV_RETRY_DELAY:-5}"
retry_max_time="${FETCH_UV_RETRY_MAX_TIME:-60}"
max_time="${FETCH_UV_MAX_TIME:-30}"
api_retry_max_time="${FETCH_UV_API_RETRY_MAX_TIME:-120}"

tarball="$cache_dir/$asset"
partial="$tarball.part"
mkdir -p "$cache_dir" "$bin_dir"

matches_pin() {
  echo "${sha256}  $1" | sha256sum -c - >/dev/null 2>&1
}

source_used="cache"

# A restored tarball is trusted only after it matches the pinned digest; a mismatch (stale key,
# truncated save) is discarded and fetched again.
if [ -f "$tarball" ] && ! matches_pin "$tarball"; then
  # The entry is immutable and the restore hits it on every run, so it is downloaded again every
  # run until someone deletes it.
  echo "::warning::cached $asset does not match the pinned sha256; downloading it again. Delete the bad entry so the next run saves a good one: gh cache delete '${cache_key:-uv-linux-x86_64-*}' -R ${GITHUB_REPOSITORY:-<owner/repo>}"
  rm -f "$tarball"
fi

if [ ! -f "$tarball" ]; then
  rm -f "$partial"

  # --max-time bounds each attempt (a 504 can take ~2 minutes to arrive, a stalled transfer would
  # otherwise hang to the job timeout); a healthy download takes about a second. Only about a
  # minute is spent here before the API fallback, which serves the same object and is checked
  # against the same digest. --retry-all-errors also retries a failed connection or a DNS error,
  # which plain --retry (408/429/5xx and timeouts only) does not.
  source_used="release"
  if ! curl -fsSL --max-time "$max_time" --connect-timeout 20 \
    --retry 40 --retry-all-errors --retry-delay "$retry_delay" --retry-max-time "$retry_max_time" \
    -o "$partial" "$primary_url"; then
    echo "::warning::release download of uv ${version} failed; falling back to the GitHub asset API"
    rm -f "$partial"
    source_used="api"
  elif ! matches_pin "$partial"; then
    echo "::warning::release download of uv ${version} does not match the pinned sha256; falling back to the GitHub asset API"
    rm -f "$partial"
    source_used="api"
  fi

  if [ "$source_used" = "api" ]; then
    # The token goes to curl on stdin as a config line, never on argv or into a log.
    # The header goes only to GitHub's API. Anything else (a stray override) gets no credential.
    case "$api_url" in
      # Userinfo (`http://127.0.0.1:1@other-host/`) would let the second arm match a URL curl sends
      # to another host.
      *@*) send_token=no ;;
      https://api.github.com/* | http://127.0.0.1:*) send_token=yes ;;
      *) send_token=no ;;
    esac
    {
      if [ -n "$token" ] && [ "$send_token" = yes ]; then
        printf 'header = "Authorization: Bearer %s"\n' "$token"
      fi
    } | curl -fsSL -K - \
      --max-time 60 --connect-timeout 20 \
      --retry 5 --retry-all-errors --retry-delay "$retry_delay" --retry-max-time "$api_retry_max_time" \
      -H "Accept: application/octet-stream" -o "$partial" "$api_url"
  fi

  # Fail closed: nothing that misses the pinned digest is extracted or moved into the cache
  # directory, whichever path it came from.
  if ! matches_pin "$partial"; then
    echo "::error::uv ${version} ($source_used) does not match the pinned sha256 ${sha256}"
    rm -f "$partial"
    exit 1
  fi
  mv -f "$partial" "$tarball"
fi

tar -xz -C "$bin_dir" --strip-components=1 < "$tarball"
chmod +x "$bin_dir/uv"

echo "uv ${version}: sha256 verified (source: ${source_used})"
echo "source=${source_used}" >> "${GITHUB_OUTPUT:-/dev/null}"
echo "$bin_dir" >> "${GITHUB_PATH:-/dev/null}"
"$bin_dir/uv" --version

# uv searches PATH for a Python, not the runner tool cache. On a hosted runner whose system Python
# is not 3.12 (ubuntu-26.04 has 3.14), `UV_PYTHON=3.12` would otherwise make uv download a managed
# CPython from github.com on every run: the same host that returned 504. So when no python3.12 is on
# PATH (a system one, or one a caller put there with setup-python, is left alone) and the tool cache
# has a finished 3.12 install, link just its python3.12 into the uv bin dir. Only that name: the
# job's `python`, `python3` and `pip` stay what they were. The ARC pool's image has both a system
# 3.12 and a tool-cache one; there the system one is found first and nothing is linked.
if ! command -v python3.12 >/dev/null 2>&1; then
  # Highest 3.12.x wins (a plain glob sorts 3.12.3 after 3.12.16). `x64.complete` is the tool
  # cache's mark of a finished install.
  python_bin="$(
    for dir in "${RUNNER_TOOL_CACHE:-/nonexistent}"/Python/3.12.*/x64/bin; do
      if [ -f "$dir/../../x64.complete" ] && [ -x "$dir/python3.12" ]; then
        echo "$dir"
      fi
    done | sort -V | tail -n 1
  )"
  if [ -n "$python_bin" ]; then
    ln -sf "$python_bin/python3.12" "$bin_dir/python3.12"
    echo "python 3.12 from the runner tool cache: $python_bin/python3.12"
  fi
fi
