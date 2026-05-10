"""
H8 — Both backends.

Design-doc invariant (§8):
  The loop runs to completion with BOTH backends:
    - 'litellm'     → LiteLLMCoder is instantiated (backend field read)
    - 'claude_code' → ClaudeCodeCoder subprocess path is taken

Each backend run must complete ≥1 experiment with outcome in the valid set.

Strategy: inject a known-good coder via the `coder` parameter so no real
LLM/subprocess call is needed. The backend dispatch assertion verifies that
(a) the program.md `backend:` field is parsed, and (b) when no coder is
injected, the loop instantiates the correct class for that backend.
"""
from __future__ import annotations

import asyncio
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
from usr.plugins.autoresearch.worker.cost_meter import CostMeter
from usr.plugins.autoresearch.worker.loop import run_loop
from usr.plugins.autoresearch.worker._program_md import parse_program_md

_VALID_OUTCOMES = frozenset({
    "kept", "reverted", "coder_failed", "smoke_failed", "patch_failed", "crashed"
})


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


def _setup_repo(tmp_path: Path, backend: str = "litellm") -> tuple[Path, str]:
    agent_dir = tmp_path / "agents" / "fixture_trader" / "prompts"
    agent_dir.mkdir(parents=True)
    (agent_dir / "system.md").write_text(
        "You are a trading assistant. When momentum is positive, go long.\n"
    )
    (tmp_path / "agents" / "fixture_trader" / "_context.yaml").write_text(
        "title: Fixture\ndescription: h8 test\ncontext: ''\n"
    )
    (tmp_path / "agent.py").write_text("# stub\n")
    (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)

    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps({
        "version": 1,
        "tasks": [{"id": "h8-001", "input": "momentum?", "assert_substring": "long"}]
    }))

    program_md = tmp_path / "program.md"
    program_md.write_text(
        f"---\nprofile: fixture_trader\nprompt_file: prompts/system.md\n"
        f"backend: {backend}\n"
        f"eval_suite: {suite_path}\nmax_experiments: 1\ncost_cap_usd: '5.00'\n---\n"
        "Improve the assistant.\n"
    )

    baseline_sha = _init_repo(tmp_path)
    return program_md, baseline_sha


