"""Load custom_build the way an agent host does (issue #45, ARCHITECTURE.md section 1).

Each scenario copies the plugin into a throwaway Hermes home and runs, in a process of its own (Hermes
keeps its plugin registry per process), Hermes v0.23.1's ordinary discovery and its memory-provider
loader. Needs Hermes importable from the interpreter that runs the tests (CONTRIBUTING.md 8.2).
"""

import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
FAKE_SECRET = "sb_secret_" + "Zq7" * 12
FAKE_PUBLISHABLE = "sb_publishable_" + "Kp4" * 12

SCENARIO = r"""
import json, sys
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from plugins.memory import load_memory_provider
from agent.memory_provider import MemoryProvider
from agent.redact import redact_sensitive_text

load_provider = sys.argv[1] == "provider"
discover_plugins()
entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
provider = load_memory_provider("custom_build") if load_provider else None
text = " ".join(sys.argv[2:])
print(json.dumps({
    "entry": entry,
    "provider": provider.name if isinstance(provider, MemoryProvider) else None,
    "redacted": redact_sensitive_text(text),
}))
"""


def load(tmp_path, *, enabled=True, provider=True, manifest_edit=None):
    """Run discovery (and the provider loader) against a home holding a copy of the plugin."""
    home = tmp_path / "home"
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"))
    if manifest_edit:
        manifest = home / "plugins" / "custom_build" / "plugin.yaml"
        manifest.write_text(manifest_edit(manifest.read_text(encoding="utf-8")), encoding="utf-8")
    config = "memory:\n  provider: custom_build\n"
    if enabled:
        config += "plugins:\n  enabled:\n    - custom_build\n"
    (home / "config.yaml").write_text(config, encoding="utf-8")
    env = {"HERMES_HOME": str(home), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-c", SCENARIO, "provider" if provider else "plain", FAKE_SECRET, FAKE_PUBLISHABLE],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def ordinary_load_ok(report):
    """The ordinary load happened, without an error, as the standalone plugin custom_build."""
    entry = report["entry"]
    return entry is not None and entry["error"] is None and entry["enabled"] and entry["kind"] == "standalone"


def test_ordinary_load_succeeds(tmp_path):
    report = load(tmp_path, provider=False)
    assert ordinary_load_ok(report), report["entry"]


def test_ordinary_load_registers_nothing_else_yet(tmp_path):
    entry = load(tmp_path, provider=False)["entry"]
    assert (entry["tools"], entry["hooks"], entry["commands"]) == (0, 0, 0)


def test_provider_load_returns_the_custom_build_provider(tmp_path):
    assert load(tmp_path)["provider"] == "custom_build"


def test_both_loads_together_succeed(tmp_path):
    report = load(tmp_path)
    assert ordinary_load_ok(report), report["entry"]
    assert report["provider"] == "custom_build"


def test_ordinary_load_registers_the_redaction_patterns(tmp_path):
    redacted = load(tmp_path, provider=False)["redacted"]
    assert FAKE_SECRET not in redacted and FAKE_PUBLISHABLE not in redacted


def test_provider_load_alone_registers_the_redaction_patterns(tmp_path):
    report = load(tmp_path, enabled=False)
    assert not ordinary_load_ok(report)
    assert FAKE_SECRET not in report["redacted"] and FAKE_PUBLISHABLE not in report["redacted"]


def test_redaction_is_not_on_without_the_plugin(tmp_path):
    # The failing case for the two tests above: stock Hermes leaves these key formats in the text.
    report = load(tmp_path, enabled=False, provider=False)
    assert FAKE_SECRET in report["redacted"] and FAKE_PUBLISHABLE in report["redacted"]


def test_a_manifest_without_kind_standalone_fails_the_load_check(tmp_path):
    def drop_kind(text):
        assert "kind: standalone" in text
        return text.replace("kind: standalone\n", "")

    assert not ordinary_load_ok(load(tmp_path, provider=False, manifest_edit=drop_kind))


DOCTOR = "import sys; from hermes_cli.main import main; sys.argv = ['hermes', *sys.argv[1:]]; main()"


def doctor(tmp_path, plugin_dir):
    """Run `hermes plugins doctor <dir> --ci` with a throwaway home; return its exit code and output."""
    env = {"HERMES_HOME": str(tmp_path / "doctor-home"), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", DOCTOR, "plugins", "doctor", str(plugin_dir), "--ci"],
                            capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120, check=False)
    return result.returncode, result.stdout + result.stderr


def test_doctor_passes_the_plugin(tmp_path):
    code, output = doctor(tmp_path, PLUGIN)
    assert code == 0, output
    assert "custom_build" in output and "standalone" in output


def test_doctor_fails_a_plugin_whose_register_raises(tmp_path):
    broken = tmp_path / "custom_build"
    shutil.copytree(PLUGIN, broken, ignore=shutil.ignore_patterns("__pycache__"))
    init = broken / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(
        "def register(ctx) -> None:\n", "def register(ctx) -> None:\n    raise RuntimeError('broken on purpose')\n"),
        encoding="utf-8")
    code, output = doctor(tmp_path, broken)
    assert code != 0, output
