"""The fact gate, in log mode (ARCHITECTURE.md sections 5.3 and 5.4; #133).

Three hooks, keyed by session and turn. `pre_llm_call` arms a turn whose message asks a checkable
question and records `fact_gate.armed`. `post_tool_call` counts a lookup Hermes reports `ok`.
`transform_llm_output` records `fact_gate.would_withhold` for an armed turn's reply with no lookup, or
with a procedure or diagnosis and no documentation lookup. In `log` mode no reminder is added and nothing
is withheld; events are written from a worker thread, as the other gates' are. `off` registers no hook.
"""

import pathlib
import re
import threading
import tomllib

from .. import failures, settings

CONFIG = pathlib.Path(__file__).with_suffix(".toml")
MODES = ("log", "off")   # `on` arrives with enforcement (ARCHITECTURE.md 5.4 item 6)
TURNS_KEPT = 1000   # armed turns held while their replies are pending, in this process
_turns = {}   # (session_id, turn_id) -> {"rule": ..., "looked": bool, "documented": bool}
_turns_lock = threading.Lock()


def load_config(path=CONFIG):
    """The gate's mode, tools and patterns; raises ValueError for a mode other than log or off."""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config["mode"] not in MODES:
        raise ValueError(f"fact gate: mode {config['mode']!r} is not one of {MODES}; on is not built yet")

    def compiled(table):
        return {name: re.compile(pattern, re.IGNORECASE) for name, pattern in table.items()}

    return {"mode": config["mode"], "lookups": set(config["lookups"]), "documentation": set(config["documentation"]),
            "questions": compiled(config["questions"]), "needs_documentation": compiled(config["needs_documentation"])}


def armed_by(config, message):
    """The name of the first question pattern the user's message matches, or None."""
    if not isinstance(message, str):   # shortcut: a message given as parts (images, files) is not read; widen if counts show it
        return None
    return next((name for name, pattern in config["questions"].items() if pattern.search(message)), None)


def arm(ctx, config, *, session_id="", turn_id="", user_message=None, **kwargs):
    """`pre_llm_call`: arm a turn that asks a checkable question. Returns nothing, so the turn is unchanged."""
    if (rule := armed_by(config, user_message)) is None:
        return
    with _turns_lock:
        _turns[(session_id, turn_id)] = {"rule": rule, "looked": False, "documented": False}
        while len(_turns) > TURNS_KEPT:
            _turns.pop(next(iter(_turns)))
    _record(ctx, session_id=session_id, rule=rule, why=None)


def count(ctx, config, *, tool_name, session_id="", turn_id="", status=None, **kwargs):
    """`post_tool_call`: count a lookup in an armed turn. A call with any status other than ok does not count."""
    if status != "ok" or tool_name not in config["lookups"]:
        return
    with _turns_lock:
        if turn := _turns.get((session_id, turn_id)):
            turn["looked"] = True
            turn["documented"] = turn["documented"] or tool_name in config["documentation"]


def check(ctx, config, *, response_text, session_id="", turn_id="", **kwargs):
    """`transform_llm_output`: record an armed turn's reply that would be withheld. Returns nothing, so
    the reply is delivered as written."""
    with _turns_lock:
        turn = _turns.pop((session_id, turn_id), None)
    if turn is None:
        return
    if not turn["looked"]:
        _record(ctx, session_id=session_id, rule=turn["rule"], why="no lookup")
        return
    needs = next((name for name, pattern in config["needs_documentation"].items() if pattern.search(response_text or "")), None)
    if needs and not turn["documented"]:
        _record(ctx, session_id=session_id, rule=needs, why="no documentation lookup")


def _record(ctx, *, session_id, rule, why):
    """Write one event from a worker thread, so a store that stops answering never holds the turn (#117)."""
    from agent.memory_provider import spawn_context_thread

    machine = settings.machine(ctx)
    event = {"source": machine, "session_id": session_id or None, "rule": rule, "why": why}
    write = failures.boundary(lambda: machine, settings.connect, lambda exc: None)(write_event)
    spawn_context_thread(write, name="custom_build.fact_gate", kwargs=event).start()


def write_event(*, source, session_id, rule, why):
    """Write one of the gate's events; runs on the worker thread `_record` starts. No message or reply
    text is stored, only pattern and rule names (rule L5)."""
    with failures.store(source, settings.connect) as conn:
        if why is None:
            failures.record(conn, "fact_gate.armed", "ok", source=source, session_id=session_id, detail={"rule": rule})
        else:
            failures.record(conn, "fact_gate.would_withhold", "ok", source=source, session_id=session_id,
                            detail={"rule": rule, "why": why})


def register(ctx):
    """Register the gate's three hooks, unless its mode is off. A mode it does not know fails the load."""
    config = load_config()
    if config["mode"] == "off":
        return
    # Log mode adds nothing and withholds nothing, a crash included: the wrapper records guard.crash.
    guard = failures.boundary(lambda: settings.machine(ctx), settings.connect, lambda exc: None)

    @guard
    def fact_gate_arm(**kwargs):
        return arm(ctx, config, **kwargs)

    @guard
    def fact_gate_count(**kwargs):
        return count(ctx, config, **kwargs)

    @guard
    def fact_gate_check(**kwargs):
        return check(ctx, config, **kwargs)

    ctx.register_hook("pre_llm_call", fact_gate_arm)
    ctx.register_hook("post_tool_call", fact_gate_count)
    ctx.register_hook("transform_llm_output", fact_gate_check)   # without hold_stream: log mode holds nothing
