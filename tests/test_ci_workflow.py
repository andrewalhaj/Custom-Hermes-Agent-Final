"""Prove each guard step in .github/workflows/ci.yml against its failing and passing cases (CONTRIBUTING.md C6).

Each test takes a step's `run` script from the workflow and runs it the way GitHub runs `shell: bash`
(`bash --noprofile --norc -eo pipefail`) inside a throwaway git repository built for the case.
A failing case passes only if the step fails with the message that names its cause, so a step that
breaks for some other reason cannot pass a test by accident.
Needs `git`, `ruff` and `python` on PATH, as the CI job has them.
"""

import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

WORKFLOW = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"

NOQA = "no" + "qa"  # kept apart so the noqa ban does not match this file
CONFIG = '[tool.ruff.lint]\nextend-select = ["BLE001", "S110"]\n'
CODE = "x = 1\n"


def step_script(name):
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    matches = [s for s in steps if s.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r} in {WORKFLOW}"
    assert matches[0].get("shell") == "bash", f"step {name!r} must set shell: bash, so pipefail applies"
    return matches[0]["run"]


def run_in_repo(tmp_path, script, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        subprocess.run(["git", "-C", str(tmp_path), "add", "-f", name], check=True)
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                            check=False, cwd=tmp_path, capture_output=True, text=True)  # the exit code is the result
    return result.returncode, result.stdout + result.stderr


def check(tmp_path, script, files, expect):
    """expect is None for a case that must pass, or the message a failing case must print."""
    rc, output = run_in_repo(tmp_path, script, files)
    if expect is None:
        assert rc == 0, output
    else:
        assert rc != 0, output
        assert expect in output, output


NOQA_STEP = "no noqa that can silence the error-handling rules"
NOQA_CASES = [
    ("clean", {"a.py": CODE}, None),
    ("another rule only", {"a.py": f"import os  # {NOQA}: F401\n"}, None),
    ("names BLE001", {"a.py": f"x = 1  # {NOQA}: BLE001\n"}, "git grep exited 0"),
    ("names S110 in another case", {"a.py": f"x = 1  # {NOQA.upper()}: s110\n"}, "git grep exited 0"),
    ("lists E722 after another rule", {"a.py": f"x = 1  # {NOQA}: F401, E722\n"}, "git grep exited 0"),
    ("bare", {"a.py": f"x = 1  # {NOQA}\n"}, "git grep exited 0"),
    ("bare, no space, trailing text", {"a.py": f"x = 1  #{NOQA}  because\n"}, "git grep exited 0"),
    ("file-level ruff", {"a.py": f"# ruff: {NOQA}\nx = 1\n"}, "git grep exited 0"),
    ("file-level flake8", {"a.py": f"# flake8: {NOQA}\nx = 1\n"}, "git grep exited 0"),
]


@pytest.mark.parametrize("label,files,expect", NOQA_CASES, ids=[c[0] for c in NOQA_CASES])
def test_noqa_ban(tmp_path, label, files, expect):
    check(tmp_path, step_script(NOQA_STEP), files, expect)


def test_noqa_ban_fails_when_git_fails(tmp_path):
    script = step_script(NOQA_STEP).replace("git grep -n", "git --not-a-real-option grep -n")
    check(tmp_path, script, {"a.py": CODE}, "git grep exited 129")


# ruff 0.17.4 honours more suppression forms than the pattern above knows (#246), so a second step asks ruff
# itself: with every suppression comment ignored, none of the three rules may fire anywhere.
UNSUPPRESSED_STEP = "no broad catch once every suppression comment is ignored"
BROAD = {"BLE001": "try:\n    x = 1\nexcept Exception:{line}\n    x = 2\n",
         "S110": "try:\n    x = 1\nexcept Exception:{line}\n    pass{line}\n",
         "E722": "try:\n    x = 1\nexcept:{line}\n    x = 2\n"}
LINE_FORMS = [f"# {NOQA} : {{rule}}", "# ruff: ignore[{rule}]"]
FILE_FORMS = ["# ruff: disable[{rule}]", "# ruff: file-ignore[{rule}]", f"# ruff : {NOQA}", f"# flake8 : {NOQA}"]
UNSUPPRESSED_CASES = [("clean", {"pyproject.toml": CONFIG, "a.py": CODE}, None),
                      ("another rule suppressed", {"pyproject.toml": CONFIG, "a.py": f"import os  # {NOQA}: F401\n"}, None)]
