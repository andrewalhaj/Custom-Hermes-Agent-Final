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
       (or --ids q1,q2 in place of --questions and --seed, to run exactly those questions)
"""

import argparse
import concurrent.futures
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
import threading
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
MACHINE = "eval"   # the eval host's machine name, and so its login's name
# 0001's role statements, run once per run by roles() and left out of each question's 0001 (#219)
ROLE_STATEMENTS = ("create role memory_agent nologin;", "create role memory_maintenance nologin;")
SETUP = threading.Lock()   # one question's database is set up at a time
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
    (the build's own setting, 3.6), and two judge requests per question, the rerank and the scores before the
    floor (#234), plus a gate call per proposed fact."""
    long_session = tomllib.loads((PLUGIN / "memory" / "write_path.toml").read_text(encoding="utf-8"))[
        "digest"]["long_session"]
    sessions = [session for entry in entries for session in entry["haystack_sessions"]]
    user_turns = sum(1 for session in sessions for message in session if message["role"] == "user")
    long_sessions = sum(1 for session in sessions if len(session) >= long_session)
    return {"questions": len(entries), "user_turns": user_turns, "sessions": len(sessions),
            "haiku_calls_at_least": user_turns + long_sessions, "judge_calls_at_least": 2 * len(entries)}


@contextlib.contextmanager
def roles(postgres, *, keep=False):
    """The migrations' group roles and the eval's login in memory_agent, made once for the whole run and
    dropped at its end, unless the run keeps its stores (#242); yields the login's password. They are the whole
    server's, so a server that already has any of them is refused, never touched: it is not a throwaway one.
    The login has one name for every question, because the store admits events only under the login's name
    (#149), so it is the run's too."""
    password = uuid.uuid4().hex
    with psycopg.connect(postgres, autocommit=True) as admin:
        found = [row[0] for row in admin.execute("select rolname from pg_roles where rolname = any(%s)",
                                                 ([MACHINE, *GROUP_ROLES],))]
        if found:
            raise EvalError(f"this Postgres already has {', '.join(found)}: point the runner at a throwaway "
                            "server; it never drops a role it did not create")
        try:
            for statement in ROLE_STATEMENTS:
                admin.execute(statement)
            admin.execute(sql.SQL("create role {} login password {} in role memory_agent").format(
                sql.Identifier(MACHINE), sql.Literal(password)))
            yield password
        finally:
            if not keep:
                _drop_roles(admin)


def _drop_roles(admin):
    for role in (MACHINE, *GROUP_ROLES):
        admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(role)))


@contextlib.contextmanager
def kept_login(postgres):
    """The kept run's login with a new password, which it yields (#242): the run's own was never written down."""
    password = uuid.uuid4().hex
    with psycopg.connect(postgres, autocommit=True) as admin:
        if not admin.execute("select 1 from pg_roles where rolname = %s", (MACHINE,)).fetchone():
            raise EvalError(f"this Postgres has no {MACHINE} login: the kept run's stores were dropped")
        admin.execute(sql.SQL("alter role {} password {}").format(sql.Identifier(MACHINE), sql.Literal(password)))
    yield password


def drop_kept(postgres, directory):
    """Drop the stores a kept run left (its kept.json) and the run's roles; with no kept.json, nothing (#242)."""
    manifest = pathlib.Path(directory) / "kept.json"
    if not manifest.exists():
        return
    with psycopg.connect(postgres, autocommit=True) as admin:
        for name in json.loads(manifest.read_text(encoding="utf-8"))["databases"].values():
            admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        _drop_roles(admin)
    manifest.unlink()


def _migrations():
    """Every migration's text, 0001 without the role statements `roles()` has already run (#219)."""
    texts = [migration.read_text(encoding="utf-8") for migration in MIGRATIONS]
    if not all(statement in texts[0] for statement in ROLE_STATEMENTS):
        raise EvalError(f"{MIGRATIONS[0].name} no longer has {ROLE_STATEMENTS}: update the eval runner's roles()")
    for statement in ROLE_STATEMENTS:
        texts[0] = texts[0].replace(statement, "", 1)
    return texts


