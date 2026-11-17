"""The judge model's answers for the write path's supersede decision (ARCHITECTURE.md 3.3 and 3.6).

The judge is TypeSafe's System One API (OPERATIONS.md section 2). Its questions, and the threshold
that reads their answers, are in `write_path.toml`.
"""

import http.client
import json
import re
import urllib.error
import urllib.request

from . import write_path

URL = "https://api.typesafe.ai"
DEADLINE = 2   # seconds for one judge request (ARCHITECTURE.md 3.6)
TOO_BIG = "max_tokens_exceeded"   # TypeSafe's error type for a request over a context limit (seen 2026-11-17)


class JudgeError(Exception):
    """`judge.error`: the judge could not be reached, answered with an HTTP error, or answered malformed."""


class JudgeRefused(JudgeError):
    """`judge.error`, for a request the judge refuses as over its context (HTTP 400, `max_tokens_exceeded`).
    The same request gets the same answer, so the caller does not try it again (#251)."""


class JudgeTimeout(Exception):
    """`judge.timeout`: the judge did not answer inside its deadline."""


def supersede_answers(stored, new, *, url, key, model, timeout):
    """The judge's probability for each supersede question about the pair, by question name."""
    return _answers({"stored": stored, "new": new}, write_path.load()["supersede"]["questions"],
                    url=url, key=key, model=model, timeout=timeout)


def keep_answers(fact, source, *, url, key, model, timeout):
    """The judge's probability that an extracted fact is worth keeping, and that its source turn supports it
    (#148). The turn can be far larger than the judge's context, so the state is the fact alone and each part
    of the source has a support question of its own: the user's message and each tool result, a long one cut
    into pieces, over as many requests as fit the judge's limits (`write_path.toml`, #251). A fact is as
    supported as its best part, and the asking stops at the first part that reaches the support threshold, or
    after the first request when the fact is not worth keeping."""
    config = write_path.load()["extraction"]
    wording, size = config["part_question"], config["part_chars"]
    texts = [("The user's message", source.get("user") or "")]
    texts += [("A tool result", result) for result in source.get("tool_results") or []]
    parts = [(label, text[at:at + size]) for label, text in texts for at in range(0, len(text), size)]
    batches, held = [{"worth_keeping": config["questions"]["worth_keeping"]}], 0   # held: the last batch's text
    for n, (label, text) in enumerate(parts):
        if held and held + len(text) > config["request_chars"]:
            batches.append({})
            held = 0
        held += len(text)
        batches[-1][f"s{n}"] = {
            **wording, "instructions": wording["instructions"].replace("{label}", label).replace("{text}", text)}
    worth, supported = None, 0.0
    for batch in batches:
        answers = _answers({"fact": fact}, batch, url=url, key=key, model=model, timeout=timeout)
        worth = answers.pop("worth_keeping", worth)
        supported = max([supported, *answers.values()])
        if supported >= config["support_threshold"] or not keeps({"worth_keeping": worth, "supported": 1}):
            break   # decided: one part supports it, or the gate drops it whatever its support
    return {"worth_keeping": worth, "supported": supported}


def digest_answers(digest, *, url, key, model, timeout):
    """The judge's probability for the digest gate's question (#155)."""
    return _answers({"digest": digest}, write_path.load()["digest"]["questions"], url=url, key=key, model=model,
                    timeout=timeout)


def relevance_answers(message, notes, *, url, key, model, timeout):
    """The judge's probability that each note helps answer the message, in the notes' order (3.4 step 4).
    Each note is in its own question, not looked up by id in a shared list."""
    wording = write_path.load()["rerank"]["question"]
    questions = {f"c{n}": {**wording, "instructions": wording["instructions"].replace("{note}", note)}
                 for n, note in enumerate(notes)}
    answers = _answers({"message": message}, questions, url=url, key=key, model=model, timeout=timeout)
    return [answers[f"c{n}"] for n in range(len(notes))]


