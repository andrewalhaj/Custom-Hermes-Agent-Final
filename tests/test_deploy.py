"""Prove the deploy script (issue #181, OPERATIONS.md section 8).

Every outside effect of `scripts/deploy.py` goes through three callables it is handed: `run` for local and
ssh commands, `http` for the store's Management API, and `ask` for the operator's answer. The tests hand it
fakes, so nothing here reaches an agent host or the store. The real `run` and `http` are tested against a
local process and a local HTTP server.
"""

import http.server
import json
import shlex
import subprocess
import sys
import threading
import time
import typing

import pytest

NEW = "a" * 40
OLD = "b" * 40
TOKEN = "sbp_" + "0" * 20   # made up; it must never appear in anything the script prints
ENV = {
    "DEPLOY_REPO": "owner/repo",
    "DEPLOY_SSH": "agent@agent-host",
    "DEPLOY_HERMES": "/opt/hermes/bin/hermes",
    "DEPLOY_HERMES_HOME": "/home/agent/.hermes",
    "DEPLOY_UNIT": "hermes-gateway",
    "DEPLOY_RESTART": "ssh root@agent-host systemctl restart hermes-gateway",
    "SUPABASE_PROJECT_REF": "projectref",
    "SUPABASE_ACCESS_TOKEN": TOKEN,
}


@pytest.fixture
def deploy():
    from scripts import deploy as module
    return module


class Fake:
    """A host, a checkout, a CI result and a store, answering by the shape of each command."""

    def __init__(self):
        self.calls = []                 # every argv handed to run
        self.fail = {}                  # a word in a command -> how many of its next runs fail
        self.on_main = True
        self.ci = [{"conclusion": "success"}]
        self.files = ["migrations/0001_a.sql", "migrations/0002_b.sql", "migrations/0003_c.sql"]
        self.applied = ["0001_a"]
        self.metadata = {"custom_build": {"pinned": True, "revision": OLD}}
        self.posted = []                # migration names sent to the store, in order
        self.host_steps_at_post = []    # how many steps had touched the host when each one was sent
        self.timeouts = {}              # a word in a command -> the timeout it was given
        self.install_fails_after_publishing = False
        self.sticks = True              # whether an applied migration then shows in the history
        self.history_fails = False
        self.answers = ["yes"]
        self.questions = []
        self.lines = []

    def run(self, argv, timeout=120):
        self.calls.append(list(argv))
        text = " ".join(argv)
        for word in ("plugins install", "systemctl restart", "custom-build check"):
            if word in text:
                self.timeouts[word] = timeout
        installing = NEW if NEW in text else OLD
        for word, left in self.fail.items():
            if word in text and left:
                self.fail[word] = left - 1
                if "plugins install" in text and self.install_fails_after_publishing:
                    self.metadata = {"custom_build": {"pinned": True, "revision": installing}}
                return subprocess.CompletedProcess(argv, 1, "", f"{word} failed")
        if "plugins install" in text:
            self.metadata = {"custom_build": {"pinned": True, "revision": installing}}
        out = ""
        code = 0
        if "merge-base" in text:
            code = 0 if self.on_main else 1
        elif argv[:3] == ["gh", "run", "list"]:
            out = json.dumps(self.ci)
        elif "ls-tree" in text:
            assert "--full-tree" in argv    # without it git lists relative to the directory it is run from
            out = "\n".join(self.files) + "\n"
        elif argv[:2] == ["git", "show"]:
            out = f"-- {argv[2]}\n"
        elif ".install-metadata.json" in text:
            out = json.dumps(self.metadata)
        return subprocess.CompletedProcess(argv, code, out, "")

    def http(self, method, url, token, body=None):
        assert token == TOKEN
        if url.endswith("/database/query/read-only"):
            if self.history_fails:
                from scripts import deploy as module
                raise module.DeployError("POST /database/query/read-only: HTTP 500")
            return [{"name": name} for name in self.applied]
        assert url.endswith("/database/migrations"), url
        self.posted.append(body["name"])
        self.host_steps_at_post.append(len(self.steps()))
        if self.sticks:
            self.applied.append(body["name"])
        return []

    def ask(self, question):
        self.questions.append(question)
        return self.answers.pop(0)

    def out(self, line):
        self.lines.append(line)

    def go(self, deploy, commit=NEW, env=None):
        return deploy.deploy(commit, ENV if env is None else env, self.run, self.http, self.ask, self.out)

    def steps(self):
        """What was done to the host, in order."""
        names = []
        for argv in self.calls:
            text = " ".join(argv)
            if "plugins install" in text:
                names.append("install " + ("new" if NEW in text else "old" if OLD in text else "?"))
            elif "custom-build check" in text:
                names.append("check")
            elif text == ENV["DEPLOY_RESTART"]:
                names.append("restart")
            elif "is-active" in text:
                names.append("active")
            elif "custom-build heartbeat" in text:
                names.append("heartbeat")
        return names

    def printed(self):
        return "\n".join(self.lines + self.questions)


