"""Make the fixture's local ``src`` package win over the host repository."""

import sys
from pathlib import Path


FIXTURE_ROOT = Path(__file__).resolve().parents[1]
if str(FIXTURE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIXTURE_ROOT))
