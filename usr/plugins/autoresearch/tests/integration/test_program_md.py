"""Tests for worker/_program_md.py — frontmatter parser. TDD: written before implementation."""
from __future__ import annotations

import sys
import warnings
from decimal import Decimal
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from usr.plugins.autoresearch.worker._program_md import (
    ProgramMd,
    ProgramMdError,
    parse_program_md,
)


def _write_program_md(tmp_path: Path, frontmatter: str, body: str = "Improve the prompt.\n") -> Path:
    p = tmp_path / "program.md"
    p.write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    return p


class TestParsesValidFrontmatter:
    def test_minimal_required_fields(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\n",
        )
        cfg, prose = parse_program_md(p)
        assert cfg.profile == "trader"
        assert cfg.prompt_file == "prompts/system.md"
        assert cfg.backend == "litellm"
        assert cfg.max_experiments == 100
        assert cfg.cost_cap_usd == Decimal("5.00")
        assert cfg.parallelism == 4
        assert "Improve" in prose

    def test_all_optional_fields_parsed(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            (
                "profile: trader\n"
                "prompt_file: prompts/system.md\n"
                "backend: claude_code\n"
                "coder_model: gpt-4o\n"
                "judge_model: gpt-4o-mini\n"
                "eval_suite: tests/fixtures/suite.json\n"
                "max_experiments: 50\n"
                "cost_cap_usd: '2.50'\n"
                "parallelism: 8\n"
            ),
        )
        cfg, prose = parse_program_md(p)
        assert cfg.backend == "claude_code"
        assert cfg.coder_model == "gpt-4o"
        assert cfg.judge_model == "gpt-4o-mini"
        assert cfg.max_experiments == 50
        assert cfg.cost_cap_usd == Decimal("2.50")
        assert cfg.parallelism == 8

    def test_prose_body_returned_verbatim(self, tmp_path: Path) -> None:
        body = "Research agenda: make the agent smarter.\n\nFocus on momentum signals."
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\n",
            body,
        )
        cfg, prose = parse_program_md(p)
        assert prose.strip() == body.strip()


class TestMissingRequiredKey:
    def test_missing_profile_raises_with_clear_message(self, tmp_path: Path) -> None:
        p = _write_program_md(tmp_path, "prompt_file: prompts/system.md\n")
        with pytest.raises(ProgramMdError, match="profile"):
            parse_program_md(p)

    def test_missing_prompt_file_raises_with_clear_message(self, tmp_path: Path) -> None:
        p = _write_program_md(tmp_path, "profile: trader\n")
        with pytest.raises(ProgramMdError, match="prompt_file"):
            parse_program_md(p)

    def test_missing_both_required_fields_raises(self, tmp_path: Path) -> None:
        p = _write_program_md(tmp_path, "backend: litellm\n")
        with pytest.raises(ProgramMdError):
            parse_program_md(p)


class TestUnknownKeys:
    def test_warns_on_unknown_keys_but_proceeds(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\nunknown_key: value\n",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg, prose = parse_program_md(p)
        # Should have issued a warning about the unknown key
        warning_messages = [str(w.message) for w in caught]
        assert any("unknown_key" in m for m in warning_messages), (
            f"Expected warning about unknown_key, got: {warning_messages}"
        )
        # But parsing succeeded
        assert cfg.profile == "trader"

    def test_multiple_unknown_keys_warns_and_proceeds(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\nalpha: 1\nbeta: 2\n",
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg, _ = parse_program_md(p)
        assert cfg.profile == "trader"


class TestMissingFrontmatter:
    def test_no_frontmatter_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "program.md"
        p.write_text("Just prose, no frontmatter.\n")
        with pytest.raises(ProgramMdError):
            parse_program_md(p)

    def test_unclosed_frontmatter_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "program.md"
        p.write_text("---\nprofile: trader\n")
        with pytest.raises(ProgramMdError):
            parse_program_md(p)


class TestProgramMdModel:
    def test_backend_literal_validation(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\nbackend: invalid\n",
        )
        with pytest.raises(ProgramMdError):
            parse_program_md(p)

    def test_cost_cap_decimal_precision(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\ncost_cap_usd: '0.01'\n",
        )
        cfg, _ = parse_program_md(p)
        assert cfg.cost_cap_usd == Decimal("0.01")

    def test_coder_model_none_by_default(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\n",
        )
        cfg, _ = parse_program_md(p)
        assert cfg.coder_model is None
        assert cfg.judge_model is None


class TestEvalModelField:
    def test_eval_model_field_parsed_when_present(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            (
                "profile: trader\n"
                "prompt_file: prompts/system.md\n"
                "eval_model: openrouter/anthropic/claude-haiku-4-5\n"
            ),
        )
        cfg, _ = parse_program_md(p)
        assert cfg.eval_model == "openrouter/anthropic/claude-haiku-4-5"

    def test_eval_model_defaults_to_none_when_absent(self, tmp_path: Path) -> None:
        p = _write_program_md(
            tmp_path,
            "profile: trader\nprompt_file: prompts/system.md\n",
        )
        cfg, _ = parse_program_md(p)
        assert cfg.eval_model is None
