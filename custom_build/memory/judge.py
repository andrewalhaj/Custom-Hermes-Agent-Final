"""The judge model's answers for the write path's supersede decision (ARCHITECTURE.md 3.3 and 3.6).

The judge is TypeSafe's System One API (OPERATIONS.md section 2). Its questions, and the threshold
that reads their answers, are in `write_path.toml`.
"""

import http.client
import json
import urllib.request

from . import write_path

URL = "https://api.typesafe.ai"
DEADLINE = 2   # seconds for one judge request (ARCHITECTURE.md 3.6)


class JudgeError(Exception):
    """`judge.error`: the judge could not be reached, answered with an HTTP error, or answered malformed."""


class JudgeTimeout(Exception):
    """`judge.timeout`: the judge did not answer inside its deadline."""


def supersede_answers(stored, new, *, url, key, model, timeout):
    """The judge's probability for each supersede question about the pair, by question name."""
    return _answers({"stored": stored, "new": new}, write_path.load()["supersede"]["questions"],
                    url=url, key=key, model=model, timeout=timeout)


def keep_answers(fact, source, *, url, key, model, timeout):
    """The judge's probability for each gate question about an extracted fact and its source turn (#148)."""
    return _answers({"fact": fact, "source": source}, write_path.load()["extraction"]["questions"],
                    url=url, key=key, model=model, timeout=timeout)


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


def _answers(state, wording, *, url, key, model, timeout):
    questions = {name: {"type": "noul", **question} for name, question in wording.items()}
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
    except (OSError, http.client.HTTPException) as exc:
        raise JudgeError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    try:
        answers = json.loads(raw)["answers"]
        return {name: answers[name]["noul"] for name in questions}
    except (ValueError, KeyError, TypeError) as exc:   # not JSON, or without the questions' answers
        raise JudgeError(f"malformed answer: {type(exc).__name__}: {exc}"[:200]) from exc


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