HAPPY = ["install new", "check", "restart", "active", "heartbeat"]
BACK = ["install old", "check", "restart", "active", "heartbeat"]


# Step 1 and the settings: refuse before anything is read or changed.

@pytest.mark.parametrize("missing", sorted(ENV))
def test_a_missing_setting_is_named_and_nothing_runs(deploy, missing):
    fake = Fake()
    env = {key: ("  " if key == missing else value) for key, value in ENV.items()}
    assert fake.go(deploy, env=env) != 0
    assert missing in fake.printed()
    assert fake.calls == [] and fake.posted == []


def test_a_short_hash_is_refused(deploy):
    fake = Fake()
    assert fake.go(deploy, commit=NEW[:12]) != 0
    assert "40" in fake.printed()
    assert fake.steps() == [] and fake.posted == []


def test_a_commit_that_is_not_on_main_is_refused(deploy):
    fake = Fake()
    fake.on_main = False
    assert fake.go(deploy) != 0
    assert "origin/main" in fake.printed()
    assert fake.steps() == [] and fake.posted == []


@pytest.mark.parametrize("runs", [[], [{"conclusion": "failure"}], [{"conclusion": ""}],
                                  [{"conclusion": "success"}, {"conclusion": "failure"}]])
def test_a_commit_without_a_green_ci_run_is_refused(deploy, runs):
    fake = Fake()
    fake.ci = runs
    assert fake.go(deploy) != 0
    assert "CI" in fake.printed()
    assert fake.steps() == [] and fake.posted == []


# Step 2: migrations.

def test_pending_migrations_are_listed_and_applied_in_order_after_yes(deploy):
    fake = Fake()
    assert fake.go(deploy) == 0
    assert fake.posted == ["0002_b", "0003_c"]
    assert len(fake.questions) == 1
    assert fake.printed().index("0002_b") < fake.printed().index("0003_c")
    assert fake.steps() == HAPPY


def test_migrations_are_applied_before_the_host_changes(deploy):
    fake = Fake()
    fake.fail = {"plugins install": 1}
    fake.go(deploy)
    assert fake.posted == ["0002_b", "0003_c"]
    assert fake.host_steps_at_post == [0, 0]     # nothing had touched the host when either was sent


def test_with_nothing_pending_nobody_is_asked(deploy):
    fake = Fake()
    fake.applied = ["0001_a", "0002_b", "0003_c"]
    assert fake.go(deploy) == 0
    assert fake.questions == [] and fake.posted == []
    assert fake.steps() == HAPPY


@pytest.mark.parametrize("answer", ["", "y", "no", "YES please"])
def test_any_answer_but_yes_stops_with_the_host_untouched(deploy, answer):
    fake = Fake()
    fake.answers = [answer]
    assert fake.go(deploy) != 0
    assert fake.posted == [] and fake.steps() == []


def test_a_migration_missing_from_the_history_after_it_is_applied_stops_the_deploy(deploy):
    fake = Fake()
    fake.sticks = False
    assert fake.go(deploy) != 0
    assert fake.posted == ["0002_b"]            # the second one is not sent
    assert "0002_b" in fake.printed()
    assert fake.steps() == []


def test_an_unreadable_history_is_a_refusal(deploy):
    fake = Fake()
    fake.history_fails = True
    assert fake.go(deploy) != 0
    assert "HTTP 500" in fake.printed()
    assert fake.posted == [] and fake.steps() == []


# Step 3: the commit the host runs now.

@pytest.mark.parametrize("metadata", [{}, {"custom_build": {"pinned": False, "revision": OLD}},
                                      {"custom_build": {"pinned": True, "revision": OLD[:8]}}])
