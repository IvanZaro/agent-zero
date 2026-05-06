#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# backup.sh — nightly backup of /opt/agent-zero/usr to local + (optional) R2.
# See plan §10.
#
# Add to root crontab on VPS:
#   30 3 * * * cd /opt/agent-zero && ./deploy/scripts/backup.sh >> backups/backup.log 2>&1
#
# After `git pull` on the VPS, mark this script executable:
#   chmod +x deploy/scripts/backup.sh
#
# TODO(ivan, verify): plan §10 prefers calling A0's loopback backup endpoint
#   over a host-side tar. The relevant endpoint is api/backup_create.py, which
#   in stock A0 has `requires_auth=True` and `requires_loopback=False`, so
#   triggering it from the host needs basic-auth credentials, not just curl
#   from 127.0.0.1. For v1 we use the simpler host-tar approach below — it
#   captures everything in usr/ (chats, settings, FAISS index, .env) without
#   needing to pass auth headers. Switch to API-driven backups once the
#   endpoint signature is verified and you decide whether you want a
#   loopback-only variant.
# ----------------------------------------------------------------------------
set -euo pipefail

BACKUP_DIR="/opt/agent-zero/backups"
SOURCE_DIR="/opt/agent-zero/usr"
RETAIN_DAYS=14
DATE="$(date +%F-%H%M)"
ARCHIVE="${BACKUP_DIR}/usr-${DATE}.tar.gz"
LOG_FILE="${BACKUP_DIR}/backup.log"

# rclone remote target (configure once with `rclone config` on VPS; pick
# Cloudflare R2 per plan §10). Skipped silently if rclone isn't installed.
RCLONE_REMOTE="r2:agent-zero-backups"

log() {
    local msg="[backup $(date -u +%FT%TZ)] $*"
    echo "${msg}"
    echo "${msg}" >> "${LOG_FILE}"
}

mkdir -p "${BACKUP_DIR}"
touch "${LOG_FILE}"

if [ ! -d "${SOURCE_DIR}" ]; then
    log "FATAL: ${SOURCE_DIR} not found; aborting."
    exit 1
fi

log "creating ${ARCHIVE}"
tar czf "${ARCHIVE}" -C /opt/agent-zero usr
log "wrote $(du -h "${ARCHIVE}" | cut -f1) to ${ARCHIVE}"

# Optional: push off-VPS to Cloudflare R2 if rclone is configured.
# One-time VPS setup (per plan §10):
#   rclone config       # add an `r2` remote with R2 token + bucket
#   rclone mkdir r2:agent-zero-backups
if command -v rclone >/dev/null 2>&1; then
    log "rclone present; pushing to ${RCLONE_REMOTE}"
    if rclone copy "${ARCHIVE}" "${RCLONE_REMOTE}/" >> "${LOG_FILE}" 2>&1; then
        log "rclone push OK"
    else
        log "rclone push FAILED (rc=$?); local copy retained"
    fi
else
    log "rclone not installed; skipping off-VPS push"
fi

# Retention: drop archives older than RETAIN_DAYS days (local only — R2
# lifecycle rules can mirror this server-side if desired).
log "pruning local archives older than ${RETAIN_DAYS} days"
find "${BACKUP_DIR}" -maxdepth 1 -type f -name "usr-*.tar.gz" -mtime "+${RETAIN_DAYS}" -print -delete \
    | while read -r f; do log "pruned ${f}"; done

log "done"
