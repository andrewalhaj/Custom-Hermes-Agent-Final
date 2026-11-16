"""The write gate, in log mode (ARCHITECTURE.md sections 5.3, 5.4 and 5.6; #107).

A `pre_tool_call` hook. A call that matches a rule in write_gate.toml is a sensitive action. In `log`
mode the gate records what `on` would do with the call, and blocks nothing: `write_gate.would_block`
when `on` would block it outright (yolo, an approval bypass, or an unattended approval mode that reads
as approve), and `write_gate.would_ask` otherwise. `off` registers no hook.
"""

import os
import pathlib
import re
import tomllib

from .. import failures, settings
from ..startup_check import UNATTENDED_MODES

CONFIG = pathlib.Path(__file__).with_suffix(".toml")
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
FILE_TOOLS = ("write_file", "patch")   # matched by the paths Hermes resolves for them
TERMINAL = "terminal"   # matched by pattern
MODES = ("log", "off")   # `on` arrives with enforcement (ARCHITECTURE.md 5.4 item 6)


def load_config(path=CONFIG):
    """(mode, rules) from the configuration file: rules maps each name to (path, compiled command)."""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config["mode"] not in MODES:
        raise ValueError(f"write gate: mode {config['mode']!r} is not one of {MODES}; on is not built yet")
    rules = {name: (rule.get("path"), re.compile(rule["command"]) if "command" in rule else None)
             for name, rule in config["rules"].items()}
    return config["mode"], rules


def find_rule(rules, tool_name, args, task_id):
    """(rule name, target) for the first rule the call matches, or None.

    The target is the path Hermes resolves for a write_file or patch call, or the terminal's command.
    """
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    paths = {name: os.path.realpath(PLUGIN_DIR if path == "{plugin}" else home / path)
             for name, (path, _) in rules.items() if path}
    if tool_name in FILE_TOOLS:
        # The paths the tool itself will write, as Hermes reads them from the call and resolves them.
        from agent.tool_dispatch_helpers import _extract_file_mutation_targets
        from tools.file_tools_paths import _resolve_path_for_task

        for target in _extract_file_mutation_targets(tool_name, args):
            resolved = os.path.realpath(_resolve_path_for_task(target, task_id or "default"))
            for name, root in paths.items():
                if resolved == root or resolved.startswith(root + os.sep):
                    return name, resolved
    elif tool_name == TERMINAL and isinstance(command := args.get("command"), str):
        for name, (_, pattern) in rules.items():
            if name in paths and any(spelling in command for spelling in _spellings(paths[name], home)):
                return name, command
            if pattern and pattern.search(command):
                return name, command
    return None


def _spellings(root, home):
    """How a command names `root`: absolutely, from the user's home (as in ~/.hermes/skills), or from
    $HERMES_HOME (as in $HERMES_HOME/skills or ${HERMES_HOME}/skills)."""
    user_home, hermes_home = os.path.realpath(os.path.expanduser("~")), os.path.realpath(home)
    found = {root}
    if root.startswith(user_home + os.sep):
        found.add(os.path.relpath(root, user_home))
    if root.startswith(hermes_home + os.sep):
        relative = os.path.relpath(root, hermes_home)
        found |= {f"HERMES_HOME/{relative}", f"HERMES_HOME}}/{relative}"}
    return found


def blocked_because():
    """Why `on` would block a call on the list outright instead of asking a person, or None.

    ARCHITECTURE.md 5.3: Hermes reports yolo or an approval bypass, or any of the three unattended
    approval modes reads as approve, read with Hermes's own checks.
    """
    from tools.approval import _yolo_active, is_approval_bypass_active
    from tools.approval_context import _binary_approval_mode

    if _yolo_active():   # the --yolo flag, HERMES_YOLO_MODE, /yolo, or a one-shot `hermes -z` turn
        return "yolo"
    if is_approval_bypass_active():   # approvals.mode switched to off, read at the call
        return "approval bypass"
    for mode in UNATTENDED_MODES:
        if _binary_approval_mode(mode) == "approve":
            return f"approvals.{mode} reads as approve"
    return None


def check(ctx, rules, *, tool_name, args, session_id="", task_id="", **kwargs):
    """The `pre_tool_call` hook in log mode: record a call on the list. It returns nothing, so the call goes ahead."""
    if (found := find_rule(rules, tool_name, args, task_id)) is None:
        return
    from agent.redact import redact_sensitive_text

    rule, target = found
    machine = settings.machine(ctx)
    # The reason is what a maintainer judges a trigger by; forced redaction, as before every write (rule L4).
    fields = {"source": machine, "session_id": session_id or None, "reason": redact_sensitive_text(target, force=True)}
    with failures.store(machine, settings.connect) as conn:
        if why := blocked_because():
            failures.record(conn, "write_gate.would_block", "ok", **fields,
                            detail={"tool": tool_name, "rule": rule, "why": why})
        else:
            failures.record(conn, "write_gate.would_ask", "ok", **fields, detail={"tool": tool_name, "rule": rule})


def register(ctx):
    """Register the gate's `pre_tool_call` hook, unless its mode is off. A mode it does not know fails the load."""
    mode, rules = load_config()
    if mode == "off":
        return

    # Log mode blocks nothing, a crash included: the wrapper records guard.crash and the call goes ahead.
    @failures.boundary(lambda: settings.machine(ctx), settings.connect, lambda exc: None)
    def write_gate(**kwargs):
        return check(ctx, rules, **kwargs)

    ctx.register_hook("pre_tool_call", write_gate)
