"""Minimal .env loader with no external dependencies.

Used by scanner.py, collector.py and scripts/*.py so that a single
``.env`` in the repository root configures the whole local environment.

Existing environment variables always win: the loader never overrides
values that the process already has (CI secrets, shell exports, etc.).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT_ENV_FILES = (
    ROOT / ".env",
    ROOT / "mcp" / "ccxt" / ".env",
)


def _parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # Strip trailing inline comments: "100  # per-order cap"
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    return value


def load_env(*paths: str | Path) -> list[Path]:
    """Load ``KEY=VALUE`` pairs from .env files without overriding the process.

    Returns the list of files that were actually read.
    """
    loaded: list[Path] = []
    for path in paths or DEFAULT_ENV_FILES:
        candidate = Path(path)
        if not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :]
            key, _, raw = line.partition("=")
            key = key.strip()
            if not key:
                continue
            os.environ.setdefault(key, _parse_value(raw))
        loaded.append(candidate)
    return loaded


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_float(name: str, default: float) -> float:
    try:
        return float(env(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(env(name, str(default))))
    except ValueError:
        return default
