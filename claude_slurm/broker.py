"""MCP server exposing the broker to Claude Code (stdio transport).

Launched by Claude Code from the per-project mcp.json that `claude-slurm init`
writes. It runs as a normal process, outside Claude's Bash sandbox, and is the
only path from Claude to Slurm.
"""
from __future__ import annotations

import json
import os

from mcp.server.fastmcp import FastMCP

from .core import Broker
from .services import ServiceError, Services

broker = Broker(os.environ["CLAUDE_SLURM_POLICY"])
svc = Services(broker.pol, broker.cfg, broker.policy_path)

mcp = FastMCP(
    "slurm",
    instructions=(
        "Submit and manage Slurm jobs for this project. Jobs run `bash <script>` "
        "inside a fixed Apptainer image with only the project and configured "
        "directories mounted. Call `policy` first to see mounts and limits."
    ),
)


@mcp.tool()
def policy() -> str:
    """Show mounts available inside jobs, allowed partitions and resource limits."""
    return broker.describe()


@mcp.tool()
def submit_job(script: str, partition: str, minutes: int, cpus: int = 1,
               mem_gb: int = 4, gpus: int = 0, name: str = "job",
               args: list[str] | None = None, env: dict[str, str] | None = None,
               array: str | None = None, after_ok: list[str] | None = None,
               after_any: list[str] | None = None, account: str | None = None,
               qos: str | None = None) -> str:
    """Submit a job. `script` is a bash script path relative to the project root;
    it runs as `bash <project_mount>/<script> <args...>` inside the container with
    cwd = project mount. #SBATCH lines in the script are ignored: pass resources
    here. `env` sets variables inside the container. `array` is a Slurm array spec
    (e.g. "0-9%2"; SLURM_ARRAY_TASK_ID is forwarded). `after_ok` lists job ids
    (from this broker) that must succeed first; `after_any` lists ids that must
    finish in any state. `account` / `qos` pick one of the policy's allowed
    values (see `policy`); omitted, the policy default is used. Returns the job
    id and log path."""
    res = broker.submit(script=script, partition=partition, minutes=minutes, cpus=cpus,
                        mem_gb=mem_gb, gpus=gpus, name=name, args=args, env=env,
                        array=array, after_ok=after_ok, after_any=after_any,
                        account=account, qos=qos)
    return json.dumps(res, indent=2)


@mcp.tool()
def list_jobs() -> str:
    """List active (pending/running) jobs submitted through this broker."""
    return broker.list_jobs()


@mcp.tool()
def job_info(job_id: str) -> str:
    """Accounting info (state, exit code, elapsed, memory) and log paths for a job."""
    return broker.job_info(job_id)


@mcp.tool()
def update_job(job_id: str, minutes: int | None = None, hold: bool | None = None,
               name: str | None = None) -> str:
    """Change a broker-submitted job: time limit (Slurm usually only allows
    decreasing it), hold/release a pending job, or rename it."""
    return broker.update(job_id, minutes=minutes, hold=hold, name=name)


@mcp.tool()
def cancel_job(job_id: str) -> str:
    """Cancel a broker-submitted job (array parent id cancels the whole array)."""
    return broker.cancel(job_id)


@mcp.tool()
def run_allowed(name: str, params: dict[str, str] | None = None) -> str:
    """Run one of the policy's allowed commands (see `policy`) outside the
    sandbox. The command line is fixed; `params` fills its placeholders, e.g.
    {"rel_path": "some/dir"}. Returns exit code and output tails."""
    return json.dumps(broker.run_command(name, params), indent=2)


def _svc(fn, *a):
    try:
        return json.dumps(fn(*a), indent=2)
    except ServiceError as e:
        return f"error: {e}"


@mcp.tool()
def start_service(name: str, script: str, args: list[str] | None = None) -> str:
    """Start a long-running background script (e.g. a watcher loop) that keeps
    running after this session ends. `script` is a bash script relative to the
    project root. It runs on the login node in a sandbox like your shell's, but
    with no network. Inside it, use `cs-slurm list|info|submit|cancel` instead
    of squeue/sbatch, and `cs-run NAME KEY=VALUE` for allowed commands.
    Output goes to the service log (see `service_status`)."""
    return _svc(svc.start, name, script, args)


@mcp.tool()
def list_services() -> str:
    """List this project's background services and their state."""
    return _svc(svc.list)


@mcp.tool()
def service_status(name: str, lines: int = 40) -> str:
    """State, exit status and the last `lines` lines of a service's log."""
    return _svc(svc.status, name, lines)


@mcp.tool()
def stop_service(name: str) -> str:
    """Stop a background service."""
    return _svc(svc.stop, name)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
