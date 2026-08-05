"""Tests for scripts/_dotenv.py."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._dotenv import load_env, parse_env


def test_parse_ignores_comments_and_blanks():
    assert parse_env("# c\n\nA=1\n") == {"A": "1"}


def test_parse_strips_matched_quotes():
    assert parse_env('A="hello"\nB=\'world\'\nC="unmatched\n') == {
        "A": "hello",
        "B": "world",
        "C": '"unmatched',
    }


def test_parse_keeps_equals_signs_inside_values():
    assert parse_env("TOKEN=abc=def==") == {"TOKEN": "abc=def=="}


def test_load_does_not_overwrite_the_real_environment(tmp_path, monkeypatch):
    """GitHub Actions secrets must win over a stray local .env."""
    monkeypatch.setenv("ALREADY_SET", "from-actions")
    env_file = tmp_path / ".env"
    env_file.write_text("ALREADY_SET=from-dotenv\nNEW_NAME=from-dotenv\n", encoding="utf-8")

    loaded = load_env(env_file)

    assert os.environ["ALREADY_SET"] == "from-actions"
    assert os.environ["NEW_NAME"] == "from-dotenv"
    assert loaded == 1


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "nope.env") == 0
