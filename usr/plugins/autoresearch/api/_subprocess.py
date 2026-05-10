"""Subprocess spawn helpers — kept separate so /start handler stays small."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Per design §10 secrets posture: deny-list to prevent the worker subprocess
# from inheriting real exchange credentials. Eval tasks must use mocked clients.
SECRET_DENY_LIST = (
    "BIRDEYE_API_KEY",
    "HL_PRIVATE_KEY",
    "HL_API_KEY",
    "HL_ACCOUNT_ADDRESS",
    "BINANCE_API_KEY",
    "BINANCE_SECRET_KEY",
    "BINANCE_API_SECRET",
    "COINBASE_API_KEY",
    "COINBASE_API_SECRET",
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
    "BYBIT_API_KEY",
    "BYBIT_API_SECRET",
    "OKX_API_KEY",
    "OKX_API_SECRET",
    "BITFINEX_API_KEY",
    "BITFINEX_API_SECRET",
)


def filtered_env() -> dict[str, str]:
    """Return os.environ minus exchange-credential vars."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_DENY_LIST}


def find_repo_root(start: Path | None = None) -> Path:
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "agent.py").exists():
            return parent
    return Path.cwd()


def spawn_worker(
    run_id: str,
    program_md_path: str,
    repo_root: Path | None = None,
    env: dict[str, str] | None = None,
    popen: type[subprocess.Popen] = subprocess.Popen,
) -> subprocess.Popen:
    """Spawn the autoresearch worker as a detached subprocess.

    `start_new_session=True` so SIGTERM to the parent does not propagate. Stdout
    and stderr are discarded — the worker writes structured artifacts under
    `usr/autoresearch/runs/<run-id>/` instead.
    """
    cwd = repo_root if repo_root is not None else find_repo_root()
    cmd = [
        sys.executable,
        "-m",
        "usr.plugins.autoresearch.worker",
        "--run-id",
        run_id,
        "--program-md",
        program_md_path,
    ]
    return popen(
        cmd,
        cwd=str(cwd),
        env=env if env is not None else filtered_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
