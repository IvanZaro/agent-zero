# A0 — Roadmap

## karpathy-autoresearch v1 — implementation slices (subagent-dispatched)

Design doc: `docs/developer/autoresearch-v1.md`. Slices in dispatch order:

- [ ] **S1** Plugin scaffolding + interface contracts (`coder/base.py`, `state/runs.py`)
- [ ] **S2** `coder/litellm_coder.py` + unit tests (parallel after S1)
- [ ] **S3** `state/git_ops.py` + `worker/cost_meter.py` + unit tests (parallel after S1)
- [ ] **S4** `harness/eval_runner.py` + `harness/smoke.py` + 2-task fixture suite (parallel after S1)
- [ ] **S5** `worker/loop.py` + `kill_handler.py` (after S2/S3/S4)
- [ ] **S6** `agents/trader/` baseline profile + real 20-task trading eval suite (parallel with S1–S5)
- [ ] **S7** `coder/claude_code_coder.py` + `A0_ENV` switch (after S5)
- [ ] **S8** `api/` handlers + `webui/` Alpine panel (after S5)
- [ ] **S9** H1–H9 acceptance tests + VPS run (after S5/S6/S7/S8)
- [ ] **S10** Outcome verification: 100-experiment overnight on trader profile + 3× re-run

## karpathy-autoresearch — deferred scopes (post v1)

V1 ships with editable scope **A: agent profile prompts only** (`agents/<profile>/prompts/*.md`, `agents/<profile>/_context.yaml`).

Add these scopes once the v1 loop, harness, and metric are validated:

- **B. Global prompt templates** — `prompts/` system + message templates.
  - Broader effect (touches every agent).
  - Pre-req: per-template impact tracking so a regression in one agent reverts the change.

- **C. Tool definitions** — `tools/*.py` descriptions, schemas, optionally logic.
  - Highest leverage / biggest blast radius.
  - Pre-req: sandbox + mandatory smoke test that imports + invokes each tool with a synthetic payload before scoring is allowed.
  - Pre-req: `git reset --hard` path on import failure, segfault, or timeout.

- **D. RAG / knowledge retrieval config** — chunking, top-k, reranker, embedding model.
  - Narrow surface; clean scoring (retrieval recall@k on a labelled set).
  - Pre-req: labelled retrieval eval set checked into `tests/eval/retrieval/`.

## Phase 2 — sidecar worker container

Once v1 (in-plugin subprocess worker) is stable, promote the worker to its own compose service:
- New service `autoresearch-worker` in `compose.dev.yml` and `deploy/compose.yml`.
- Own restart policy, own logs, own resource limits (don't share CPU/RAM with A0).
- A0 plugin talks to it over HTTP on the internal Docker network.
- Shared volume mount for `usr/autoresearch/runs/<run-id>/` and the repo working tree.
- Pre-req for parallel concurrency (plan option C from brainstorm: 2–3 concurrent experiments via worktrees).

## Phase 2.5 — two-loop self-improvement (Hermes/GEPA-aligned)

Once standalone autoresearch is stable, layer an online loop on top:
- **Online arm (GEPA-lite + skill distiller for A0):**
  - Reads real A0 execution traces (existing logs).
  - Distills successful, non-trivial workflows into A0 skills (Markdown).
  - Genetic-Pareto search over prompt/skill candidates (multi-objective: success, tokens, latency).
- **Offline arm = the autoresearch loop already shipped in v1.**
- Bridge: candidates emitted by the online loop become editable hypotheses the offline loop validates against the held-out eval before promotion.
- Reference impls: `NousResearch/hermes-agent-self-evolution` (DSPy + GEPA), Hindsight memory (`vectorize-io/hindsight`).
- Decision deferred: build A0-native vs. orchestrate Hermes Agent from A0 (option D from brainstorm).

## Phase 3 — trading-strategy autoresearch (option B)

Once v1 (self-improvement) is stable, fork the same harness for `moon-dev-trading-bots/`:
- editable asset: `strategy.py`
- metric: Sharpe-based score (Nunchi formula) — see vault `wiki/karpathy-autoresearch/autoresearch-trading-applications.md`.
- locked: `backtest.py`, data prep, fee assumptions.
