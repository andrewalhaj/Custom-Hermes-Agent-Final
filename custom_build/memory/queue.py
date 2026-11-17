"""Facts whose supersede judgement failed, kept until the judge answers (ARCHITECTURE.md 3.3, step 3).

They wait, already redacted, in the plugin's own SQLite database under the Hermes home (section 2),
so a fact is never stored unchecked. One whose request the judge refuses as over its context cannot be
checked at all; it is dropped and recorded, not queued (#251).
"""

import contextlib
import dataclasses
import json
import os
import pathlib

from plugins.plugin_storage import plugin_db

from .store import Fact

BOOT_ID = pathlib.Path("/proc/sys/kernel/random/boot_id")   # Linux: a new value at every boot


def _open():
    db = plugin_db("custom_build", "store_queue.db")
    db.execute("create table if not exists queued (id integer primary key, fact text not null, attempts integer not null)")
    return contextlib.closing(db)


def add(fact):
    with _open() as db, db:   # the inner `db` commits
        db.execute("insert into queued (fact, attempts) values (?, 1)", (json.dumps(dataclasses.asdict(fact)),))


def waiting():
    """Every queued fact, fewest attempts first, then oldest, as (row id, Fact, attempts so far), so a fact
    the judge keeps refusing moves behind the others."""
    with _open() as db:
        rows = db.execute("select id, fact, attempts from queued order by attempts, id").fetchall()
    return [(row_id, Fact(**json.loads(text)), attempts) for row_id, text, attempts in rows]


def done(row_id):
    with _open() as db, db:
        db.execute("delete from queued where id = ?", (row_id,))


def again(row_id):
    with _open() as db, db:
        db.execute("update queued set attempts = attempts + 1 where id = ?", (row_id,))


# A session's digest, queued before its model call so that a process stopped during the call (the gateway
# stopping) leaves it for the next start (#156 item 2). A table of its own: older builds read only `queued`,
# and a row they cannot read must not stop their provider (#229).
def _open_pending():
    db = plugin_db("custom_build", "store_queue.db")
    db.execute("create table if not exists pending_digests (id integer primary key, pid integer not null,"
               " boot text not null, digest text not null)")
    return contextlib.closing(db)


def add_digest(pending):
    """Queue `pending` (the digest's session, project, source, turn, time of writing and redacted transcript)
    under this process; its row id."""
    with _open_pending() as db, db:
        return db.execute("insert into pending_digests (pid, boot, digest) values (?, ?, ?)",
                          (os.getpid(), _boot(), json.dumps(pending))).lastrowid


def orphaned_digests():
    """Each queued digest whose process is gone, as (row id, its owner, pending), oldest first. A row of a
    running process is its digest still being written, not a lost one; one queued before this boot is lost,
    whatever now runs under its pid (a gateway restarted at boot can get the stopped one's pid)."""
    with _open_pending() as db:
        rows = db.execute("select id, pid, boot, digest from pending_digests order by id").fetchall()
    return [(row_id, (pid, boot), json.loads(text)) for row_id, pid, boot, text in rows
            if boot != _boot() or not _running(pid)]


def claim(row_id, owner):
    """Take an orphaned digest for this process, if it still has `owner` (pid, boot): True for exactly one
    of several starts, so one lost digest costs one more call (#156 review). A claimer that stops leaves its
    own gone pid on the row, and the next start takes it."""
    with _open_pending() as db, db:
        return db.execute("update pending_digests set pid = ?, boot = ? where id = ? and pid = ? and boot = ?",
                          (os.getpid(), _boot(), row_id, *owner)).rowcount == 1


def digest_done(row_id):
    with _open_pending() as db, db:
        db.execute("delete from pending_digests where id = ?", (row_id,))


def _boot():
    """This boot's id on Linux, the agent hosts' system; "" where there is none, where only the pid is checked."""
    try:
        return BOOT_ID.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _running(pid):
    try:
        os.kill(pid, 0)   # signal 0 sends nothing; it only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:   # another user's process with that id: running
        return True
    return True
