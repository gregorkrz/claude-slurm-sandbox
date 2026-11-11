"""Background services: long-running project scripts started by the broker.

A service is a `systemd-run --user` unit, so it outlives the Claude session.
Its ExecStart is `_service.py` (installed code, outside the sandbox), which

  1. opens <logs_dir>/services/<name>.log for append,
  2. serves a Unix socket in $XDG_RUNTIME_DIR/claude-slurm/ that answers broker
     requests (cs-slurm / cs-run) with the project's policy, and
  3. runs the script under bubblewrap with the same write set as Claude's
     shell, no network, a fresh /tmp and /run, and the socket bound in.

The sandbox never gets systemd-run, D-Bus, munge or the Slurm daemons; the
only way out is the socket, which allows what `services` in the policy allows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .paths import HOME, PREFIX, protected_paths, within

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
CLIENT_DIR = PREFIX / "client"
SOCKET_IN_SANDBOX = "/run/claude-slurm/broker.sock"
DEFAULTS = {"enabled": False, "max_services": 4, "max_lifetime_hours": 72,
            "memory_max_gb": None, "nice": 10, "allow_submit": False,
            "allow_cancel": True, "commands": [], "path": []}


class ServiceError(ValueError):
    pass


# ---------------------------------------------------------------- init time
def validate(spec: dict | None, allowed_commands: dict) -> dict:
    spec = spec or {}
    extra = set(spec) - set(DEFAULTS)
    if extra:
        raise ServiceError(f"services: unknown keys {sorted(extra)}")
    out = {**DEFAULTS, **spec}
    for k in ("enabled", "allow_submit", "allow_cancel"):
        if not isinstance(out[k], bool):
            raise ServiceError(f"services.{k}: must be true/false")
    for k, lo, hi in (("max_services", 1, 32), ("max_lifetime_hours", 1, 24 * 30),
                      ("nice", 0, 19)):
        v = out[k]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ServiceError(f"services.{k}: integer in [{lo}, {hi}]")
    m = out["memory_max_gb"]
    if m is not None and (not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 512):
        raise ServiceError("services.memory_max_gb: integer in [1, 512] or null")
    if not (isinstance(out["commands"], list) and all(c in allowed_commands
                                                      for c in out["commands"])):
        raise ServiceError(f"services.commands: names from allowed_commands "
                           f"{sorted(allowed_commands)}")
    if not (isinstance(out["path"], list) and all(isinstance(p, str) and p.startswith("/")
                                                  for p in out["path"])):
        raise ServiceError("services.path: list of absolute directories")
    out["path"] = [str(Path(p).expanduser()) for p in out["path"]]
    if out["enabled"]:
        bins = {}
        for t in ("systemd-run", "systemctl", "bwrap"):
            p = shutil.which(t, path="/usr/bin:/bin")
            if not p:
                raise ServiceError(f"services: cannot find {t} in /usr/bin or /bin")
            bins[t] = str(Path(p).resolve())
        out["bin"] = bins
    return out


# ---------------------------------------------------------------- naming
def unit_prefix(cfg: Path) -> str:
    return f"cs-{cfg.name}-"


def unit_name(cfg: Path, name: str) -> str:
    return f"{unit_prefix(cfg)}{name}.service"


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def socket_path(cfg: Path, name: str) -> Path:
    return runtime_dir() / "claude-slurm" / f"{cfg.name}-{name}.sock"


def log_path(pol: dict, name: str) -> Path:
    return Path(pol["logs_dir"]) / "services" / f"{name}.log"


def systemd_env() -> dict:
    env = {"PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(runtime_dir())}
    for k in ("HOME", "USER", "LOGNAME", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir()}/bus")
    return env


# ---------------------------------------------------------------- sandbox
def bwrap_argv(pol: dict, script: Path, args: list[str], sock: Path, name: str) -> list[str]:
    """bubblewrap command for a service: same writable set as Claude's shell."""
    project = Path(pol["project"])
    svc = pol["services"]
    a = [svc["bin"]["bwrap"], "--die-with-parent", "--new-session", "--unshare-all",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
         "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/dev/shm"]
    for w in [str(project), *pol["login"]["extra_write"]]:
        a += ["--bind", w, w]
    ro = [*map(str, protected_paths()), pol["logs_dir"], pol["image"]["path"],
          str(project / ".claude"), str(project / ".mcp.json")]
    for p in ro:
        # A symlink can't be a mount point; its target is covered on its own.
        if os.path.exists(p) and not os.path.islink(p):
            a += ["--ro-bind", p, p]
    # Hide deny_read paths behind mode-000 placeholders (an empty tmpfs would
    # still be listable). Mount over the symlink target, which is what a read
    # through the link would reach.
    deny_dir, deny_file = denied_placeholders(sock.parent)
    for p in pol["login"]["deny_read"]:
        real = os.path.realpath(os.path.expanduser(p.rstrip("*").rstrip("/")))
        if os.path.isdir(real):
            a += ["--ro-bind", str(deny_dir), real]
        elif os.path.exists(real):
            a += ["--ro-bind", str(deny_file), real]
    a += ["--ro-bind", str(sock), SOCKET_IN_SANDBOX, "--chdir", str(project)]
    path = ":".join([str(CLIENT_DIR), *svc["path"], "/usr/local/bin", "/usr/bin", "/bin"])
    env = {"PATH": path, "HOME": str(HOME), "USER": os.environ.get("USER", ""),
           "LANG": "C.UTF-8", "TMPDIR": "/tmp", "CS_BROKER_SOCKET": SOCKET_IN_SANDBOX,
           "CS_SERVICE": name, "CS_PROJECT": str(project)}
    a += ["--", "/usr/bin/env", "-i", *(f"{k}={v}" for k, v in env.items()),
          "/bin/bash", str(script), *args]
    return a