def test_a_host_with_no_pinned_install_is_refused_before_anything_changes(deploy, metadata):
    fake = Fake()
    fake.metadata = metadata
    assert fake.go(deploy) != 0
    assert "pinned" in fake.printed()
    assert fake.posted == [] and fake.steps() == []


def test_an_unreadable_install_record_is_refused_before_anything_changes(deploy):
    fake = Fake()
    fake.fail = {".install-metadata.json": 1}
    assert fake.go(deploy) != 0
    assert fake.posted == [] and fake.steps() == []


# Steps 4 to 8: the host update, its smoke test and its rollback.

def test_a_deploy_installs_checks_restarts_and_smoke_tests_in_that_order(deploy):
    fake = Fake()
    assert fake.go(deploy) == 0
    assert fake.steps() == HAPPY
    install = next(argv for argv in fake.calls if "plugins install" in " ".join(argv))
    assert install[:2] == ["ssh", "agent@agent-host"]
    # One string for the host's shell, quoted so that `#` and spaces reach Hermes as written.
    assert shlex.split(install[2]) == [
        "env", "HERMES_HOME=/home/agent/.hermes", "/opt/hermes/bin/hermes", "plugins", "install",
        "owner/repo#custom_build", "--force", "--ref", NEW, "--yes-deps"]


def test_the_summary_names_both_commits_and_the_migrations_applied(deploy):
    fake = Fake()
    fake.go(deploy)
    summary = fake.printed()
    assert OLD in summary and NEW in summary
    assert "0002_b" in summary and "0003_c" in summary


def test_a_failed_check_never_restarts_the_new_code_and_rolls_back(deploy):
    fake = Fake()
    fake.fail = {"custom-build check": 1}
    assert fake.go(deploy) != 0
    assert fake.steps() == ["install new", "check", *BACK]
    assert "check" in fake.printed() and OLD in fake.printed()


@pytest.mark.parametrize("word, done", [
    ("systemctl restart", ["install new", "check", "restart"]),
    ("is-active", ["install new", "check", "restart", "active"]),
    ("custom-build heartbeat", ["install new", "check", "restart", "active", "heartbeat"]),
])
def test_a_failed_step_rolls_back_to_the_previous_commit(deploy, word, done):
    fake = Fake()
    fake.fail = {word: 1}
    code = fake.go(deploy)
    assert code != 0
    assert fake.steps() == [*done, *BACK]
    assert "rolled back" in fake.printed()


def test_a_rollback_that_fails_is_reported_as_failed(deploy):
    fake = Fake()
    fake.fail = {"custom-build heartbeat": 2}
    failed = fake.go(deploy)
    rolled_back = Fake()
    rolled_back.fail = {"custom-build heartbeat": 1}
    assert failed not in (0, rolled_back.go(deploy))    # its own exit code
    assert "ROLLBACK FAILED" in fake.printed()
    assert "rolled back" not in fake.printed()


def test_migrations_are_never_rolled_back(deploy):
    fake = Fake()
    fake.fail = {"custom-build check": 1}
    fake.go(deploy)
    assert fake.applied == ["0001_a", "0002_b", "0003_c"]
    assert "stay applied" in fake.printed()


def test_the_token_is_never_printed(deploy):
    for setup in ({}, {"custom-build check": 1}, {"custom-build heartbeat": 2}):
        fake = Fake()
        fake.fail = dict(setup)
        fake.go(deploy)
        assert TOKEN not in fake.printed()
        assert TOKEN not in json.dumps(fake.calls)


# The real runners and the command line.

def test_run_returns_a_commands_exit_code_and_output(deploy):
    done = deploy.run([sys.executable, "-c", "import sys; print('seen'); sys.exit(3)"])
    assert done.returncode == 3 and done.stdout.strip() == "seen"


def test_run_turns_a_timeout_into_a_failed_step(deploy):
    with pytest.raises(deploy.DeployError, match="timed out"):
        deploy.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