def route_answers(state, actions, wording, *, url, key, model, timeout):
    """The action query's routing (3.5 point 2): the picked action's name, and the probability that the message
    asks for work at all. The judge always picks one of `actions`, whatever its probability (#26)."""
    questions = {"action": {"type": "choice", "instructions": wording["action"]["instructions"], "criteria": actions},
                 "asks_for_work": {"type": "noul", **wording["asks_for_work"]}}
    answers = _request(state, questions, url=url, key=key, model=model, timeout=timeout)
    try:
        pick, asks = answers["action"]["choice"], answers["asks_for_work"]["noul"]
    except (KeyError, TypeError) as exc:
        raise JudgeError(f"malformed answer: {type(exc).__name__}: {exc}"[:200]) from exc
    if not isinstance(pick, str) or pick not in actions:
        raise JudgeError(f"malformed answer: picked {pick!r}, which is not on the action list"[:200])
    if isinstance(asks, bool) or not isinstance(asks, int | float):
        raise JudgeError(f"malformed answer: 'asks for work' is {asks!r}, not a probability"[:200])
    return pick, asks


def _answers(state, wording, *, url, key, model, timeout):
    questions = {name: {"type": "noul", **question} for name, question in wording.items()}
    answers = _request(state, questions, url=url, key=key, model=model, timeout=timeout)
    try:
        return {name: answers[name]["noul"] for name in questions}
    except (KeyError, TypeError) as exc:   # without the questions' answers
        raise JudgeError(f"malformed answer: {type(exc).__name__}: {exc}"[:200]) from exc


def _request(state, questions, *, url, key, model, timeout):
    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    request = urllib.request.Request(url + "/v1/systemone", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except TimeoutError as exc:
        raise JudgeTimeout(f"no answer within {timeout} seconds") from exc
    # urllib's URLError and HTTPError, and a dropped connection, are OSError; an answer cut off while it is
    # read is http.client's IncompleteRead (HTTPException).
    except urllib.error.HTTPError as exc:
        cause = _cause(exc) if exc.code == 400 else ""
        if cause == TOO_BIG:   # the one refusal known to repeat; any other 400 stays an error a retry may fix
            raise JudgeRefused(f"HTTPError: {exc}: {cause}"[:200]) from exc
        raise JudgeError(f"HTTPError: {exc}{cause and ': ' + cause}"[:200]) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise JudgeError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    try:
        return json.loads(raw)["answers"]
    except (ValueError, KeyError, TypeError) as exc:   # not JSON, or without answers
        raise JudgeError(f"malformed answer: {type(exc).__name__}: {exc}"[:200]) from exc


def _cause(error):
    """A refusal's error type from its body when it is a plain code name, such as max_tokens_exceeded, else
    nothing: the body is the judge's text and could repeat the request's, so no other part of it is kept."""
    try:
        cause = json.loads(error.read())["detail"]["error_type"]
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return ""
    return cause if isinstance(cause, str) and re.fullmatch(r"[a-z_]{1,60}", cause) else ""


def supersedes(answers):
    """The decision, in code: every answer at or above the threshold."""
    threshold = write_path.load()["supersede"]["judge_threshold"]
    return all(probability >= threshold for probability in answers.values())


def keeps(answers):
    """The decision on an extracted fact, in code: the gate drops it at a drop score (1 - "worth keeping") of
    `drop_score` or higher, and the support check needs "supported" at `support_threshold` or higher (3.6)."""
    config = write_path.load()
    return (1 - answers["worth_keeping"] < config["gate"]["drop_score"]
            and answers["supported"] >= config["extraction"]["support_threshold"])


def keeps_digest(answers):
    """The decision on a digest, in code: the gate drops it at a drop score of `drop_score` or higher (3.6)."""
    return 1 - answers["worth_keeping"] < write_path.load()["gate"]["drop_score"]
