"""Facts whose supersede judgement failed, kept until the judge answers (ARCHITECTURE.md 3.3, step 3).

They wait, already redacted, in the plugin's own SQLite database under the Hermes home (section 2),
so a fact is never stored unchecked and never lost.
"""

import contextlib
import dataclasses
import json
import os

from plugins.plugin_storage import plugin_db

from .store import Fact


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
               " digest text not null)")
    return contextlib.closing(db)


def add_digest(pending):
    """Queue `pending` (the digest's session, project, source, turn, time of writing and redacted transcript)
    under this process; its row id."""
    with _open_pending() as db, db:
        return db.execute("insert into pending_digests (pid, digest) values (?, ?)",
                          (os.getpid(), json.dumps(pending))).lastrowid


def orphaned_digests():
    """Each queued digest whose process is gone, as (row id, pending), oldest first. A row of a running
    process is its digest still being written, not a lost one."""
    with _open_pending() as db:
        rows = db.execute("select id, pid, digest from pending_digests order by id").fetchall()
    return [(row_id, json.loads(text)) for row_id, pid, text in rows if not _running(pid)]


def digest_done(row_id):
    with _open_pending() as db, db:
        db.execute("delete from pending_digests where id = ?", (row_id,))


def _running(pid):
    try:
        os.kill(pid, 0)   # signal 0 sends nothing; it only asks whether the process exists
    except ProcessLookupError:
        return False
    except PermissionError:   # another user's process with that id: running
        return True
    return True
