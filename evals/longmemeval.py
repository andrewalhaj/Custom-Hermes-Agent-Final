"""The LongMemEval-S eval runner (issue #167; ARCHITECTURE.md 3.8; decided: option C, the full pipeline).

For each question of a seeded subset, the runner builds a throwaway database (every migration applied) and
a throwaway Hermes home with the plugin, replays the question's chat history through the build
(`evals/replay.py`), dates the stored rows by their sessions, asks the question through `prefetch()`, and
scores the notes: recall@5 and MRR against the evidence sessions, and abstention for questions whose answer
is in no session. The database and its logins are dropped after each question.

Option C calls Haiku on the Claude subscription for every user turn and every long session's digest, and
the judge for every proposed fact and every rerank: a run prints its estimate and does nothing more without
`--yes`, and stops at the first `extract.refused` or `digest.refused` (#148's enforcement alert), keeping
what it measured. It runs on a maintainer's machine against a throwaway Postgres, never the shared store.

Usage: python -m evals.longmemeval --data longmemeval_s_cleaned.json --questions 20 --postgres URL --out DIR
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import uuid

import psycopg
import yaml
from psycopg import sql

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "custom_build"
MIGRATIONS = sorted((ROOT / "migrations").glob("*.sql"))
GROUP_ROLES = ("memory_agent", "memory_maintenance")
CONFIG = pathlib.Path(__file__).with_suffix(".toml")   # the pinned revision and hash of the data file
PROJECT_ORIGIN = "https://eval.invalid/longmemeval.git"   # every replay's project
DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"   # LongMemEval's haystack_dates
FAILED = "Recall failed ("   # how recall.failed() begins a note that reports a failure


class EvalError(Exception):
    """The run cannot go on: a data file that fails its check, or a replay that crashed."""


def load(path, sha256):
    """The data file's entries, refused unless its SHA-256 is the one recorded (3.8)."""
    data = pathlib.Path(path).read_bytes()
    found = hashlib.sha256(data).hexdigest()
    if found != sha256:
        raise EvalError(f"{path}: SHA-256 {found} is not the recorded {sha256}")
    return json.loads(data)


def fetch(url, dest, sha256):
    """Download the data file to `dest`, kept only when its SHA-256 is the one recorded; a file that fails
    the check, or a download cut short, leaves nothing behind."""
    dest = pathlib.Path(dest)
    partial = dest.with_name(dest.name + ".partial")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as response, partial.open("wb") as out:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != sha256:
            raise EvalError(f"{url}: SHA-256 {digest.hexdigest()} is not the recorded {sha256}")
        partial.rename(dest)
    finally:
        partial.unlink(missing_ok=True)
    return dest


def subset(entries, count, *, seed):
    """`count` entries spread evenly across question types, the same for the same seed."""
    by_type = {}
    for entry in entries:
        by_type.setdefault(entry["question_type"], []).append(entry)
    rng = random.Random(seed)
    for group in by_type.values():
        rng.shuffle(group)
    picked = []
    while len(picked) < count and any(by_type.values()):
        for kind in sorted(by_type):
            if by_type[kind] and len(picked) < count:
                picked.append(by_type[kind].pop())
    return picked


def estimate(entries):
    """What a run would call, at least: one Haiku extraction per user turn and one digest per long session
    (the build's own setting, 3.6), and one judge rerank per question (plus a gate call per proposed fact)."""
    long_session = tomllib.loads((PLUGIN / "memory" / "write_path.toml").read_text(encoding="utf-8"))[
        "digest"]["long_session"]
    sessions = [session for entry in entries for session in entry["haystack_sessions"]]
    user_turns = sum(1 for session in sessions for message in session if message["role"] == "user")
    long_sessions = sum(1 for session in sessions if len(session) >= long_session)
    return {"questions": len(entries), "user_turns": user_turns, "sessions": len(sessions),
            "haiku_calls_at_least": user_turns + long_sessions, "judge_calls_at_least": len(entries)}


@contextlib.contextmanager
def database(postgres):
    """A throwaway database with every migration applied, and a login in memory_agent: (owner, agent) URLs.
    The database, the login and the migrations' group roles are dropped afterwards. The group roles are the
    whole server's, so a server that already has them is refused, never touched: it is not a throwaway one."""
    name = f"eval_{uuid.uuid4().hex[:12]}"
    login, password = f"{name}_agent", uuid.uuid4().hex
    info = psycopg.conninfo.conninfo_to_dict(postgres)
    owner = psycopg.conninfo.make_conninfo(**{**info, "dbname": name})
    with psycopg.connect(postgres, autocommit=True) as admin:
        found = [row[0] for row in admin.execute("select rolname from pg_roles where rolname = any(%s)",
                                                 (list(GROUP_ROLES),))]
        if found:
            raise EvalError(f"this Postgres already has {', '.join(found)}: point the runner at a throwaway "
                            "server; it never drops a role it did not create")
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            with psycopg.connect(owner, autocommit=True) as conn:
                for migration in MIGRATIONS:
                    conn.execute(migration.read_text(encoding="utf-8"))
                conn.execute(sql.SQL("create role {} login password {} in role memory_agent").format(
                    sql.Identifier(login), sql.Literal(password)))
            yield owner, psycopg.conninfo.make_conninfo(**{**info, "dbname": name, "user": login, "password": password})
        finally:
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
            for role in (login, *GROUP_ROLES):
                admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role)))


