"""
H9 — VPS parity (pytest-skipped by default).

Design-doc invariant (§8 + §10):
  The same H2/H3/H6/H7 gates that pass locally also pass on alfredon
  (the production VPS) running the A0 docker container.

This test is skipped unless AUTORESEARCH_VPS_TEST=1 is set.
Run manually via:
    AUTORESEARCH_VPS_TEST=1 pytest tests/acceptance/test_h9_vps_parity.py -v
Or use the companion script:
    bash scripts/run_h9_on_vps.sh
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[5]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_VPS_HOST = "alfredon"
_CONTAINER = "a0"
_PYTHON = "/opt/venv-a0/bin/python"
_PYTEST_CMD = (
    f"cd /opt/agent-zero && "
    f"docker compose exec -T {_CONTAINER} {_PYTHON} "
    f"-m pytest usr/plugins/autoresearch/tests/acceptance "
    f"-k 'h2 or h3 or h6 or h7' --tb=short -v"
)
_TIMEOUT_SECONDS = 600  # 10-minute cap per §8 H9


@pytest.mark.vps
@pytest.mark.acceptance
@pytest.mark.skipif(
    not os.environ.get("AUTORESEARCH_VPS_TEST"),
    reason="VPS test only runs when AUTORESEARCH_VPS_TEST=1 is set",
)
def test_h9_vps_parity():
    """
    Run H2/H3/H6/H7 acceptance gates on alfredon via SSH.

    Requires:
      - SSH alias 'alfredon' configured (see memory/vps_alfredon.md)
      - VPS running with docker compose up (a0 container healthy)
      - autoresearch plugin deployed on the VPS (code merged to main)
      - AUTORESEARCH_VPS_TEST=1 env var set

    Times out after 10 minutes.
    """
    result = subprocess.run(
        ["ssh", _VPS_HOST, _PYTEST_CMD],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
    )

    stdout = result.stdout
    stderr = result.stderr

    # Parse pytest output — look for the summary line
    # pytest exits 0 on all-pass, non-zero on failures
    if result.returncode != 0:
        pytest.fail(
            f"VPS parity check FAILED (returncode={result.returncode})\n"
            f"--- stdout ---\n{stdout}\n"
            f"--- stderr ---\n{stderr}"
        )

    # Confirm at least 4 gates passed (h2 + h3 + h6 + h7)
    # pytest summary line looks like: "N passed" or "X passed, Y failed"
    assert "failed" not in stdout.lower() or "0 failed" in stdout.lower(), (
        f"Some VPS gate tests failed:\n{stdout}"
    )
    assert "passed" in stdout.lower(), (
        f"No 'passed' found in pytest output — something went wrong:\n{stdout}"
    )

    # Extract pass count and verify at least 4 tests passed
    import re
    match = re.search(r"(\d+) passed", stdout)
    if match:
        passed_count = int(match.group(1))
        assert passed_count >= 4, (
            f"Expected ≥4 gate tests to pass on VPS, got {passed_count}:\n{stdout}"
        )
