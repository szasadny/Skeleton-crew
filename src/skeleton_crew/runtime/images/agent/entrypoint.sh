#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=/dev/null
source /usr/local/lib/skeleton-crew/dockerd.sh

if [[ -n "${HTTPS_PROXY:-}" ]]; then
    export HTTP_PROXY="${HTTP_PROXY:-$HTTPS_PROXY}"
    export NO_PROXY="${NO_PROXY:-}"
    export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY" no_proxy="$NO_PROXY"
fi

if start_dockerd; then
    log "dockerd ready"
fi

mark_ready
exec sleep infinity
