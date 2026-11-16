"""Prove the allow-list .gitignore (CONTRIBUTING.md section 8).

A credential or runtime file placed in the working tree must not show as untracked, and nothing the
repository tracks or accepts may be hidden. `git check-ignore` answers for a path without the file
having to exist: it exits 0 when the path is ignored, 1 when it is not, and 128 when git itself fails.
"""

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

HIDDEN = [
    ".env",
    "secrets.json",
    "notes.md",
    "tests/.env",
    "tests/key.pem",
    ".github/workflows/token.txt",
    ".pytest_cache/README.md",
    ".ruff_cache/CACHEDIR.TAG",
    "tests/__pycache__/test_gitignore.cpython-312.pyc",
    "custom_hermes_agent.egg-info/PKG-INFO",
    ".venv/bin/python",
    "migrations/.env",
    "migrations/notes.md",
    "custom_build/memory/.env",
    "custom_build/memory/notes.md",
    "custom_build/memory/__pycache__/embedding.cpython-312.pyc",
    "custom_build/.env",
    "custom_build/__pycache__/failures.cpython-312.pyc",
    "custom_build/scripts/.env",
    "custom_build/scripts/notes.md",
    ".githooks/.env",
    ".githooks/notes.md",
    "custom_build/guards/.env",
    "custom_build/guards/notes.md",
    "custom_build/guards/__pycache__/write_gate.cpython-314.pyc",
]
VISIBLE = [
    "tests/test_new.py",
    ".github/workflows/new.yml",
    "migrations/0002_next.sql",
    "custom_build/memory/new_module.py",
    "custom_build/new_module.py",
    "custom_build/plugin.yaml",
    "custom_build/scripts/custom-build-new-job.py",
    "hermes-requirements.txt",
    ".githooks/pre-commit",
    "custom_build/guards/write_gate.py",
    "custom_build/guards/write_gate.toml",
]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=False, capture_output=True, text=True)


@pytest.mark.parametrize("path", HIDDEN)
def test_runtime_and_credential_files_are_ignored(path):
    result = git("check-ignore", "-q", "--no-index", path)
    assert result.returncode == 0, f"{path} would show as untracked: check-ignore exited {result.returncode} {result.stderr}"


@pytest.mark.parametrize("path", VISIBLE)
def test_new_tests_and_workflows_stay_visible(path):
    result = git("check-ignore", "-q", "--no-index", path)
    assert result.returncode == 1, f"{path} would be hidden from git: check-ignore exited {result.returncode} {result.stderr}"


def test_no_tracked_file_is_ignored():
    result = git("ls-files", "-ci", "--exclude-standard")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", f"tracked files the .gitignore hides:\n{result.stdout}"
