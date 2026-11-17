"""Update one agent host to a proven commit, and put the previous commit back when the update fails.

Usage, from a checkout on the operator's machine: `python scripts/deploy.py <commit>` (OPERATIONS.md section 8;
issue #181). The steps run in order and the first one that fails stops the deploy:

1. refuse a commit that is not on `origin/main` with a green CI run;
2. read the store's migration history and the commit the host runs now, so nothing changes if either is unreadable;
3. apply the pending migrations, in file-name order, after the operator types `yes`;
4. install the commit on the host and run the startup check, then restart the gateway and smoke-test it;
5. on a failure in step 4, install the previous commit again, restart and smoke-test. Migrations stay applied:
   each one works with the plugin deployed before it (CONTRIBUTING.md rule C10).

Deployment values come from the environment, never from this repository (OPERATIONS.md section 1). The script
runs outside Hermes with the operator reading its output, so it records no failure code: every refusal and
failed step exits non-zero and names the step.
"""

import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.supabase.com"
REQUIRED = ("DEPLOY_REPO", "DEPLOY_SSH", "DEPLOY_HERMES", "DEPLOY_HERMES_HOME", "DEPLOY_UNIT", "DEPLOY_RESTART",
            "SUPABASE_PROJECT_REF", "SUPABASE_ACCESS_TOKEN")
COMMIT = re.compile(r"[0-9a-f]{40}")
INSTALL_TIMEOUT = 600   # a clone, and the plugin's packages when they change
RESTART_TIMEOUT = 660   # under the service a failing startup check waits 5 minutes of the unit's 10 (section 3)


class DeployError(Exception):
    """A refusal or a failed step. Its text starts with the step's name."""


def settings(env):
    missing = [name for name in REQUIRED if not env.get(name, "").strip()]
    if missing:
        raise DeployError("settings: not set in the environment: " + ", ".join(missing))
    return {name: env[name].strip() for name in REQUIRED}


def must(done, step):
    if done.returncode != 0:
        raise DeployError(f"{step}: exit {done.returncode}: {(done.stderr or done.stdout).strip()[-500:]}")
    return done.stdout


def proven(commit, run):
    if not COMMIT.fullmatch(commit):
        raise DeployError("commit: give the full 40-character hash of a commit on origin/main")
    must(run(["git", "fetch", "--quiet", "origin", "main"]), "git fetch")
    if run(["git", "merge-base", "--is-ancestor", commit, "origin/main"]).returncode != 0:
        raise DeployError(f"commit: {commit} is not on origin/main")
    runs = json.loads(must(run(["gh", "run", "list", "--workflow", "CI", "--commit", commit, "--json", "conclusion"]),
                           "CI"))
    if not runs or any(r["conclusion"] != "success" for r in runs):   # no run yet is not a pass
        raise DeployError(f"CI: no run of {commit} that only succeeded: {[r['conclusion'] for r in runs]}")


def history(cfg, http):
    rows = http("POST", f"{API}/v1/projects/{cfg['SUPABASE_PROJECT_REF']}/database/query/read-only",
                cfg["SUPABASE_ACCESS_TOKEN"], {"query": "select name from supabase_migrations.schema_migrations"})
    return {row["name"] for row in rows}


def pending_migrations(commit, cfg, run, http):
    """The (name, SQL) of each file in `migrations/` at the commit that the store's history does not list."""
    applied = history(cfg, http)
    files = sorted(must(run(["git", "ls-tree", "--name-only", commit, "migrations/"]), "migrations").split())
    return [(pathlib.PurePosixPath(path).stem, must(run(["git", "show", f"{commit}:{path}"]), "migrations"))
            for path in files if pathlib.PurePosixPath(path).stem not in applied]


