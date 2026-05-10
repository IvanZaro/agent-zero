# Karpathy Autoresearch — A0 v1 Integration Design

**Status:** Design locked, awaiting implementation
**Date locked:** 2026-05-06
**Owner:** solo (Ivan)
**Plugin path:** `usr/plugins/autoresearch/`
**Doc origin:** `/brainstorming` session 2026-05-06; full transcript persisted via that session.

---

## 1. Understanding Summary

A new A0 plugin runs Karpathy-style overnight experiment loops to evolve A0 agent profile prompts against a frozen, scored eval suite. v1 specifically optimizes a new `agents/trader/` profile against a 20-task trading-domain eval suite; the same harness generalizes to any profile + suite.

**Why:** A0 has no closed-loop optimization mechanism. The loop turns "agent prompts" into editable weights and "task-suite pass-rate" into the loss function — small, dependable, A0-native.

**For:** solo dev. Plugin under `usr/plugins/` so it's not core-A0 maintenance burden.

**Hard constraints (from Karpathy's three primitives):**
- **Editable asset:** exactly one file per run — `agents/<profile>/prompts/<file>.md`, named in `program.md`.
- **Scalar metric:** pass-rate over a frozen 20-task trading suite (gate); LLM-judge composite (tie-breaker / observability only).
- **Time-boxed cycle:** ≤ 5 min wall-clock per experiment; 1 concurrent; overnight runs; **$5 hard cost cap** per run.

**Non-goals (v1, deferred to `todo.md`):**
- Editing global prompts (`prompts/`), tool defs (`tools/`), or RAG config.
- Online/continuous distillation à la GEPA / Hermes Skill Distiller.
- Optimizing `strategy.py` directly (trading-strategy autoresearch).
- Parallel concurrency.
- Pushing commits, merging to `main`, real-money execution.

---

## 2. Assumptions

1. `git` is available inside the A0 framework container; the plugin can invoke it from `/a0`.
2. Bind-mount of `usr/` is correct in both compose files for read/write to host.
3. LiteLLM config in `usr/.env` is reachable from a child Python subprocess inheriting the A0 framework env.
4. Claude Code is on local PATH (Ivan has it); on VPS, recent commit `d4be53c1` provides `claude-mcp` container — `docker exec claude-mcp claude -p …` is the path.
5. Trader agent profile **does not yet exist** — v1 creates `agents/trader/` with baseline prompts as part of the work.
6. The 20-task trading eval suite can be authored in ≈ 1 day from `moon-dev-trading-bots/` artifacts + Obsidian wiki Nunchi-trade reference.
7. Worker traps SIGTERM, runs `git reset --hard <baseline_sha>`, exits clean — no orphaned git state.
8. Judge model can be a cheaper LiteLLM model than the coder model (independently configurable).
9. Cost meter built on LiteLLM response usage tokens × per-model rates supplied in plugin settings (LiteLLM doesn't always emit dollar costs natively).
10. Pass-rate determinism (gate H2) requires `temperature=0` in eval-task and judge calls.

---

## 3. Decision Log

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | Domain = self-improvement (primary) trained on trading-domain eval (secondary) | trading-strategy direct, general-purpose loop | combines both efficiently |
| 2 | Editable scope = agent profile prompts only | global prompts, tool defs, RAG, multi-target | smallest blast radius; closest analog to Karpathy's `train.py` |
| 3 | Metric = hybrid (pass-rate gate + judge tie-breaker); suite = 20 trading tasks | pass-rate only, judge only, real session replays | unfakeable gate + qualitative observability |
| 4 | Hermes/GEPA = standalone v1 (no Hermes dependency) | trace-guided, two-loop, orchestrate Hermes | ship fast; layer in later (phase 2.5) |
| 5 | Driver = plugin + sidecar subprocess worker; swappable backend | external CLI, A0 agent, headless cron | A0-native; both environments cleanly |
| 6 | Default backend = LiteLLM | Claude Code subprocess | self-contained on VPS, cheaper to iterate; both supported via `backend:` field |
| 7 | Budget = ≤ 5 min / 1 concurrent / overnight / $5 hard cap | relaxed, parallel, manual | mirrors Karpathy; fastest signal to validate the loop |
| 8 | Safety = strict (single editable file, dedicated branch, never push/touch `main`, `git reset --hard` on crash; smoke test on; mock-only trades) | permissive, custom | scoring honesty + crash containment |
| 9 | Worker placement = subprocess from plugin (option C) | in-process inside A0 container, separate sidecar container | simplest; identical local + VPS; promote to container in phase 2 |
| 10 | Outcome bar = Layer 1 all 9 gates + Layer 2 = B (≥ 1 committed improvement, +5% pass-rate, reproducible 3×) | aggressive (≥15% delta), process-only | verifiable on a 20-task suite |
| 11 | Module layout = Approach 2 (loosely-coupled with explicit interfaces) | monolith, library + thin shell | maps 1:1 to H1–H9 gates; clean subagent slicing; not over-engineered |
| 12 | Implementation style = subagent-driven coding | direct coding by main agent | user feedback memory; allows parallel slicing |

---

## 4. Architecture

```
┌──────────────────── A0 process (existing, unchanged) ─────────────────────┐
│  Flask API + Alpine.js UI + AgentContext loop                              │
│  ┌── usr/plugins/autoresearch/ (NEW) ─────────────────────────────────┐   │
│  │  api/ /webui/ — start/stop/status/trace endpoints + Alpine panel   │   │
│  │  start → registers run, spawns subprocess, returns run_id          │   │
│  └────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼  (subprocess, same container, A0_ENV-aware)
┌──────────────── autoresearch worker (NEW) ────────────────────────────────┐
│  loop.py: read program.md → coder.propose_edit() → smoke → eval → judge   │
│           → git commit (improved) | git reset --hard (worse/broken)       │
│           → cost_meter.tick() → repeat until time/N/cost cap              │
│  All artifacts → usr/autoresearch/runs/<run-id>/<exp-N>/                  │
└────────────────────────────────────────────────────────────────────────────┘
```

**Key invariants:** process isolation, filesystem as cross-process truth, `main` never touched, cost capped per LiteLLM call (not per experiment).

---

## 5. program.md Schema

```yaml
---
profile: <agent-profile-name>           # required
prompt_file: <relative-path-to-md>      # required; relative to agents/<profile>/
backend: litellm                        # litellm | claude_code
coder_model: openrouter/...             # optional; backend-specific model string
eval_model: openrouter/...              # optional; falls back to AUTORESEARCH_EVAL_MODEL env, then `openrouter/openai/gpt-4o-mini`
judge_model: openrouter/...             # optional; skip judge if absent
eval_suite: tests/fixtures/suite.json   # path to eval suite JSON
max_experiments: 100
cost_cap_usd: "5.00"
parallelism: 4
---
```

**`eval_model` note:** Kept intentionally separate from `coder_model` — eval tasks should use a cheap, stable, reproducible model independent of the coder. Setting `eval_model` in program.md overrides the env var. The env var `AUTORESEARCH_EVAL_MODEL` overrides the hardcoded default.

---

## 5b. Module Layout

```
usr/plugins/autoresearch/
├── plugin.yaml
├── api/                    handlers; no logic
├── webui/                  Alpine panel + store
├── worker/
│   ├── __init__.py        `python -m … --run-id X` entry
│   ├── loop.py             orchestrator
│   ├── cost_meter.py       BudgetExceeded raises
│   └── kill_handler.py     SIGTERM → git reset --hard → exit
├── coder/
│   ├── base.py             Coder Protocol + EditPatch
│   ├── litellm_coder.py    default
│   └── claude_code_coder.py
├── harness/
│   ├── smoke.py            1-task load+respond
│   ├── eval_runner.py      20-task suite
│   └── judge.py            tie-breaker only
├── state/
│   ├── runs.py             RunState; atomic state.json
│   └── git_ops.py          branch / commit / reset / kill-recovery
└── tests/
    ├── unit/               coder, judge, git_ops, cost_meter
    ├── integration/        full lifecycle on fixture suite
    └── acceptance/         H1–H9 gates, named identically
```

---

## 6. Lifecycle (per run, per experiment)

See full data-flow diagrams in the brainstorm transcript. Summary:

1. UI Start → API validates program.md → creates branch `autoresearch/<run-id>` from `main` → spawns worker subprocess → returns run_id.
2. Worker, per experiment: coder proposes edit → apply patch → smoke check (gate H6) → run 20-task eval → judge tie-breaker → git commit (improved) or git reset --hard (worse/broken) → cost meter ticks → write artifacts.
3. Stop conditions: time elapsed, max_experiments, cost cap, manual stop, all-eval-tasks-crashed.

**Disk layout (single source of truth):**
```
usr/autoresearch/runs/<run-id>/
├── config.json   (immutable)
├── state.json    (mutable, atomic-rename writes)
├── program.md
├── baseline_eval.json
└── exp-001/, exp-002/ … {program.md, diff.patch, score.json, trace.log,
                           eval_result.json, judge.json, kept, sha?}
```

---

## 7. Error Handling (full table E1–E14 in brainstorm transcript)

Highlights:
- **E2** patch `old_text` not unique → mark patch_failed, advance.
- **E3** smoke fails → revert + advance (does NOT spend eval budget).
- **E5** all 20 eval tasks crash in a per-experiment eval → abort run (structural break).
- **E5b** baseline eval structurally broken (all tasks errored) → abort run before any experiments; preserves `baseline_eval.json` for diagnostics.
- **E7** cost cap hit → revert in-flight changes, status `cost_capped`, exit clean.
- **E8** SIGTERM → git reset --hard → status `stopped`.
- **E11** suite drift mid-run → abort (preserves H2 determinism).
- Two non-obvious traps closed: per-LiteLLM-call cost check (not per experiment); `baseline_sha` rotates on each kept commit (improvements stack, no rollback to zero).

---

## 8. Acceptance Gates (H1–H9)

| Gate | Assertion |
|---|---|
| H1 | Plugin discovered + UI panel rendered (local + VPS) |
| H2 | Eval determinism: re-run identical prompt → pass-rate identical, judge ±5% |
| H3 | 10-experiment dry run ≤ 50 min wall, ≤ $0.50 spend |
| H4 | Crash recovery: syntax-error edit → revert + advance |
| H5 | Branch isolation: post-run, `main` unchanged, no `git push` |
| H6 | Smoke gate: broken-YAML → eval NOT invoked, cost unchanged |
| H7 | Cost cap: mock $5.01 → loop exits within 1 experiment |
| H8 | Both backends: LiteLLM + Claude Code each complete ≥ 1 experiment |
| H9 | VPS parity: same 10-experiment dry run on alfredon, H2 still holds |

**Coverage target:** 80% on `coder/`, `harness/`, `state/`, `worker/`.

## 9. Outcome Verification (Layer 2 = B)

After all H gates green, run **one 100-experiment overnight job** on `agents/trader/` + 20-task suite. Pass criteria (all must hold):

1. ≥ 1 commit on `autoresearch/<run-id>`.
2. Re-run eval suite **3 separate times** on winning prompt and on baseline; median pass-rate delta ≥ +1 task (5%).
3. Improvement holds across all 3 re-runs.

Fail criteria → loop or eval is broken; revisit H2 determinism.

---

## 10. Deployment

| | Local (`compose.dev.yml`) | VPS alfredon (`deploy/compose.yml`) |
|---|---|---|
| Plugin path on host | `…\agent-zero\usr\plugins\autoresearch\` | `/opt/agent-zero/usr/plugins/autoresearch/` |
| Plugin path in container | `/a0/usr/plugins/autoresearch/` | same |
| LiteLLM creds | `usr/.env` | `usr/.env` |
| Claude Code | `claude` on PATH | `docker exec claude-mcp claude …` |
| Trigger | UI button or `claude` from repo | UI button only |
| `A0_ENV` | unset | set to `vps` (single backend-resolution flag) |

**Secrets posture:** worker subprocess launched with filtered env — deny-list excludes real exchange creds (`BIRDEYE_API_KEY`, `HL_*`, `BINANCE_*`, etc.). Eval tasks use mocked exchange clients only.

**VPS verification command:**
```bash
ssh alfredon
cd /opt/agent-zero
docker compose exec a0 python -m pytest usr/plugins/autoresearch/tests/acceptance -k 'h2 or h3 or h6 or h7'
```

---

## 11. Implementation Slicing (subagent assignments)

Per locked feedback "use subagents for coding". Slices ordered for parallel dispatch where possible (S1 + S6 first; S2/S3/S4/S5 in parallel after S1 lands; S7/S8/S9 sequentially after).

| # | Slice | Subagent type | Depends on | Acceptance |
|---|---|---|---|---|
| S1 | Plugin scaffolding (plugin.yaml, empty modules, `python -m … worker` entrypoint, contracts in `coder/base.py` and `state/runs.py`) | python-reviewer | — | imports cleanly; `pytest -q` runs 0 tests |
| S2 | `coder/litellm_coder.py` + unit tests | python-reviewer / tdd-guide | S1 | unit tests green; mock LiteLLM responses |
| S3 | `state/git_ops.py` + `worker/cost_meter.py` + unit tests | python-reviewer / tdd-guide | S1 | unit tests green; tmp-repo lifecycle proven |
| S4 | `harness/eval_runner.py` + `harness/smoke.py` + 2-task fixture suite | python-reviewer / tdd-guide | S1 | integration test: 1-experiment dry run on fixture |
| S5 | `worker/loop.py` + `kill_handler.py` (orchestrator wires S2/S3/S4) | architect → python-reviewer | S2, S3, S4 | E2E integration test |
| S6 | `agents/trader/` baseline profile + 20-task trading eval suite (real) | python-reviewer (+ research) | — | suite hash-stable; baseline pass-rate recorded |
| S7 | `coder/claude_code_coder.py` adapter + `A0_ENV` switch | python-reviewer | S5 | H8 gate green |
| S8 | `api/` handlers + `webui/` Alpine panel | frontend-design | S5 | H1 gate green; live trace tail works |
| S9 | All H1–H9 acceptance tests + VPS run | code-reviewer / e2e-runner | S5, S6, S7, S8 | all gates green local + VPS |
| S10 | Outcome verification: 100-exp overnight + 3× re-run protocol | (manual) | S9 | Layer 2 = B criteria met |

**Critical-path estimate:** ~1 week of focused work + the overnight outcome run.

---

## 12. References

- Karpathy autoresearch original: https://github.com/karpathy/autoresearch
- Wiki: `Desktop/VAULT/wiki/karpathy-autoresearch/` (3 pages)
- Trading adaptation reference: Nunchi-trade, ATLAS, self-updating bot
- Hermes/GEPA (deferred phase 2.5): https://github.com/NousResearch/hermes-agent-self-evolution
