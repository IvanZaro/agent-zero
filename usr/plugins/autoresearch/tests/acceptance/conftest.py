"""
Shared fixtures for H1–H9 acceptance gates.

All fixtures here are available to every acceptance test file without import.
"""
from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome


# ---------------------------------------------------------------------------
# Helpers (also importable by test files that need more control)
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args, cwd=cwd, check=True, capture_output=True, text=True
    )


def _init_repo(path: Path) -> str:
    """Init a bare repo on 'main', commit all files, return HEAD sha."""
    _git(["init", "-b", "main"], cwd=path)
    _git(["config", "user.email", "ci@test.com"], cwd=path)
    _git(["config", "user.name", "CI"], cwd=path)
    _git(["add", "-A"], cwd=path)
    _git(["commit", "-m", "init"], cwd=path)
    return _git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], cwd=repo).stdout.strip()


def _make_eval(passed: int = 1, total: int = 1, tokens: int = 100) -> EvalResult:
    per_task = [
        TaskOutcome(
            task_id=f"t-{i:03d}",
            passed=(i < passed),
            tokens=tokens // max(total, 1),
            output="long" if i < passed else "hold",
            error=None,
        )
        for i in range(total)
    ]
    return EvalResult(passed=passed, total=total, total_tokens=tokens, per_task=per_task)


def _make_suite_json(tasks: list[dict]) -> str:
    return json.dumps({"version": 1, "tasks": tasks})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path: Path):
    """A tmp git repo pre-loaded with a fixture trader profile committed on main."""
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture Trader\ndescription: acceptance test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(_make_suite_json([
        {"id": "fx-001", "input": "momentum positive?", "assert_substring": "long"},
        {"id": "fx-002", "input": "downtrend signal?", "assert_substring": "short"},
    ]))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 3\ncost_cap_usd: '5.00'\n---\n"
        "Improve momentum clarity.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return tmp_path, program_md, baseline_sha


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    """Isolated tmp directory for run artifacts."""
    root = tmp_path / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


class _DeterministicCoder:
    """
    Returns a scripted sequence of EditPatches, cycling through the list.

    Pass improvement_curve=[True, False, True] to control whether each patch
    targets the 'assert_substring' (improving) or not.
    """

    name = "mock_litellm"

    def __init__(
        self,
        patches: list[tuple[str, str]] | None = None,
        cost_per_call: Decimal = Decimal("0.05"),
        cost_meter=None,
    ) -> None:
        self._patches = patches or [("go long", "go long and buy immediately")]
        self._call_count = 0
        self._total_cost = Decimal("0")
        self._cost_per_call = cost_per_call
        self._meter = cost_meter

    async def propose_edit(
        self, program_md: str, file_text: str, recent_history: list
    ) -> EditPatch:
        idx = self._call_count % len(self._patches)
        old_text, new_text = self._patches[idx]
        self._call_count += 1
        self._total_cost += self._cost_per_call
        if self._meter is not None:
            self._meter.tick("mock-model", 1000, 500)
        return EditPatch(
            target_path=Path("__pending__"),
            old_text=old_text,
            new_text=new_text,
            rationale=f"test patch #{self._call_count}",
        )

    def report_cost(self) -> Decimal:
        return self._total_cost


@pytest.fixture
def mock_litellm_coder():
    """Factory: call with kwargs to build a scripted deterministic coder."""
    def _factory(**kwargs):
        return _DeterministicCoder(**kwargs)
    return _factory


@pytest.fixture
def mock_passing_baseline():
    """
    Opt-in fixture: patches _run_baseline_eval to return a usable baseline
    (all tasks passed) so the loop does not abort on broken-auth environments.

    NOT autouse — H2 tests need the real baseline eval path; do not apply
    this fixture to those tests.

    Usage:
        async def test_foo(tmp_path, mock_passing_baseline):
            with mock_passing_baseline:
                state = await run_loop(...)
    """
    from unittest.mock import patch, AsyncMock

    baseline = _make_eval(passed=1, total=1, tokens=100)
    return patch(
        "usr.plugins.autoresearch.worker.loop._run_baseline_eval",
        new=AsyncMock(return_value=baseline),
    )


@pytest.fixture
def mock_claude_subprocess(monkeypatch):
    """
    Patches subprocess.run so ClaudeCodeCoder never invokes real claude.

    Returns a MagicMock so tests can inspect calls.
    """
    import subprocess as _sp

    response_json = json.dumps({
        "type": "result",
        "result": json.dumps({
            "old_text": "go long",
            "new_text": "go long immediately",
            "rationale": "mock claude edit",
        }),
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })

    mock = MagicMock(return_value=MagicMock(
        returncode=0,
        stdout=response_json,
        stderr="",
    ))
    monkeypatch.setattr(_sp, "run", mock)
    return mock
