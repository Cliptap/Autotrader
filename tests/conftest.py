import sys
from pathlib import Path

# Make the repository root and scripts/ importable without packaging them.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
