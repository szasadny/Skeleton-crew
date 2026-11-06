#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR=/runner-config
CONFIG_FILES=(.runner .credentials .credentials_rsaparams)
DOCKERD_WAIT_S=30
NOT_REGISTERED_EXIT=3

log() { printf '[entrypoint] %s\n' "$*" >&2; }

register() {
    if [[ ! -d "$CONFIG_DIR" ]]; then
        log "mount the runner's config directory at $CONFIG_DIR"
        exit 2
    fi
    if [[ -e "$CONFIG_DIR/.runner" ]]; then
        log "$CONFIG_DIR already holds a registration; remove it first"
        exit 2
    fi
    cd "$RUNNER_DIR"
    runuser -u runner -- ./config.sh --unattended --disableupdate --work _work "$@"
    for file in "${CONFIG_FILES[@]}"; do
        install -m 0600 -o runner -g runner "$file" "$CONFIG_DIR/$file"
    done
    log "registered; the config is in $CONFIG_DIR"
}

# Nested containers on the default bridge cannot use Docker's DNS, so they get the proxy by IP.
setup_proxy() {
    local url="${HTTPS_PROXY:-}" host ip
    [[ -n "$url" ]] || return 0
    host="${url#*://}"
    host="${host%%[:/]*}"
    ip="$(getent ahostsv4 "$host" | awk 'NR == 1 { print $1 }')"
    [[ -n "$ip" ]] && url="${url/"$host"/"$ip"}"
    export HTTPS_PROXY="$url" HTTP_PROXY="$url" https_proxy="$url" http_proxy="$url"
    export NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,::1}" no_proxy="${NO_PROXY:-localhost,127.0.0.1,::1}"
    install -d -o runner -g runner -m 0700 /home/runner/.docker
    jq -n --arg proxy "$url" --arg bypass "$NO_PROXY" \
        '{proxies: {default: {httpProxy: $proxy, httpsProxy: $proxy, noProxy: $bypass}}}' \
        > /home/runner/.docker/config.json
    chown runner:runner /home/runner/.docker/config.json
}

start_dockerd() {
    dockerd >/var/log/dockerd.log 2>&1 &
    for _ in $(seq 1 "$DOCKERD_WAIT_S"); do
        if docker info >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    log "dockerd not ready after ${DOCKERD_WAIT_S}s; see /var/log/dockerd.log"
    exit 1
}

run_once() {
    for file in "${CONFIG_FILES[@]}"; do
        if [[ ! -f "$CONFIG_DIR/$file" ]]; then
            log "not registered: $CONFIG_DIR/$file is missing"
            exit "$NOT_REGISTERED_EXIT"
        fi
        # A copy: the mount is read-only and every job starts from the registered config.
        install -m 0600 -o runner -g runner "$CONFIG_DIR/$file" "$RUNNER_DIR/$file"
    done
    setup_proxy
    start_dockerd
    touch /run/skeleton-crew-ready
    cd "$RUNNER_DIR"
    exec runuser -u runner -- ./run.sh --once
}

case "${1:-run}" in
    register)
        shift
        register "$@"
        ;;
    run)
        run_once
        ;;
    *)
        log "usage: entrypoint.sh [run | register <config.sh options>]"
        exit 2
        ;;
esac
