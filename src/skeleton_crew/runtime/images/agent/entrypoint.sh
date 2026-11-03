#!/usr/bin/env bash
set -euo pipefail

DOCKERD_WAIT_S=30

log() { printf '[entrypoint] %s\n' "$*" >&2; }

if [[ -n "${HTTPS_PROXY:-}" ]]; then
    export HTTP_PROXY="${HTTP_PROXY:-$HTTPS_PROXY}"
    export NO_PROXY="${NO_PROXY:-}"
    export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY" no_proxy="$NO_PROXY"
fi

start_dockerd() {
    if ! command -v dockerd >/dev/null 2>&1; then
        log "dockerd not installed; continuing without nested docker"
        return 0
    fi
    dockerd >/var/log/dockerd.log 2>&1 &
    for _ in $(seq 1 "$DOCKERD_WAIT_S"); do
        if docker info >/dev/null 2>&1; then
            log "dockerd ready"
            return 0
        fi
        sleep 1
    done
    log "dockerd not ready after ${DOCKERD_WAIT_S}s; see /var/log/dockerd.log"
}

start_dockerd || log "dockerd failed to start"

exec sleep infinity
