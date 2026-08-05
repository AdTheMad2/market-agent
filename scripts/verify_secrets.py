"""Confirm every required secret is present, and that none has leaked into the tree.

Two independent checks:

  1. Every name in config/.env.example is set to a non-blank value in the environment.
  2. No value of any of those names appears in any git-tracked file.

Check 2 matters because this repository is public. Anything committed to it must be
assumed permanently public regardless of later deletion, so the moment to catch a leak
is before the commit, not after.

This script never prints a secret value. A leak report that echoes the leaked value is
itself a second leak — it reports the file path and the variable name only.

Run:  python scripts/verify_secrets.py
Exit: 0 clean, 1 missing names, 2 leak found (2 takes precedence).

See docs/SPEC.md §9.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._dotenv import load_env  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / "config" / ".env.example"

# Below this length a value matches too much of the tree to mean anything. A real
# API key is far longer; a short value here signals a placeholder, not a secret.
MIN_SCANNABLE_LENGTH = 8

# Declared in .env.example but not required to be set. Absence is reported as a
# note, never as a failure.
OPTIONAL_NAMES = frozenset({"TELEGRAM_TEST_CHAT_ID"})


def required_names(env_example: Path) -> list[str]:
    """Names declared in .env.example, in file order. Comments and blanks skipped."""
    names: list[str] = []
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.append(stripped.split("=", 1)[0].strip())
    return names


def missing_names(names: list[str], env: dict[str, str]) -> list[str]:
    """Names absent from the environment, or present but blank."""
    return [n for n in names if not env.get(n, "").strip()]


def tracked_files(repo_root: Path) -> list[Path]:
    """Every file git tracks. Untracked and ignored files cannot leak by being committed."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [repo_root / p for p in result.stdout.split("\0") if p]


def scan_files_for_values(
    files: list[Path], values: dict[str, str]
) -> list[tuple[Path, str]]:
    """Files containing a secret value, reported as (path, variable_name).

    The value never appears in the return. Values shorter than MIN_SCANNABLE_LENGTH
    are skipped as unmatchable noise. Unreadable or binary files are skipped rather
    than raising — a committed SQLite database is expected in this repository.
    """
    scannable = {
        name: value
        for name, value in values.items()
        if value and len(value.strip()) >= MIN_SCANNABLE_LENGTH
    }
    if not scannable:
        return []

    findings: list[tuple[Path, str]] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name, value in scannable.items():
            if value.strip() in content:
                findings.append((path, name))
    return findings


def main() -> int:
    load_env(REPO_ROOT / ".env")

    names = required_names(ENV_EXAMPLE)
    env = dict(os.environ)

    all_absent = missing_names(names, env)
    absent = [n for n in all_absent if n not in OPTIONAL_NAMES]
    absent_optional = [n for n in all_absent if n in OPTIONAL_NAMES]
    present = {n: env[n] for n in names if n not in all_absent}

    required_count = len([n for n in names if n not in OPTIONAL_NAMES])
    print(f"Checked {required_count} required names from {ENV_EXAMPLE.relative_to(REPO_ROOT)}")

    if absent:
        print(f"\n  MISSING ({len(absent)}): not set, or set but blank")
        for name in absent:
            print(f"    - {name}")
    else:
        print("  All required names are set.")

    if absent_optional:
        print(f"\n  Optional, not set ({len(absent_optional)}): falls back to a default")
        for name in absent_optional:
            print(f"    - {name}")

    files = tracked_files(REPO_ROOT)
    findings = scan_files_for_values(files, present)

    print(f"\nScanned {len(files)} tracked files against {len(present)} live values")

    if findings:
        print(f"\n  LEAK ({len(findings)}): a secret value appears in a tracked file")
        for path, name in findings:
            print(f"    - {path.relative_to(REPO_ROOT)} contains the value of {name}")
        print("\n  Rotate the exposed key before doing anything else. If it was ever")
        print("  committed, assume it is public and rotate regardless of whether the")
        print("  commit was pushed.")
        return 2

    print("  No secret value found in any tracked file.")

    return 1 if absent else 0


if __name__ == "__main__":
    sys.exit(main())
