#!/usr/bin/env bash
# Start the Docker-based NAS updater.
#
# Use this when the NAS user cannot install crontab entries. The updater
# container checks GitHub every two minutes and runs deploy-from-github.sh when
# main has a new commit.

set -euo pipefail

RUNTIME_DIR="${MEDIA_REFINER_RUNTIME_DIR:-/volume2/docker/media-refiner}"
SRC_DIR="${MEDIA_REFINER_SRC_DIR:-/volume2/docker/media-refiner-src}"
REPO_URL="${MEDIA_REFINER_REPO:-https://github.com/i-kirito/media-refiner.git}"
BRANCH="${MEDIA_REFINER_BRANCH:-main}"
GIT_PROXY="${MEDIA_REFINER_GIT_PROXY:-}"
INTERVAL_SECONDS="${MEDIA_REFINER_UPDATE_INTERVAL_SECONDS:-120}"
CONTAINER_NAME="${MEDIA_REFINER_UPDATER_CONTAINER:-media-refiner-updater}"
STATE_FILE="${MEDIA_REFINER_STATE_FILE:-${RUNTIME_DIR}/.github-deploy-sha}"

mkdir -p "$RUNTIME_DIR" "$SRC_DIR"

if [ ! -f "${RUNTIME_DIR}/deploy-from-github.sh" ]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cp "${script_dir}/deploy-from-github.sh" "${RUNTIME_DIR}/deploy-from-github.sh"
fi
chmod +x "${RUNTIME_DIR}/deploy-from-github.sh"

if [ ! -f "$STATE_FILE" ] && command -v git >/dev/null 2>&1; then
    git_opts=(-c http.version=HTTP/1.1)
    if [ -n "$GIT_PROXY" ]; then
        git_opts+=(-c "http.proxy=${GIT_PROXY}" -c "https.proxy=${GIT_PROXY}")
    fi
    remote_sha="$(git "${git_opts[@]}" ls-remote "$REPO_URL" "refs/heads/${BRANCH}" | awk '{print $1}')"
    if [ -n "$remote_sha" ]; then
        printf '%s\n' "$remote_sha" > "$STATE_FILE"
    fi
fi

docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v /volume2/docker:/volume2/docker \
    -e TZ=Asia/Shanghai \
    -e MEDIA_REFINER_REPO="$REPO_URL" \
    -e MEDIA_REFINER_BRANCH="$BRANCH" \
    -e MEDIA_REFINER_GIT_PROXY="$GIT_PROXY" \
    -e MEDIA_REFINER_SRC_DIR="$SRC_DIR" \
    -e MEDIA_REFINER_RUNTIME_DIR="$RUNTIME_DIR" \
    -e MEDIA_REFINER_CONFIG_DIR="${MEDIA_REFINER_CONFIG_DIR:-${RUNTIME_DIR}/config}" \
    -e MEDIA_REFINER_DATA_DIR="${MEDIA_REFINER_DATA_DIR:-${RUNTIME_DIR}/data}" \
    docker:27-cli \
    sh -c "apk add --no-cache bash git curl >/tmp/media-refiner-updater-apk.log 2>&1; while true; do bash '${RUNTIME_DIR}/deploy-from-github.sh'; sleep '${INTERVAL_SECONDS}'; done"

docker ps --filter "name=${CONTAINER_NAME}" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
