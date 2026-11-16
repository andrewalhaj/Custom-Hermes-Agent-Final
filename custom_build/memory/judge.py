"""The judge model's answers for the write path's supersede decision (ARCHITECTURE.md 3.3 and 3.6).

The judge is TypeSafe's System One API (OPERATIONS.md section 2). Its questions, and the threshold
that reads their answers, are in `write_path.toml`.
"""

import json
import urllib.error
import urllib.request

from . import write_path

URL = "https://api.typesafe.ai"
DEADLINE = 2   # seconds for one judge request (ARCHITECTURE.md 3.6)


class JudgeError(Exception):
    """`judge.error`: the judge could not be reached, or answered with an HTTP error."""


class JudgeTimeout(Exception):
    """`judge.timeout`: the judge did not answer inside its deadline."""


def supersede_answers(stored, new, *, url, key, model, timeout):
    """The judge's probability for each supersede question about the pair, by question name."""
    questions = {name: {"type": "noul", **question}
                 for name, question in write_path.load()["supersede"]["questions"].items()}
    body = json.dumps({"model": model, "state": {"stored": stored, "new": new}, "questions": questions}).encode()
    request = urllib.request.Request(url + "/v1/systemone", data=body,
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answers = json.load(response)["answers"]
    except TimeoutError as exc:
        raise JudgeTimeout(f"no answer within {timeout} seconds") from exc
    except urllib.error.URLError as exc:   # HTTPError too; a deadline missed while connecting arrives inside one
        if isinstance(exc.reason, TimeoutError):
            raise JudgeTimeout(f"no answer within {timeout} seconds") from exc
        raise JudgeError(str(exc)) from exc
    except OSError as exc:   # the connection dropped while the answer was read
        raise JudgeError(str(exc)) from exc
    return {name: answers[name]["noul"] for name in questions}


def supersedes(answers):
    """The decision, in code: every answer at or above the threshold."""
    threshold = write_path.load()["supersede"]["judge_threshold"]
    return all(probability >= threshold for probability in answers.values())
