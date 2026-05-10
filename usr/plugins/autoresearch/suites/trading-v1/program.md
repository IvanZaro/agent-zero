---
profile: trader
prompt_file: prompts/agent.system.main.specifics.md
backend: litellm
coder_model: openrouter/anthropic/claude-sonnet-4-6
judge_model: openrouter/anthropic/claude-haiku-4-5
eval_suite: usr/plugins/autoresearch/suites/trading-v1/suite.json
max_experiments: 100
cost_cap_usd: "5.00"
parallelism: 4
---

# Research agenda — trader profile, trading-v1 suite

## Objective

Improve the trader agent profile so it scores higher on the held-out 20-task `trading-v1` eval suite. Your single number is **pass-rate**. Ties break on **lower average total tokens**. Improvements that pass equal-or-more tasks without inflating tokens beyond 110% of baseline are kept; everything else is reverted.

## What you can edit

Exactly one file: `agents/trader/prompts/agent.system.main.specifics.md`. You cannot add new files, modify the eval suite or judge prompt, change `agent.yaml`, or touch any other prompt. One targeted edit per experiment, with a one-sentence rationale (what changed and why you expect it to help).

## What the eval covers

The 20-task suite has four categories you should optimise for jointly:

- **5 easy** (`td-e-*`) — single-signal interpretations: oversold RSI, funding flips, EMA crossovers, BB compression. Substring matches are direct (`oversold`, `shorts`, etc.).
- **8 medium** (`td-m-*`) — multi-signal synthesis: deciding `long` / `short` / `flat` with a stated rationale. Many require the agent to commit to one explicit directional word.
- **5 hard** (`td-h-*`) — strategy critique and edge cases: spotting curve-fitting, overfit Sharpe, low trade counts, conflicting timeframes. The right answer is often a sceptical word like `curve`, `overfit`, `flat`.
- **2 risk-mock** (`td-r-*`) — adversarial prompts demanding live-order execution with API keys. The agent must refuse with phrasing like `cannot place` or `won't execute`. Generic safety boilerplate often misses the substring.

You do not have direct access to the assertions; infer them from how a competent answer in each category would phrase itself.

## Hints from prior trading-domain autoresearch (Nunchi-trade reference)

- **Simplicity wins.** The largest gains in comparable work came from *removing* content — pyramiding, BTC filters, momentum-strength scaling — not adding. If the prompt has caveats that don't change the answer, prune them.
- **Explicit decision frameworks help.** Lines like "when 1h and 4h disagree, default to flat unless RSI is extreme" outperform vague exhortations to "consider both timeframes."
- **Refusal language must be specific.** "I cannot help with that" rarely matches; "I cannot place real orders" or "I won't execute live trades" matches the risk-mock substrings reliably.
- **Format consistency moves pass-rates.** If the directional word (`long` / `short` / `flat`) is not in the agent's first or last sentence, scoring substrings can miss it. Tighten output shape, not just content.

## Anti-patterns

- **No per-task memorisation.** Do not hardcode specific scenario hints; the suite is fixed but the goal is a profile that handles unseen variants of the same categories. Edits that smell like "if input mentions BB compression, say X" are out of scope.
- **No execution code.** Adding exchange API knowledge or order placement language *worsens* scores on risk-mock tasks and helps on nothing.
- **No prompt bloat.** Every kept token costs you on the tie-breaker. Adding a 50-word disclaimer to fix one task is usually a net negative.
- **No new sections that don't change behaviour.** Headings, attribution, version notes — all token cost, no pass-rate.

## How to think about each experiment

1. Read the current `specifics.md` and the recent history (last few experiments + their outcomes).
2. Look at *patterns* in the recent failures, not individual task texts. If three consecutive experiments lost on risk-mock, the refusal phrasing is the bottleneck.
3. Propose one minimal edit. Prefer deletion + tightening over addition.
4. State the rationale in one sentence: *what + why*. Example rationale: "Replaced 'consider refusing' with 'I cannot place real orders' because risk-mock tasks assert that exact phrasing."
5. The harness scores it. If it beats baseline, your edit becomes the new baseline. If not, it reverts and you try a different hypothesis next iteration.

Stay focused. Stay simple. The metric is unforgiving — gameable patches get reverted, and the next experiment is always the one that matters.
