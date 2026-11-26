import sys
from pathlib import Path

# Make the repository root importable from tests without packaging it.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