def apply_migrations(pending, cfg, http, ask, out):
    if not pending:
        out("migrations: none pending")
        return []
    out("migrations pending, in the order they would be applied:")
    for name, _ in pending:
        out(f"  {name}")
    if ask(f"Apply these {len(pending)} to the store? A migration is never undone. Type yes: ") != "yes":
        raise DeployError("migrations: not applied, the answer was not yes")
    for name, sql in pending:
        http("POST", f"{API}/v1/projects/{cfg['SUPABASE_PROJECT_REF']}/database/migrations",
             cfg["SUPABASE_ACCESS_TOKEN"], {"query": sql, "name": name})
        if name not in history(cfg, http):
            raise DeployError(f"migrations: {name} was sent but the store's history does not list it")
        out(f"migrations: applied {name}")
    return [name for name, _ in pending]


def on_host(cfg, run, *words, timeout=120):
    return run(["ssh", cfg["DEPLOY_SSH"], shlex.join(words)], timeout=timeout)


def hermes(cfg, run, *words, timeout=120):
    return on_host(cfg, run, "env", f"HERMES_HOME={cfg['DEPLOY_HERMES_HOME']}", cfg["DEPLOY_HERMES"], *words,
                   timeout=timeout)


def installed_commit(cfg, run):
    """The commit Hermes pinned the plugin to (Hermes v0.23.1, `hermes_cli/plugins_cmd_git.py`)."""
    text = must(on_host(cfg, run, "cat", f"{cfg['DEPLOY_HERMES_HOME']}/plugins/.install-metadata.json"),
                "installed commit")
    entry = json.loads(text).get("custom_build", {})
    revision = entry.get("revision", "")
    if entry.get("pinned") is not True or not COMMIT.fullmatch(revision):
        raise DeployError("installed commit: the host has no install pinned to a full commit; "
                          "the first install is done by hand (OPERATIONS.md section 3)")
    return revision


def install(commit, cfg, run):
    must(hermes(cfg, run, "plugins", "install", f"{cfg['DEPLOY_REPO']}#custom_build", "--force", "--ref", commit,
                "--yes-deps", timeout=INSTALL_TIMEOUT), "install")
    must(hermes(cfg, run, "custom-build", "check"), "check")   # before any restart: a failed check under the service takes the host offline


def restart(cfg, run):
    must(run(shlex.split(cfg["DEPLOY_RESTART"]), timeout=RESTART_TIMEOUT), "restart")


def smoke(cfg, run):
    must(on_host(cfg, run, "systemctl", "is-active", "--quiet", cfg["DEPLOY_UNIT"]), "service active")
    must(hermes(cfg, run, "custom-build", "heartbeat"), "heartbeat")   # the new code loads and reaches the store


def update(commit, cfg, run):
    install(commit, cfg, run)
    restart(cfg, run)
    smoke(cfg, run)


def deploy(commit, env, run, http, ask, out):
    try:
        cfg = settings(env)
        proven(commit, run)
        pending = pending_migrations(commit, cfg, run, http)
        previous = installed_commit(cfg, run)
        applied = apply_migrations(pending, cfg, http, ask, out)
    except DeployError as exc:
        out(f"deploy refused, the host is unchanged: {exc}")
        return 1
    kept = f"migrations applied, which stay applied: {', '.join(applied) or 'none'}"
    try:
        update(commit, cfg, run)
    except DeployError as exc:
        out(f"deploy failed at {exc}")
        try:
            update(previous, cfg, run)
        except DeployError as back:
            out(f"ROLLBACK FAILED at {back}. The host is not confirmed on {previous}. {kept}")
            return 3
        out(f"rolled back: the host runs {previous} again and passed its smoke test. {kept}")
        return 2
    out(f"deployed: {previous} -> {commit}. Startup check, service and heartbeat passed. {kept}")
    return 0


def run(argv, timeout=120):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise DeployError(f"{argv[0]}: timed out after {timeout} seconds") from exc


def http(method, url, token, body=None):
    request = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "custom-build-deploy"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        path = urllib.parse.urlsplit(url).path
        raise DeployError(f"{method} {path}: HTTP {exc.code} {exc.read().decode(errors='replace')[:500]}") from exc


def main(argv):
    if len(argv) != 2:
        print("usage: python scripts/deploy.py <commit>", file=sys.stderr)
        return 64
    return deploy(argv[1], os.environ, run, http, input, print)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
