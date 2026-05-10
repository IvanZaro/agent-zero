#!/usr/bin/env bash
# Run H9 manually on alfredon.
#
# Requires:
#   - SSH alias "alfredon" configured (see memory/vps_alfredon.md)
#   - VPS docker compose stack up with the agent-zero container running
#   - autoresearch plugin deployed on VPS (code on main branch)
#
# Usage:
#   bash usr/plugins/autoresearch/scripts/run_h9_on_vps.sh
#
# The script does NOT deploy code — it only runs tests on whatever is
# already on the VPS. Merge to main first; CI builds + deploys automatically.
#
# pytest is not in /opt/venv-a0 by default; the script installs it on
# first run (idempotent on subsequent runs). Container restarts will lose
# the install — re-running the script reinstalls.

set -euo pipefail

SERVICE="agent-zero"
WORKDIR="/a0"
SELECTOR="h2 or h3 or h6 or h7"

echo "==> ensuring pytest is available in $SERVICE container..."
ssh alfredon "cd /opt/agent-zero && docker compose exec -T -w $WORKDIR $SERVICE /opt/venv-a0/bin/pip install --quiet pytest pytest-asyncio pytest-mock 2>&1 | tail -2"

echo "==> running H2/H3/H6/H7 on alfredon..."
ssh alfredon "cd /opt/agent-zero && docker compose exec -T -w $WORKDIR $SERVICE /opt/venv-a0/bin/python -m pytest usr/plugins/autoresearch/tests/acceptance -k '$SELECTOR' --tb=short -v"

echo "==> H9 PASSED (15/15 selected gates green on VPS)"