def _urls(postgres, name, password):
    """The (owner, agent) URLs of the database `name`, the agent's being the run's login."""
    info = psycopg.conninfo.conninfo_to_dict(postgres)
    return (psycopg.conninfo.make_conninfo(**{**info, "dbname": name}),
            psycopg.conninfo.make_conninfo(**{**info, "dbname": name, "user": MACHINE, "password": password}))


@contextlib.contextmanager
def database(postgres, password, *, kept=None, question_id=None):
    """A throwaway database with every migration applied: (owner, agent) URLs, the agent's being the run's
    login (`roles()`, whose `password` it takes). The database is dropped afterwards, unless the run keeps its
    stores: then `kept` records it under `question_id` (#242)."""
    name = f"eval_{uuid.uuid4().hex[:12]}"
    owner, agent = _urls(postgres, name, password)
    texts = _migrations()
    with psycopg.connect(postgres, autocommit=True) as admin:
        try:
            # One setup at a time: 0004 grants and revokes a role membership, which is the whole server's.
            with SETUP, psycopg.connect(postgres, autocommit=True) as creator:
                creator.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
                if kept is not None:
                    kept[question_id] = name
                with psycopg.connect(owner, autocommit=True) as conn:
                    for text in texts:
                        conn.execute(text)
            yield owner, agent
        finally:
            if kept is None:
                admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))


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
    entries.setdefault("custom_build", {})["settings"] = {"machine_name": MACHINE, "judge_model": judge_model}
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


@contextlib.contextmanager
def _question(entry, agent, template, judge_model, judge_key):
    """A fresh Hermes home for one question: (question file, home, work tree, the replay's environment)."""
    with tempfile.TemporaryDirectory() as directory:
        tmp = pathlib.Path(directory)
        home, work = hermes_home(tmp, template, judge_model)
        question = tmp / "question.json"
        question.write_text(json.dumps(entry), encoding="utf-8")
        # Not the caller's Claude or Anthropic settings (a Claude Code shell's base URL, account and organisation):
        # the subscription provider refuses them, and its own setting picks the eval's login (#170).
        inherited = {name: value for name, value in os.environ.items() if not name.startswith(("ANTHROPIC", "CLAUDE"))}
        yield question, home, work, {**inherited, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT),
                                     "CUSTOM_BUILD_DATABASE_URL": agent,
                                     "TYPESAFE_API_KEY": judge_key or os.environ.get("TYPESAFE_API_KEY", "")}


def one(entry, *, postgres, password, command, template, judge_model, judge_key, kept=None):
    """Replay and ask one question; its scores, the build it ran on, and the failure codes it recorded."""
    with (database(postgres, password, kept=kept, question_id=entry["question_id"]) as (owner, agent),
          _question(entry, agent, template, judge_model, judge_key) as (question, home, work, env)):
        stopped = replay(command, "ingest", question, home, work, env)["stopped"]
        with psycopg.connect(owner, autocommit=True) as conn:   # each row dated by its session (for recency)
            for session_id, date in zip(entry["haystack_session_ids"], entry["haystack_dates"], strict=True):
                when = datetime.datetime.strptime(date, DATE_FORMAT).replace(tzinfo=datetime.UTC)
                conn.execute("update memory.facts set created_at = %s where session_id = %s", (when, session_id))
        return _asked(entry, owner, command, question, home, work, env, since=0, stopped=stopped)


def again(entry, *, name, postgres, password, command, template, judge_model, judge_key):
    """Ask one question of its kept store again, from a fresh Hermes home and with no ingest, so no extraction
    and no digest (#242); its scores count only the events of this ask."""
    owner, agent = _urls(postgres, name, password)
    with psycopg.connect(owner) as conn:
        since = conn.execute("select coalesce(max(id), 0) from memory.events").fetchone()[0]
    with _question(entry, agent, template, judge_model, judge_key) as (question, home, work, env):
        return _asked(entry, owner, command, question, home, work, env, since=since, stopped=None)


