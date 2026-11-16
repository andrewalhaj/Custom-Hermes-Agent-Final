"""The job-health check, `hermes custom-build job-health` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it daily on the job host, before the report. It records host.missing for each agent
host on its list with no heartbeat in the last 24 hours, then a job.succeeded event of its own; the
daily report sends what it records.
"""

from . import failures, jobs, settings


def run(ctx):
    """Check every host on the job host's list; return the exit code, as every job does (`jobs.run`)."""
    hosts = settings.agent_hosts(ctx)
    missing = [] if hosts else ["plugins.entries.custom_build.settings." + settings.AGENT_HOSTS]
    return jobs.run(ctx, "job-health", lambda conn, machine: check(conn, machine, hosts), missing=missing)


def check(conn, machine, hosts):
    """Record each missing host, then this run's success; return whether that success was written."""
    for host in hosts:
        if not conn.execute("select 1 from memory.events where code = 'host.heartbeat' and source = %s"
                            " and at > now() - interval '24 hours' limit 1", (host,)).fetchone():
            failures.record(conn, "host.missing", "failed", source=machine, detail={"host": host})
    return failures.record(conn, "job.succeeded", "ok", source=machine, detail={"job": "job-health"})
