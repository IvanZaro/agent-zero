# Agent-Zero VPS Bootstrap

First-time setup for the Hostinger Ubuntu 22.04 VPS that hosts the production
Agent-Zero stack (Caddy + A0 + Syncthing + reindex_watcher). See the full plan
at [agent-zero-full-stack-drifting-frog.md](../../.claude/plans/agent-zero-full-stack-drifting-frog.md).

## Prereqs

- DNS A record `a0.alfredon.cloud` → VPS public IP (TTL 300 is fine).
- Hostinger Ubuntu 22.04 droplet, root SSH access.
- A GitHub Personal Access Token with `read:packages` scope (to pull from GHCR).
- Tailscale account (free tier is enough).

## Step 1 — Install Docker + Compose

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

Verify: `docker compose version`.

## Step 2 — Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
```

Add the Win11 dev box to the same tailnet. Note the VPS's tailnet IP for the Syncthing peer config.

## Step 3 — Clone the fork to `/opt/agent-zero`

```bash
sudo git clone https://github.com/IvanZaro/agent-zero.git /opt/agent-zero
cd /opt/agent-zero
```

## Step 4 — Create `.env`

```bash
cp deploy/.env.example .env
nano .env   # fill in AUTH_PASSWORD (openssl rand -base64 24), API keys, TELEGRAM_BOT_TOKEN
```

## Step 5 — Authenticate to GHCR

```bash
docker login ghcr.io -u <gh-user>   # paste the read:packages PAT
```

## Step 6 — First boot

```bash
chmod +x deploy/scripts/reindex_watcher.sh deploy/scripts/backup.sh
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml ps
```

Replace `alfredon.cloud` in [deploy/caddy/Caddyfile](caddy/Caddyfile) before bringing Caddy up, and (per the file's header) decide whether to keep the `rate_limit` block (custom xcaddy build) or strip it.

## Step 7 — First-time Web UI config

Browse to `https://a0.alfredon.cloud` and log in.

- **Settings → Telegram tab**: set `mode=webhook`, `webhook_url=https://a0.alfredon.cloud`, generate a 32-char `webhook_secret`, fill `allowed_users`. See plan §8.
- **Settings → MCP/A2A tab**: paste the `claude-code` MCP server config. See plan §7.

## Step 8 — Cron the backup

```bash
(crontab -l 2>/dev/null; echo '30 3 * * * cd /opt/agent-zero && ./deploy/scripts/backup.sh') | crontab -
```

(Optionally configure the off-VPS R2 push: `rclone config` → add an `r2` remote → `rclone mkdir r2:agent-zero-backups`.)

## Verification

Walk the plan §12 checklist top-to-bottom — git push triggers GHA, `/health` returns 200 with valid LE chain, vault edit on Win11 reaches VPS in <60s, reindex watcher fires, Telegram webhook validates, MCP `claude-code.*` tools appear, reboot survives, rollback drill works, backup round-trips, lockout/rate-limit kicks in at 5 attempts.
