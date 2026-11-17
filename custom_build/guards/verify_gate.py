"""The verify gate, in log mode (ARCHITECTURE.md sections 5.3 and 5.4; #124).

Three hooks. `post_tool_call` keeps, per session, the listed files and settings a call reported `ok`
has read. `pre_tool_call` records `verify_gate.would_block` for a call that would change one the
session never read. `transform_llm_output` records `verify_gate.would_withhold` for a reply that
states a listed file's contents the session never read. In `log` mode nothing is blocked or withheld,
and events are written from a worker thread, as the write gate's are. `off` registers no hook.
"""

import os
import pathlib
import re
import threading
import tomllib

from .. import failures, settings
from .write_gate import FILE_TOOLS, PLUGIN_DIR, TERMINAL, _names

CONFIG = pathlib.Path(__file__).with_suffix(".toml")
MODES = ("log", "off")   # `on` arrives with enforcement (ARCHITECTURE.md 5.4 item 6)
SESSIONS_KEPT = 100   # sessions whose reads the plugin's state keeps (#124)
HERMES_CONFIG = re.compile(r"\bhermes(?:\s+-\S+(?:\s+[^-\s]\S*)?)*\s+config\s+(set|unset|get|show)\b(?:\s+(\S+))?")
SENTENCES = re.compile(r"(?<=[.!?])\s+|\n")
_state_lock = threading.Lock()   # one read-modify-write of the reads at a time, in this process


def load_config(path=CONFIG):
    """The gate's mode, files, settings and patterns; raises ValueError for a mode other than log or off."""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config["mode"] not in MODES:
        raise ValueError(f"verify gate: mode {config['mode']!r} is not one of {MODES}; on is not built yet")
    return {"mode": config["mode"], "files": config["files"], "settings": set(config["settings"]),
            "writes": re.compile(config["writes"]), "contents": re.compile(config["contents"])}


def _paths(config):
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    return home, {name: PLUGIN_DIR if path == "{plugin}" else home / path for name, path in config["files"].items()}


def _listed(paths, target, task_id):
    """The listed files a path Hermes resolves for this task lies in, with the resolved path."""
    from tools.file_tools_paths import _resolve_path_for_task

    resolved = os.path.realpath(_resolve_path_for_task(target, task_id or "default"))
    return [(name, resolved) for name, path in paths.items()
            if resolved == (root := os.path.realpath(path)) or resolved.startswith(root + os.sep)]


def _named(config, args, task_id):
    """The listed files a terminal command names, and whether it writes."""
    from tools.file_tools_paths import _resolve_path_for_task

    home, paths = _paths(config)
    command = args["command"]
    where = _resolve_path_for_task(args.get("workdir") or ".", task_id or "default")
    return [name for name, path in paths.items() if _names(command, path, home, where)], bool(config["writes"].search(command))


def changes(config, tool_name, args, task_id):
    """(rule, target, file) for each listed file or setting the call would change. The target is the file's
    path, or the setting and what is done to it: never a terminal command, whose arguments can carry a
    credential that no redactor knows (#115). `file` is the path a file tool will write, else None: a
    command's words do not say reliably which file of a listed folder it touches."""
    found = []
    if tool_name in FILE_TOOLS:
        from agent.tool_dispatch_helpers import _extract_file_mutation_targets

        _, paths = _paths(config)
        for target in _extract_file_mutation_targets(tool_name, args):
            found += [(name, resolved, resolved) for name, resolved in _listed(paths, target, task_id)]
    elif tool_name == TERMINAL and isinstance(command := args.get("command"), str):
        names, writes = _named(config, args, task_id)
        paths = _paths(config)[1]
        found += [(name, os.path.realpath(paths[name]), None) for name in names if writes]
        found += [(match[2], f"hermes config {match[1]} {match[2]}", None) for match in HERMES_CONFIG.finditer(command)
                  if match[1] in ("set", "unset") and match[2] in config["settings"]]
    return found


def reads(config, tool_name, args, task_id):
    """What the call reads, as the keys its reads are kept under: the path `read_file` reads, so reading one
    file of a listed folder clears only that file (#128); the rule's name for a command that names a listed
    file or folder, which reads all of it; and a setting's name."""
    if tool_name == "read_file" and isinstance(path := args.get("path"), str):
        return [resolved for _, resolved in _listed(_paths(config)[1], path, task_id)]
    if tool_name != TERMINAL or not isinstance(command := args.get("command"), str):
        return []
    names, writes = _named(config, args, task_id)
    found = [] if writes else names
    for match in HERMES_CONFIG.finditer(command):   # every one in a chained command
        if match[1] == "show":
            found += sorted(config["settings"])
        elif match[1] == "get" and match[2] in config["settings"]:
            found.append(match[2])
    return found


