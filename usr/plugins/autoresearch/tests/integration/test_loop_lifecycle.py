"""Full E2E lifecycle tests for worker/loop.py. TDD: written before implementation."""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.worker.loop import run_loop


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _current_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _setup_repo(tmp_path: Path, max_experiments: int = 1) -> tuple[Path, str]:
    """Create a minimal repo with agent profile + program.md."""
    agent_dir = tmp_path / "agents" / "trader" / "prompts"
    agent_dir.mkdir(parents=True)
    prompt_file = agent_dir / "system.md"
    prompt_file.write_text("You are a trading assistant.\nBuy when momentum is positive.\n")
    (tmp_path / "agents" / "trader" / "_context.yaml").write_text(
        "title: Trader\ndescription: test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [
            {"id": "t-001", "input": "momentum [0.02]?", "assert_substring": "buy"},
        ]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: {max_experiments}\ncost_cap_usd: '5.00'\n---\n"
        "Improve the trading assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


def _make_eval(passed: int = 1, total: int = 1, tokens: int = 50) -> EvalResult:
    return EvalResult(
        passed=passed,
        total=total,
        total_tokens=tokens,
        per_task=[
            TaskOutcome(task_id="t-001", passed=passed > 0, tokens=tokens, output="buy now", error=None)
        ],
    )


def _make_patch(tmp_path: Path, old_text: str, new_text: str) -> EditPatch:
    return EditPatch(
        target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
        old_text=old_text,
        new_text=new_text,
        rationale="test rationale",
    )


class MockCoder:
    name = "mock"

    def __init__(self, patch_factory) -> None:
        self._patch_factory = patch_factory
        self._cost = Decimal("0")

    async def propose_edit(self, program_md, file_text, recent_history):
        return self._patch_factory()

    def report_cost(self) -> Decimal:
        return self._cost


# ---------------------------------------------------------------------------
# test_full_lifecycle_kept_commit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_lifecycle_kept_commit(tmp_path: Path) -> None:
    """Improving edit → outcome='kept', sha set, baseline rotates, status='completed'."""
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=1)

    call_count = 0

    def make_patch():
        return _make_patch(
            tmp_path,
            "Buy when momentum is positive.",
            "Buy when momentum is consistently positive and accelerating.",
        )

    improving_eval = _make_eval(passed=1, total=1, tokens=50)
    # baseline eval also returns passed=0 to ensure improvement is detected
    baseline_eval = _make_eval(passed=0, total=1, tokens=50)

    eval_results = iter([baseline_eval, improving_eval])

    async def mock_run_eval(*args, **kwargs):
        return next(eval_results)

    coder = MockCoder(make_patch)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="lc-kept-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    assert state.status == "completed"
    assert len(state.experiments) == 1
    exp = state.experiments[0]
    assert exp.outcome == "kept"
    assert exp.sha is not None
    # baseline sha rotated — branch HEAD != original baseline_sha
    branch_sha = _current_sha(tmp_path)
    assert branch_sha != baseline_sha
    # Artifacts written
    run_dir = tmp_path / "usr" / "autoresearch" / "runs" / "lc-kept-001"
    assert (run_dir / "exp-001" / "outcome.json").exists()
    outcome_data = json.loads((run_dir / "exp-001" / "outcome.json").read_text())
    assert outcome_data["outcome"] == "kept"
    assert outcome_data["sha"] is not None


# ---------------------------------------------------------------------------
# test_full_lifecycle_reverted_when_no_improvement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_lifecycle_reverted_when_no_improvement(tmp_path: Path) -> None:
    """Non-improving edit → outcome='reverted', sha=None, branch HEAD == baseline_sha."""
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=1)

    def make_patch():
        return _make_patch(
            tmp_path,
            "Buy when momentum is positive.",
            "Consider buying when momentum might be positive.",
        )

    baseline_eval = _make_eval(passed=1, total=1, tokens=50)
    worse_eval = _make_eval(passed=0, total=1, tokens=50)
    eval_results = iter([baseline_eval, worse_eval])

    async def mock_run_eval(*args, **kwargs):
        return next(eval_results)

    coder = MockCoder(make_patch)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="lc-rev-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    assert state.status == "completed"
    assert len(state.experiments) == 1
    exp = state.experiments[0]
    assert exp.outcome == "reverted"
    assert exp.sha is None
    # Branch HEAD == baseline_sha (revert worked)
    assert _current_sha(tmp_path) == baseline_sha


