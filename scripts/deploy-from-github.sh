#!/usr/bin/env bash
# Deploy Media Refiner on NAS from the GitHub main branch.
#
# This script is intended to run on the NAS, either from cron or a self-hosted
# GitHub Actions runner. It never copies local machine state and never touches
# runtime config/data beyond mounting the existing directories into the
# recreated container.

set -euo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

REPO_URL="${MEDIA_REFINER_REPO:-https://github.com/i-kirito/media-refiner.git}"
BRANCH="${MEDIA_REFINER_BRANCH:-main}"
GIT_PROXY="${MEDIA_REFINER_GIT_PROXY:-}"
SRC_DIR="${MEDIA_REFINER_SRC_DIR:-/volume2/docker/media-refiner-src}"
RUNTIME_DIR="${MEDIA_REFINER_RUNTIME_DIR:-/volume2/docker/media-refiner}"
CONFIG_DIR="${MEDIA_REFINER_CONFIG_DIR:-${RUNTIME_DIR}/config}"
DATA_DIR="${MEDIA_REFINER_DATA_DIR:-${RUNTIME_DIR}/data}"
CMS_CONFIG_DIR="${MEDIA_REFINER_CMS_CONFIG_DIR:-/volume2/docker/cms/config}"
MEDIA_DIR="${MEDIA_REFINER_MEDIA_DIR:-/volume1/media}"
CLOUDNAS_DIR="${MEDIA_REFINER_CLOUDNAS_DIR:-/volume1/CloudNAS}"
IMAGE="${MEDIA_REFINER_IMAGE:-media-refiner:latest}"
PORT="${MEDIA_REFINER_PORT:-10309}"
PROJECT_NAME="${MEDIA_REFINER_PROJECT:-media-refiner}"
FORCE="${MEDIA_REFINER_FORCE:-0}"
STATE_FILE="${MEDIA_REFINER_STATE_FILE:-${RUNTIME_DIR}/.github-deploy-sha}"
LOG_FILE="${MEDIA_REFINER_LOG_FILE:-${RUNTIME_DIR}/deploy.log}"
LOCK_DIR="${MEDIA_REFINER_LOCK_DIR:-/tmp/media-refiner-deploy.lock}"
COMPOSE_FILE="${MEDIA_REFINER_COMPOSE_FILE:-${RUNTIME_DIR}/docker-compose.deploy.yml}"

log() {
    local line
    line="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
    echo "$line"
    mkdir -p "$(dirname "$LOG_FILE")"
    printf '%s\n' "$line" >> "$LOG_FILE"
}

die() {
    log "ERROR: $*"
    exit 1
}

cleanup() {
    rmdir "$LOCK_DIR" 2>/dev/null || true
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "Another deployment is already running; skip."
    exit 0
fi
trap cleanup EXIT

command -v git >/dev/null || die "git is required"
command -v docker >/dev/null || die "docker is required"
docker compose version >/dev/null || die "docker compose plugin is required"

git_cmd() {
    local opts=(-c http.version=HTTP/1.1)
    if [ -n "$GIT_PROXY" ]; then
        opts+=(-c "http.proxy=${GIT_PROXY}" -c "https.proxy=${GIT_PROXY}")
    fi
    git "${opts[@]}" "$@"
}

git_src() {
    local opts=(-C "$SRC_DIR" -c http.version=HTTP/1.1)
    if [ -n "$GIT_PROXY" ]; then
        opts+=(-c "http.proxy=${GIT_PROXY}" -c "https.proxy=${GIT_PROXY}")
    fi
    git "${opts[@]}" "$@"
}

mkdir -p "$SRC_DIR" "$CONFIG_DIR" "$DATA_DIR"

if [ ! -d "$SRC_DIR/.git" ]; then
    log "Cloning ${REPO_URL} (${BRANCH}) to ${SRC_DIR}"
    rm -rf "$SRC_DIR"
    git_cmd clone --branch "$BRANCH" --single-branch "$REPO_URL" "$SRC_DIR"
else
    log "Fetching ${REPO_URL} (${BRANCH})"
    git_src remote set-url origin "$REPO_URL"
    git_src fetch --prune origin "$BRANCH"
fi

REMOTE_SHA="$(git_src rev-parse "origin/${BRANCH}")"
CURRENT_SHA=""
if [ -f "$STATE_FILE" ]; then
    CURRENT_SHA="$(tr -d '[:space:]' < "$STATE_FILE")"
fi

if [ "$FORCE" != "1" ] && [ "$REMOTE_SHA" = "$CURRENT_SHA" ]; then
    log "Already deployed ${REMOTE_SHA}; nothing to do."
    exit 0
fi

log "Checking out ${REMOTE_SHA}"
git_src checkout -B "$BRANCH" "origin/${BRANCH}"
git_src reset --hard "$REMOTE_SHA"
git_src clean -fdx \
    -e config/.env \
    -e config/.env.example \
    -e data/refiner.db

log "Building image ${IMAGE}"
docker build -t "$IMAGE" "$SRC_DIR"

log "Writing NAS compose file ${COMPOSE_FILE}"
cat > "$COMPOSE_FILE" <<EOF
services:
  media-refiner:
    image: ${IMAGE}
    container_name: media-refiner
    ports:
      - "${PORT}:10308"
    volumes:
      - ${DATA_DIR}:/workspace/data
      - ${CONFIG_DIR}:/workspace/config
      - ${CMS_CONFIG_DIR}:/cms-config:ro
      - ${MEDIA_DIR}:/media:ro
      - ${CLOUDNAS_DIR}:/CloudNAS:ro
    environment:
      - TZ=Asia/Shanghai
      - REFINER_DB_PATH=/workspace/data/refiner.db
    restart: unless-stopped
EOF

log "Recreating container with NAS config/data mounts"
docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --remove-orphans

log "Waiting for http://127.0.0.1:${PORT}/api/config"
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/config" >/dev/null 2>&1; then
        printf '%s\n' "$REMOTE_SHA" > "$STATE_FILE"
        log "Deployed ${REMOTE_SHA} successfully."
        exit 0
    fi
    sleep 2
done

docker logs --tail 80 media-refiner >> "$LOG_FILE" 2>&1 || true
die "Service did not become healthy after deployment"
