"""Put `check.py` on the path so `python -m pytest scripts/agent-surface -q` works from the repo root."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
