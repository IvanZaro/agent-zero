from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.worker.loop import run_loop

if TYPE_CHECKING:
    from usr.plugins.autoresearch.state.runs import ExperimentRecord

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _current_sha(repo: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _setup_fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    prompt_file = agent_dir / "system.md"
    prompt_file.write_text(
        "You are a trading assistant.\nWhen momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps({
            "version": 1,
            "tasks": [{"id": "fx-001", "input": "momentum [0.02, 0.03, 0.05]?", "assert_substring": "long"}]
        })
    )

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 1\ncost_cap_usd: '5.00'\n---\n"
        "Improve the trading assistant momentum interpretation.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


def _make_mock_eval_result(passed: int = 1, total: int = 1):
    from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
    return EvalResult(
        passed=passed,
        total=total,
        total_tokens=50,
        per_task=[TaskOutcome(task_id="fx-001", passed=passed > 0, tokens=50, output="go long", error=None)],
    )


@pytest.mark.asyncio
async def test_one_experiment_kept(tmp_path: Path) -> None:
    program_md, baseline_sha = _setup_fixture_repo(tmp_path)

    def mock_coder_propose(*args, **kwargs):
        return EditPatch(
            target_path=tmp_path / "agents" / "fixture_trader" / "prompts" / "system.md",
            old_text="When momentum is positive, go long.",
            new_text="When momentum is consistently positive and rising, go long with high confidence.",
            rationale="Adding confidence level",
        )

    class MockCoder:
        name = "mock"

        async def propose_edit(self, program_md, file_text, recent_history):
            return mock_coder_propose()

        def report_cost(self):
            return Decimal("0")

    improved_eval = _make_mock_eval_result(passed=1, total=1)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=AsyncMock(return_value=improved_eval)):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="integ-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=MockCoder(),
                cost_meter=meter,
            )

    assert len(state.experiments) == 1
    exp = state.experiments[0]
    assert exp.outcome in ("kept", "reverted")
    assert state.status == "completed"

    run_dir = tmp_path / "usr" / "autoresearch" / "runs" / "integ-001"
    assert (run_dir / "state.json").exists()
    assert (run_dir / "config.json").exists()
    assert (run_dir / "exp-001" / "outcome.json").exists()
    assert (run_dir / "exp-001" / "trace.log").exists()
    assert (run_dir / "exp-001" / "score.json").exists()

    state_data = json.loads((run_dir / "state.json").read_text())
    assert state_data["run_id"] == "integ-001"
    assert state_data["status"] == "completed"


@pytest.mark.asyncio
async def test_one_experiment_reverted_when_no_improvement(tmp_path: Path) -> None:
    program_md, baseline_sha = _setup_fixture_repo(tmp_path)

    class MockCoder:
        name = "mock"

        async def propose_edit(self, program_md, file_text, recent_history):
            return EditPatch(
                target_path=tmp_path / "agents" / "fixture_trader" / "prompts" / "system.md",
                old_text="When momentum is positive, go long.",
                new_text="When momentum is positive, consider going long.",
                rationale="Weakening the signal",
            )

        def report_cost(self):
            return Decimal("0")

    failed_eval = _make_mock_eval_result(passed=0, total=1)

    with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=AsyncMock(return_value=failed_eval)):
        with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            from usr.plugins.autoresearch.worker.cost_meter import CostMeter
            meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
            state = await run_loop(
                run_id="integ-002",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=MockCoder(),
                cost_meter=meter,
            )

    assert state.experiments[0].outcome == "reverted"
    assert state.experiments[0].sha is None
    main_sha = _current_sha(tmp_path)
    # branch exists
    result = subprocess.run(
        ["git", "branch", "--list", "autoresearch/integ-002"],
        cwd=tmp_path, capture_output=True, text=True
    )
    assert "autoresearch/integ-002" in result.stdout
