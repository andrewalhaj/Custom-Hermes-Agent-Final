"""The job-health check, `hermes custom-build job-health` (ARCHITECTURE.md section 6.3, OPERATIONS.md section 4).

Hermes cron runs it daily on the job host, before the report. It records host.missing for each agent
host on its list with no heartbeat in the last 24 hours; job.missed for each job in JOBS with no
job.succeeded in that time; job.no_output for each whose last two runs took input and put nothing out;
then a job.succeeded event of its own. The daily report sends what it records.
"""

from . import failures, jobs, settings

# The jobs whose runs record job.succeeded. Not the heartbeat, which host.missing covers, nor this check,
# whose own missing job.succeeded is the report's to notice; the report and retention add themselves.
JOBS = ()


def run(ctx):
    """Check every host on the job host's list; return the exit code, as every job does (`jobs.run`)."""
    hosts = settings.agent_hosts(ctx)
    missing = [] if hosts else ["plugins.entries.custom_build.settings." + settings.AGENT_HOSTS]
    return jobs.run(ctx, "job-health", lambda conn, machine: check(conn, machine, hosts), missing=missing)


def check(conn, machine, hosts):
    """Record each missing host and job finding, then this run's success; return whether every row was written."""
    written = []
    for host in hosts:
        if not conn.execute("select 1 from memory.events where code = 'host.heartbeat' and source = %s"
                            " and at > now() - interval '24 hours' limit 1", (host,)).fetchone():
            written.append(failures.record(conn, "host.missing", "failed", source=machine, detail={"host": host}))
    for job in JOBS:
        if not conn.execute("select 1 from memory.events where code = 'job.succeeded' and detail->>'job' = %s"
                            " and at > now() - interval '24 hours' limit 1", (job,)).fetchone():
            written.append(failures.record(conn, "job.missed", "failed", source=machine, detail={"job": job}))
        last = conn.execute("select detail from memory.events where code = 'job.succeeded' and detail->>'job' = %s"
                            " order by at desc limit 2", (job,)).fetchall()
        if len(last) == 2 and all(d.get("input", 0) > 0 and d.get("output") == 0 for (d,) in last):
            written.append(failures.record(conn, "job.no_output", "failed", source=machine, detail={"job": job}))
    if not all(written):   # a run that lost a finding has not succeeded; jobs.run reports events.write_failed
        return False
    return failures.record(conn, "job.succeeded", "ok", source=machine, detail={"job": "job-health"})
