"""Prove each guard step in .github/workflows/ci.yml against its failing and passing cases (CONTRIBUTING.md C6).

Each test takes a step's `run` script from the workflow and runs it the way GitHub runs `shell: bash`
(`bash --noprofile --norc -eo pipefail`) inside a throwaway git repository built for the case.
A failing case passes only if the step fails with the message that names its cause, so a step that
breaks for some other reason cannot pass a test by accident.
Needs `git`, `ruff` and `python` on PATH, as the CI job has them.
"""

import pathlib
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


def test_workflow_runs_on_pull_requests_and_main():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 reads a bare `on` key as True
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    commands = [s.get("run", "") for s in workflow["jobs"]["tests"]["steps"]]
    assert "ruff check ." in commands
    assert "python -m pytest -q" in commands


def test_tests_get_a_postgres_17_to_apply_migrations_to():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["tests"]
    service = job["services"]["postgres"]
    assert service["image"].startswith("postgres:17")
    assert "5432:5432" in service["ports"]
    assert job["env"]["TEST_DATABASE_URL"].startswith("postgresql://postgres:")
    assert job["env"]["TEST_DATABASE_URL"].endswith("@localhost:5432/postgres")