def _asked(entry, owner, command, question, home, work, env, *, since, stopped):
    """Ask the question and score it, with the events recorded after the event `since`."""
    asked = replay(command, "ask", question, home, work, env)
    notes, pool = asked["notes"], asked.get("pool")   # pool None: the second fetch failed
    evidence = set(entry["answer_session_ids"])
    with psycopg.connect(owner) as conn:   # after the question too, so recall's own failures are counted
        build = conn.execute("select embedding_model from memory.facts limit 1").fetchone()
        codes = [row[0] for row in conn.execute("select code from memory.events where id > %s order by id", (since,))]
        failures = [{"code": code, "reason": reason, "count": count} for code, reason, count in conn.execute(
            "select code, reason, count(*) from memory.events where id > %s group by code, reason", (since,))]
        rows, live = conn.execute(
            "select count(*), count(*) filter (where superseded_by is null and retired_at is null)"
            " from memory.facts where session_id = any(%s)", (list(evidence),)).fetchone()
    bodies = note_bodies(notes)
    ranks = _evidence_ranks(owner, [candidate["body"] for candidate in pool or []], evidence)
    return {"question_id": entry["question_id"], "question_type": entry["question_type"],
            "abstention": entry["question_id"].endswith("_abs"), "notes": len(bodies),
            # recall reported a failure: never "no notes", so never a correct abstention (3.5)
            "recall_failed": notes.startswith(FAILED),
            "first_hit": first_hit(owner, bodies, evidence), "codes": codes,
            "failures": failures,
            # Why a miss missed (#234): the evidence's facts ever and live, and its candidates before the floor.
            "evidence_rows": rows, "evidence_stored": live, "pool": pool is not None,
            "pool_hit": ranks[0] if ranks else None, "pool_best_score": _best_score(pool or [], ranks),
            "fact_pairs": fact_pairs(owner),
            "embedding_build": build[0] if build else None, "stopped": stopped}


def fact_pairs(owner):
    """How many pairs of facts in the store reach each of the supersede check's cosines (#242; 3.6): the check
    reads the vectors too, so a different build could supersede differently. Digests are never compared."""
    limits = tomllib.loads((PLUGIN / "memory" / "write_path.toml").read_text(encoding="utf-8"))["supersede"]
    with psycopg.connect(owner) as conn:
        without_judge, ask_judge_from = conn.execute(
            "select count(*) filter (where cosine >= %s), count(*) filter (where cosine >= %s) from"
            " (select 1 - (a.embedding <=> b.embedding) as cosine from memory.facts a join memory.facts b"
            " on a.id < b.id and a.project is not distinct from b.project"
            " where a.kind = 'fact' and b.kind = 'fact') pairs",
            (limits["without_judge"], limits["ask_judge_from"])).fetchone()
    return {"without_judge": without_judge, "ask_judge_from": ask_judge_from}


def _evidence_ranks(owner, bodies, evidence):
    """The 1-based ranks of every body stored from an evidence session."""
    with psycopg.connect(owner) as conn:
        return [rank for rank, body in enumerate(bodies, start=1)
                if any(row[0] in evidence for row in conn.execute(
                    "select session_id from memory.facts where body = %s", (body,)).fetchall())]


def _best_score(pool, ranks):
    """The highest score among the evidence candidates, or None when none was scored: recall sorts by score,
    so the best one decides whether the floor cut the evidence (#234 review)."""
    scores = [pool[rank - 1]["score"] for rank in ranks if pool[rank - 1]["score"] is not None]
    return max(scores) if scores else None


def _miss(result, floor):
    """Why an answerable question missed recall@5 (#234): found below rank 5; never stored; stored, then
    superseded or retired; unknown, when the second fetch failed; stored but not in recall's candidate pool;
    ranked out by the fused order, when recall's own rerank failed and no floor ran; unscored, when the
    second judge request failed; or in the pool with its best score under the floor, or above it."""
    if result["first_hit"] is not None:
        return "below_rank_5"
    if not result["evidence_rows"]:
        return "not_stored"
    if not result["evidence_stored"]:
        return "replaced"
    if not result["pool"]:
        return "unknown"
    if result["pool_hit"] is None:
        return "not_in_pool"
    if "rerank.fallback" in result["codes"]:
        return "fused_order"
    if result["pool_best_score"] is None:
        return "unscored"
    return "cut_by_floor" if result["pool_best_score"] < floor else "above_floor"


