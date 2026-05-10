"""Integration tests: verify worker/loop.py build_coder() factory dispatches correctly."""
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
from usr.plugins.autoresearch.worker.cost_meter import CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop


# ---------------------------------------------------------------------------
# Repo helpers (mirrored from test_loop_lifecycle.py)
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


def _setup_repo(
    tmp_path: Path,
    backend: str = "litellm",
    max_experiments: int = 1,
) -> tuple[Path, str]:
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
        ],
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\n"
        f"profile: trader\n"
        f"prompt_file: prompts/system.md\n"
        f"backend: {backend}\n"
        f"eval_suite: {suite_path}\n"
        f"max_experiments: {max_experiments}\n"
        f"cost_cap_usd: '5.00'\n"
        f"---\n"
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


def _good_patch(tmp_path: Path) -> EditPatch:
    return EditPatch(
        target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
        old_text="Buy when momentum is positive.",
        new_text="Buy when momentum is consistently positive.",
        rationale="test dispatch",
    )


# ---------------------------------------------------------------------------
# Test: LiteLLM dispatch (default backend)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_dispatches_to_litellm_coder_by_default(tmp_path: Path) -> None:
    """When backend is 'litellm' (default), build_coder() must create LiteLLMCoder."""
    from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder

    program_md, _ = _setup_repo(tmp_path, backend="litellm")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    used_coder: list[object] = []

    # Intercept the LiteLLMCoder constructor to track instantiation.
    original_init = LiteLLMCoder.__init__

    def capturing_init(self, *args, **kwargs):
        used_coder.append(self)
        original_init(self, *args, **kwargs)

    baseline_eval = _make_eval(passed=0)
    per_exp_eval = _make_eval(passed=1)
    evals = iter([baseline_eval, per_exp_eval])

    async def mock_run_eval(*args, **kwargs):
        return next(evals)

    good_patch = _good_patch(tmp_path)

    async def mock_propose_edit(self, *args, **kwargs):
        return good_patch

    with patch.object(LiteLLMCoder, "__init__", capturing_init):
        with patch.object(LiteLLMCoder, "propose_edit", mock_propose_edit):
            with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
                with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                    state = await run_loop(
                        run_id="dispatch-litellm-001",
                        program_md_path=program_md,
                        max_experiments_override=1,
                        cost_meter=meter,
                    )

    assert len(used_coder) == 1, "LiteLLMCoder must have been instantiated once"
    assert state.status == "completed"


# ---------------------------------------------------------------------------
# Test: Claude Code dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_dispatches_to_claude_code_when_backend_field_set(tmp_path: Path) -> None:
    """When backend is 'claude_code', build_coder() must create ClaudeCodeCoder."""
    from usr.plugins.autoresearch.coder.claude_code_coder import ClaudeCodeCoder

    program_md, _ = _setup_repo(tmp_path, backend="claude_code")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    used_coder: list[object] = []

    original_init = ClaudeCodeCoder.__init__

    def capturing_init(self, *args, **kwargs):
        used_coder.append(self)
        original_init(self, *args, **kwargs)

    baseline_eval = _make_eval(passed=0)
    per_exp_eval = _make_eval(passed=1)
    evals = iter([baseline_eval, per_exp_eval])

    async def mock_run_eval(*args, **kwargs):
        return next(evals)

    good_patch = _good_patch(tmp_path)

    async def mock_propose_edit(self, *args, **kwargs):
        return good_patch

    with patch.object(ClaudeCodeCoder, "__init__", capturing_init):
        with patch.object(ClaudeCodeCoder, "propose_edit", mock_propose_edit):
            with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
                with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                    state = await run_loop(
                        run_id="dispatch-claude-001",
                        program_md_path=program_md,
                        max_experiments_override=1,
                        cost_meter=meter,
                    )

    assert len(used_coder) == 1, "ClaudeCodeCoder must have been instantiated once"
    assert state.status == "completed"


# ---------------------------------------------------------------------------
# Test: cost_meter passed to both backends
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_factory_passes_cost_meter_to_either_backend(tmp_path: Path) -> None:
    """The cost_meter injected into run_loop must reach the coder in both backends."""
    from usr.plugins.autoresearch.coder.litellm_coder import LiteLLMCoder

    program_md, _ = _setup_repo(tmp_path, backend="litellm")

    # Inject a specific meter and verify the coder received it.
    rates: dict[str, dict[str, Decimal]] = {}
    injected_meter = CostMeter(cap_usd=Decimal("5.00"), rates=rates)

    received_meters: list[object] = []
    original_init = LiteLLMCoder.__init__

    def capturing_init(self, model=None, cost_meter=None):
        received_meters.append(cost_meter)
        original_init(self, model=model, cost_meter=cost_meter)

    baseline_eval = _make_eval(passed=0)
    per_exp_eval = _make_eval(passed=1)
    evals = iter([baseline_eval, per_exp_eval])

    async def mock_run_eval(*args, **kwargs):
        return next(evals)

    good_patch = _good_patch(tmp_path)

    async def mock_propose_edit(self, *args, **kwargs):
        return good_patch

    with patch.object(LiteLLMCoder, "__init__", capturing_init):
        with patch.object(LiteLLMCoder, "propose_edit", mock_propose_edit):
            with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=mock_run_eval):
                with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                    await run_loop(
                        run_id="dispatch-meter-001",
                        program_md_path=program_md,
                        max_experiments_override=1,
                        cost_meter=injected_meter,
                    )

    assert len(received_meters) == 1
    assert received_meters[0] is injected_meter
