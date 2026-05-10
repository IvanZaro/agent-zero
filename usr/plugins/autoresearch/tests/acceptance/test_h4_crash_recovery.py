"""
H4: Crash recovery — coder returns malformed JSON 4 attempts in a row
→ coder_failed → loop advances → branch HEAD unchanged from baseline → no commit.
"""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.worker.loop import run_loop


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _sha(repo: Path) -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _sha_of_branch(repo: Path, branch: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", branch], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _main_sha(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _setup_h4_repo(tmp_path: Path) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "fx-001", "input": "go long or short?", "assert_substring": "long"}]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 2\ncost_cap_usd: '5.00'\n---\n"
        "Research agenda: improve momentum clarity.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


class AlwaysMalformedCoder:
    """Returns malformed JSON every call — exhausts all 3 retries in litellm_coder."""
    name = "always_malformed"

    async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> None:
        # Simulate the behaviour after LiteLLMCoder exhausts its 3 retries
        raise ValueError("Simulated: all 3 retries returned malformed JSON")

    def report_cost(self) -> Decimal:
        return Decimal("0")


@pytest.mark.asyncio
async def test_h4_coder_failed_loop_advances(tmp_path: Path, mock_passing_baseline) -> None:
    program_md, baseline_sha = _setup_h4_repo(tmp_path)

    from usr.plugins.autoresearch.worker.cost_meter import CostMeter
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
         mock_passing_baseline:
        state = await run_loop(
            run_id="h4-test-001",
            program_md_path=program_md,
            max_experiments_override=2,
            coder=AlwaysMalformedCoder(),
            cost_meter=meter,
        )

    # Both experiments must have advanced with coder_failed
    assert len(state.experiments) == 2, f"Expected 2 experiments, got {len(state.experiments)}"
    assert state.experiments[0].outcome == "coder_failed"
    assert state.experiments[1].outcome == "coder_failed"

    # Loop completed normally (coder_failed is expected, not a crash)
    assert state.status == "completed", f"Expected 'completed', got {state.status!r}"

    # Branch exists
    branch = "autoresearch/h4-test-001"
    result = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=tmp_path, capture_output=True, text=True
    )
    assert branch in result.stdout, f"Branch {branch} not found"

    # Branch HEAD == baseline_sha (no commits)
    branch_sha = _sha_of_branch(tmp_path, branch)
    assert branch_sha == baseline_sha, (
        f"Branch HEAD {branch_sha} != baseline {baseline_sha}; unexpected commit happened"
    )

    # main HEAD unchanged
    assert _main_sha(tmp_path) == baseline_sha, "main was modified — safety violation"

    # No push anywhere in git_ops source
    import usr.plugins.autoresearch.state.git_ops as git_ops_module
    import inspect
    src = inspect.getsource(git_ops_module)
    assert "push" not in src.lower().replace("subprocess", ""), (
        "git push detected in git_ops source"
    )

    # exp-001/outcome.json shows coder_failed
    run_dir = tmp_path / "usr" / "autoresearch" / "runs" / "h4-test-001"
    outcome_path = run_dir / "exp-001" / "outcome.json"
    assert outcome_path.exists(), "exp-001/outcome.json missing"
    outcome_data = json.loads(outcome_path.read_text())
    assert outcome_data["outcome"] == "coder_failed"

    # state.json is valid JSON (no partial-write corruption)
    state_path = run_dir / "state.json"
    assert state_path.exists()
    state_data = json.loads(state_path.read_text())
    assert state_data["run_id"] == "h4-test-001"
    assert state_data["status"] == "completed"
    assert len(state_data["experiments"]) == 2
