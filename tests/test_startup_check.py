"""Prove the startup check, `hermes custom-build check` (issue #48, ARCHITECTURE.md section 5.5).

Each case builds a throwaway Hermes home holding a copy of the plugin, a config.yaml and a .env, runs the
real `hermes` CLI there in a process of its own, and reads what the check wrote to `events` in the
throwaway store from the `db` fixture (conftest.py).
"""

import pathlib
import shutil
import subprocess
import sys

import psycopg
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
HERMES = "import sys; from hermes_cli.main import main; sys.argv = ['hermes', *sys.argv[1:]]; main()"


def safe_config():
    return {
        "memory": {"provider": "custom_build"},
        "plugins": {"enabled": ["custom_build"], "entries": {"custom_build": {"settings": {"machine_name": "host-a"}}}},
    }


def run_check(tmp_path, db, *, config=None, raw_config="", env=None, database_url=True, plugin_edit=None):
    """Run `hermes custom-build check` against a throwaway home; return (exit code, output, events rows)."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    if plugin_edit:
        init = home / "plugins" / "custom_build" / "__init__.py"
        init.write_text(plugin_edit(init.read_text(encoding="utf-8")), encoding="utf-8")
    (home / "config.yaml").write_text(yaml.safe_dump(config or safe_config()) + raw_config, encoding="utf-8")
    if database_url:
        (home / ".env").write_text(f"CUSTOM_BUILD_DATABASE_URL={db['agent']}\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", HERMES, "custom-build", "check"],
        capture_output=True, text=True, timeout=180, check=False, cwd=tmp_path,
        env={"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin", **(env or {})},
    )
    with psycopg.connect(db["owner"]) as conn:
        rows = conn.execute("select code, result, reason, source from memory.events order by id").fetchall()
    return result.returncode, result.stdout + result.stderr, rows


def test_a_safe_setup_passes(tmp_path, db):
    code, output, rows = run_check(tmp_path, db)
    assert code == 0, output
    assert rows == []


def _with(path, value):
    config = safe_config()
    node = config
    for key in path[:-1]:
        node = node.setdefault(key, {})
    node[path[-1]] = value
    return config


@pytest.mark.parametrize("label, kwargs, setting", [
    ("another memory provider", {"config": _with(("memory", "provider"), "holographic")}, "memory.provider"),
    ("approvals off, unquoted", {"raw_config": "approvals:\n  mode: off\n"}, "approvals.mode"),
    ("cron mode approve", {"config": _with(("approvals", "cron_mode"), "approve")}, "approvals.cron_mode"),
    ("single-query mode yes", {"config": _with(("approvals", "single_query_mode"), "yes")}, "approvals.single_query_mode"),
    ("unattended mode allow", {"config": _with(("approvals", "unattended_mode"), "allow")}, "approvals.unattended_mode"),
    ("compression not in place", {"config": _with(("compression", "in_place"), False)}, "compression.in_place"),
    ("yolo variable set", {"env": {"HERMES_YOLO_MODE": "1"}}, "HERMES_YOLO_MODE"),
])
def test_an_unsafe_setting_fails_with_config_unsafe(tmp_path, db, label, kwargs, setting):
    code, output, rows = run_check(tmp_path, db, **kwargs)
    assert code != 0, output
    assert [(c, r, s) for c, r, _, s in rows] == [("config.unsafe", "failed", "host-a")], output
    assert setting in rows[0][2]


def test_a_provider_that_fails_to_load_fails_with_guard_not_loaded(tmp_path, db):
    def break_provider_load(text):
        # No registration and no MemoryProvider subclass to fall back on: the loader returns nothing.
        for old, new in (("        ctx.register_memory_provider(CustomBuildProvider())", "        pass"),
                         ("class CustomBuildProvider(MemoryProvider):", "class CustomBuildProvider:")):
            assert old in text
            text = text.replace(old, new)
        return text

    code, output, rows = run_check(tmp_path, db, plugin_edit=break_provider_load)
    assert code != 0, output
    assert [(c, r) for c, r, _, _ in rows] == [("guard.not_loaded", "failed")], output


def test_a_missing_machine_name_fails_with_config_missing(tmp_path, db):
    config = safe_config()
    del config["plugins"]["entries"]
    code, output, rows = run_check(tmp_path, db, config=config)
    assert code != 0
    assert "config.missing" in output and "machine_name" in output
    assert rows == []   # nowhere to write: logged with events.write_failed instead
    assert "events.write_failed" in output


def test_a_missing_database_url_fails_with_config_missing(tmp_path, db):
    code, output, rows = run_check(tmp_path, db, database_url=False)
    assert code != 0
    assert "config.missing" in output and "CUSTOM_BUILD_DATABASE_URL" in output
    assert rows == []


def test_without_the_ordinary_load_the_check_does_not_exist(tmp_path, db):
    config = safe_config()
    config["plugins"]["enabled"] = []
    code, output, rows = run_check(tmp_path, db, config=config)
    assert code != 0, output   # the service does not start Hermes
    assert rows == []          # and nothing can be recorded: the absence check covers this host


def test_host_plugin_isolation_leaves_no_check_to_pass(tmp_path, db):
    # Under `host` isolation Hermes registers no subcommand for the plugin, so the check cannot run,
    # the service does not start Hermes, and nothing is recorded (ARCHITECTURE.md section 5.5).
    code, output, rows = run_check(tmp_path, db, config=_with(("plugins", "isolation"), "host"))
    assert code != 0, output
    assert "is not a `hermes` command" in output
    assert rows == []
