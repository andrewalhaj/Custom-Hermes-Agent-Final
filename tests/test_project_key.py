"""Prove the project key (issue #113, ARCHITECTURE.md section 2).

A fact's project is the normalised URL of the `origin` remote of the repository the session works in,
so it is the same on every machine; a workspace that is not a repository has none, and neither has a
repository whose origin is missing, a local path or a file URL (#112). Each test builds a throwaway repository with git itself.
"""

import subprocess

import pytest

from custom_build.memory import project


def _repo(path, origin=None):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


@pytest.mark.parametrize("origin", [
    "https://github.com/Owner/Repo.git",
    "https://github.com/Owner/Repo",
    "https://github.com/Owner/Repo/",
    "git@github.com:Owner/Repo.git",
    "ssh://git@github.com/Owner/Repo.git",
    "ssh://git@GitHub.com:22/Owner/Repo",
    "https://user:token@github.com/Owner/Repo.git",
])
def test_every_spelling_of_one_remote_gives_one_key(tmp_path, origin):
    assert project.key(_repo(tmp_path / "work", origin)) == "github.com/Owner/Repo"


def test_the_key_never_carries_credentials(tmp_path):
    key = project.key(_repo(tmp_path / "work", "https://someone:s3cret@example.org/team/tool.git"))
    assert key == "example.org/team/tool"


def test_a_subdirectory_of_the_repository_has_the_repository_key(tmp_path):
    root = _repo(tmp_path / "work", "git@github.com:Owner/Repo.git")
    (root / "src" / "deep").mkdir(parents=True)
    assert project.key(root / "src" / "deep") == "github.com/Owner/Repo"


def test_a_workspace_that_is_not_a_repository_has_no_project(tmp_path):
    (tmp_path / "plain").mkdir()
    assert project.key(tmp_path / "plain") is None


def test_a_repository_with_no_origin_has_no_project(tmp_path):
    assert project.key(_repo(tmp_path / "work")) is None


@pytest.mark.parametrize("origin", ["/srv/repo.git", "../other", "file:///srv/repo.git", "file://localhost/srv/repo.git"])
def test_a_repository_whose_origin_is_not_a_network_remote_has_no_project(tmp_path, origin):
    # #112 decided: such a repository stores its facts with no project, like one with no origin.
    assert project.key(_repo(tmp_path / "work", origin)) is None
