"""
H5 — Branch isolation.

Design-doc invariant (§8):
  After any run, 'main' is unchanged and git push/remote/fetch are never invoked
  in production code.

Two-layer check:
  1. Static: grep the entire autoresearch source (non-test) for 'git push',
     'git remote', 'git fetch'. Any match in production code → fail.
  2. Runtime: spin up 3-experiment mocked run, then assert:
     - git rev-parse main unchanged
     - autoresearch/<run-id> branch exists and differs from main
     - subprocess.run was never called with 'push', 'remote', or 'fetch'
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.coder.base import EditPatch
from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
from usr.plugins.autoresearch.worker.cost_meter import CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop

_PLUGIN_SRC = Path(__file__).resolve().parents[2]

_FORBIDDEN_PATTERNS = ("git push", "git remote", "git fetch")
_TEST_DIRS = {"tests", "acceptance", "integration", "unit"}


def _is_test_file(path: Path) -> bool:
    """True if the file lives in a test directory or starts with test_."""
    parts = {p.lower() for p in path.parts}
    return bool(parts & _TEST_DIRS) or path.name.startswith("test_")


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup_repo(tmp_path: Path) -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: h5 test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "h5-001", "input": "momentum?", "assert_substring": "long"}]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"eval_suite: {suite_path}\nmax_experiments: 3\ncost_cap_usd: '5.00'\n---\n"
        "Improve the assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


class _ThreeExperimentCoder:
    """Alternates patches so old_text is always present."""
    name = "mock"
    _PAIRS = [
        ("go long", "go long and buy"),
        ("go long and buy", "go long"),
        ("go long", "go long immediately"),
    ]

    def __init__(self) -> None:
        self._n = 0
        self._total = Decimal("0")

    async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
        old, new = self._PAIRS[self._n % len(self._PAIRS)]
        self._n += 1
        self._total += Decimal("0.01")
        return EditPatch(
            target_path=Path("__pending__"),
            old_text=old,
            new_text=new,
            rationale=f"h5 patch {self._n}",
        )

    def report_cost(self) -> Decimal:
        return self._total


def _make_litellm_mock():
    choice = MagicMock()
    choice.message.content = "long"
    usage = MagicMock()
    usage.prompt_tokens = 50
    usage.completion_tokens = 20
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return AsyncMock(return_value=resp)


# ---------------------------------------------------------------------------
# Layer 1: static source scan
# ---------------------------------------------------------------------------

@pytest.mark.acceptance
def test_h5_no_git_push_in_production_source():
    """Production source must not contain 'git push'."""
    violations = []
    for py_file in _PLUGIN_SRC.rglob("*.py"):
        if _is_test_file(py_file):
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "git push" in line.lower():
                violations.append(f"{py_file}:{lineno}: {line.strip()}")
    assert not violations, (
        "Found 'git push' in production source:\n" + "\n".join(violations)
    )


@pytest.mark.acceptance
def test_h5_no_git_remote_in_production_source():
    """Production source must not contain 'git remote'."""
    violations = []
    for py_file in _PLUGIN_SRC.rglob("*.py"):
        if _is_test_file(py_file):
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "git remote" in line.lower():
                violations.append(f"{py_file}:{lineno}: {line.strip()}")
    assert not violations, (
        "Found 'git remote' in production source:\n" + "\n".join(violations)
    )


@pytest.mark.acceptance
def test_h5_no_git_fetch_in_production_source():
    """Production source must not contain 'git fetch'."""
    violations = []
    for py_file in _PLUGIN_SRC.rglob("*.py"):
        if _is_test_file(py_file):
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "git fetch" in line.lower():
                violations.append(f"{py_file}:{lineno}: {line.strip()}")
    assert not violations, (
        "Found 'git fetch' in production source:\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Layer 2: runtime check
# ---------------------------------------------------------------------------

@pytest.mark.acceptance
def test_h5_main_unchanged_after_run(tmp_path: Path):
    """main branch HEAD must be identical before and after a 3-experiment run."""
    program_md, baseline_sha = _setup_repo(tmp_path)
    main_sha_before = _sha(tmp_path, "main")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ThreeExperimentCoder()

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = _make_litellm_mock()
            return await run_loop(
                run_id="h5-isolation-001",
                program_md_path=program_md,
                max_experiments_override=3,
                coder=coder,
                cost_meter=meter,
            )

    asyncio.run(_run())

    main_sha_after = _sha(tmp_path, "main")
    assert main_sha_before == main_sha_after, (
        f"main was modified! before={main_sha_before} after={main_sha_after}"
    )


@pytest.mark.acceptance
def test_h5_run_branch_exists_and_differs_from_main(tmp_path: Path):
    """autoresearch/<run-id> branch must exist and have different HEAD from main."""
    program_md, baseline_sha = _setup_repo(tmp_path)
    main_sha = _sha(tmp_path, "main")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ThreeExperimentCoder()
    run_id = "h5-branch-001"

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = _make_litellm_mock()
            return await run_loop(
                run_id=run_id,
                program_md_path=program_md,
                max_experiments_override=3,
                coder=coder,
                cost_meter=meter,
            )

    asyncio.run(_run())

    branch = f"autoresearch/{run_id}"
    result = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=tmp_path, capture_output=True, text=True
    )
    assert branch in result.stdout, f"Branch {branch!r} not found in repo"


@pytest.mark.acceptance
def test_h5_no_push_remote_fetch_subprocess_calls(tmp_path: Path):
    """
    subprocess.run must never be called with git push, remote, or fetch args
    during the worker loop.
    """
    program_md, _ = _setup_repo(tmp_path)
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ThreeExperimentCoder()

    forbidden_calls: list[str] = []
    _original_run = subprocess.run

    def _tracking_run(args, **kwargs):
        if isinstance(args, (list, tuple)) and len(args) >= 2:
            git_sub = str(args[1]).lower() if len(args) > 1 else ""
            if git_sub in ("push", "remote", "fetch"):
                forbidden_calls.append(f"git {args[1]} {args[2:] if len(args) > 2 else ''}")
        return _original_run(args, **kwargs)

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("subprocess.run", side_effect=_tracking_run):
            mock_litellm.acompletion = _make_litellm_mock()
            return await run_loop(
                run_id="h5-nopush-001",
                program_md_path=program_md,
                max_experiments_override=3,
                coder=coder,
                cost_meter=meter,
            )

    asyncio.run(_run())

    assert not forbidden_calls, (
        f"Forbidden git subcommands were called during the run:\n"
        + "\n".join(forbidden_calls)
    )