def denied_placeholders(d: Path) -> tuple[Path, Path]:
    """An unreadable dir and file to mount over hidden paths."""
    dd, df = d / "denied-dir", d / "denied-file"
    if not dd.is_dir() or dd.is_symlink():
        dd.mkdir(mode=0o700, exist_ok=True)
    if not df.is_file() or df.is_symlink():
        df.touch()
    os.chmod(dd, 0)
    os.chmod(df, 0)
    return dd, df


def check_script(pol: dict, script: str) -> Path:
    project = Path(pol["project"])
    if not isinstance(script, str) or script.startswith("/") or "\0" in script:
        raise ServiceError("script must be a path relative to the project root")
    real = Path(os.path.realpath(project / script))
    if not within(real, project) or real == project:
        raise ServiceError(f"script {script} resolves outside the project")
    if not real.is_file():
        raise ServiceError(f"script {script} is not a file")
    return real


def check_args(args) -> list[str]:
    args = args or []
    if not isinstance(args, list) or len(args) > 64 or not all(
            isinstance(x, str) and "\0" not in x and len(x) <= 4096 for x in args):
        raise ServiceError("args must be a list of ≤64 strings")
    return args


# ---------------------------------------------------------------- systemd
class Services:
    def __init__(self, pol: dict, cfg: Path, policy_path: Path):
        self.pol, self.cfg, self.policy_path = pol, cfg, policy_path
        self.svc = pol.get("services") or DEFAULTS

    def _require(self):
        if not self.svc.get("enabled"):
            raise ServiceError("services are disabled for this project "
                               "(set services.enabled in the policy)")

    def _ctl(self, *args, check=True) -> str:
        r = subprocess.run([self.svc["bin"]["systemctl"], "--user", *args], capture_output=True,
                           text=True, timeout=30, env=systemd_env())
        if check and r.returncode:
            raise ServiceError(f"systemctl {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    def _units(self) -> list[dict]:
        out = self._ctl("list-units", "--all", "--no-legend", "--plain", "--full",
                        f"{unit_prefix(self.cfg)}*")
        rows = []
        for line in out.splitlines():
            f = line.split(None, 4)
            if len(f) >= 4 and f[0].startswith(unit_prefix(self.cfg)):
                rows.append({"name": f[0][len(unit_prefix(self.cfg)):-len(".service")],
                             "unit": f[0], "active": f[2], "sub": f[3]})
        return rows

    def start(self, name: str, script: str, args=None) -> dict:
        self._require()
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ServiceError(f"service name must match {NAME_RE.pattern}")
        real = check_script(self.pol, script)
        args = check_args(args)
        units = self._units()
        if any(u["name"] == name and u["active"] in ("active", "activating") for u in units):
            raise ServiceError(f"service {name} is already running")
        running = [u for u in units if u["active"] in ("active", "activating")]
        if len(running) >= self.svc["max_services"]:
            raise ServiceError(f"{len(running)} services running; limit is "
                               f"{self.svc['max_services']}")
        unit = unit_name(self.cfg, name)
        self._ctl("reset-failed", unit, check=False)
        log_path(self.pol, name).parent.mkdir(parents=True, exist_ok=True)
        py = PREFIX / "venv" / "bin" / "python"
        cmd = [self.svc["bin"]["systemd-run"], "--user", f"--unit={unit}",
               f"--description=claude-slurm service {name} ({self.pol['project']})",
               "-p", f"RuntimeMaxSec={self.svc['max_lifetime_hours'] * 3600}",
               "-p", f"Nice={self.svc['nice']}", "-p", "KillMode=control-group",
               "--", str(py), "-I", str(PREFIX / "bin" / "_service.py"),
               str(self.policy_path), name, str(real.relative_to(self.pol["project"])), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=systemd_env())
        if r.returncode:
            raise ServiceError(f"systemd-run failed: {r.stderr.strip()}")
        return {"service": name, "unit": unit, "log": str(log_path(self.pol, name)),
                "max_lifetime_hours": self.svc["max_lifetime_hours"]}

    def list(self) -> list[dict]:
        self._require()
        return self._units()

    def status(self, name: str, lines: int = 40) -> dict:
        self._require()
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ServiceError("bad service name")
        props = self._ctl("show", unit_name(self.cfg, name), "-p", "ActiveState", "-p",
                          "SubState", "-p", "Result", "-p", "ExecMainStatus", "-p",
                          "ActiveEnterTimestamp", "-p", "InactiveEnterTimestamp")
        info = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        log = log_path(self.pol, name)
        tail = ""
        if log.is_file():
            with open(log, "rb") as f:
                f.seek(max(0, log.stat().st_size - 64 * 1024))
                tail = "\n".join(f.read().decode(errors="replace").splitlines()[-lines:])
        return {"service": name, **info, "log": str(log), "log_tail": tail}

    def stop(self, name: str) -> str:
        self._require()
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ServiceError("bad service name")
        unit = unit_name(self.cfg, name)
        self._ctl("stop", unit, check=False)
        self._ctl("reset-failed", unit, check=False)
        return f"stopped {name}"


def stop_all(cfg: Path, systemctl: str | None) -> list[str]:
    """Stop every unit of a project (used by `claude-slurm remove`)."""
    systemctl = systemctl or shutil.which("systemctl", path="/usr/bin:/bin")
    if not systemctl:
        return []
    r = subprocess.run([systemctl, "--user", "list-units", "--all", "--no-legend", "--plain",
                        "--full", f"{unit_prefix(cfg)}*"], capture_output=True, text=True,
                       timeout=30, env=systemd_env())
    units = [line.split()[0] for line in r.stdout.splitlines()
             if line.split() and line.split()[0].startswith(unit_prefix(cfg))]
    for u in units:
        subprocess.run([systemctl, "--user", "stop", u], capture_output=True, timeout=60,
                       env=systemd_env())
        subprocess.run([systemctl, "--user", "reset-failed", u], capture_output=True,
                       timeout=30, env=systemd_env())
    return units


def describe(pol: dict) -> dict:
    s = pol.get("services") or DEFAULTS
    if not s.get("enabled"):
        return {"enabled": False}
    return {"enabled": True, "max_services": s["max_services"],
            "max_lifetime_hours": s["max_lifetime_hours"],
            "can_submit_jobs": s["allow_submit"], "can_cancel_jobs": s["allow_cancel"],
            "commands": s["commands"], "network": "none (use cs-run for allowed commands)"}


def handle_request(broker, pol: dict, req) -> object:
    """One request from inside a service (cs-slurm / cs-run)."""
    s = pol["services"]
    if not isinstance(req, dict) or not isinstance(req.get("op"), str):
        raise ServiceError("bad request")
    op, a = req["op"], req.get("args") or {}
    if not isinstance(a, dict):
        raise ServiceError("args must be an object")
    if op == "list":
        return broker.list_rows()
    if op == "info":
        return broker.job_info(str(a.get("job_id", "")))
    if op == "submit":
        if not s["allow_submit"]:
            raise ServiceError("this policy doesn't let services submit jobs")
        keys = {"script", "partition", "minutes", "cpus", "mem_gb", "gpus", "name", "args",
                "env", "array", "after_ok", "after_any", "account", "qos"}
        if set(a) - keys:
            raise ServiceError(f"unknown submit fields {sorted(set(a) - keys)}")
        return broker.submit(**a)
    if op in ("cancel", "hold", "release"):
        if not s["allow_cancel"]:
            raise ServiceError("this policy doesn't let services cancel/hold jobs")
        jid = str(a.get("job_id", ""))
        return broker.cancel(jid) if op == "cancel" else broker.update(jid, hold=(op == "hold"))
    if op == "run":
        name = a.get("name")
        if name not in s["commands"]:
            raise ServiceError(f"command {name!r} isn't allowed from services "
                               f"(services.commands = {s['commands']})")
        return broker.run_command(name, a.get("params") or {})
    raise ServiceError(f"unknown op {op!r}")


def encode(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()