MISSES = {"below_rank_5": "found below rank 5", "not_stored": "never stored",
          "replaced": "stored, then superseded or retired", "unknown": "cause unknown (the second fetch failed)",
          "not_in_pool": "stored but not found", "fused_order": "ranked out after recall's rerank failed (no floor)",
          "unscored": "found but unscored (the second judge request failed)", "cut_by_floor": "cut by the floor",
          "above_floor": "found above the floor but not delivered"}


def _scores(results, rank="first_hit"):
    count = len(results)
    hits = [result[rank] for result in results]
    return {"count": count,
            "recall_at_5": round(sum(1 for hit in hits if hit and hit <= 5) / count, 4) if count else None,
            "mrr": round(sum(1 / hit for hit in hits if hit) / count, 4) if count else None}


def report(results, *, sha256, seed, judge_model, ids=None, asked_again=None):
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
    floor = tomllib.loads((PLUGIN / "memory" / "write_path.toml").read_text(encoding="utf-8"))["rerank"]["floor"]
    missed = [_miss(r, floor) for r in answerable
              if (r["first_hit"] is None or r["first_hit"] > 5) and not r["recall_failed"]]
    return {"questions": len(results), "answerable": _scores(answerable),
            # The evidence's rank in recall's pool, before the rerank: the vectors and the word search decide it,
            # not the judge, so it isolates an embedding change (#242).
            "pool": _scores(answerable, "pool_hit"),
            "supersede_pairs": {limit: sum(r["fact_pairs"][limit] for r in results)
                                for limit in ("without_judge", "ask_judge_from")},
            "asked_again": asked_again,   # the kept run whose stores were asked again (#242), or None
            # Answerable misses by cause (#234); a recall that failed is counted under recall_failed instead.
            "misses": {cause: missed.count(cause) for cause in MISSES if cause in missed},
            # First cut: an abstention question is answered right when recall brings no notes (3.8).
            "abstention": {"count": len(abstaining),
                           "correct": sum(1 for r in abstaining if r["notes"] == 0 and not r["recall_failed"])},
            "recall_failed": sum(1 for r in results if r["recall_failed"]),
            "by_type": {kind: _scores([r for r in answerable if r["question_type"] == kind])
                        for kind in sorted({r["question_type"] for r in answerable})},
            "failure_codes": codes,
            "failure_reasons": [{"code": code, "reason": reason, "count": count} for (code, reason), count in
                                sorted(reasons.items(), key=lambda item: -item[1])],
            "commit": commit, "data_sha256": sha256, "seed": seed, "ids": ids,   # ids: chosen, not drawn (#236)
            "judge_model": judge_model,
            "embedding_build": next((r["embedding_build"] for r in results if r["embedding_build"]), None),
            "per_question": results}


