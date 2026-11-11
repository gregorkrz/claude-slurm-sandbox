"""Render the per-project Claude Code config files from a policy."""
from __future__ import annotations

import sys

from . import services
from .paths import PREFIX, protected_paths

SLURM_CLIENTS = ("sbatch", "srun", "salloc", "scancel", "scontrol", "squeue", "sacct", "sinfo")
SELF_EDIT_DENY = [
    "Edit(./.claude/**)", "Edit(./.mcp.json)", "Edit(./CLAUDE.local.md)",
    "Edit(~/.claude/**)", "Edit(~/.claude.json)",
    "Edit(~/.bashrc)", "Edit(~/.bash_profile)", "Edit(~/.profile)", "Edit(~/.bash_login)",
    "Edit(~/.zshrc)", "Edit(~/.zprofile)", "Edit(~/.ssh/**)", "Edit(~/.config/systemd/**)",
]
READ_ONLY_TOOLS = ("policy", "list_jobs", "job_info")


def _perm_abs(p) -> str:
    # Permission rules use //abs for absolute paths.
    return "/" + str(p)


def settings(pol: dict) -> dict:
    dirs = [str(p) for p in protected_paths()] + [pol["logs_dir"]]
    prot = dirs + [pol["image"]["path"]]
    login = pol["login"]
    deny = [f"Edit({_perm_abs(p)}/**)" for p in dirs]
    deny += [f"Edit({_perm_abs(pol['image']['path'])})"]
    deny += SELF_EDIT_DENY
    deny += [f"Read({p}/**)" if not p.endswith("**") else f"Read({p})"
             for p in login["deny_read"]]
    for c in SLURM_CLIENTS:  # fail fast; the sandbox blocks them anyway
        deny += [f"Bash({c})", f"Bash({c} *)"]

    allow = [f"mcp__slurm__{t}" for t in READ_ONLY_TOOLS]
    # Edit/Write run in-process, outside bwrap, so mirror the sandbox's write
    # set here. Deny rules above still win for .claude/, policy, image, logs.
    allow += [f"Edit({_perm_abs(p)}/**)" for p in [pol["project"], *login["extra_write"]]]
    ask = []
    for tool, auto in (("submit_job", pol["claude"]["auto_submit"]),
                       ("cancel_job", pol["claude"]["auto_cancel"]),
                       ("update_job", pol["claude"]["auto_cancel"])):
        (allow if auto else ask).append(f"mcp__slurm__{tool}")
    if (pol.get("services") or {}).get("enabled"):
        ask.append("mcp__slurm__start_service")
        allow += [f"mcp__slurm__{t}" for t in ("list_services", "service_status",
                                                "stop_service")]
    cmds = pol.get("allowed_commands") or {}
    if cmds:
        any_ask = any(c["ask"] for c in cmds.values())
        (ask if any_ask else allow).append("mcp__slurm__run_allowed")

    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "filesystem": {
                "allowWrite": list(login["extra_write"]),
                "denyWrite": prot + ["./.claude", "./.mcp.json"],
                # Service broker sockets: Claude's shell must not reach them.
                "denyRead": [*login["deny_read"], str(services.runtime_dir() / "claude-slurm")],
            },
            "network": {
                "allowedDomains": list(login["allowed_domains"]),
                "allowAllUnixSockets": False,
                "allowUnixSockets": [],
                "allowLocalBinding": False,
            },
        },
        "permissions": {"deny": deny, "ask": ask, "allow": allow},
    }


def mcp_config(pol: dict, policy_path) -> dict:
    return {"mcpServers": {"slurm": {
        "type": "stdio",
        "command": sys.executable,
        # -I: never import from the cwd (= the project, writable by Claude).
        "args": ["-I", str(PREFIX / "bin" / "_broker.py")],
        "env": {"CLAUDE_SLURM_POLICY": str(policy_path)},
    }}}


