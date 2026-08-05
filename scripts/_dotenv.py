"""Minimal .env loader.

Exists so that no script ever requires a secret to be typed on a command line —
session transcripts are permanent on-disk logs. See docs/SPEC.md §9.

Deliberately not python-dotenv: this is twenty lines, and a dependency that
handles secrets is a dependency worth not having.

Values already present in the real environment win, so GitHub Actions secrets
are never overwritten by a stray local .env.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_env(text: str) -> dict[str, str]:
    """Parse .env text. Ignores comments and blanks; strips matched surrounding quotes."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def load_env(path: Path) -> int:
    """Load `path` into os.environ without overwriting anything already set.

    Returns the number of names actually set. A missing file is not an error —
    in GitHub Actions there is no .env, and the secrets are already in the
    environment.
    """
    if not path.is_file():
        return 0
    loaded = 0
    for name, value in parse_env(path.read_text(encoding="utf-8")).items():
        if not os.environ.get(name, "").strip():
            os.environ[name] = value
            loaded += 1
    return loaded