def _read(ctx, session_id):
    return set(ctx.state.get("reads", {}).get(session_id, []))


def _was_read(read, rule, file, root):
    """Whether a change is covered by the session's reads. A command's read of the rule covers every change
    to it. Otherwise a file tool's change needs that file read, and a command's change needs any file of
    the rule read."""
    if rule in read:
        return True
    if file is not None:
        return file in read
    return root is not None and any(key == root or key.startswith(root + os.sep) for key in read)


def _remember(ctx, session_id, names):
    """Add to the session's reads, keeping the most recently active SESSIONS_KEPT sessions."""
    with _state_lock:
        kept = ctx.state.get("reads", {})
        kept[session_id] = sorted(set(kept.pop(session_id, [])) | set(names))   # re-added last: most recent
        while len(kept) > SESSIONS_KEPT:
            kept.pop(next(iter(kept)))
        ctx.state.set("reads", kept)


def before(ctx, config, *, tool_name, args, session_id="", task_id="", **kwargs):
    """`pre_tool_call`: record a change to a listed file or setting this session never read."""
    # The state is read only for a change on the list, so a call off it never touches the state file.
    if not (changed := changes(config, tool_name, args, task_id)):
        return
    read = _read(ctx, session_id)
    roots = {name: os.path.realpath(path) for name, path in _paths(config)[1].items()}
    if unread := [(rule, target) for rule, target, file in changed if not _was_read(read, rule, file, roots.get(rule))]:
        _record(ctx, session_id=session_id, rule=unread[0][0], reason=unread[0][1], tool=tool_name, withhold=False)


def after(ctx, config, *, tool_name, args, session_id="", task_id="", status=None, **kwargs):
    """`post_tool_call`: remember what a call reported `ok` read. A read with any other status does not count."""
    if status == "ok" and (names := reads(config, tool_name, args, task_id)):
        _remember(ctx, session_id, names)


def reply(ctx, config, *, response_text, session_id="", **kwargs):
    """`transform_llm_output`: record a reply that states a listed file's contents this session never read.
    It returns nothing, so the reply is delivered as written."""
    stated = [sentence for sentence in SENTENCES.split(response_text or "") if config["contents"].search(sentence)]
    if not (named := [(name, path) for name, path in _paths(config)[1].items()
                      if any(path.name in sentence for sentence in stated)]):
        return
    read = _read(ctx, session_id)
    for name, path in named:
        if not _was_read(read, name, None, os.path.realpath(path)):
            _record(ctx, session_id=session_id, rule=name, reason=path.name, tool=None, withhold=True)


def _record(ctx, *, session_id, rule, reason, tool, withhold):
    """Write one event from a worker thread, so a store that stops answering never holds the call (#117)."""
    from agent.memory_provider import spawn_context_thread
    from agent.redact import redact_sensitive_text

    machine = settings.machine(ctx)
    event = {"source": machine, "session_id": session_id or None, "rule": rule, "tool": tool, "withhold": withhold,
             "reason": redact_sensitive_text(reason, force=True)}
    write = failures.boundary(lambda: machine, settings.connect, lambda exc: None)(write_event)
    spawn_context_thread(write, name="custom_build.verify_gate", kwargs=event).start()


def write_event(*, source, session_id, rule, tool, withhold, reason):
    """Write one of the gate's events to the store; runs on the worker thread `_record` starts."""
    detail = {"rule": rule} | ({"gen_ai.tool.name": tool} if tool else {})   # OpenTelemetry's name (rule L3)
    fields = {"source": source, "session_id": session_id, "reason": reason, "detail": detail}
    with failures.store(source, settings.connect) as conn:
        if withhold:
            failures.record(conn, "verify_gate.would_withhold", "ok", **fields)
        else:
            failures.record(conn, "verify_gate.would_block", "ok", **fields)


def register(ctx):
    """Register the gate's three hooks, unless its mode is off. A mode it does not know fails the load."""
    config = load_config()
    if config["mode"] == "off":
        return
    # Log mode blocks and withholds nothing, a crash included: the wrapper records guard.crash and returns None.
    guard = failures.boundary(lambda: settings.machine(ctx), settings.connect, lambda exc: None)

    @guard
    def verify_gate_before(**kwargs):
        return before(ctx, config, **kwargs)

    @guard
    def verify_gate_after(**kwargs):
        return after(ctx, config, **kwargs)

    @guard
    def verify_gate_reply(**kwargs):
        return reply(ctx, config, **kwargs)

    ctx.register_hook("pre_tool_call", verify_gate_before)
    ctx.register_hook("post_tool_call", verify_gate_after)
    ctx.register_hook("transform_llm_output", verify_gate_reply)   # without hold_stream: log mode holds nothing