def markdown(summary):
    lines = [f"# LongMemEval-S, {summary['questions']} questions", "",
             (f"Commit `{summary['commit']}`, embedding `{summary['embedding_build']}`, judge "
              f"`{summary['judge_model']}`, "
              + (f"questions chosen by id: {', '.join(summary['ids'])}." if summary.get("ids")
                 else f"seed {summary['seed']}.")), "",
             "| Questions | Count | Recall@5 | MRR |", "|---|---|---|---|"]
    rows = [("All answerable", summary["answerable"]), *summary["by_type"].items()]
    lines += [f"| {name} | {s['count']} | {s['recall_at_5']} | {s['mrr']} |" for name, s in rows]
    pool, pairs = summary["pool"], summary["supersede_pairs"]
    lines += ["", (f"Before the rerank (the evidence's rank in recall's pool): recall@5 {pool['recall_at_5']}, "
                   f"MRR {pool['mrr']}."),
              (f"Fact pairs at the supersede check's cosines: {pairs['without_judge']} without the judge, "
               f"{pairs['ask_judge_from']} with it.")]
    if summary.get("asked_again"):
        lines += [f"Asked again from the stores kept by `{summary['asked_again']}`: no extraction, no digest."]
    abstention = summary["abstention"]
    lines += ["", f"Abstention: {abstention['correct']} of {abstention['count']} with no notes.",
              f"Recall failed on {summary['recall_failed']} questions (counted as misses)."]
    if summary["misses"]:   # the candidates' scores come from a second judge request, so can differ slightly
        lines += ["", "Misses: " + ", ".join(f"{count} {MISSES[cause]}" for cause, count in summary["misses"].items())
                  + " (scores before the floor come from a second judge request)."]
    if summary["failure_reasons"]:
        lines += ["", "Failures recorded:", ""]
        lines += [f"- `{f['code']}` x{f['count']}: {f['reason']}" for f in summary["failure_reasons"]]
    if summary.get("stopped"):
        lines += ["", f"**Stopped early: `{summary['stopped']}`.**"]
    return "\n".join(lines) + "\n"


def chosen(entries, ids):
    """The questions with these ids, in this order (#236); an id not in the data file is an EvalError, before
    anything is called."""
    by_id = {entry["question_id"]: entry for entry in entries}
    if missing := [question_id for question_id in ids if question_id not in by_id]:
        raise EvalError(f"not in the data file: {', '.join(missing)}")
    return [by_id[question_id] for question_id in ids]


