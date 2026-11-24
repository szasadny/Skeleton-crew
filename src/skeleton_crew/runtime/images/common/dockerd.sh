#!/usr/bin/env bash

DOCKERD_WAIT_S=30

log() { printf '[entrypoint] %s\n' "$*" >&2; }

start_dockerd() {
    dockerd >/var/log/dockerd.log 2>&1 &
    for _ in $(seq 1 "$DOCKERD_WAIT_S"); do
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    log "dockerd not ready after ${DOCKERD_WAIT_S}s; see /var/log/dockerd.log"
    return 1
}

# The controller hands out a container only once this marker exists (runtime/containers.py READY_FILE).
mark_ready() { touch /run/skeleton-crew-ready; }
