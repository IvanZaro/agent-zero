#!/usr/bin/env bash
# Run H9 manually on alfredon.
#
# Requires:
#   - SSH alias "alfredon" configured (see memory/vps_alfredon.md)
#   - VPS docker compose stack up with a0 container healthy
#   - autoresearch plugin deployed on VPS (code on main branch)
#
# Usage:
#   bash scripts/run_h9_on_vps.sh
#
# The script does NOT deploy code — it only runs tests on whatever is
# already on the VPS. Merge to main and pull on VPS first.

set -euo pipefail

echo "==> running H2/H3/H6/H7 on alfredon..."
ssh alfredon "cd /opt/agent-zero && docker compose exec -T a0 /opt/venv-a0/bin/python -m pytest usr/plugins/autoresearch/tests/acceptance -k 'h2 or h3 or h6 or h7' --tb=short -v"
echo "==> H9 PASSED (4/4 gates green on VPS)"