class Handler(http.server.BaseHTTPRequestHandler):
    seen: typing.ClassVar[list] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Handler.seen.append((self.path, self.headers["Authorization"], json.loads(body)))
        if self.path.endswith("/slow"):
            time.sleep(1)
        status = 500 if self.path.endswith("/broken") else 200
        self.send_response(status)
        self.end_headers()
        if self.path.endswith("/garbage"):
            self.wfile.write(b"<html>not json</html>")
            return
        self.wfile.write(b'{"message": "it broke"}' if status == 500 else b'[{"name": "0001_a"}]')

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Handler.seen = []
    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_http_sends_the_token_as_a_bearer_and_returns_the_json(deploy, server):
    assert deploy.http("POST", f"{server}/ok", TOKEN, {"query": "select 1"}) == [{"name": "0001_a"}]
    assert Handler.seen == [("/ok", f"Bearer {TOKEN}", {"query": "select 1"})]


def test_http_reports_an_error_status_without_the_token(deploy, server):
    with pytest.raises(deploy.DeployError) as caught:
        deploy.http("POST", f"{server}/broken", TOKEN, {"query": "select 1"})
    assert "HTTP 500" in str(caught.value) and "it broke" in str(caught.value)
    assert TOKEN not in str(caught.value)


def test_the_command_line_needs_exactly_one_commit(deploy, capsys):
    assert deploy.main(["deploy.py"]) != 0
    assert "usage" in capsys.readouterr().err.lower()


# Findings from the review of #181's pull request.

def test_a_history_that_records_names_with_the_sql_ending_still_matches(deploy):
    fake = Fake()
    fake.applied = ["0001_a.sql", "0002_b.sql", "0003_c.sql"]
    assert fake.go(deploy) == 0
    assert fake.questions == [] and fake.posted == []    # nothing is sent a second time


def test_only_sql_files_in_migrations_are_migrations(deploy):
    fake = Fake()
    fake.files = [*fake.files, "migrations/README.md"]
    assert fake.go(deploy) == 0
    assert fake.posted == ["0002_b", "0003_c"]


def test_a_commit_with_no_migration_files_is_refused(deploy):
    fake = Fake()
    fake.files = []     # what git prints when the path is wrong: it must not read as "none pending"
    assert fake.go(deploy) != 0
    assert "migrations" in fake.printed()
    assert fake.steps() == []


def test_each_migration_is_named_before_it_is_sent(deploy):
    fake = Fake()
    fake.sticks = False
    fake.go(deploy)
    assert "applying 0002_b" in fake.printed()


def test_a_failed_install_that_left_the_previous_commit_in_place_restarts_nothing(deploy):
    fake = Fake()
    fake.fail = {"plugins install": 1}
    assert fake.go(deploy) == 1
    assert fake.steps() == ["install new"]
    assert "ROLLBACK" not in fake.printed() and "rolled back" not in fake.printed()
    assert OLD in fake.printed() and "0002_b" in fake.printed()


def test_a_failed_install_that_replaced_the_previous_commit_rolls_back(deploy):
    fake = Fake()
    fake.fail = {"plugins install": 1}
    fake.install_fails_after_publishing = True
    assert fake.go(deploy) != 0
    assert fake.steps() == ["install new", *BACK]
    assert "rolled back" in fake.printed()


def test_a_failed_install_whose_record_cannot_be_read_rolls_back(deploy):
    fake = Fake()
    fake.fail = {"plugins install": 1, ".install-metadata.json": 0}
    original = fake.run

    def run(argv, timeout=120):
        if "plugins install" in " ".join(argv):
            fake.fail[".install-metadata.json"] = 1     # the next read of the record fails
        return original(argv, timeout=timeout)

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) != 0
    assert fake.steps() == ["install new", *BACK]


def test_the_install_and_the_restart_get_their_own_long_timeouts(deploy):
    fake = Fake()
    fake.go(deploy)
    assert fake.timeouts["plugins install"] == 600
    assert fake.timeouts["systemctl restart"] == 660    # above the unit's 10-minute start timeout
    assert fake.timeouts["custom-build check"] == 120


def test_run_turns_a_missing_program_into_a_failed_step(deploy):
    with pytest.raises(deploy.DeployError, match="no-such-program-for-the-deploy-test"):
        deploy.run(["no-such-program-for-the-deploy-test", "now"])


