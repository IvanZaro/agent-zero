from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path so `usr.plugins.autoresearch` imports resolve.
PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
