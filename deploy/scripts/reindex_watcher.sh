#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# reindex_watcher.sh — debounced inotify watcher → POST /api/knowledge_reindex
#
# Runs inside the `reindex_watcher` sibling container (see deploy/compose.yml).
# Mounts: ./vault -> /watch (ro), ./scripts -> /scripts (ro).
#
# After `git pull` on the VPS, mark this script executable:
#   chmod +x deploy/scripts/reindex_watcher.sh
# (Win11 NTFS does not preserve POSIX mode bits through git, so we set it on
#  the VPS, not on the dev box.)
#
# Auth: shared-secret header X-Reindex-Secret matched against the REINDEX_SECRET
# env var on both this container and agent-zero (same .env file). Loopback
# enforcement was rejected because helpers/network.py:is_loopback_address only
# accepts 127.0.0.0/8 and ::1, not Docker bridge IPs.
#
# TODO(image): inotify-tools should be baked into the agent-zero base image
#   rather than installed at container start. Doing it here keeps the script
#   self-contained for v1, but it adds ~5s to every restart.
# ----------------------------------------------------------------------------
set -euo pipefail

WATCH_DIR="/watch"
ENDPOINT="http://agent-zero:80/api/knowledge_reindex"
DEBOUNCE_SECONDS=30
MEMORY_SUBDIR="default"
REINDEX_SECRET="${REINDEX_SECRET:?REINDEX_SECRET env var required (see deploy/.env)}"

log() {
    echo "[reindex_watcher $(date -u +%FT%TZ)] $*"
}

ensure_deps() {
    if ! command -v inotifywait >/dev/null 2>&1; then
        log "inotify-tools not found; attempting install (apt-get / apk)…"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq && apt-get install -y --no-install-recommends inotify-tools >/dev/null
        elif command -v apk >/dev/null 2>&1; then
            apk add --no-cache inotify-tools >/dev/null
        else
            log "FATAL: no apt-get or apk available, cannot install inotify-tools"
            exit 1
        fi
    fi
    if ! command -v curl >/dev/null 2>&1; then
        log "FATAL: curl not present in image"
        exit 1
    fi
}

fire_reindex() {
    log "firing reindex POST -> ${ENDPOINT}"
    if curl -sf -X POST "${ENDPOINT}" \
        -H "Content-Type: application/json" \
        -H "X-Reindex-Secret: ${REINDEX_SECRET}" \
        -d "{\"memory_subdir\":\"${MEMORY_SUBDIR}\"}" \
        -o /tmp/reindex_resp.json
    then
        log "reindex OK: $(cat /tmp/reindex_resp.json 2>/dev/null || true)"
    else
        local rc=$?
        log "reindex FAILED rc=${rc}; continuing watch loop (will retry on next event)"
    fi
}

main() {
    ensure_deps
    log "watching ${WATCH_DIR} (debounce=${DEBOUNCE_SECONDS}s)"

    # Pattern: collect events into a 30s idle window. inotifywait -t prints
    # nothing on timeout (rc=2). On any event, set the dirty flag; once we
    # get a timeout AND we are dirty, fire and reset.
    local dirty=0
    while true; do
        # -q quiet, -r recursive, -m monitor, -t debounce timeout.
        # We actually use the non-monitor form so -t works as the idle gate.
        if inotifywait -q -r -t "${DEBOUNCE_SECONDS}" \
            -e close_write -e moved_to -e moved_from -e create -e delete \
            "${WATCH_DIR}" >/tmp/inotify.line 2>/dev/null
        then
            # An event fired before timeout — note it and keep waiting.
            dirty=1
            log "event: $(cat /tmp/inotify.line)"
        else
            # Timeout (rc=2). If anything happened during the window, fire.
            if [ "${dirty}" -eq 1 ]; then
                fire_reindex
                dirty=0
            fi
        fi
    done
}

main "$@"