UNSUPPRESSED_CASES += [
    ("a nested file", {"pyproject.toml": CONFIG, "a.py": CODE,
                       "pkg/sub/b.py": "# ruff: file-ignore[BLE001]\n" + BROAD["BLE001"].format(line="")}, ": BLE001 "),
    ("ruff cannot read its configuration", {"pyproject.toml": "[tool.ruff.lint]\nselect = 1\n", "a.py": CODE},
     "ruff exited 2"),
]
for rule, body in BROAD.items():
    UNSUPPRESSED_CASES += [(f"{rule} under {form}", {"pyproject.toml": CONFIG, "a.py": body.format(
        line="  " + form.format(rule=rule))}, f": {rule} ") for form in LINE_FORMS]
    UNSUPPRESSED_CASES += [(f"{rule} under {form}", {"pyproject.toml": CONFIG, "a.py": form.format(rule=rule) + "\n"
                            + body.format(line="")}, f": {rule} ") for form in FILE_FORMS]


@pytest.mark.parametrize("label,files,expect", UNSUPPRESSED_CASES, ids=[c[0] for c in UNSUPPRESSED_CASES])
def test_no_broad_catch_survives_under_a_suppression_comment(tmp_path, label, files, expect):
    check(tmp_path, step_script(UNSUPPRESSED_STEP), files, expect)


def test_the_unsuppressed_lint_fails_when_ruff_is_missing(tmp_path):
    script = step_script(UNSUPPRESSED_STEP).replace("ruff check", "/nonexistent/ruff check")
    check(tmp_path, script, {"pyproject.toml": CONFIG, "a.py": CODE}, "ruff exited 127")


PROBE_STEP = "the error-handling rules are switched on"
PROBE_CASES = [
    ("configured", {"pyproject.toml": CONFIG}, None),
    ("ignore BLE001", {"pyproject.toml": CONFIG + 'ignore = ["BLE001"]\n'}, "ruff no longer reports BLE001"),
    ("extend-ignore prefix E", {"pyproject.toml": CONFIG + 'extend-ignore = ["E"]\n'}, "ruff no longer reports E722"),
    ("select replaces defaults", {"pyproject.toml": '[tool.ruff.lint]\nselect = ["F"]\n'}, "ruff exited 0"),
    ("top-level ignore E722", {"pyproject.toml": '[tool.ruff]\nignore = ["E722"]\n' + CONFIG},
     "ruff no longer reports E722"),
    ("root ruff.toml ignores S110",
     {"pyproject.toml": CONFIG, "ruff.toml": '[lint]\nextend-select = ["BLE001", "S110"]\nignore = ["S110"]\n'},
     "ruff no longer reports S110"),
]


@pytest.mark.parametrize("label,files,expect", PROBE_CASES, ids=[c[0] for c in PROBE_CASES])
def test_probe(tmp_path, label, files, expect):
    check(tmp_path, step_script(PROBE_STEP), files, expect)


def test_probe_fails_when_ruff_is_missing(tmp_path):
    script = step_script(PROBE_STEP).replace("| ruff check", "| /nonexistent/ruff check")
    check(tmp_path, script, {"pyproject.toml": CONFIG}, "ruff exited 127")


COVERAGE_STEP = "ruff lints every tracked Python file"
COVERAGE_CASES = [
    ("every file, nested and tests", {"pyproject.toml": CONFIG, "a.py": CODE, "pkg/b.py": CODE,
                                      "tests/test_c.py": CODE}, None),
    ("include narrows to src", {"pyproject.toml": '[tool.ruff]\ninclude = ["src/**/*.py"]\n' + CONFIG,
                                "a.py": CODE, "src/b.py": CODE}, "does not lint:\na.py"),
    ("default exclude dist", {"pyproject.toml": CONFIG, "dist/y.py": CODE}, "does not lint:\ndist/y.py"),
    ("gitignore matches a tracked file", {"pyproject.toml": CONFIG, ".gitignore": "ignored.py\n",
                                          "a.py": CODE, "ignored.py": CODE}, "does not lint:\nignored.py"),
    ("extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG,
                        "a.py": CODE, "pkg/b.py": CODE}, "does not lint:\npkg/b.py"),
]


