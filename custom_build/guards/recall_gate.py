"""The recall gate, in log mode (ARCHITECTURE.md sections 5.3 and 5.4; #173).

Three hooks, keyed by the turn. `pre_llm_call` arms a turn whose message asks about past work or open
work, by the patterns memory's recall reads, and records `recall_gate.armed`. `post_tool_call` counts a
search Hermes reports `ok`. `transform_llm_output` records `recall_gate.would_withhold` for an armed
turn's reply that came without both searches. In `log` mode no reminder is added and nothing is withheld;
events are written from a worker thread, as the other gates' are. `off` registers no hook.
"""

import pathlib
import threading
import tomllib

from .. import failures, settings
from ..memory import recall

CONFIG = pathlib.Path(__file__).with_suffix(".toml")
MODES = ("log", "off")   # `on` arrives with enforcement (ARCHITECTURE.md 5.4 item 6)
TURNS_KEPT = 1000   # armed turns held while their replies are pending, in this process
# turn_id -> {"kind": ..., "rule": ..., "searched": set of tool names}. Keyed by the turn alone: Hermes's
# turn ids embed a uuid, and compression can give the session a new id mid-turn.
_turns = {}
_turns_lock = threading.Lock()


def load_config(path=CONFIG):
    """The gate's mode and its searches; raises ValueError for a mode other than log or off."""
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    if config["mode"] not in MODES:
        raise ValueError(f"recall gate: mode {config['mode']!r} is not one of {MODES}; on is not built yet")
    return {"mode": config["mode"], "searches": list(config["searches"])}


def arm(ctx, config, *, session_id="", turn_id="", user_message=None, **kwargs):
    """`pre_llm_call`: arm a turn that asks about past work. Returns nothing, so the turn is unchanged."""
    if not isinstance(user_message, str):   # shortcut: a message given as parts (images, files) is not read; widen if counts show it
        return
    kind, rule = recall.question(user_message)   # memory's own matcher, so the search and the gate agree
    if kind is None:
        return
    with _turns_lock:
        _turns[turn_id] = {"kind": kind, "rule": rule, "searched": set()}
        while len(_turns) > TURNS_KEPT:
            _turns.pop(next(iter(_turns)))
    _record(ctx, session_id=session_id, kind=kind, rule=rule, why=None)


def count(ctx, config, *, tool_name, turn_id="", status=None, **kwargs):
    """`post_tool_call`: count a search in an armed turn. A call with any status other than ok does not count."""
    if status != "ok" or tool_name not in config["searches"]:
        return
    with _turns_lock:
        if turn := _turns.get(turn_id):
            turn["searched"].add(tool_name)


def check(ctx, config, *, session_id="", turn_id="", **kwargs):
    """`transform_llm_output`: record an armed turn's reply that would be withheld. Returns nothing, so
    the reply is delivered as written."""
    with _turns_lock:
        turn = _turns.pop(turn_id, None)
    if turn is None:
        return
    if missing := [search for search in config["searches"] if search not in turn["searched"]]:
        _record(ctx, session_id=session_id, kind=turn["kind"], rule=turn["rule"], why="no " + " or ".join(missing))


def _record(ctx, *, session_id, kind, rule, why):
    """Write one event from a worker thread, so a store that stops answering never holds the turn (#117)."""
    from agent.memory_provider import spawn_context_thread

    machine = settings.machine(ctx)
    event = {"source": machine, "session_id": session_id or None, "kind": kind, "rule": rule, "why": why}
    write = failures.boundary(lambda: machine, settings.connect, lambda exc: None)(write_event)
    spawn_context_thread(write, name="custom_build.recall_gate", kwargs=event).start()


def write_event(*, source, session_id, kind, rule, why):
    """Write one of the gate's events; runs on the worker thread `_record` starts. No message or reply
    text is stored, only table and pattern names (rule L5)."""
    with failures.store(source, settings.connect) as conn:
        if why is None:
            failures.record(conn, "recall_gate.armed", "ok", source=source, session_id=session_id,
                            detail={"kind": kind, "rule": rule})
        else:
            failures.record(conn, "recall_gate.would_withhold", "ok", source=source, session_id=session_id,
                            detail={"kind": kind, "rule": rule, "why": why})


def register(ctx):
    """Register the gate's three hooks, unless its mode is off. A mode it does not know fails the load."""
    config = load_config()
    if config["mode"] == "off":
        return
    # Log mode adds nothing and withholds nothing, a crash included: the wrapper records guard.crash.
    guard = failures.boundary(lambda: settings.machine(ctx), settings.connect, lambda exc: None)

    @guard
    def recall_gate_arm(**kwargs):
        return arm(ctx, config, **kwargs)

    @guard
    def recall_gate_count(**kwargs):
        return count(ctx, config, **kwargs)

    @guard
    def recall_gate_check(**kwargs):
        return check(ctx, config, **kwargs)

    ctx.register_hook("pre_llm_call", recall_gate_arm)
    ctx.register_hook("post_tool_call", recall_gate_count)
    ctx.register_hook("transform_llm_output", recall_gate_check)   # without hold_stream: log mode holds nothing
