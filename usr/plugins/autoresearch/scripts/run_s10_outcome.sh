#!/usr/bin/env bash
# Run the S10 outcome verification (Layer 2 = B) on alfredon.
#
# What it does:
#   1. Pre-flight: SSH reachable, agent-zero container up, pytest available,
#      <run-id> doesn't already exist, baseline eval is workable.
#   2. Launches the worker inside a detached tmux session on alfredon.
#   3. Prints monitoring + stop commands.
#
# Why pre-flight matters:
#   The full outcome run is 100 experiments / ~6-8h / ~$5 budget. If
#   credentials are misrouted or a model is unavailable, you'd discover it
#   the morning after wasting a night. The pre-flight catches that in <60s.
#
# Usage:
#   bash usr/plugins/autoresearch/scripts/run_s10_outcome.sh [run-id]
#   (default run-id: outcome-001)
#
# Requirements:
#   - SSH alias "alfredon" configured
#   - tmux + jq available on alfredon host (verified at start)
#   - agent-zero container up; deploy at HEAD of main

set -euo pipefail

RUN_ID="${1:-outcome-001}"
PROGRAM_MD="usr/plugins/autoresearch/suites/trading-v1/program.md"
TMUX_SESSION="autoresearch-${RUN_ID}"
PREFLIGHT_ID="s10-preflight-$(date +%s)"
RUNS_BASE="/opt/agent-zero/usr/autoresearch/runs"

ssh_exec() { ssh alfredon "$@"; }
in_container() {
  ssh alfredon "cd /opt/agent-zero && docker compose exec -T -w /a0 agent-zero $1"
}

echo "==> 1/5 SSH reachability..."
ssh_exec true

echo "==> 2/5 agent-zero container up?"
ssh_exec "cd /opt/agent-zero && docker compose ps agent-zero --format json | jq -e '.State == \"running\"' >/dev/null" \
  || { echo "ERROR: agent-zero container not running"; exit 1; }

echo "==> 3/5 host has tmux + jq..."
ssh_exec "command -v tmux >/dev/null" || { echo "ERROR: tmux missing on alfredon"; exit 1; }
ssh_exec "command -v jq >/dev/null"   || { echo "ERROR: jq missing on alfredon"; exit 1; }

echo "==> 4/5 ${RUN_ID} doesn't already exist (no clobber)..."
if ssh_exec "test -d ${RUNS_BASE}/${RUN_ID}"; then
  echo "ERROR: run ${RUN_ID} already exists at ${RUNS_BASE}/${RUN_ID}"
  echo "       Pick a different run-id or delete on VPS:"
  echo "         ssh alfredon 'sudo rm -rf ${RUNS_BASE}/${RUN_ID}'"
  exit 1
fi

echo "==> 5/5 baseline sanity (1 experiment, \$0.30 cap, run-id ${PREFLIGHT_ID})..."
in_container "/opt/venv-a0/bin/pip install --quiet pytest pytest-asyncio pytest-mock" || true
in_container "/opt/venv-a0/bin/python -m usr.plugins.autoresearch.worker \
  --run-id ${PREFLIGHT_ID} \
  --program-md ${PROGRAM_MD} \
  --max-experiments 1 \
  --cost-cap-usd 0.30" 2>&1 | tail -3 || true

preflight_status=$(ssh_exec "cat ${RUNS_BASE}/${PREFLIGHT_ID}/state.json | jq -r .status" 2>/dev/null || echo "missing")
preflight_passed=$(ssh_exec "cat ${RUNS_BASE}/${PREFLIGHT_ID}/baseline_eval.json | jq -r .passed" 2>/dev/null || echo "0")
preflight_total=$(ssh_exec "cat ${RUNS_BASE}/${PREFLIGHT_ID}/baseline_eval.json | jq -r .total" 2>/dev/null || echo "0")

echo "    preflight status=${preflight_status} baseline=${preflight_passed}/${preflight_total}"

if [ "${preflight_status}" = "aborted" ] || [ "${preflight_passed}" = "0" ]; then
  first_err=$(ssh_exec "cat ${RUNS_BASE}/${PREFLIGHT_ID}/baseline_eval.json | jq -r '.per_task[0].error // \"no-error\"'" 2>/dev/null || echo "unknown")
  echo "ERROR: baseline eval not workable. Outcome run would be meaningless."
  echo "       First task error: ${first_err}"
  echo "       Inspect: ssh alfredon 'cat ${RUNS_BASE}/${PREFLIGHT_ID}/baseline_eval.json | jq | head -40'"
  echo "       (Preflight artifacts left in place at ${RUNS_BASE}/${PREFLIGHT_ID}/.)"
  exit 2
fi

echo "==> Pre-flight passed. Launching ${RUN_ID} in tmux session '${TMUX_SESSION}'..."

# Launch detached. The worker writes state.json continuously; tmux just
# keeps the foreground process alive after we exit the SSH session.
ssh_exec "tmux new-session -d -s '${TMUX_SESSION}' \"cd /opt/agent-zero && docker compose exec -T -w /a0 agent-zero /opt/venv-a0/bin/python -m usr.plugins.autoresearch.worker --run-id ${RUN_ID} --program-md ${PROGRAM_MD} 2>&1 | tee ${RUNS_BASE}/${RUN_ID}.runner.log\""

cat <<EOF

==> ${RUN_ID} launched on alfredon.

Monitor live (interactive — Ctrl-B then D to detach without killing):
  ssh alfredon -t tmux attach -t '${TMUX_SESSION}'

Status snapshot at any time:
  ssh alfredon "jq '{status, experiments: (.experiments|length), spend_usd, baseline_sha}' ${RUNS_BASE}/${RUN_ID}/state.json"

Tail runner log:
  ssh alfredon "tail -f ${RUNS_BASE}/${RUN_ID}.runner.log"

Stop cleanly (SIGTERM → kill_recovery → revert to baseline_sha → exit):
  ssh alfredon "docker compose -f /opt/agent-zero/compose.yml exec -T agent-zero pkill -TERM -f 'autoresearch.worker.*${RUN_ID}'"

When the run terminates, inspect outcome:
  ssh alfredon "cd /opt/agent-zero && git log --oneline main..autoresearch/${RUN_ID}"
EOF