def hermes_home(tmp, template, judge_model):
    """A Hermes home with the plugin and its settings, on top of `template` (which holds, for a real run, the
    Claude subscription provider plugin and its login), and a repository whose origin is the eval's project."""
    home = tmp / "home"
    if template:
        shutil.copytree(template, home)
    shutil.copytree(PLUGIN, home / "plugins" / "custom_build", ignore=shutil.ignore_patterns("__pycache__"),
                    dirs_exist_ok=True)
    config_file = home / "config.yaml"
    config = yaml.safe_load(config_file.read_text(encoding="utf-8")) if config_file.exists() else {}
    config = config or {}
    config.setdefault("memory", {})["provider"] = "custom_build"
    plugins = config.setdefault("plugins", {})
    # Enabled as an agent host enables it: only then does it register its own route for extraction (#148)
    plugins["enabled"] = sorted({*(plugins.get("enabled") or ()), "custom_build"})
    entries = plugins.setdefault("entries", {})
    entries.setdefault("custom_build", {})["settings"] = {"machine_name": "eval", "judge_model": judge_model}
    config_file.write_text(yaml.safe_dump(config), encoding="utf-8")
    work = tmp / "work"
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", PROJECT_ORIGIN], check=True)
    return home, work


def replay(command, mode, question, home, work, env):
    result = subprocess.run([*command, mode, str(question), str(home)], cwd=work, env=env, capture_output=True,
                            text=True, check=False)
    if result.returncode != 0:
        raise EvalError(f"replay {mode} failed:\n{result.stderr[-3000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def note_bodies(notes):
    """The text of each recalled note, in order (recall.notes' format: "- [provenance] text", indented
    continuation lines); none for a failure or no notes."""
    bodies = []
    for line in notes.splitlines():
        if line.startswith("- [") and "] " in line:
            bodies.append(line.split("] ", 1)[1])
        elif line.startswith("    ") and bodies:
            bodies[-1] += "\n" + line[4:]
    return bodies


def first_hit(owner, bodies, evidence):
    """The 1-based rank of the first note stored from an evidence session, or None."""
    with psycopg.connect(owner) as conn:
        for rank, body in enumerate(bodies, start=1):
            rows = conn.execute("select session_id from memory.facts where body = %s", (body,)).fetchall()
            if any(row[0] in evidence for row in rows):
                return rank
    return None


def one(entry, *, postgres, command, template, judge_model, judge_key):
    """Replay and ask one question; its scores, the build it ran on, and the failure codes it recorded."""
    with database(postgres) as (owner, agent), tempfile.TemporaryDirectory() as directory:
        tmp = pathlib.Path(directory)
        home, work = hermes_home(tmp, template, judge_model)
        question = tmp / "question.json"
        question.write_text(json.dumps(entry), encoding="utf-8")
        # Not the caller's Claude or Anthropic settings (a Claude Code shell's base URL, account and organisation):
        # the subscription provider refuses them, and its own setting picks the eval's login (#170).
        inherited = {name: value for name, value in os.environ.items() if not name.startswith(("ANTHROPIC", "CLAUDE"))}
        env = {**inherited, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT), "CUSTOM_BUILD_DATABASE_URL": agent,
               "TYPESAFE_API_KEY": judge_key or os.environ.get("TYPESAFE_API_KEY", "")}
        stopped = replay(command, "ingest", question, home, work, env)["stopped"]
        with psycopg.connect(owner, autocommit=True) as conn:   # each row dated by its session (for recency)
            for session_id, date in zip(entry["haystack_session_ids"], entry["haystack_dates"], strict=True):
                when = datetime.datetime.strptime(date, DATE_FORMAT).replace(tzinfo=datetime.UTC)
                conn.execute("update memory.facts set created_at = %s where session_id = %s", (when, session_id))
            build = conn.execute("select embedding_model from memory.facts limit 1").fetchone()
        notes = replay(command, "ask", question, home, work, env)["notes"]
        with psycopg.connect(owner) as conn:   # after the question too, so recall's own failures are counted
            codes = [row[0] for row in conn.execute("select code from memory.events order by id")]
            failures = [{"code": code, "reason": reason, "count": count} for code, reason, count in conn.execute(
                "select code, reason, count(*) from memory.events group by code, reason")]
        bodies = note_bodies(notes)
        return {"question_id": entry["question_id"], "question_type": entry["question_type"],
                "abstention": entry["question_id"].endswith("_abs"), "notes": len(bodies),
                # recall reported a failure: never "no notes", so never a correct abstention (3.5)
                "recall_failed": notes.startswith(FAILED),
                "first_hit": first_hit(owner, bodies, set(entry["answer_session_ids"])), "codes": codes,
                "failures": failures,
                "embedding_build": build[0] if build else None, "stopped": stopped}


