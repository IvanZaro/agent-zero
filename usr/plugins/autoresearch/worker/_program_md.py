from __future__ import annotations

import re
import warnings
from decimal import Decimal
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, model_validator


class ProgramMdError(ValueError):
    """Raised when program.md frontmatter is missing, malformed, or invalid."""


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)

_KNOWN_FIELDS = {
    "profile",
    "prompt_file",
    "backend",
    "coder_model",
    "eval_model",
    "judge_model",
    "eval_suite",
    "max_experiments",
    "cost_cap_usd",
    "parallelism",
}


class ProgramMd(BaseModel):
    profile: str
    prompt_file: str
    backend: Literal["litellm", "claude_code"] = "litellm"
    coder_model: str | None = None
    eval_model: str | None = None
    judge_model: str | None = None
    eval_suite: str = "tests/fixtures/suite.json"
    max_experiments: int = 100
    cost_cap_usd: Decimal = Decimal("5.00")
    parallelism: int = 4

    model_config = {"extra": "ignore"}


def parse_program_md(path) -> tuple[ProgramMd, str]:
    """Parse a program.md file, returning (ProgramMd, prose_body).

    Raises ProgramMdError on:
    - missing frontmatter delimiters
    - missing required keys (profile, prompt_file)
    - invalid field values (bad backend literal, etc.)

    Warns (UserWarning) on unknown keys, then ignores them.
    """
    from pathlib import Path as _Path
    raw = _Path(path).read_text(encoding="utf-8")

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        raise ProgramMdError(
            "program.md must start with YAML frontmatter between --- delimiters"
        )

    fm_text = m.group(1)
    prose = m.group(2).strip()

    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise ProgramMdError(f"program.md frontmatter YAML parse error: {exc}") from exc

    if not isinstance(fm_dict, dict):
        raise ProgramMdError("program.md frontmatter must be a YAML mapping")

    # Warn on unknown keys before Pydantic strips them
    unknown = set(fm_dict.keys()) - _KNOWN_FIELDS
    if unknown:
        for key in sorted(unknown):
            warnings.warn(
                f"program.md: unknown frontmatter key {key!r} — ignored",
                UserWarning,
                stacklevel=2,
            )

    try:
        cfg = ProgramMd.model_validate(fm_dict)
    except ValidationError as exc:
        # Translate Pydantic errors into ProgramMdError with clear messages
        errors = exc.errors()
        missing = [
            e["loc"][0]
            for e in errors
            if e["type"] == "missing"
        ]
        if missing:
            raise ProgramMdError(f"missing key: {', '.join(str(k) for k in missing)}") from exc
        raise ProgramMdError(f"program.md validation failed: {exc}") from exc

    return cfg, prose
