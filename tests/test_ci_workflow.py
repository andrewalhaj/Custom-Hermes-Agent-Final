"""Prove each guard step in .github/workflows/ci.yml against its failing and passing cases (CONTRIBUTING.md C6).

Each test takes a step's `run` script from the workflow and runs it the way GitHub runs `shell: bash`
(`bash --noprofile --norc -eo pipefail`) inside a throwaway git repository built for the case.
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
    return result.returncode


NOQA_STEP = "no noqa that can silence the error-handling rules"
NOQA_CASES = [
    ("clean", {"a.py": CODE}, False),
    ("another rule only", {"a.py": f"import os  # {NOQA}: F401\n"}, False),
    ("names BLE001", {"a.py": f"x = 1  # {NOQA}: BLE001\n"}, True),
    ("names S110 in another case", {"a.py": f"x = 1  # {NOQA.upper()}: s110\n"}, True),
    ("lists E722 after another rule", {"a.py": f"x = 1  # {NOQA}: F401, E722\n"}, True),
    ("bare", {"a.py": f"x = 1  # {NOQA}\n"}, True),
    ("bare, no space, trailing text", {"a.py": f"x = 1  #{NOQA}  because\n"}, True),
    ("file-level ruff", {"a.py": f"# ruff: {NOQA}\nx = 1\n"}, True),
    ("file-level flake8", {"a.py": f"# flake8: {NOQA}\nx = 1\n"}, True),
]


@pytest.mark.parametrize("label,files,should_fail", NOQA_CASES, ids=[c[0] for c in NOQA_CASES])
def test_noqa_ban(tmp_path, label, files, should_fail):
    assert (run_in_repo(tmp_path, step_script(NOQA_STEP), files) != 0) == should_fail


def test_noqa_ban_fails_when_git_fails(tmp_path):
    script = step_script(NOQA_STEP).replace("git grep", "git --not-a-real-option grep")
    assert run_in_repo(tmp_path, script, {"a.py": CODE}) != 0


PROBE_STEP = "the error-handling rules are switched on"
PROBE_CASES = [
    ("configured", {"pyproject.toml": CONFIG}, False),
    ("ignore BLE001", {"pyproject.toml": CONFIG + 'ignore = ["BLE001"]\n'}, True),
    ("extend-ignore prefix E", {"pyproject.toml": CONFIG + 'extend-ignore = ["E"]\n'}, True),
    ("select replaces defaults", {"pyproject.toml": '[tool.ruff.lint]\nselect = ["F"]\n'}, True),
    ("top-level ignore E722", {"pyproject.toml": '[tool.ruff]\nignore = ["E722"]\n' + CONFIG}, True),
    ("root ruff.toml ignores S110",
     {"pyproject.toml": CONFIG, "ruff.toml": '[lint]\nextend-select = ["BLE001", "S110"]\nignore = ["S110"]\n'}, True),
]


@pytest.mark.parametrize("label,files,should_fail", PROBE_CASES, ids=[c[0] for c in PROBE_CASES])
def test_probe(tmp_path, label, files, should_fail):
    assert (run_in_repo(tmp_path, step_script(PROBE_STEP), files) != 0) == should_fail


def test_probe_fails_when_ruff_is_missing(tmp_path):
    script = step_script(PROBE_STEP).replace("| ruff check", "| /nonexistent/ruff check")
    assert run_in_repo(tmp_path, script, {"pyproject.toml": CONFIG}) != 0


COVERAGE_STEP = "ruff lints every tracked Python file"
COVERAGE_CASES = [
    ("every file, nested and tests", {"pyproject.toml": CONFIG, "a.py": CODE, "pkg/b.py": CODE,
                                      "tests/test_c.py": CODE}, False),
    ("include narrows to src", {"pyproject.toml": '[tool.ruff]\ninclude = ["src/**/*.py"]\n' + CONFIG,
                                "a.py": CODE, "src/b.py": CODE}, True),
    ("default exclude dist", {"pyproject.toml": CONFIG, "dist/y.py": CODE}, True),
    ("gitignore matches a tracked file", {"pyproject.toml": CONFIG, ".gitignore": "ignored.py\n",
                                          "a.py": CODE, "ignored.py": CODE}, True),
    ("extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG,
                        "a.py": CODE, "pkg/b.py": CODE}, True),
]


@pytest.mark.parametrize("label,files,should_fail", COVERAGE_CASES, ids=[c[0] for c in COVERAGE_CASES])
def test_coverage(tmp_path, label, files, should_fail):
    assert (run_in_repo(tmp_path, step_script(COVERAGE_STEP), files) != 0) == should_fail


def test_coverage_fails_when_ruff_is_missing(tmp_path):
    script = step_script(COVERAGE_STEP).replace('run("ruff",', 'run("/nonexistent/ruff",')
    assert run_in_repo(tmp_path, script, {"pyproject.toml": CONFIG, "a.py": CODE}) != 0


CONFIG_STEP = "ruff configuration lives in the root pyproject.toml and narrows nothing"
CONFIG_CASES = [
    ("root pyproject only", {"pyproject.toml": CONFIG, "a.py": CODE}, False),
    ("per-file ignore for another rule",
     {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["F401"]\n'}, False),
    ("nested ruff.toml", {"pyproject.toml": CONFIG, "pkg/ruff.toml": '[lint]\nignore = ["BLE001"]\n'}, True),
    ("nested .ruff.toml", {"pyproject.toml": CONFIG, "pkg/.ruff.toml": '[lint]\nignore = ["BLE001"]\n'}, True),
    ("nested pyproject.toml", {"pyproject.toml": CONFIG, "pkg/pyproject.toml": '[tool.ruff.lint]\nignore = ["E722"]\n'}, True),
    ("root ruff.toml", {"pyproject.toml": CONFIG, "ruff.toml": "line-length = 100\n"}, True),
    ("tool.ruff extend-exclude", {"pyproject.toml": '[tool.ruff]\nextend-exclude = ["pkg/"]\n' + CONFIG}, True),
    ("tool.ruff.lint exclude", {"pyproject.toml": CONFIG + 'exclude = ["x.py"]\n'}, True),
    ("per-file ignore S110", {"pyproject.toml": CONFIG + '[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["S110"]\n'}, True),
    ("extend-per-file ignore prefix E",
     {"pyproject.toml": CONFIG + '[tool.ruff.lint.extend-per-file-ignores]\n"x.py" = ["E"]\n'}, True),
    ("no pyproject.toml", {"a.py": CODE}, True),
]


@pytest.mark.parametrize("label,files,should_fail", CONFIG_CASES, ids=[c[0] for c in CONFIG_CASES])
def test_config_location(tmp_path, label, files, should_fail):
    assert (run_in_repo(tmp_path, step_script(CONFIG_STEP), files) != 0) == should_fail


def test_workflow_runs_on_pull_requests_and_main():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]  # YAML 1.1 reads a bare `on` key as True
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["push"]["branches"] == ["main"]
    commands = [s.get("run", "") for s in workflow["jobs"]["tests"]["steps"]]
    assert "ruff check ." in commands
    assert "python -m pytest -q" in commands