@pytest.mark.parametrize("label,files,expect", COVERAGE_CASES, ids=[c[0] for c in COVERAGE_CASES])
def test_coverage(tmp_path, label, files, expect):
    check(tmp_path, step_script(COVERAGE_STEP), files, expect)


@pytest.mark.parametrize("tool,old", [("ruff", 'run("ruff",'), ("git", 'run("git",')])
def test_coverage_fails_when_a_tool_is_missing(tmp_path, tool, old):
    script = step_script(COVERAGE_STEP).replace(old, f'run("/nonexistent/{tool}",')
    check(tmp_path, script, {"pyproject.toml": CONFIG, "a.py": CODE}, f"/nonexistent/{tool}")


CONFIG_STEP = "ruff configuration lives in the root pyproject.toml and narrows nothing"
OUTSIDE = "ruff configuration outside the root pyproject.toml: "
CONFIG_CASES = [
    ("root pyproject only", {"pyproject.toml": CONFIG, "a.py": CODE}, None),
    ("per-file ignore for another rule",
     {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["F401"]\n'}, None),
    ("nested ruff.toml", {"pyproject.toml": CONFIG, "pkg/ruff.toml": '[lint]\nignore = ["BLE001"]\n'},
     OUTSIDE + "pkg/ruff.toml"),
    ("nested .ruff.toml", {"pyproject.toml": CONFIG, "pkg/.ruff.toml": '[lint]\nignore = ["BLE001"]\n'},
     OUTSIDE + "pkg/.ruff.toml"),
    ("nested pyproject.toml", {"pyproject.toml": CONFIG, "pkg/pyproject.toml": '[tool.ruff.lint]\nignore = ["E722"]\n'},
     OUTSIDE + "pkg/pyproject.toml"),
    ("root ruff.toml", {"pyproject.toml": CONFIG, "ruff.toml": "line-length = 100\n"}, OUTSIDE + "ruff.toml"),
    ("tool.ruff extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG},
     "extend-exclude: pkg/"),
    ("tool.ruff.lint exclude", {"pyproject.toml": CONFIG + 'exclude = ["x.py"]\n'}, "exclude: x.py"),
    ("per-file ignore S110", {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["S110"]\n'},
     "per-file-ignores: tests/*: S110"),
    ("per-file ignore ALL", {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["ALL"]\n'},
     "per-file-ignores: tests/*: ALL"),
    ("extend-per-file ignore prefix E",
     {"pyproject.toml": CONFIG + '[tool.ruff.lint.extend-per-file-ignores]\n"x.py" = ["E"]\n'},
     "extend-per-file-ignores: x.py: E"),
    ("no pyproject.toml", {"a.py": CODE}, "no pyproject.toml at the repository root"),
]


@pytest.mark.parametrize("label,files,expect", CONFIG_CASES, ids=[c[0] for c in CONFIG_CASES])
def test_config_location(tmp_path, label, files, expect):
    check(tmp_path, step_script(CONFIG_STEP), files, expect)


def test_config_location_fails_when_git_is_missing(tmp_path):
    script = step_script(CONFIG_STEP).replace('["git", "ls-files"', '["/nonexistent/git", "ls-files"')
    check(tmp_path, script, {"pyproject.toml": CONFIG}, "/nonexistent/git")


# The secret scan (CONTRIBUTING.md section 8, W13; #84): gitleaks in CI over the whole history, and as a
# pre-commit hook over what is staged. Needs gitleaks on PATH, as the CI job installs it.

GITLEAKS_VERSION = "8.30.1"
HOOK = WORKFLOW.parent.parent.parent / ".githooks" / "pre-commit"
SCAN_STEP = "no secret in the repository's history"
TOKEN_FILE = "leaky.py"   # the redacted findings must name it


def fake_token():
    # A GitHub token's shape, put together at run time so no key-shaped literal sits in the repository.
    return "gh" + "p_" + "R8vXq2LmT5wZ9kPa3Nc7Jd1Hf6Gs4Bt0Yu2E"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                   check=True, capture_output=True)


def committed_repo(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, body in files.items():
        (tmp_path / name).write_text(body)
        git(tmp_path, "add", "-f", name)
    git(tmp_path, "commit", "-q", "-m", "x")


def merge_brings_in_a_token(tmp_path):
    # The merge commit adds a file neither parent has; plain `git log -p` shows merges without a diff.
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    for branch, name in (("main", "a.py"), ("side", "b.py"), ("main", "c.py")):
        if branch == "side":
            git(tmp_path, "switch", "-q", "-c", "side")
        elif name == "c.py":
            git(tmp_path, "switch", "-q", "main")
        (tmp_path / name).write_text(CODE)
        git(tmp_path, "add", name)
        git(tmp_path, "commit", "-q", "-m", name)
    git(tmp_path, "merge", "-q", "--no-commit", "--no-ff", "side")
    (tmp_path / TOKEN_FILE).write_text(f'token = "{fake_token()}"\n')
    git(tmp_path, "add", TOKEN_FILE)
    git(tmp_path, "commit", "-q", "-m", "merge")


def run_scan(cwd, path=None):
    env = None if path is None else {"PATH": path, "HOME": str(cwd)}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step_script(SCAN_STEP)],
                            check=False, cwd=cwd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout + result.stderr


def test_the_scan_passes_a_clean_history(tmp_path):
    committed_repo(tmp_path, {"a.py": CODE})
    code, output = run_scan(tmp_path)
    assert code == 0, output


def test_the_scan_refuses_a_committed_token_and_names_its_file_redacted(tmp_path):
    committed_repo(tmp_path, {TOKEN_FILE: f'token = "{fake_token()}"\n'})
    code, output = run_scan(tmp_path)
    assert code != 0 and "gitleaks found a secret" in output, output
    assert TOKEN_FILE in output and fake_token() not in output


def test_the_scan_refuses_a_token_a_merge_commit_brings_in(tmp_path):
    merge_brings_in_a_token(tmp_path)
    code, output = run_scan(tmp_path)
    assert code != 0 and TOKEN_FILE in output, output
    assert fake_token() not in output


def test_the_scan_fails_when_git_cannot_read_the_history(tmp_path):
    # gitleaks itself exits 0 when git fails; that is no scan, not a clean history.
    code, output = run_scan(tmp_path)   # not a repository
    assert code != 0 and "reported an error" in output, output


def test_the_scan_fails_when_gitleaks_is_missing(tmp_path):
    committed_repo(tmp_path, {"a.py": CODE})
    code, output = run_scan(tmp_path, path="/usr/bin:/bin")
    assert code != 0 and "gitleaks is not installed" in output, output


def test_ci_installs_a_pinned_gitleaks_checked_against_its_checksum_before_the_tests():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    install = step_script(f"install gitleaks {GITLEAKS_VERSION}, checked against its published checksum")
    assert f"v{GITLEAKS_VERSION}/gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz" in install
    assert "sha256sum -c" in install
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout.get("with", {}).get("fetch-depth") == 0   # the scan reads the whole history
    commands = [s.get("run", "") for s in steps]
    assert commands.index(install) < commands.index("python -m pytest -q")


def run_hook(cwd, path, files=None):
    if files is not None:
        subprocess.run(["git", "init", "-q", str(cwd)], check=True)
        for name, body in files.items():
            (cwd / name).write_text(body)
            git(cwd, "add", "-f", name)
    result = subprocess.run([str(HOOK)], check=False, cwd=cwd, capture_output=True, text=True,
                            env={"PATH": path, "HOME": str(cwd)})
    return result.returncode, result.stdout + result.stderr


def gitleaks_path():
    found = shutil.which("gitleaks")
    assert found, "gitleaks is not on PATH; install it as CONTRIBUTING.md 4.4 says"
    return f"{pathlib.Path(found).parent}:/usr/bin:/bin"


def test_the_hook_refuses_a_staged_token_and_names_its_file_redacted(tmp_path):
    code, output = run_hook(tmp_path, gitleaks_path(), {TOKEN_FILE: f'token = "{fake_token()}"\n'})
    assert code != 0 and "gitleaks found a secret" in output, output
    assert TOKEN_FILE in output and fake_token() not in output


def test_the_hook_passes_a_clean_commit(tmp_path):
    code, output = run_hook(tmp_path, gitleaks_path(), {"a.py": CODE})
    assert code == 0, output


def test_the_hook_refuses_when_gitleaks_is_missing(tmp_path):
    code, output = run_hook(tmp_path, "/usr/bin:/bin", {"a.py": CODE})
    assert code != 0 and "gitleaks is not installed" in output, output


def test_the_hook_refuses_when_git_cannot_read_what_is_staged(tmp_path):
    code, output = run_hook(tmp_path, gitleaks_path())   # not a repository
    assert code != 0 and "reported an error" in output, output


def test_workflow_runs_on_pull_requests_and_main():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 reads a bare `on` key as True
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    commands = [s.get("run", "") for s in workflow["jobs"]["tests"]["steps"]]
    assert "ruff check ." in commands
    assert "python -m pytest -q" in commands


# The store runs Postgres 17.6 with pgvector 0.8.0 (read from the Supabase project on 2026-11-16; #96).
PGVECTOR_IMAGE = "pgvector/pgvector:0.8.0-pg17@sha256:40b404964359299eefdd5f8518facf1886c562848cf4de13b6eaf91cb70c2b87"


def test_tests_get_a_postgres_17_with_pgvector_to_apply_migrations_to():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]
    service = job["services"]["postgres"]
    assert service["image"] == PGVECTOR_IMAGE
    assert "5432:5432" in service["ports"]
    assert job["env"]["TEST_DATABASE_URL"].startswith("postgresql://postgres:")
    assert job["env"]["TEST_DATABASE_URL"].endswith("@localhost:5432/postgres")


HERMES_COMMIT = "3b421578588243f920886e6fe79a6faa87ea026e"   # tag v0.23.1


def test_tests_run_on_hermes_v0_23_1_and_its_python():
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    python = next(s for s in steps if s.get("uses", "").startswith("actions/setup-python"))
    assert python["with"]["python-version"] == "3.14"
    script = step_script("install Hermes v0.23.1 from its own lock file")
    assert HERMES_COMMIT in script
    assert "pip install --no-deps -r hermes-requirements.txt" in script
    commands = [s.get("run", "") for s in steps]
    assert commands.index(script) < commands.index("python -m pytest -q")


def test_dependencies_are_audited_once_everything_is_installed():
    # CONTRIBUTING.md 8.2: `pip-audit` with no arguments audits the whole environment the tests ran in (#36).
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    commands = [s.get("run", "") for s in steps]
    assert "pip-audit" in commands
    assert commands.index('pip install -e ".[dev]"') < commands.index("pip-audit")


def unpinned_actions(directory):
    """Each `uses:` in the directory's workflow files, on a step or on a whole job, that is not a full commit SHA."""
    found = []
    for path in sorted(directory.glob("*.yml")):
        for job in yaml.safe_load(path.read_text())["jobs"].values():
            for used in [job.get("uses"), *(step.get("uses") for step in job.get("steps", []))]:
                if used and not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", used):
                    found.append(f"{path.name}: {used}")
    return found


def test_every_action_is_pinned_to_a_full_commit_sha():
    # GitHub's secure-use reference: a tag can be moved to other code, a full-length commit SHA cannot (#176).
    steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]["steps"]
    assert any("uses" in step for step in steps)    # there is something to check
    assert unpinned_actions(WORKFLOW.parent) == []


def test_dependabot_keeps_the_pinned_actions_current():
    # A pinned SHA never moves by itself, so Dependabot proposes each new release as a pull request (#176).
    config = yaml.safe_load((WORKFLOW.parent.parent / "dependabot.yml").read_text())
    assert config["version"] == 2
    actions = next(u for u in config["updates"] if u["package-ecosystem"] == "github-actions")
    assert actions["directory"] == "/"
    assert actions["schedule"]["interval"] == "weekly"
    # Its pull request titles become commits on main, which carry a conventional prefix (CONTRIBUTING.md W4, 4.8).
    assert actions["commit-message"]["prefix"] == "ci"


def test_the_pin_check_reads_every_workflow_file(tmp_path):
    # The allow-list admits any .github/workflows/*.yml, so a second workflow must be held to the same pin (#183).
    pinned = "name: a\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@" + "3" * 40 + "\n"
    (tmp_path / "ci.yml").write_text(pinned)
    (tmp_path / "release.yml").write_text(
        "name: b\njobs:\n  b:\n    steps:\n      - uses: actions/checkout@v7\n      - run: echo ok\n"
        "  c:\n    uses: owner/repo/.github/workflows/shared.yml@main\n")
    assert unpinned_actions(tmp_path) == [
        "release.yml: actions/checkout@v7", "release.yml: owner/repo/.github/workflows/shared.yml@main"]
