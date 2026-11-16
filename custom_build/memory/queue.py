"""Facts whose supersede judgement failed, kept until the judge answers (ARCHITECTURE.md 3.3, step 3).

They wait, already redacted, in the plugin's own SQLite database under the Hermes home (section 2),
so a fact is never stored unchecked and never lost.
"""

import contextlib
import dataclasses
import json

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
