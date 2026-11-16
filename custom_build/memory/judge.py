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
    """The gate's decision on an extracted fact, in code: every answer at or above the threshold."""
    threshold = write_path.load()["extraction"]["judge_threshold"]
    return all(probability >= threshold for probability in answers.values())
