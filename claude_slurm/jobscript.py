"""Turn a structured job request into sbatch options + a batch script.

Pure functions, no Slurm calls, so they can be unit-tested anywhere.

Design rules:
  * Every Slurm option goes on the sbatch command line (highest precedence),
    never into #SBATCH lines. The user's script is never seen by sbatch; it
    is only run by bash *inside* the container, so its #SBATCH lines are inert.
  * The batch script is generated here and contains no user-controlled shell
    code: every user value is shlex-quoted.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from .policy import TAG

NAME_RE = re.compile(r"[^\w.-]")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
JOBID_RE = re.compile(r"^\d{1,12}$")
SLURM_NAME_RE = re.compile(r"^[\w.:-]{1,64}$")
ARRAY_RE = re.compile(r"^\d+(-\d+(:\d+)?)?(,\d+(-\d+(:\d+)?)?)*(%\d+)?$")

# Host variables forwarded into the container (--containall cleans the env).
FORWARD_VARS = (
    "SLURM_JOB_ID", "SLURM_JOB_NAME", "SLURM_ARRAY_JOB_ID", "SLURM_ARRAY_TASK_ID",
    "SLURM_ARRAY_TASK_COUNT", "SLURM_ARRAY_TASK_MIN", "SLURM_ARRAY_TASK_MAX",
    "SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE", "SLURM_MEM_PER_NODE",
    "SLURM_NTASKS", "SLURM_JOB_NUM_NODES", "SLURM_NODELIST", "SLURM_JOB_GPUS",
    "SLURM_GPUS_ON_NODE", "SLURM_SUBMIT_DIR", "SLURM_RESTART_COUNT",
    "CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL",
    "OMP_NUM_THREADS",
)


class RequestError(ValueError):
    pass


def _int(req: dict, key: str, lo: int, hi: int) -> int:
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise RequestError(f"{key} must be an integer")
    if not lo <= v <= hi:
        raise RequestError(f"{key}={v} outside allowed range [{lo}, {hi}]")
    return v


def array_size(spec: str) -> int:
    if not ARRAY_RE.fullmatch(spec):
        raise RequestError(f"bad array spec {spec!r} (e.g. '0-9', '1,3,5', '0-99%10')")
    n = 0
    for part in spec.split("%")[0].split(","):
        if "-" in part:
            rng, _, step = part.partition(":")
            a, b = map(int, rng.split("-"))
            step_i = int(step or 1)
            if b < a or step_i < 1:
                raise RequestError(f"bad array range {part!r}")
            n += (b - a) // step_i + 1
        else:
            n += 1
    return n


def denied_account(account: str, deny: list[str]) -> bool:
    """`deny` entries match an account exactly or as its prefix before `:`
    ("othergroup" denies "othergroup", "othergroup:default", "othergroup:sub", ...)."""
    a = account.lower()
    return any(a == d.lower() or a.startswith(d.lower() + ":") for d in deny)


def choose(req: dict, key: str, default: str | None, allowed: list[str] | None) -> str | None:
    """The requested account/qos, else the policy default. A request must name
    one of `allowed` (the default is always allowed)."""
    v = req.get(key)
    if v is None or v == "":
        return default
    if not isinstance(v, str) or not SLURM_NAME_RE.fullmatch(v):
        raise RequestError(f"bad {key} {v!r}")
    ok = set(allowed or []) | ({default} if default else set())
    if v not in ok:
        raise RequestError(f"{key} must be one of {sorted(ok)}" if ok
                           else f"this policy doesn't allow choosing {key}")
    return v


def resolve_script(project: Path, script: str) -> Path:
    if not isinstance(script, str) or not script or "\0" in script:
        raise RequestError("script must be a non-empty path")
    p = (project / script).resolve()
    if project not in p.parents:
        raise RequestError("script must be inside the project directory")
    if not p.is_file():
        raise RequestError(f"script not found: {p.relative_to(project)}")
    return p


def build(pol: dict, req: dict, owned: set[str]) -> tuple[list[str], str, dict]:
    """Validate `req` against `pol`. Returns (sbatch_options, script, summary)."""
    project = Path(pol["project"])
    mount = pol["project_mount"]
    lim = pol["slurm"]

    script = resolve_script(project, req.get("script"))
    rel = script.relative_to(project).as_posix()

    partition = req.get("partition")
    if partition not in lim["partitions"]:
        raise RequestError(f"partition must be one of {lim['partitions']}")
    account = choose(req, "account", lim.get("account"), lim.get("accounts"))
    qos = choose(req, "qos", lim.get("qos"), lim.get("qos_list"))
    deny = lim.get("deny_accounts") or []
    if deny and not account:
        # Slurm would fall back to the user's default account, which may be denied.
        raise RequestError("an account is required (the policy denies some accounts)")
    if account and denied_account(account, deny):
        raise RequestError(f"account {account!r} is denied by policy")
    minutes = _int(req, "minutes", 1, lim["max_minutes"])
    cpus = _int({"cpus": req.get("cpus", 1)}, "cpus", 1, lim["max_cpus"])
    mem_gb = _int({"mem_gb": req.get("mem_gb", 4)}, "mem_gb", 1, lim["max_mem_gb"])
    gpus = _int({"gpus": req.get("gpus", 0)}, "gpus", 0, lim["max_gpus"])

    name = NAME_RE.sub("_", str(req.get("name") or "job"))[:40] or "job"

    args = req.get("args") or []
    if not isinstance(args, list) or len(args) > 64 or not all(
            isinstance(a, str) and "\0" not in a and len(a) < 4096 for a in args):
        raise RequestError("args must be a list of <=64 strings")

    env = req.get("env") or {}
    if not isinstance(env, dict) or len(env) > 64:
        raise RequestError("env must be a dict with <=64 entries")
    for k, v in env.items():
        if not ENV_KEY_RE.fullmatch(str(k)):
            raise RequestError(f"bad env var name {k!r}")
        if not isinstance(v, str) or "\0" in v or "\n" in v or len(v) > 4096:
            raise RequestError(f"bad value for env var {k}")

    array = req.get("array")
    n_tasks = 1
    if array:
        n_tasks = array_size(str(array))
        if n_tasks > lim["max_array"]:
            raise RequestError(f"array has {n_tasks} tasks, max is {lim['max_array']}")

    deps = {}
    for key, kind in (("after_ok", "afterok"), ("after_any", "afterany")):
        ids = req.get(key) or []
        if not isinstance(ids, list):
            raise RequestError(f"{key} must be a list of job ids")
        for d in ids:
            if not JOBID_RE.fullmatch(str(d)) or str(d) not in owned:
                raise RequestError(f"dependency {d} is not a job submitted through this broker")
        if ids:
            deps[kind] = ids

    # Never inside the project: Claude could plant a symlink at the log path
    # and Slurm would write job output through it, outside the sandbox.
    logs = Path(pol["logs_dir"])
    out_pat = "%A_%a.out" if array else "%j.out"

    opts = [
        "--parsable",
        f"--job-name={name}",
        f"--partition={partition}",
        f"--time={minutes}",
        "--nodes=1", "--ntasks=1",
        f"--cpus-per-task={cpus}",
        f"--mem={mem_gb}G",
        f"--chdir={project}",
        f"--output={logs}/{out_pat}",
        "--export=NONE",
        f"--comment={TAG}",
    ]
    if account:
        opts.append(f"--account={account}")
    if qos:
        opts.append(f"--qos={qos}")
    if gpus:
        opts.append(f"--gpus={gpus}" if lim.get("gpu_flag", "gpus") == "gpus"
                    else f"--gres=gpu:{gpus}")
    if array:
        opts.append(f"--array={array}")
    if deps:
        opts.append("--dependency=" + ",".join(
            f"{kind}:" + ":".join(str(d) for d in ids) for kind, ids in deps.items()))
        opts.append("--kill-on-invalid-dep=yes")

    script_text = render_script(pol, rel, args, env, gpus)
    summary = {"name": name, "partition": partition, "account": account, "qos": qos,
               "minutes": minutes, "cpus": cpus,
               "mem_gb": mem_gb, "gpus": gpus, "array": array, "tasks": n_tasks,
               "script": rel, "log": str(logs / out_pat)}
    return opts, script_text, summary


def apptainer_argv(pol: dict, rel_script: str, args: list[str], gpus: int) -> list[str]:
    mount = pol["project_mount"]
    argv = [pol["apptainer"]["bin"], "exec", "--containall", "--no-home"]
    if pol["apptainer"].get("no_mount"):
        argv += ["--no-mount", ",".join(pol["apptainer"]["no_mount"])]
    argv += ["--bind", f"{pol['project']}:{mount}:rw"]
    for b in pol["binds"]:
        argv += ["--bind", f"{b['host']}:{b['container']}:{b['mode']}"]
    argv += ["--pwd", mount]
    if gpus:
        argv.append(pol["apptainer"].get("gpu_flag", "--nv"))
    if pol["apptainer"].get("net_none"):
        argv += ["--net", "--network", "none"]
    argv += [pol["image"]["path"], "bash", f"{mount}/{rel_script}", *args]
    return argv


def render_script(pol: dict, rel_script: str, args: list[str], env: dict, gpus: int) -> str:
    q = shlex.quote
    app = pol["apptainer"]
    prefix = app["env_prefix"]  # APPTAINERENV_ or SINGULARITYENV_
    path_dirs = ["/usr/local/bin", "/usr/bin", "/bin", str(Path(app["bin"]).parent)]
    lines = [
        "#!/bin/bash",
        "# Generated by claude-slurm. Options were passed on the sbatch command line.",
        "set -eo pipefail",
        f"export PATH={q(':'.join(dict.fromkeys(path_dirs)))}",
    ]
    if pol.get("job_prelude"):
        lines += ["# --- prelude (from claude-slurm init) ---", pol["job_prelude"].rstrip(),
                  "# --- end prelude ---"]
    lines += [
        "# Drop anything that could add binds or alter Apptainer behaviour.",
        'for __v in $(compgen -e); do',
        '  case "$__v" in APPTAINER_*|SINGULARITY_*|APPTAINERENV_*|SINGULARITYENV_*) unset "$__v" ;; esac',
        'done',
        "# Forward selected Slurm/GPU variables into the clean container env.",
        f"for __v in {' '.join(FORWARD_VARS)}; do",
        '  if [[ -n "${!__v+x}" ]]; then export "' + prefix + '${__v}=${!__v}"; fi',
        "done",
    ]
    for k, v in sorted(env.items()):
        lines.append(f"export {prefix}{k}={q(v)}")
    lines.append("exec " + " ".join(q(a) for a in apptainer_argv(pol, rel_script, args, gpus)))
    return "\n".join(lines) + "\n"