def run(*, data, sha256, questions, seed, yes, postgres, out, replay_command=None, template=None,
        judge_model="jev-1.14.0", judge_key=None, parallel=1, ids=None, keep=False, ask_kept=None):
    """Estimate, or with `yes` run, the eval over a seeded subset, or over the questions `ids` names (#236), up
    to `parallel` questions at once (#219); returns (and writes) the report, its questions in that order.
    With `keep`, each question's store and the run's roles are left, listed in `out`/kept.json; `ask_kept`, a
    kept run's `out`, asks its questions of those stores again, with no extraction (#242)."""
    kept_names = None
    if ask_kept:
        kept_names = json.loads((pathlib.Path(ask_kept) / "kept.json").read_text(encoding="utf-8"))["databases"]
        ids = list(kept_names)
    entries = chosen(load(data, sha256), ids) if ids else subset(load(data, sha256), questions, seed=seed)
    if not yes:
        if ask_kept:   # the rerank and the scores before the floor, per question; nothing else is called
            return {"estimate": {"questions": len(entries), "haiku_calls_at_least": 0,
                                 "judge_calls_at_least": 2 * len(entries)}}
        return {"estimate": estimate(entries)}
    command = replay_command or [sys.executable, "-m", "evals.replay"]
    results, stopped = [], None
    kept = {} if keep else None
    halt = threading.Event()   # once set, no question starts: a stop or a crash in any of them

    def question(entry):
        if halt.is_set():
            return None
        try:
            if ask_kept:
                result = again(entry, name=kept_names[entry["question_id"]], postgres=postgres, password=password,
                               command=command, template=template, judge_model=judge_model, judge_key=judge_key)
            else:
                result = one(entry, postgres=postgres, password=password, command=command, template=template,
                             judge_model=judge_model, judge_key=judge_key, kept=kept)
        except BaseException:
            halt.set()
            raise
        if result["stopped"]:   # the account was refused, or a plan limit hit: stop, keeping what was measured
            halt.set()
        return result

    try:
        login = kept_login(postgres) if ask_kept else roles(postgres, keep=keep)
        with login as password:   # the run's login, which question() hands to each one()
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=parallel)
            futures = [pool.submit(question, entry) for entry in entries]
            error = None
            try:
                concurrent.futures.wait(futures)
            finally:   # on Ctrl-C too: start nothing more, let the running questions finish, and keep every result
                halt.set()
                pool.shutdown(cancel_futures=True)
                for future in futures:   # in the subset's order, so a parallel report reads as a sequential one
                    if future.cancelled() or not future.done():
                        continue
                    if future.exception() is not None:
                        error = error or future.exception()
                    elif (result := future.result()) is not None:
                        results.append(result)
                        stopped = stopped or result["stopped"]
            if error is not None:
                raise error
    except (EvalError, KeyboardInterrupt) as exc:   # the questions already measured are paid for: report them
        stopped = f"crashed: {type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        summary = report(results, sha256=sha256, seed=None if ids else seed, judge_model=judge_model, ids=ids,
                         asked_again=str(ask_kept) if ask_kept else None)
        if stopped:
            summary["stopped"] = stopped
        out = pathlib.Path(out)
        out.mkdir(parents=True, exist_ok=True)
        if kept is not None:   # every store made, a crashed question's too, so drop_kept() leaves none behind
            (out / "kept.json").write_text(json.dumps({"databases": kept}, indent=2), encoding="utf-8")
        (out / "report.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        (out / "report.md").write_text(markdown(summary), encoding="utf-8")
    return summary


def main(argv=None):
    pinned = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", required=True, help="the S file; written there first with --download")
    parser.add_argument("--download", action="store_true", help="download the pinned S file to --data first")
    parser.add_argument("--questions", type=int, help="how many to draw (default 20)")
    parser.add_argument("--seed", type=int, help="the draw's seed (default 1)")
    parser.add_argument("--ids", help="comma-separated question ids to run, in place of the seeded subset (#236)")
    parser.add_argument("--yes", action="store_true", help="run, not only estimate")
    parser.add_argument("--postgres", default=os.environ.get("EVAL_DATABASE_URL"),
                        help="a superuser URL on a throwaway Postgres; never the shared store")
    parser.add_argument("--template", help="a Hermes home with the Claude subscription provider set up")
    parser.add_argument("--out", default="eval-report")
    parser.add_argument("--parallel", type=int, default=1,
                        help="questions run at once; more reach the plan's limits sooner (#219)")
    parser.add_argument("--keep", action="store_true",
                        help="leave each question's store and the run's roles, listed in --out's kept.json (#242)")
    parser.add_argument("--ask-kept", metavar="DIR",
                        help="ask a kept run's questions of its stores again: no extraction, no digest (#242)")
    parser.add_argument("--drop-kept", metavar="DIR", help="drop a kept run's stores and roles, then stop (#242)")
    args = parser.parse_args(argv)
    if args.parallel < 1:
        parser.error("--parallel must be 1 or more")
    if args.drop_kept:
        drop_kept(args.postgres, args.drop_kept)
        return 0
    if args.ask_kept and (args.ids is not None or args.questions is not None or args.seed is not None or args.keep):
        parser.error("--ask-kept asks the kept run's questions; leave out --ids, --questions, --seed and --keep")
    ids = None
    if args.ids is not None:   # given at all, even empty: never a silent fallback to a paid seeded run
        ids = [question_id.strip() for question_id in args.ids.split(",") if question_id.strip()]
        if not ids:
            parser.error("--ids names no question")
        if repeated := sorted({question_id for question_id in ids if ids.count(question_id) > 1}):
            parser.error(f"--ids names {', '.join(repeated)} twice")   # it would be paid for and scored twice
        if args.questions is not None or args.seed is not None:
            parser.error("--ids replaces the seeded subset; leave out --questions and --seed")
    if not pinned["sha256"]:
        raise SystemExit(f"{CONFIG.name} records no revision and SHA-256 yet: pin them first (#167)")
    if args.download:
        fetch(pinned["url"].format(revision=pinned["revision"]), args.data, pinned["sha256"])
    summary = run(data=args.data, sha256=pinned["sha256"], questions=20 if args.questions is None else args.questions,
                  seed=1 if args.seed is None else args.seed, yes=args.yes, postgres=args.postgres, out=args.out,
                  template=args.template, parallel=args.parallel, ids=ids, keep=args.keep, ask_kept=args.ask_kept)
    print(json.dumps(summary if "estimate" in summary else {k: v for k, v in summary.items() if k != "per_question"},
                     indent=2))
    return 3 if summary.get("stopped") else 0


if __name__ == "__main__":
    sys.exit(main())
