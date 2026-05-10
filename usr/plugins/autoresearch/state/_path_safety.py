from __future__ import annotations

from pathlib import Path


class EditablePathError(ValueError):
    """Raised when a proposed edit path violates containment rules.

    Used by the API layer (S8) to reject a malicious program.md before the
    worker is ever spawned — fail fast, never touch the filesystem.
    """


def validate_editable_path(path: Path, profile_root: Path) -> Path:
    """Assert that `path` is a safe, editable file under profile_root/prompts/.

    Rules (all must hold):
    1. Resolved path must sit strictly inside profile_root/prompts/ — not at
       its boundary, not above it, not in a sibling directory.
    2. If the path contains a symlink, the real resolved destination must also
       sit inside profile_root/prompts/ (no symlink escape).

    Returns the resolved absolute Path on success.
    Raises EditablePathError with a descriptive message on any violation.
    """
    resolved_profile = profile_root.resolve()
    prompts_root = resolved_profile / "prompts"

    # Resolve without strict=True first so we can give a useful message even
    # for paths that don't yet exist; use strict=True only for symlink checking.
    resolved_path = path.resolve()

    # Rule 1: must be strictly under prompts_root (not equal to it).
    try:
        resolved_path.relative_to(prompts_root)
    except ValueError:
        raise EditablePathError(
            f"Path '{resolved_path}' is not under the required prompts directory "
            f"'{prompts_root}'. Only files inside <profile>/prompts/ are editable."
        )

    if resolved_path == prompts_root:
        raise EditablePathError(
            f"Path '{resolved_path}' points to the prompts directory itself, "
            "not a file inside it."
        )

    # Rule 2: symlink escape check — resolve strictly (follows every link) and
    # compare again.  If the path doesn't exist yet, strict resolve will raise
    # FileNotFoundError which we treat as safe (no symlink to follow).
    try:
        strict_resolved = Path(path).resolve(strict=True)
        try:
            strict_resolved.relative_to(prompts_root)
        except ValueError:
            raise EditablePathError(
                f"Path '{path}' resolves via symlink to '{strict_resolved}', "
                f"which is outside the prompts directory '{prompts_root}'."
            )
    except FileNotFoundError:
        pass  # File does not exist yet; no symlink to escape through.

    return resolved_path
