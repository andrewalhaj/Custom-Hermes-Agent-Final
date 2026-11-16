"""The project a fact belongs to (ARCHITECTURE.md section 2).

The key comes from the repository, so it is the same on every machine: the normalised URL of its
`origin` remote. A workspace that is not a repository has no project, and neither has a repository
whose `origin` is missing, a local path or a file URL (#112).
"""

import os
import subprocess
import urllib.parse


def key(cwd):
    """`host/path` of the `origin` remote of the repository holding `cwd`, or None.

    The scheme, any credentials, the port and a trailing `.git` are dropped, and the host is lowercased,
    so every spelling of one remote gives one key. A git failure other than "no repository" or "no
    origin" raises CalledProcessError: it is not an answer.
    """
    command = ["git", "-C", str(cwd), "remote", "get-url", "origin"]
    # git's messages are translated; the one read below is matched in English.
    result = subprocess.run(command, capture_output=True, text=True, check=False, env={**os.environ, "LC_ALL": "C"})
    if result.returncode == 0:
        return _normalise(result.stdout.strip())
    if result.returncode == 2 or "not a git repository" in result.stderr:   # 2: no remote by that name
        return None
    raise subprocess.CalledProcessError(result.returncode, command, result.stdout, result.stderr)


def _normalise(url):
    """`host/path`, or None for a remote that is not on a network: a local path or a file URL (#112)."""
    if "://" not in url:
        # git reads `[user@]host:path` as a remote only when a colon comes before any slash; else it is a path.
        if ":" not in url or "/" in url.split(":", 1)[0]:
            return None
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1]
    else:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname, parts.path   # hostname drops credentials and port, and lowercases
        if not host or parts.scheme == "file":   # git opens file://host/path as a local path
            return None
    return f"{host.lower()}/{path.strip('/').removesuffix('.git')}"