class _ControlledCoder:
    """Injected coder with a fixed name — tracks propose_edit calls."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.call_count = 0
        self._total = Decimal("0")

    async def propose_edit(self, **_) -> EditPatch:  # type: ignore[override]
        self.call_count += 1
        self._total += Decimal("0.001")
        return EditPatch(
            target_path=Path("__pending__"),
            old_text="go long",
            new_text="go long and buy immediately",
            rationale=f"{self.name} patch #{self.call_count}",
        )

    def report_cost(self) -> Decimal:
        return self._total


def _mock_litellm_eval() -> AsyncMock:
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
# H8a: litellm backend — program.md parsing + dispatch
# ---------------------------------------------------------------------------

@pytest.mark.acceptance
def test_h8_litellm_backend_field_parsed(tmp_path: Path):
    """program.md with backend: litellm must parse to cfg.backend == 'litellm'."""
    program_md, _ = _setup_repo(tmp_path, backend="litellm")
    cfg, _ = parse_program_md(program_md)
    assert cfg.backend == "litellm", (
        f"Expected backend='litellm', got {cfg.backend!r}"
    )


@pytest.mark.acceptance
def test_h8_litellm_backend_completes_experiment(tmp_path: Path):
    """litellm backend: injected coder completes ≥1 experiment with valid outcome."""
    program_md, _ = _setup_repo(tmp_path, backend="litellm")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ControlledCoder(name="litellm_controlled")

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = _mock_litellm_eval()
            return await run_loop(
                run_id="h8-litellm-exp-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert coder.call_count >= 1, "Injected coder was never called"
    assert len(state.experiments) >= 1, "At least 1 experiment must have run"
    assert state.experiments[0].outcome in _VALID_OUTCOMES, (
        f"Outcome {state.experiments[0].outcome!r} not in valid set"
    )


@pytest.mark.acceptance
def test_h8_litellm_class_instantiated_when_no_coder_injected(tmp_path: Path):
    """When no coder is injected with backend=litellm, LiteLLMCoder is constructed."""
    program_md, _ = _setup_repo(tmp_path, backend="litellm")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    instantiated: list[str] = []

    from usr.plugins.autoresearch.coder import litellm_coder as _lc_mod

    _orig_init = _lc_mod.LiteLLMCoder.__init__

    def _tracking_init(self, **kwargs):
        instantiated.append("LiteLLMCoder")
        _orig_init(self, **kwargs)

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch.object(_lc_mod.LiteLLMCoder, "__init__", _tracking_init):
            # Also stub out propose_edit so we don't actually call LiteLLM
            _stub_patch = EditPatch(
                target_path=Path("__pending__"),
                old_text="go long",
                new_text="go long immediately",
                rationale="stub",
            )
            mock_litellm.acompletion = _mock_litellm_eval()

            async def _stub_propose(_self, *a, **kw):
                return _stub_patch

            with patch.object(_lc_mod.LiteLLMCoder, "propose_edit", _stub_propose):
                return await run_loop(
                    run_id="h8-litellm-class-001",
                    program_md_path=program_md,
                    max_experiments_override=1,
                    cost_meter=meter,
                )

    asyncio.run(_run())
    assert "LiteLLMCoder" in instantiated, (
        "LiteLLMCoder was not instantiated for backend='litellm'"
    )


# ---------------------------------------------------------------------------
# H8b: claude_code backend
# ---------------------------------------------------------------------------

@pytest.mark.acceptance
def test_h8_claude_code_backend_field_parsed(tmp_path: Path):
    """program.md with backend: claude_code must parse to cfg.backend == 'claude_code'."""
    program_md, _ = _setup_repo(tmp_path, backend="claude_code")
    cfg, _ = parse_program_md(program_md)
    assert cfg.backend == "claude_code", (
        f"Expected backend='claude_code', got {cfg.backend!r}"
    )


@pytest.mark.acceptance
def test_h8_claude_code_backend_completes_experiment(tmp_path: Path):
    """claude_code backend: injected coder completes ≥1 experiment with valid outcome."""
    program_md, _ = _setup_repo(tmp_path, backend="claude_code")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})
    coder = _ControlledCoder(name="claude_code_controlled")

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
            mock_litellm.acompletion = _mock_litellm_eval()
            return await run_loop(
                run_id="h8-claude-exp-001",
                program_md_path=program_md,
                max_experiments_override=1,
                coder=coder,
                cost_meter=meter,
            )

    state = asyncio.run(_run())

    assert coder.call_count >= 1, "Injected coder was never called"
    assert len(state.experiments) >= 1, "At least 1 experiment must have run"
    assert state.experiments[0].outcome in _VALID_OUTCOMES


@pytest.mark.acceptance
def test_h8_claude_code_class_instantiated_when_no_coder_injected(tmp_path: Path):
    """When no coder is injected with backend=claude_code, ClaudeCodeCoder is constructed."""
    program_md, _ = _setup_repo(tmp_path, backend="claude_code")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    instantiated: list[str] = []

    from usr.plugins.autoresearch.coder import claude_code_coder as _cc_mod

    _orig_init = _cc_mod.ClaudeCodeCoder.__init__

    def _tracking_init(self, **kwargs):
        instantiated.append("ClaudeCodeCoder")
        _orig_init(self, **kwargs)

    _stub_patch = EditPatch(
        target_path=Path("__pending__"),
        old_text="go long",
        new_text="go long immediately",
        rationale="stub",
    )

    async def _stub_propose(_self, *a, **kw):
        return _stub_patch

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.coder.claude_code_coder._claude_cmd",
                   return_value=["claude"]), \
             patch.object(_cc_mod.ClaudeCodeCoder, "__init__", _tracking_init), \
             patch.object(_cc_mod.ClaudeCodeCoder, "propose_edit", _stub_propose):
            mock_litellm.acompletion = _mock_litellm_eval()
            return await run_loop(
                run_id="h8-claude-class-001",
                program_md_path=program_md,
                max_experiments_override=1,
                cost_meter=meter,
            )

    asyncio.run(_run())
    assert "ClaudeCodeCoder" in instantiated, (
        "ClaudeCodeCoder was not instantiated for backend='claude_code'"
    )


@pytest.mark.acceptance
def test_h8_claude_code_subprocess_called_with_claude_cli(tmp_path: Path):
    """claude_code backend: subprocess.run is invoked with '--print' (claude CLI flag)."""
    program_md, _ = _setup_repo(tmp_path, backend="claude_code")
    meter = CostMeter(cap_usd=Decimal("5.00"), rates={})

    response_payload = json.dumps({
        "old_text": "go long",
        "new_text": "go long and buy immediately",
        "rationale": "mock claude edit",
    })
    envelope = json.dumps({
        "type": "result",
        "result": response_payload,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    })
    mock_claude_result = MagicMock(returncode=0, stdout=envelope, stderr="")
    claude_calls: list[list] = []
    _real_subprocess_run = subprocess.run

    def _selective_mock(args, **kwargs):
        """Intercept only calls where 'claude' is the executable; pass through git calls."""
        cmd = args[0] if isinstance(args, (list, tuple)) else args
        if cmd == "claude" or (isinstance(args, (list, tuple)) and args and args[0] == "claude"):
            claude_calls.append(list(args))
            return mock_claude_result
        return _real_subprocess_run(args, **kwargs)

    async def _run():
        with patch("usr.plugins.autoresearch.harness.eval_runner.litellm") as mock_litellm, \
             patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path), \
             patch("usr.plugins.autoresearch.coder.claude_code_coder.subprocess.run",
                   side_effect=_selective_mock), \
             patch("usr.plugins.autoresearch.coder.claude_code_coder._claude_cmd",
                   return_value=["claude"]):
            mock_litellm.acompletion = _mock_litellm_eval()
            state = await run_loop(
                run_id="h8-claude-cli-001",
                program_md_path=program_md,
                max_experiments_override=1,
                cost_meter=meter,
            )
            assert claude_calls, "subprocess.run was never called with 'claude' for claude_code backend"
            first_call_args = claude_calls[0]
            assert "--print" in first_call_args, (
                f"'--print' flag missing from claude CLI call: {first_call_args}"
            )
            return state

    state = asyncio.run(_run())
    assert len(state.experiments) >= 1
    assert state.experiments[0].outcome in _VALID_OUTCOMES
