# Autoresearch v1 Acceptance Gates (H1–H9)

## Purpose

These tests are the v1 sign-off acceptance suite per design doc §8. Each file maps
1:1 to an H-gate invariant. All H1–H8 gates must be green locally before shipping.
H9 is VPS-only and runs against the production alfredon container.

| Gate | File | Invariant |
|------|------|-----------|
| H1 | `test_h1_plugin_discovery.py` | plugin.yaml declares autoresearch; panel.html has Alpine bindings |
| H2 | `test_h2_eval_determinism.py` | identical prompt → identical pass count and token total |
| H3 | `test_h3_dry_run_budget.py` | 10-exp mocked run in <60s; spend ≤ $0.50 |
| H4 | `test_h4_crash_recovery.py` | malformed-JSON coder → coder_failed → branch HEAD unchanged |
| H5 | `test_h5_branch_isolation.py` | no git push/remote/fetch in source; main unchanged after run |
| H6 | `test_h6_smoke_gate.py` | broken-YAML patch → smoke_failed; eval NOT called; branch reverted |
| H7 | `test_h7_cost_cap.py` | mock $5.01 spend → cost_capped in ≤1 experiment; branch reverted |
| H8 | `test_h8_both_backends.py` | litellm backend dispatches LiteLLMCoder; claude_code dispatches subprocess |
| H9 | `test_h9_vps_parity.py` | H2+H3+H6+H7 all pass on alfredon VPS |

## How to run locally (H1–H8)

```bash
pytest usr/plugins/autoresearch/tests/acceptance/ -v --tb=short
```

H9 is automatically skipped unless `AUTORESEARCH_VPS_TEST=1` is set.

## How to run H9 on VPS

**Option 1 — shell script (recommended):**
```bash
bash usr/plugins/autoresearch/scripts/run_h9_on_vps.sh
```

**Option 2 — pytest directly:**
```bash
AUTORESEARCH_VPS_TEST=1 pytest usr/plugins/autoresearch/tests/acceptance/test_h9_vps_parity.py -v
```

Prerequisites for H9:
- SSH alias `alfredon` configured (see `memory/vps_alfredon.md`)
- VPS docker compose stack up with `a0` container healthy
- autoresearch plugin on VPS (merge to main and pull on VPS first)

## Pass criteria

- **Local**: H1–H8 all green, H9 skipped — run complete
- **VPS**: H9 green (H2+H3+H6+H7 pass inside alfredon's a0 container)

## What is NOT in this suite

Layer 2 = B outcome verification (the 100-experiment overnight run with
3× re-run protocol) is S10 — a manual process documented separately. These
acceptance tests cover the infrastructure invariants, not trading improvement metrics.