def test_a_restart_command_that_cannot_start_rolls_back(deploy):
    fake = Fake()
    original = fake.run

    def run(argv, timeout=120):
        if " ".join(argv) == ENV["DEPLOY_RESTART"] and not fake.fail.get("used"):
            fake.fail["used"] = 1
            fake.calls.append(list(argv))
            raise deploy.DeployError("ssh: not found on this machine")
        return original(argv, timeout=timeout)

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) == 2
    assert fake.steps() == ["install new", "check", "restart", *BACK]


def test_run_gives_a_command_no_terminal_to_wait_on(deploy, monkeypatch):
    given = {}

    def record(argv, **kwargs):     # pytest already closes stdin, so read what run asks for, not what the child sees
        given.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(deploy.subprocess, "run", record)
    deploy.run(["true"])
    assert given["stdin"] is subprocess.DEVNULL


def test_http_reports_a_host_it_cannot_reach(deploy):
    with pytest.raises(deploy.DeployError, match="could not reach"):
        deploy.http("POST", "http://127.0.0.1:9/unreachable", TOKEN, {})


def test_http_reports_an_answer_that_never_comes(deploy, server):
    with pytest.raises(deploy.DeployError, match="could not reach"):
        deploy.http("POST", f"{server}/slow", TOKEN, {}, timeout=0.3)



# Findings from the second review round.

def test_a_restart_command_that_cannot_be_split_is_a_refusal(deploy):
    fake = Fake()
    assert fake.go(deploy, env={**ENV, "DEPLOY_RESTART": "ssh root@agent-host 'systemctl restart"}) != 0
    assert "DEPLOY_RESTART" in fake.printed()
    assert fake.calls == [] and fake.posted == []


def test_run_turns_a_program_it_may_not_execute_into_a_failed_step(deploy, tmp_path):
    program = tmp_path / "restart"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o644)
    with pytest.raises(deploy.DeployError, match="restart"):
        deploy.run([str(program)])


def test_an_install_that_timed_out_is_not_taken_as_left_in_place(deploy):
    fake = Fake()
    original = fake.run

    def run(argv, timeout=120):
        if "plugins install" in " ".join(argv) and NEW in " ".join(argv):
            fake.calls.append(list(argv))
            raise deploy.Unknown("ssh: timed out after 600 seconds")   # the install may still finish on the host
        return original(argv, timeout=timeout)

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) == 2
    assert fake.steps() == ["install new", *BACK]


def test_run_raises_unknown_for_a_timeout(deploy):
    with pytest.raises(deploy.Unknown):
        deploy.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


@pytest.mark.parametrize("text", ["not json", "[]", '"a string"'])
def test_an_install_record_that_is_not_a_json_object_is_refused(deploy, text):
    fake = Fake()
    original = fake.run

    def run(argv, timeout=120):
        done = original(argv, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, text, "") if ".install-metadata.json" in " ".join(argv) else done

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) == 1
    assert "installed commit" in fake.printed()
    assert fake.posted == [] and fake.steps() == []


def test_a_failed_install_with_a_record_that_is_not_json_rolls_back(deploy):
    fake = Fake()
    fake.fail = {"plugins install": 1}
    original = fake.run
    reads = []

    def run(argv, timeout=120):
        done = original(argv, timeout=timeout)
        if ".install-metadata.json" in " ".join(argv):
            reads.append(1)
            if len(reads) == 2:     # the read after the failed install
                return subprocess.CompletedProcess(argv, 0, "{truncated", "")
        return done

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) == 2
    assert fake.steps() == ["install new", *BACK]


def test_http_reports_an_answer_that_is_not_json(deploy, server):
    with pytest.raises(deploy.DeployError, match="not JSON"):
        deploy.http("POST", f"{server}/garbage", TOKEN, {})


# Findings from the third review round.

def test_run_survives_output_that_is_not_utf8(deploy):
    done = deploy.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([255, 10]))"])
    assert done.returncode == 0     # a decode error here would escape after the host had changed


def test_an_install_whose_ssh_connection_failed_is_not_taken_as_left_in_place(deploy):
    fake = Fake()
    original = fake.run

    def run(argv, timeout=120):
        if "plugins install" in " ".join(argv) and NEW in " ".join(argv):
            fake.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 255, "", "Connection to agent-host closed by remote host.")
        return original(argv, timeout=timeout)

    assert deploy.deploy(NEW, ENV, run, fake.http, fake.ask, fake.out) == 2    # ssh's own exit code: unknown
    assert fake.steps() == ["install new", *BACK]
