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
# Shaped like Supabase's documented keys: 22 random characters, which can include "-", then "_" and an
# 8-character checksum. Fake values, assembled at run time so no key-shaped literal sits in the
# repository for a secret scanner to flag.
FAKE_BODY = "N7UNd0UgjKTV-Uodkm0Hg0_xSvQ7d3m"
FAKE_SECRET = "sb_" + "secret_" + FAKE_BODY
FAKE_PUBLISHABLE = "sb_" + "publishable_" + FAKE_BODY

SCENARIO = r"""
import json, sys
import plugins.memory
from hermes_cli.plugins_context import PluginContext

# Only register() may supply the provider: Hermes otherwise falls back to instantiating any
# MemoryProvider subclass in the module, which would hide a register() that skips it.
assert hasattr(plugins.memory, "_instantiate_subclass"), "Hermes renamed its provider fallback; re-check this patch"
plugins.memory._instantiate_subclass = lambda namespace: None
ordinary_provider_calls = []
_original = PluginContext.register_memory_provider
def _record(self, provider):
    ordinary_provider_calls.append(type(provider).__name__)
    return _original(self, provider)
PluginContext.register_memory_provider = _record

from hermes_cli.plugins import discover_plugins, get_plugin_manager
from agent.memory_provider import MemoryProvider
from agent.redact import redact_sensitive_text

load_provider = sys.argv[1] == "provider"
discover_plugins()
entry = next((p for p in get_plugin_manager().list_plugins() if p["name"] == "custom_build"), None)
provider = plugins.memory.load_memory_provider("custom_build") if load_provider else None
text = " ".join(sys.argv[2:])
print(json.dumps({
    "entry": entry,
    "provider": provider.name if isinstance(provider, MemoryProvider) else None,
    "ordinary_provider_calls": ordinary_provider_calls,
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


def test_ordinary_load_registers_one_hook_and_no_tools_slash_commands_or_middleware(tmp_path):
    entry = load(tmp_path, provider=False)["entry"]
    # the hook is the per-turn settings check on pre_llm_call (#68)
    assert (entry["tools"], entry["hooks"], entry["commands"], entry["middleware"]) == (0, 1, 0, 0)


def test_only_the_provider_load_registers_the_provider(tmp_path):
    report = load(tmp_path)
    assert report["provider"] == "custom_build"
    assert report["ordinary_provider_calls"] == []


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


def test_hermes_reads_the_plugins_packages_and_they_match_the_project():
    import tomllib

    from pm.plugin_declarations import read_python_declaration

    declaration = read_python_declaration(PLUGIN)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert declaration.installs_packages
    assert list(declaration.install_requirements) == project
