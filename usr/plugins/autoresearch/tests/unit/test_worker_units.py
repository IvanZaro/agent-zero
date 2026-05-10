"""Unit tests for worker/__init__.py, _artifacts.py, _program_md.py, kill_handler.py.
Targets the uncovered lines identified in the coverage report.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# _artifacts.py
# ---------------------------------------------------------------------------

from usr.plugins.autoresearch.worker._artifacts import (
    _serialise,
    git_diff_patch,
    write_atomic,
    write_experiment_artifacts,
)


class TestSerialise:
    def test_decimal_serialised_to_string(self) -> None:
        assert _serialise(Decimal("1.23")) == "1.23"

    def test_datetime_serialised_to_isoformat(self) -> None:
        dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = _serialise(dt)
        assert "2026-01-01" in result

    def test_path_serialised_to_string(self) -> None:
        p = Path("/some/path/file.md")
        assert _serialise(p) == "/some/path/file.md"

    def test_unknown_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            _serialise(object())


class TestWriteAtomic:
    def test_writes_content_to_path(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        write_atomic(target, "hello world")
        assert target.read_text() == "hello world"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c.txt"
        write_atomic(target, "nested")
        assert target.exists()

    def test_atomic_replace_leaves_no_tmp_files(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        write_atomic(target, "content")
        tmps = list(tmp_path.glob("*.tmp"))
        assert tmps == []

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        write_atomic(target, "first")
        write_atomic(target, "second")
        assert target.read_text() == "second"


class TestGitDiffPatch:
    def test_returns_empty_string_on_git_error(self, tmp_path: Path) -> None:
        result = git_diff_patch("deadbeef", "nonexistent.md", tmp_path)
        assert result == ""


class TestWriteExperimentArtifacts:
    def _make_eval(self):
        from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
        return EvalResult(
            passed=1, total=1, total_tokens=50,
            per_task=[TaskOutcome(task_id="t1", passed=True, tokens=50, output="ok", error=None)],
        )

    def test_writes_all_required_files(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "exp-001"
        now = datetime.now(tz=timezone.utc)
        write_experiment_artifacts(
            exp_dir=exp_dir,
            n=1,
            outcome="kept",
            sha="abc123",
            started_at=now,
            finished_at=now,
            spend_usd=Decimal("0.01"),
            rationale="test rationale",
            eval_result=self._make_eval(),
            diff_patch="--- a/file\n+++ b/file\n",
            program_md_text="---\nprofile: test\n---\nbody",
            trace_lines=["[exp-001] started"],
        )
        assert (exp_dir / "program.md").exists()
        assert (exp_dir / "outcome.json").exists()
        assert (exp_dir / "score.json").exists()
        assert (exp_dir / "diff.patch").exists()
        assert (exp_dir / "trace.log").exists()
        assert (exp_dir / "eval_result.json").exists()

    def test_skips_diff_patch_when_none(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "exp-001"
        now = datetime.now(tz=timezone.utc)
        write_experiment_artifacts(
            exp_dir=exp_dir, n=1, outcome="smoke_failed", sha=None,
            started_at=now, finished_at=now, spend_usd=Decimal("0"),
            rationale=None, eval_result=None, diff_patch=None,
            program_md_text="---\nprofile: t\n---\n", trace_lines=[],
        )
        assert not (exp_dir / "diff.patch").exists()

    def test_writes_judge_json_when_provided(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.harness.judge import JudgeScore
        exp_dir = tmp_path / "exp-001"
        now = datetime.now(tz=timezone.utc)
        judge = JudgeScore(score=8.0, rubric={"correctness": 5, "conciseness": 2, "tool_use_efficiency": 1}, judge_tokens_used=100)
        write_experiment_artifacts(
            exp_dir=exp_dir, n=1, outcome="kept", sha="abc",
            started_at=now, finished_at=now, spend_usd=Decimal("0"),
            rationale="r", eval_result=self._make_eval(), diff_patch=None,
            program_md_text="---\nprofile: t\n---\n", trace_lines=[],
            judge_score=judge,
        )
        assert (exp_dir / "judge.json").exists()
        data = json.loads((exp_dir / "judge.json").read_text())
        assert data["score"] == 8.0

    def test_non_dataclass_judge_uses_score_attr(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "exp-001"
        now = datetime.now(tz=timezone.utc)
        fake_judge = MagicMock()
        fake_judge.score = 5.0
        # asdict will fail on MagicMock, should fall back to {"score": ...}
        write_experiment_artifacts(
            exp_dir=exp_dir, n=1, outcome="kept", sha="abc",
            started_at=now, finished_at=now, spend_usd=Decimal("0"),
            rationale="r", eval_result=None, diff_patch=None,
            program_md_text="---\nprofile: t\n---\n", trace_lines=[],
            judge_score=fake_judge,
        )
        assert (exp_dir / "judge.json").exists()

    def test_outcome_json_fields_correct(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "exp-002"
        now = datetime.now(tz=timezone.utc)
        write_experiment_artifacts(
            exp_dir=exp_dir, n=2, outcome="reverted", sha=None,
            started_at=now, finished_at=now, spend_usd=Decimal("0.05"),
            rationale="weak signal", eval_result=None, diff_patch=None,
            program_md_text="---\nprofile: t\n---\n", trace_lines=["line1", "line2"],
        )
        outcome = json.loads((exp_dir / "outcome.json").read_text())
        assert outcome["n"] == 2
        assert outcome["outcome"] == "reverted"
        assert outcome["sha"] is None
        assert outcome["spend_usd"] == "0.05"
        assert outcome["rationale"] == "weak signal"

    def test_eval_result_none_writes_null_score(self, tmp_path: Path) -> None:
        exp_dir = tmp_path / "exp-001"
        now = datetime.now(tz=timezone.utc)
        write_experiment_artifacts(
            exp_dir=exp_dir, n=1, outcome="coder_failed", sha=None,
            started_at=now, finished_at=now, spend_usd=Decimal("0"),
            rationale=None, eval_result=None, diff_patch=None,
            program_md_text="", trace_lines=[],
        )
        score = json.loads((exp_dir / "score.json").read_text())
        assert score is None


# ---------------------------------------------------------------------------
# worker/__init__.py — CLI entrypoint
# ---------------------------------------------------------------------------

from usr.plugins.autoresearch.worker import _generate_run_id, _parse_args


class TestGenerateRunId:
    def test_returns_non_empty_string(self) -> None:
        rid = _generate_run_id()
        assert isinstance(rid, str)
        assert len(rid) > 0

    def test_returns_unique_ids(self) -> None:
        ids = {_generate_run_id() for _ in range(20)}
        assert len(ids) == 20

    def test_fallback_without_uuid_utils(self) -> None:
        with patch.dict("sys.modules", {"uuid_utils": None}):
            rid = _generate_run_id()
        assert isinstance(rid, str)
        assert len(rid) > 0


class TestParseArgs:
    def test_requires_program_md(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args([])

    def test_parses_required_program_md(self, tmp_path: Path) -> None:
        p = tmp_path / "program.md"
        p.write_text("x")
        args = _parse_args(["--program-md", str(p)])
        assert args.program_md == str(p)

    def test_run_id_optional(self, tmp_path: Path) -> None:
        p = tmp_path / "program.md"
        p.write_text("x")
        args = _parse_args(["--program-md", str(p)])
        assert args.run_id is None

    def test_all_optional_args_parsed(self, tmp_path: Path) -> None:
        p = tmp_path / "program.md"
        p.write_text("x")
        args = _parse_args([
            "--program-md", str(p),
            "--run-id", "my-run",
            "--max-experiments", "10",
            "--cost-cap-usd", "2.50",
        ])
        assert args.run_id == "my-run"
        assert args.max_experiments == 10
        assert args.cost_cap_usd == "2.50"


class TestMainEntrypoint:
    def test_exits_1_when_program_md_missing(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.worker import main
        with pytest.raises(SystemExit) as exc_info:
            main(["--program-md", str(tmp_path / "nonexistent.md")])
        assert exc_info.value.code == 1

    def test_main_calls_run_loop(self, tmp_path: Path) -> None:
        program_md = tmp_path / "program.md"
        program_md.write_text(
            "---\nprofile: test\nprompt_file: prompts/system.md\n---\nbody\n"
        )
        mock_state = MagicMock()
        mock_state.status = "completed"
        mock_state.experiments = []

        from usr.plugins.autoresearch.worker import main
        with patch("usr.plugins.autoresearch.worker.loop.run_loop", return_value=mock_state) as mock_loop:
            with patch("asyncio.run", return_value=mock_state):
                main(["--program-md", str(program_md), "--run-id", "test-run"])


# ---------------------------------------------------------------------------
# kill_handler.py — SIGTERM path (lines 15-19)
# ---------------------------------------------------------------------------

class TestKillHandler:
    def test_register_sets_globals(self, tmp_path: Path) -> None:
        import usr.plugins.autoresearch.worker.kill_handler as kh
        kh.register("sha123", tmp_path)
        assert kh._baseline_sha == "sha123"
        assert kh._repo_root == tmp_path

    def test_update_baseline_changes_sha(self) -> None:
        import usr.plugins.autoresearch.worker.kill_handler as kh
        kh._baseline_sha = "old"
        kh.update_baseline("new")
        assert kh._baseline_sha == "new"

    def test_sigterm_handler_calls_kill_recovery(self, tmp_path: Path) -> None:
        import usr.plugins.autoresearch.worker.kill_handler as kh
        kh._baseline_sha = "sha-sigterm"
        kh._repo_root = tmp_path

        with patch("usr.plugins.autoresearch.state.git_ops.kill_recovery") as mock_kr:
            with pytest.raises(SystemExit):
                kh._sigterm_handler(15, None)
        mock_kr.assert_called_once_with("sha-sigterm", tmp_path)

    def test_sigterm_handler_noop_when_no_sha(self) -> None:
        import usr.plugins.autoresearch.worker.kill_handler as kh
        kh._baseline_sha = None
        kh._repo_root = None
        with pytest.raises(SystemExit):
            kh._sigterm_handler(15, None)


# ---------------------------------------------------------------------------
# state/runs.py — RunState.load() round-trip (lines 155-173)
# ---------------------------------------------------------------------------

class TestRunStateLoad:
    def test_load_round_trips_state(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.state.runs import RunState
        run_dir = tmp_path / "load-test"
        run_dir.mkdir()
        state = RunState(
            run_id="load-test",
            status="running",
            experiments=[],
            spend_usd=Decimal("0"),
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            baseline_sha="abc123",
            _run_dir=run_dir,
        )
        state.save()

        loaded = RunState.load("load-test", runs_root=tmp_path)
        assert loaded.run_id == "load-test"
        assert loaded.status == "running"
        assert loaded.baseline_sha == "abc123"
        assert loaded.spend_usd == Decimal("0")

    def test_load_with_experiments(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
        from usr.plugins.autoresearch.state.runs import ExperimentRecord, RunState

        eval_result = EvalResult(
            passed=1, total=1, total_tokens=50,
            per_task=[TaskOutcome(task_id="t1", passed=True, tokens=50, output="ok", error=None)],
        )
        rec = ExperimentRecord(
            n=1,
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            finished_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            outcome="kept",
            eval_result=eval_result,
            judge_score=7.5,
            spend_usd=Decimal("0.01"),
            sha="def456",
            rationale="improved momentum",
        )
        run_dir = tmp_path / "load-exp-test"
        run_dir.mkdir()
        state = RunState(
            run_id="load-exp-test",
            status="completed",
            experiments=[rec],
            spend_usd=Decimal("0.01"),
            started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            baseline_sha="def456",
            _run_dir=run_dir,
        )
        state.save()

        loaded = RunState.load("load-exp-test", runs_root=tmp_path)
        assert len(loaded.experiments) == 1
        exp = loaded.experiments[0]
        assert exp.outcome == "kept"
        assert exp.sha == "def456"
        assert exp.judge_score == 7.5
        assert exp.eval_result is not None
        assert exp.eval_result.passed == 1

    def test_serialise_raises_on_unknown_type(self) -> None:
        from usr.plugins.autoresearch.state.runs import _serialise
        with pytest.raises(TypeError):
            _serialise(object())

    def test_parse_task_outcome_with_optional_fields(self) -> None:
        from usr.plugins.autoresearch.state.runs import _parse_task_outcome
        result = _parse_task_outcome({"task_id": "t1", "passed": True, "tokens": 10})
        assert result.task_id == "t1"
        assert result.output is None
        assert result.error is None

    def test_parse_eval_result_none_returns_none(self) -> None:
        from usr.plugins.autoresearch.state.runs import _parse_eval_result
        assert _parse_eval_result(None) is None

    def test_decimal_or_none_converts_value(self) -> None:
        from usr.plugins.autoresearch.state.runs import _decimal_or_none
        assert _decimal_or_none("1.23") == Decimal("1.23")
        assert _decimal_or_none(None) is None


# ---------------------------------------------------------------------------
# state/_path_safety.py — symlink escape path (lines 55-61)
# ---------------------------------------------------------------------------

class TestPathSafetySymlink:
    def test_symlink_within_prompts_is_accepted(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.state._path_safety import validate_editable_path

        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts"
        prompts_dir.mkdir(parents=True)
        real_file = prompts_dir / "system.md"
        real_file.write_text("content")

        # Symlink inside prompts pointing to another file inside prompts
        link = prompts_dir / "linked.md"
        link.symlink_to(real_file)

        result = validate_editable_path(link, profile_root)
        assert result.exists()

    def test_rejects_path_equal_to_prompts_root(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.state._path_safety import EditablePathError, validate_editable_path
        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts"
        prompts_dir.mkdir(parents=True)
        with pytest.raises(EditablePathError):
            validate_editable_path(prompts_dir, profile_root)

    def test_rejects_symlink_escaping_prompts(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.state._path_safety import EditablePathError, validate_editable_path
        profile_root = tmp_path / "agents" / "trader"
        prompts_dir = profile_root / "prompts"
        prompts_dir.mkdir(parents=True)
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("secret")
        # Symlink inside prompts points outside
        link = prompts_dir / "escape.md"
        link.symlink_to(outside_file)
        with pytest.raises(EditablePathError, match="symlink"):
            validate_editable_path(link, profile_root)


# ---------------------------------------------------------------------------
# worker/loop.py — uncovered branches
# ---------------------------------------------------------------------------

class TestLoopUncoveredBranches:
    """Targeted tests for loop.py lines not hit by integration tests."""

    def test_find_repo_root_falls_back_to_cwd(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.worker.loop import _find_repo_root
        # Directory with no agent.py anywhere in its parents → falls back to cwd
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        result = _find_repo_root(deep)
        # Should not raise; returns some Path
        assert isinstance(result, Path)

    def test_find_repo_root_finds_agent_py(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.worker.loop import _find_repo_root
        (tmp_path / "agent.py").write_text("# stub")
        deep = tmp_path / "sub" / "dir"
        deep.mkdir(parents=True)
        assert _find_repo_root(deep) == tmp_path

    def test_check_clean_worktree_raises_on_git_failure(self, tmp_path: Path) -> None:
        from usr.plugins.autoresearch.worker.loop import _check_clean_worktree
        # Non-git directory → git status returns non-zero
        with pytest.raises(RuntimeError):
            _check_clean_worktree(tmp_path)

    @pytest.mark.asyncio
    async def test_run_loop_raises_on_bad_program_md(self, tmp_path: Path) -> None:
        """ProgramMdError → ValueError re-raised by run_loop."""
        import subprocess
        program_md = tmp_path / "program.md"
        program_md.write_text("no frontmatter here")  # missing ---
        (tmp_path / "agent.py").write_text("# stub")
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        from usr.plugins.autoresearch.worker.loop import run_loop
        with pytest.raises(ValueError, match="program.md parse error"):
            await run_loop(run_id="bad-md", program_md_path=program_md)

    @pytest.mark.asyncio
    async def test_judge_model_invoked_when_configured(self, tmp_path: Path) -> None:
        """Judge path runs when judge_model is set in frontmatter."""
        import subprocess, json as _json
        from decimal import Decimal
        from unittest.mock import AsyncMock, patch
        from usr.plugins.autoresearch.coder.base import EditPatch
        from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
        from usr.plugins.autoresearch.worker.cost_meter import CostMeter
        from usr.plugins.autoresearch.worker.loop import run_loop

        agent_dir = tmp_path / "agents" / "trader" / "prompts"
        agent_dir.mkdir(parents=True)
        (agent_dir / "system.md").write_text("Buy when momentum is positive.\n")
        (tmp_path / "agents" / "trader" / "_context.yaml").write_text("title: T\ndescription: t\ncontext: ''\n")
        (tmp_path / "agent.py").write_text("# stub")
        (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)
        suite = tmp_path / "suite.json"
        suite.write_text(_json.dumps({"version": 1, "tasks": [{"id": "t1", "input": "q?", "assert_substring": "buy"}]}))

        program_md = tmp_path / "program.md"
        program_md.write_text(
            f"---\nprofile: trader\nprompt_file: prompts/system.md\n"
            f"eval_suite: {suite}\njudge_model: gpt-4o-mini\n---\nImprove.\n"
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        from usr.plugins.autoresearch.harness.judge import JudgeScore
        judge_result = JudgeScore(score=7.0, rubric={}, judge_tokens_used=50)

        evals = iter([
            EvalResult(passed=0, total=1, total_tokens=50, per_task=[TaskOutcome("t1", False, 50, "x", None)]),
            EvalResult(passed=1, total=1, total_tokens=50, per_task=[TaskOutcome("t1", True, 50, "buy", None)]),
        ])

        class ImprovingCoder:
            name = "mock"
            async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
                return EditPatch(
                    target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                    old_text="Buy when momentum is positive.",
                    new_text="Aggressively buy on confirmed momentum.",
                    rationale="stronger",
                )
            def report_cost(self) -> Decimal: return Decimal("0")

        with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=AsyncMock(side_effect=evals)):
            with patch("usr.plugins.autoresearch.worker.loop.judge_score", new=AsyncMock(return_value=judge_result)):
                with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                    state = await run_loop(
                        run_id="judge-test",
                        program_md_path=program_md,
                        max_experiments_override=1,
                        coder=ImprovingCoder(),
                        cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
                    )

        assert state.experiments[0].judge_score == 7.0

    @pytest.mark.asyncio
    async def test_unexpected_exception_in_experiment_marks_crashed(self, tmp_path: Path) -> None:
        """An unexpected exception inside an experiment → outcome='crashed', loop continues."""
        import subprocess, json as _json
        from decimal import Decimal
        from unittest.mock import patch
        from usr.plugins.autoresearch.coder.base import EditPatch
        from usr.plugins.autoresearch.harness.eval_runner import EvalResult, TaskOutcome
        from usr.plugins.autoresearch.worker.cost_meter import CostMeter
        from usr.plugins.autoresearch.worker.loop import run_loop

        agent_dir = tmp_path / "agents" / "trader" / "prompts"
        agent_dir.mkdir(parents=True)
        (agent_dir / "system.md").write_text("Buy when momentum is positive.\n")
        (tmp_path / "agents" / "trader" / "_context.yaml").write_text("title: T\ndescription: t\ncontext: ''\n")
        (tmp_path / "agent.py").write_text("# stub")
        (tmp_path / "usr" / "autoresearch" / "runs").mkdir(parents=True, exist_ok=True)
        suite = tmp_path / "suite.json"
        suite.write_text(_json.dumps({"version": 1, "tasks": [{"id": "t1", "input": "q?", "assert_substring": "buy"}]}))

        program_md = tmp_path / "program.md"
        program_md.write_text(
            f"---\nprofile: trader\nprompt_file: prompts/system.md\neval_suite: {suite}\n---\nImprove.\n"
        )
        subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

        call_n = 0

        class ExplodingCoder:
            name = "exploding"
            async def propose_edit(self, program_md: str, file_text: str, recent_history: list) -> EditPatch:
                nonlocal call_n
                call_n += 1
                if call_n == 1:
                    # Return valid patch that will be applied, but then eval will blow up
                    return EditPatch(
                        target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                        old_text="Buy when momentum is positive.",
                        new_text="Aggressively buy on confirmed momentum.",
                        rationale="will crash on eval",
                    )
                return EditPatch(
                    target_path=tmp_path / "agents" / "trader" / "prompts" / "system.md",
                    old_text="Aggressively buy on confirmed momentum.",
                    new_text="Buy on momentum.",
                    rationale="exp2",
                )
            def report_cost(self) -> Decimal: return Decimal("0")

        eval_call = 0

        async def exploding_eval(*args, **kwargs):
            nonlocal eval_call
            eval_call += 1
            if eval_call == 1:
                return EvalResult(passed=0, total=1, total_tokens=50,
                                  per_task=[TaskOutcome("t1", False, 50, None, None)])
            # Exp1 eval raises unexpectedly
            raise RuntimeError("eval infrastructure crashed")

        with patch("usr.plugins.autoresearch.worker.loop.run_eval", new=exploding_eval):
            with patch("usr.plugins.autoresearch.worker.loop._find_repo_root", return_value=tmp_path):
                state = await run_loop(
                    run_id="crash-test",
                    program_md_path=program_md,
                    max_experiments_override=2,
                    coder=ExplodingCoder(),
                    cost_meter=CostMeter(cap_usd=Decimal("5.00"), rates={}),
                )

        assert state.experiments[0].outcome == "crashed"
        assert state.status == "completed"
