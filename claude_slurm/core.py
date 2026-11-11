"""Broker core: the only code that talks to Slurm on Claude's behalf.

Used by the MCP server (broker.py) and by `claude-slurm check --job`.
"""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import subprocess
import time
from pathlib import Path

from . import commands, jobscript, services
from .jobscript import JOBID_RE, RequestError
from .policy import image_unchanged, load

# Variables that would change sbatch/scontrol behaviour behind our back.
_SCRUB_PREFIXES = ("SBATCH_", "SALLOC_", "SRUN_", "SLURM_", "SQUEUE_", "SACCT_",
                   "SCONTROL_", "SCANCEL_")
_KEEP = {"SLURM_CONF"}


class BrokerError(RuntimeError):
    pass


class Broker:
    def __init__(self, policy_path: str | Path):
        self.policy_path = Path(policy_path)
        self.pol = load(self.policy_path)
        self.cfg = self.policy_path.parent
        self.project = Path(self.pol["project"])
        self.ledger = self.cfg / "jobs.jsonl"
        self.user = getpass.getuser()
        self.bin = self.pol["slurm"]["bin"]
        if not self.project.is_dir():
            raise BrokerError(f"project directory {self.project} does not exist")
        if not self.pol.get("logs_dir"):
            raise BrokerError("policy has no logs_dir; re-run `claude-slurm init --force "
                              "--logs-dir DIR ...`")
        self.logs = Path(self.pol["logs_dir"])

    # ---- plumbing -------------------------------------------------------
    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k in _KEEP or not k.startswith(_SCRUB_PREFIXES)}
        env["PATH"] = "/usr/bin:/bin"
        return env

    def _run(self, tool: str, args: list[str], stdin: str | None = None) -> str:
        cmd = [self.bin[tool], *args]
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                               timeout=90, env=self._env())
        except subprocess.TimeoutExpired:
            raise BrokerError(f"{tool} timed out")
        if r.returncode != 0:
            raise BrokerError(f"{tool} failed: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout.strip()

    def _owned(self) -> set[str]:
        if not self.ledger.exists():
            return set()
        ids = set()
        for line in self.ledger.read_text().splitlines():
            try:
                ids.add(str(json.loads(line)["id"]))
            except (ValueError, KeyError):
                continue
        return ids

    def _record(self, rec: dict) -> None:
        with open(self.ledger, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(rec) + "\n")

    def _check_owned(self, jobid: str) -> str:
        jobid = str(jobid).strip()
        if not JOBID_RE.fullmatch(jobid):
            raise RequestError("job id must be numeric (use the array parent id for arrays)")
        if jobid not in self._owned():
            raise RequestError(f"job {jobid} was not submitted through this broker")
        return jobid

    def _queue_rows(self) -> list[list[str]]:
        out = self._run("squeue", ["-h", "-u", self.user, "-o", "%A|%i|%j|%T|%M|%l|%P|%R"])
        owned = self._owned()
        return [r.split("|") for r in out.splitlines() if r and r.split("|")[0] in owned]

    # ---- operations -------------------------------------------------------
    def submit(self, **req) -> dict:
        if not image_unchanged(self.pol):
            raise BrokerError("container image changed since `claude-slurm init`; "
                              "the user must run `claude-slurm refresh --accept-image`")
        active = {r[0] for r in self._queue_rows()}
        if len(active) >= self.pol["slurm"]["max_active"]:
            raise RequestError(f"{len(active)} jobs active; limit is "
                               f"{self.pol['slurm']['max_active']}")
        opts, script, summary = jobscript.build(self.pol, req, self._owned())
        self.logs.mkdir(parents=True, exist_ok=True)
        out = self._run("sbatch", opts, stdin=script)
        jobid = out.split(";")[0].strip()
        if not JOBID_RE.fullmatch(jobid):
            raise BrokerError(f"unexpected sbatch output: {out!r}")
        self._record({"id": jobid, "t": int(time.time()), **summary})
        return {"job_id": jobid, **summary}

    def cancel(self, jobid: str) -> str:
        jobid = self._check_owned(jobid)
        self._run("scancel", [jobid])
        return f"cancel requested for {jobid}"

    def update(self, jobid: str, minutes: int | None = None, hold: bool | None = None,
               name: str | None = None) -> str:
        jobid = self._check_owned(jobid)
        done = []
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, int) or \
                    not 1 <= minutes <= self.pol["slurm"]["max_minutes"]:
                raise RequestError(f"minutes must be in [1, {self.pol['slurm']['max_minutes']}]")
            self._run("scontrol", ["update", f"JobId={jobid}", f"TimeLimit={minutes}"])
            done.append(f"TimeLimit={minutes}")
        if name is not None:
            clean = jobscript.NAME_RE.sub("_", name)[:40] or "job"
            self._run("scontrol", ["update", f"JobId={jobid}", f"JobName={clean}"])
            done.append(f"JobName={clean}")
        if hold is not None:
            self._run("scontrol", ["hold" if hold else "release", jobid])
            done.append("held" if hold else "released")
        return f"{jobid}: " + (", ".join(done) or "nothing to change")

    def list_rows(self) -> list[dict]:
        keys = ("job_id", "task", "name", "state", "elapsed", "limit", "partition", "reason")
        return [dict(zip(keys, r)) for r in self._queue_rows()]

    def list_jobs(self) -> str:
        rows = self._queue_rows()
        if not rows:
            return "no active broker-submitted jobs"
        head = "ARRAY_ID|JOBID|NAME|STATE|ELAPSED|LIMIT|PARTITION|REASON"
        return "\n".join([head, *("|".join(r) for r in rows)])

    def job_info(self, jobid: str) -> str:
        jobid = self._check_owned(jobid)
        acct = self._run("sacct", ["-n", "-P", "-j", jobid, "--format="
                                   "JobID,JobName,State,ExitCode,Elapsed,MaxRSS,AllocTRES"])
        logs = sorted({*self.logs.glob(f"{jobid}.out"), *self.logs.glob(f"{jobid}_*.out")})
        return ("JobID|JobName|State|ExitCode|Elapsed|MaxRSS|AllocTRES\n" + acct +
                "\nlogs: " + (", ".join(map(str, logs)) or "none yet"))

    def run_command(self, name: str, params) -> dict:
        try:
            return commands.run(self.pol, name, params or {}, self.logs)
        except commands.CommandError as e:
            raise RequestError(str(e)) from e

    def describe(self) -> str:
        p, s = self.pol, self.pol["slurm"]
        binds = [f"{b['container']} ({b['mode']}) <- {b['host']}" for b in p["binds"]]
        return json.dumps({
            "project_host_path": p["project"],
            "project_mount_in_job": p["project_mount"],
            "image": p["image"]["path"],
            "other_binds_in_job": binds,
            "partitions": s["partitions"],
            "accounts": {"default": s.get("account"), "allowed": s.get("accounts") or [],
                         "denied": s.get("deny_accounts") or []},
            "qos": {"default": s.get("qos"), "allowed": s.get("qos_list") or []},
            "limits": {k: s[k] for k in ("max_minutes", "max_cpus", "max_mem_gb",
                                         "max_gpus", "max_active", "max_array")},
            "logs": f"{p['logs_dir']}/<jobid>.out (read-only to you)",
            "network_in_job": "none" if p["apptainer"].get("net_none") else "site default",
            "allowed_commands": commands.describe(p),
            "services": services.describe(p),
        }, indent=2)
