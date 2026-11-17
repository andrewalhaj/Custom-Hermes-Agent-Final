"""Prove the queue of digests a stopped process left (issue #156 item 2), from the review of its pull request.

A digest is queued under its process before the model is called. Another process takes it only when that
process is gone, and only one start takes it, so one lost digest costs one more call, not one per session
that starts. The queue is the plugin's SQLite database under a throwaway Hermes home.
"""

import subprocess
import sys

import pytest

from custom_build.memory import queue

PENDING = {"session_id": "s1", "project": "p1", "source": "host-a", "turn": 7, "written_at": "2026-11-17T03:00:00+00:00",
           "transcript": [{"role": "user", "content": "Question 0"}]}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def _gone_pid():
    """The pid of a process that has finished."""
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                          check=True)
    return int(done.stdout)


def _queue_under(pid, boot):
    with queue._open_pending() as db, db:
        return db.execute("insert into pending_digests (pid, boot, digest) values (?, ?, ?)",
                          (pid, boot, queue.json.dumps(PENDING))).lastrowid


def test_a_digest_of_a_running_process_is_not_taken():
    queue.add_digest(PENDING)   # this process: its digest is still being written
    assert queue.orphaned_digests() == []


def test_a_digest_queued_before_this_boot_is_taken_even_when_its_pid_runs_again(monkeypatch):
    # The review: a gateway restarted at boot can get the pid of the one the stop ended.
    monkeypatch.setattr(queue, "_boot", lambda: "boot-2")
    import os
    _queue_under(os.getpid(), "boot-1")
    assert [pending for _, _, pending in queue.orphaned_digests()] == [PENDING]


def test_only_one_start_claims_a_digest_a_stopped_process_left():
    # The review: each session that starts after a restart saw the same orphan and called the model again.
    _queue_under(_gone_pid(), queue._boot())
    [(row_id, owner, _)] = queue.orphaned_digests()
    assert queue.claim(row_id, owner) is True
    assert queue.claim(row_id, owner) is False
    assert queue.orphaned_digests() == []   # claimed by this running process
