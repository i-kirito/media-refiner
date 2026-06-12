#!/usr/bin/env bash
# Install the NAS-side polling updater for Media Refiner.
#
# Run this on the NAS. It installs a crontab entry that checks GitHub every
# two minutes and deploys only when the configured branch has a new commit.

set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

RUNTIME_DIR="${MEDIA_REFINER_RUNTIME_DIR:-/volume2/docker/media-refiner}"
SRC_DIR="${MEDIA_REFINER_SRC_DIR:-/volume2/docker/media-refiner-src}"
REPO_URL="${MEDIA_REFINER_REPO:-https://github.com/i-kirito/media-refiner.git}"
BRANCH="${MEDIA_REFINER_BRANCH:-main}"
GIT_PROXY="${MEDIA_REFINER_GIT_PROXY:-}"
CRON_SCHEDULE="${MEDIA_REFINER_CRON_SCHEDULE:-*/2 * * * *}"
SCRIPT_PATH="${MEDIA_REFINER_DEPLOY_SCRIPT:-${RUNTIME_DIR}/deploy-from-github.sh}"
LOG_FILE="${MEDIA_REFINER_LOG_FILE:-${RUNTIME_DIR}/deploy.log}"
STATE_FILE="${MEDIA_REFINER_STATE_FILE:-${RUNTIME_DIR}/.github-deploy-sha}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DEPLOY_SCRIPT="${SCRIPT_DIR}/deploy-from-github.sh"

mkdir -p "$RUNTIME_DIR" "$SRC_DIR"

if [ ! -f "$SCRIPT_PATH" ]; then
    if [ -f "$SOURCE_DEPLOY_SCRIPT" ]; then
        cp "$SOURCE_DEPLOY_SCRIPT" "$SCRIPT_PATH"
    else
        echo "Missing deploy script at $SCRIPT_PATH" >&2
        exit 1
    fi
fi
chmod +x "$SCRIPT_PATH"

git_cmd() {
    local opts=(-c http.version=HTTP/1.1)
    if [ -n "$GIT_PROXY" ]; then
        opts+=(-c "http.proxy=${GIT_PROXY}" -c "https.proxy=${GIT_PROXY}")
    fi
    git "${opts[@]}" "$@"
}

REMOTE_SHA="$(git_cmd ls-remote "$REPO_URL" "refs/heads/${BRANCH}" | awk '{print $1}')"
if [ -n "$REMOTE_SHA" ] && [ ! -f "$STATE_FILE" ]; then
    printf '%s\n' "$REMOTE_SHA" > "$STATE_FILE"
    echo "Initialized deployed SHA marker to $REMOTE_SHA"
fi

CRON_CMD="MEDIA_REFINER_REPO='${REPO_URL}' MEDIA_REFINER_BRANCH='${BRANCH}' MEDIA_REFINER_GIT_PROXY='${GIT_PROXY}' MEDIA_REFINER_SRC_DIR='${SRC_DIR}' MEDIA_REFINER_RUNTIME_DIR='${RUNTIME_DIR}' '${SCRIPT_PATH}' >> '${LOG_FILE}' 2>&1"
CRON_LINE="${CRON_SCHEDULE} ${CRON_CMD}"

tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v "deploy-from-github.sh" > "$tmp" || true
printf '%s\n' "$CRON_LINE" >> "$tmp"
crontab "$tmp"
rm -f "$tmp"

echo "Installed Media Refiner NAS auto-update cron:"
echo "$CRON_LINE"