# ---------------------------------------------------------------------------
# test_baseline_eval_runs_at_start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_baseline_eval_runs_at_start(tmp_path: Path) -> None:
    """run_eval called once at start (baseline) + once per experiment.

    Both experiments use the same old_text so the patch always applies
    after any revert back to the original file.
    """
    program_md, _ = _setup_repo(tmp_path, max_experiments=2)

    call_count = 0

    async def counting_eval(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _make_eval(passed=0, total=1, tokens=50)

    class RepeatCoder:
        """Returns the same patch every call — old_text always present after revert."""
        name = "mock"

        async def propose_edit(self, program_md: str, file_text: str, recent_history: list):
            return EditPatch(
                target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                old_text="Buy when momentum is positive.",
                new_text="Buy when momentum is strongly positive.",
                rationale="repeat patch",
            )

        def report_cost(self) -> Decimal:
            return Decimal("0")

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=counting_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            state = await run_loop(
                run_id="lc-baseline-001",
                program_md_path=program_md,
                max_experiments_override=2,
                coder=RepeatCoder(),
                cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
            )

    # 1 baseline + 2 experiment evals = 3 total
    assert call_count == 3, f"Expected 3 eval calls (1 baseline + 2 experiments), got {call_count}"
    assert state.status == "completed"


# ---------------------------------------------------------------------------
# test_baseline_sha_rotates_on_kept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_baseline_sha_rotates_on_kept(tmp_path: Path) -> None:
    """3 kept commits in a row; baseline_sha changes after each."""
    program_md, baseline_sha = _setup_repo(tmp_path, max_experiments=3)

    # Evals: baseline=0, exp1=1 (kept), exp2=2 (kept), exp3=3 (kept but max 1 task)
    evals = iter([
        _make_eval(passed=0, total=1),  # baseline
        _make_eval(passed=1, total=1),  # exp1 → kept
        _make_eval(passed=1, total=1),  # exp2 — same as rotated baseline (1), so reverted
        _make_eval(passed=1, total=1),  # exp3 — same again
    ])

    async def seq_eval(*args, **kwargs):
        return next(evals)

    original_text = "Buy when momentum is positive."
    texts = [
        ("Buy when momentum is positive.", "Aggressively buy on positive momentum."),
        ("Aggressively buy on positive momentum.", "Strongly buy on positive momentum."),
        ("Strongly buy on positive momentum.", "Always buy on positive momentum."),
    ]
    texts_iter = iter(texts)

    class RotatingCoder:
        name = "mock"
        async def propose_edit(self, program_md, file_text, recent_history):
            old, new = next(texts_iter)
            return EditPatch(
                target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                old_text=old,
                new_text=new,
                rationale="rotating",
            )
        def report_cost(self):
            return Decimal("0")

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=seq_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="lc-rotate-001",
                program_md_path=program_md,
                max_experiments_override=3,
                coder=RotatingCoder(),
                cost_meter=meter,
            )

    assert state.status == "completed"
    # First experiment should be kept (improved from 0→1)
    assert state.experiments[0].outcome == "kept"
    # Rotated baseline_sha must differ from original
    kept_sha = state.experiments[0].sha
    assert kept_sha is not None
    assert kept_sha != baseline_sha


# ---------------------------------------------------------------------------
# test_artifacts_written_per_experiment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_artifacts_written_per_experiment(tmp_path: Path) -> None:
    """outcome.json, score.json, trace.log written for every experiment."""
    program_md, _ = _setup_repo(tmp_path, max_experiments=1)

    def make_patch():
        return _make_patch(
            tmp_path,
            "Buy when momentum is positive.",
            "Buy on confirmed uptrend.",
        )

    eval_results = iter([
        _make_eval(passed=0),  # baseline
        _make_eval(passed=0),  # exp1
    ])

    async def mock_run_eval(*args, **kwargs):
        return next(eval_results)

    coder = MockCoder(make_patch)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="lc-arts-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    exp_dir = tmp_path / "usr" / "autoresearch" / "runs" / "lc-arts-001" / "exp-001"
    assert (exp_dir / "outcome.json").exists()
    assert (exp_dir / "score.json").exists()
    assert (exp_dir / "trace.log").exists()
    assert (exp_dir / "program.md").exists()

    outcome = json.loads((exp_dir / "outcome.json").read_text())
    assert outcome["n"] == 1
    assert "started_at" in outcome
    assert "finished_at" in outcome