def _scores(results):
    count = len(results)
    hits = [result["first_hit"] for result in results]
    return {"count": count,
            "recall_at_5": round(sum(1 for hit in hits if hit and hit <= 5) / count, 4) if count else None,
            "mrr": round(sum(1 / hit for hit in hits if hit) / count, 4) if count else None}


def report(results, *, sha256, seed, judge_model):
    answerable = [result for result in results if not result["abstention"]]
    abstaining = [result for result in results if result["abstention"]]
    commit = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                            check=False).stdout.strip()
    codes, reasons = {}, {}
    for result in results:
        for code in result["codes"]:
            codes[code] = codes.get(code, 0) + 1
        for failure in result["failures"]:   # each distinct reason across the run: the databases are dropped
            key = (failure["code"], failure["reason"])
            reasons[key] = reasons.get(key, 0) + failure["count"]
    return {"questions": len(results), "answerable": _scores(answerable),
            # First cut: an abstention question is answered right when recall brings no notes (3.8).
            "abstention": {"count": len(abstaining),
                           "correct": sum(1 for r in abstaining if r["notes"] == 0 and not r["recall_failed"])},
            "recall_failed": sum(1 for r in results if r["recall_failed"]),
            "by_type": {kind: _scores([r for r in answerable if r["question_type"] == kind])
                        for kind in sorted({r["question_type"] for r in answerable})},
            "failure_codes": codes,
            "failure_reasons": [{"code": code, "reason": reason, "count": count} for (code, reason), count in
                                sorted(reasons.items(), key=lambda item: -item[1])],
            "commit": commit, "data_sha256": sha256, "seed": seed,
            "judge_model": judge_model,
            "embedding_build": next((r["embedding_build"] for r in results if r["embedding_build"]), None),
            "per_question": results}


def markdown(summary):
    lines = [f"# LongMemEval-S, {summary['questions']} questions", "",
             (f"Commit `{summary['commit']}`, embedding `{summary['embedding_build']}`, judge "
              f"`{summary['judge_model']}`, seed {summary['seed']}."), "",
             "| Questions | Count | Recall@5 | MRR |", "|---|---|---|---|"]
    rows = [("All answerable", summary["answerable"]), *summary["by_type"].items()]
    lines += [f"| {name} | {s['count']} | {s['recall_at_5']} | {s['mrr']} |" for name, s in rows]
    abstention = summary["abstention"]
    lines += ["", f"Abstention: {abstention['correct']} of {abstention['count']} with no notes.",
              f"Recall failed on {summary['recall_failed']} questions (counted as misses)."]
    if summary["failure_reasons"]:
        lines += ["", "Failures recorded:", ""]
        lines += [f"- `{f['code']}` x{f['count']}: {f['reason']}" for f in summary["failure_reasons"]]
    if summary.get("stopped"):
        lines += ["", f"**Stopped early: `{summary['stopped']}`.**"]
    return "\n".join(lines) + "\n"


def run(*, data, sha256, questions, seed, yes, postgres, out, replay_command=None, template=None,
        judge_model="jev-1.14.0", judge_key=None):
    """Estimate, or with `yes` run, the eval over a seeded subset; returns (and writes) the report."""
    entries = subset(load(data, sha256), questions, seed=seed)
    if not yes:
        return {"estimate": estimate(entries)}
    command = replay_command or [sys.executable, "-m", "evals.replay"]
    results, stopped = [], None
    try:
        for entry in entries:
            result = one(entry, postgres=postgres, command=command, template=template, judge_model=judge_model,
                         judge_key=judge_key)
            results.append(result)
            if result["stopped"]:   # the account was refused, or a plan limit hit: stop, keeping what was measured
                stopped = result["stopped"]
                break
    except (EvalError, KeyboardInterrupt) as exc:   # the questions already measured are paid for: report them
        stopped = f"crashed: {type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        summary = report(results, sha256=sha256, seed=seed, judge_model=judge_model)
        if stopped:
            summary["stopped"] = stopped
        out = pathlib.Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        (out / "report.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def main(argv=None):
    pinned = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", required=True, help="the S file; written there first with --download")
    parser.add_argument("--download", action="store_true", help="download the pinned S file to --data first")
    parser.add_argument("--questions", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--yes", action="store_true", help="run, not only estimate")
    parser.add_argument("--postgres", default=os.environ.get("EVAL_DATABASE_URL"),
                        help="a superuser URL on a throwaway Postgres; never the shared store")
    parser.add_argument("--template", help="a Hermes home with the Claude subscription provider set up")
    parser.add_argument("--out", default="eval-report")
    args = parser.parse_args(argv)
    if not pinned["sha256"]:
        raise SystemExit(f"{CONFIG.name} records no revision and SHA-256 yet: pin them first (#167)")
    if args.download:
        fetch(pinned["url"].format(revision=pinned["revision"]), args.data, pinned["sha256"])
    summary = run(data=args.data, sha256=pinned["sha256"], questions=args.questions, seed=args.seed, yes=args.yes,
                  postgres=args.postgres, out=args.out, template=args.template)
    print(json.dumps(summary if "estimate" in summary else {k: v for k, v in summary.items() if k != "per_question"},
                     indent=2))
    return 3 if summary.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
