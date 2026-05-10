from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from usr.plugins.autoresearch.state.runs import ExperimentRecord


@dataclass(frozen=True)
class EditPatch:
    target_path: Path           # must resolve under agents/<profile>/prompts/
    old_text: str               # must be unique in the target file
    new_text: str
    rationale: str


class Coder(Protocol):
    name: str                   # "litellm" | "claude_code"

    async def propose_edit(
        self,
        program_md: str,
        file_text: str,
        recent_history: list[ExperimentRecord],
    ) -> EditPatch: ...

    def report_cost(self) -> Decimal: ...