def context_md(pol: dict) -> str:
    s = pol["slurm"]
    binds = "\n".join(f"  - `{b['container']}` ({b['mode']}) ← host `{b['host']}`"
                      for b in pol["binds"]) or "  - (none)"
    extra = ", ".join(f"`{p}`" for p in pol["login"]["extra_write"]) or "none"
    return f"""# HPC sandbox (claude-slurm)

You are on an HPC login node inside a sandbox. Treat these as hard constraints,
not obstacles to work around.

- Your Bash tool can write only inside the project `{pol['project']}`
  (extra writable dirs: {extra}). Don't run heavy compute on the login node.
- You cannot reach Slurm from Bash: `sbatch`, `srun`, `squeue`, `scancel` etc.
  will fail. Use the `slurm` MCP tools instead: `policy`, `submit_job`,
  `list_jobs`, `job_info`, `update_job`, `cancel_job`.
- A job runs `bash {pol['project_mount']}/<script> <args>` inside the Apptainer
  image `{pol['image']['path']}`, with cwd `{pol['project_mount']}`.
  Mounted in the job:
  - `{pol['project_mount']}` (rw) ← the project directory
{binds}
  Nothing else from the host is visible (no host $HOME, no scratch).
  Use container paths inside job scripts, not host paths.
- Job scripts are plain bash. `#SBATCH` lines are ignored: give resources as
  `submit_job` arguments. Logs go to `{pol['logs_dir']}/<jobid>.out`
  (`<jobid>_<task>.out` for arrays), outside the project. You can read them
  but not write there; `job_info` lists a job's log files.
- Accounts: default {s.get('account')!r}, may request {s.get('accounts') or []};
  QOS: default {s.get('qos')!r}, may request {s.get('qos_list') or []} (`account` /
  `qos` arguments of `submit_job`).{_deny_md(s)}
- Limits: partitions {s['partitions']}, ≤{s['max_minutes']} min, ≤{s['max_cpus']} CPUs,
  ≤{s['max_mem_gb']} GB, ≤{s['max_gpus']} GPUs, ≤{s['max_active']} active jobs,
  arrays ≤{s['max_array']} tasks.
- Before a long or large job, run a short test job first.
""" + _commands_md(pol) + _services_md(pol)


def _deny_md(s: dict) -> str:
    deny = s.get("deny_accounts") or []
    if not deny:
        return ""
    return (f"\n  Never use {', '.join(f'`{d}`' for d in deny)} (or any `<name>:*` of them):"
            " the broker rejects it.")


def _services_md(pol: dict) -> str:
    s = pol.get("services") or {}
    if not s.get("enabled"):
        return ""
    return f"""- Background services: `start_service` runs a project script that keeps going
  after this session (≤{s['max_services']} at once, ≤{s['max_lifetime_hours']} h each). It
  has your shell's write access, no network, and its own /tmp. squeue/sbatch don't work
  there; use `cs-slurm list --json`, `cs-slurm info ID`
  {"`cs-slurm submit ...`, " if s['allow_submit'] else ""}{"`cs-slurm cancel ID`, " if s['allow_cancel'] else ""}and
  {"`cs-run NAME KEY=VALUE` for: " + ", ".join(s['commands']) if s['commands'] else "no allowed commands"}.
  Logs: `{pol['logs_dir']}/services/<name>.log` (read-only to you).
"""


def _commands_md(pol: dict) -> str:
    cmds = pol.get("allowed_commands") or {}
    if not cmds:
        return ""
    lines = ["- Allowed commands (run outside the sandbox by `run_allowed`; the command",
             "  line is fixed, you only fill its parameters):"]
    for n, c in cmds.items():
        params = ", ".join(f"`{k}`" + (f" (path under `{d['root']}`)" if d["type"] == "rel_path"
                                       else f" (one of {d['values']})")
                           for k, d in c["placeholders"].items()) or "none"
        lines.append(f"  - `{n}`: {c['description'] or 'no description'}. Params: {params}")
    return "\n".join(lines) + "\n"
