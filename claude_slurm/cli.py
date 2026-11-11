"""claude-slurm: per-project setup and checks for the sandboxed Claude + Slurm broker."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import commands, config, install, jobscript, probes, render, services
from .paths import (CONFIG_ROOT, HOME, PREFIX, SAFE_PATH_RE, locate, overlaps,
                    project_cfg, protected_paths, within)
from .policy import (POLICY_VERSION, PolicyError, check_container_path, container_overlap,
                     image_fingerprint, image_unchanged, load, sha256_file)

SLURM_TOOLS = ("sbatch", "squeue", "scancel", "scontrol", "sacct")
DEFAULT_DENY_READ = ["~/.ssh", "~/.gnupg", "~/.netrc", "~/.aws", "~/.config/gcloud",
                     "~/.kube", "~/.docker", "~/.git-credentials"]


def die(msg: str, code: int = 1):
    print(f"claude-slurm: error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"claude-slurm: warning: {msg}", file=sys.stderr)


def safe_abs(p: str, what: str, must_exist: bool = True) -> Path:
    path = Path(p).expanduser().resolve()
    if not SAFE_PATH_RE.fullmatch(str(path)):
        die(f"{what} {path}: only [A-Za-z0-9._/+@=-] allowed in paths")
    if must_exist and not path.exists():
        die(f"{what} {path} does not exist")
    return path


def guard_writable(p: Path, what: str, force: bool):
    if within(HOME, p):
        die(f"{what} {p} contains your entire home directory")
    if str(p) == "/":
        die(f"{what} cannot be /")
    for prot in protected_paths():
        if overlaps(p, prot):
            die(f"{what} {p} overlaps protected path {prot} "
                "(policy/code would become writable)")


def write_probe(project: Path, name: str, text: str) -> Path:
    """Write <project>/.slurm/<name> without following symlinks.

    The project is writable by Claude (and by jobs), so `.slurm` or the file
    itself may be a symlink planted to make this unsandboxed process clobber
    something else. Open the directory with O_NOFOLLOW, drop whatever is at
    `name` (unlink removes a link, never its target), then create the file
    O_EXCL|O_NOFOLLOW relative to that directory fd, so a link re-planted in
    between makes the open fail instead of being followed.
    """
    d = project / ".slurm"
    if d.is_symlink():
        die(f"{d} is a symlink; remove it and re-run")
    d.mkdir(exist_ok=True)
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=dfd)
        except FileExistsError:
            die(f"{d / name} reappeared while writing it; something is racing us")
        with os.fdopen(fd, "w") as f:
            f.write(text)
    finally:
        os.close(dfd)
    return d / name


def check_logs_dir(logs: Path, writable: list[Path], what: str = "--logs-dir"):
    """Logs must be somewhere nothing Claude-controlled can write, and kept out
    of Claude's own config (~/.claude) and claude-slurm's config/install dirs."""
    if str(logs) == "/" or within(HOME, logs):
        die(f"{what} {logs} contains your entire home directory")
    for prot in [*protected_paths(), HOME / ".claude"]:
        if overlaps(logs, prot):
            die(f"{what} {logs} overlaps {prot}; keep Slurm logs in their own directory")
    for area in writable:
        if overlaps(logs, area):
            die(f"{what} {logs} overlaps writable area {area} (Claude could plant "
                "symlinks there and Slurm would write job output through them)")


def parse_bind(spec: str, force: bool) -> dict:
    parts = spec.split(":")
    if len(parts) == 1:
        host, cont, mode = parts[0], None, "ro"
    elif len(parts) == 2:
        host, cont = parts
        mode = "ro"
        if cont in ("ro", "rw"):
            cont, mode = None, cont
    elif len(parts) == 3:
        host, cont, mode = parts
    else:
        die(f"bad --bind {spec!r}; use HOST[:CONTAINER][:ro|rw]")
    if mode not in ("ro", "rw"):
        die(f"bad bind mode {mode!r} in {spec!r}")
    hostp = safe_abs(host, "bind source")
    try:
        cont = check_container_path(cont or str(hostp))
    except PolicyError as e:
        die(str(e))
    if mode == "rw":
        guard_writable(hostp, "rw bind", force)
    elif within(HOME, hostp) and not force:
        die(f"ro bind {hostp} exposes your whole home (incl. ~/.ssh) to jobs; "
            "bind a subdirectory, or pass --force")
    for prot in protected_paths():
        if mode == "rw" and overlaps(hostp, prot):
            die(f"rw bind {hostp} overlaps {prot}")
    return {"host": str(hostp), "container": cont, "mode": mode}


def find_tool(name: str, bindir: str | None) -> str | None:
    if bindir:
        cand = Path(bindir) / name
        return str(cand) if os.access(cand, os.X_OK) else None
    return shutil.which(name)


def registered_policies(exclude: Path | None = None) -> list[dict]:
    d = CONFIG_ROOT / "projects"
    out = []
    for p in sorted(d.glob("*/policy.json")) if d.exists() else []:
        try:
            pol = load(p)
        except Exception:  # noqa: BLE001
            continue
        if exclude is None or Path(pol["project"]) != exclude:
            out.append(pol)
    return out


def writable_areas(pol: dict) -> list[Path]:
    """Everything Claude (shell or jobs) can write for one project."""
    areas = [Path(pol["project"])] + [Path(p) for p in pol["login"]["extra_write"]]
    areas += [Path(b["host"]) for b in pol["binds"] if b["mode"] == "rw"]
    return areas


def logs_readable(logs: Path, deny_read: list[str]) -> bool:
    for d in deny_read:
        dp = Path(d.rstrip("*").rstrip("/")).expanduser()
        if within(logs, dp):
            return False
    return True


# ---------------------------------------------------------------- init
def cmd_init(a):
    for dest, flag in (("image", "--image"), ("logs_dir", "--logs-dir"),
                       ("partition", "--partition")):
        if not getattr(a, dest):
            die(f"{flag} is required (on the command line or in --config)")
    project = safe_abs(a.project, "project")
    if not project.is_dir():
        die(f"{project} is not a directory")
    guard_writable(project, "project", a.force)

    cfg = project_cfg(project)
    pol_path = cfg / "policy.json"
    if pol_path.exists() and not a.force:
        die(f"{project} is already initialised ({cfg}); use --force to overwrite")

    image = safe_abs(a.image, "image")
    if not image.is_file():
        die(f"image {image} is not a file (use a .sif)")
    if within(image, project):
        die("image must live outside the project (Claude could replace it)")

    try:
        mount = check_container_path(a.project_mount)
    except PolicyError as e:
        die(str(e))

    binds = [parse_bind(b, a.force) for b in a.bind]
    seen = [mount]
    for b in binds:
        for s in seen:
            if container_overlap(b["container"], s):
                die(f"container path {b['container']} overlaps {s}")
        seen.append(b["container"])
        if b["mode"] == "rw" and within(image, b["host"]):
            die(f"rw bind {b['host']} contains the image")

    extra_write = []
    for d in a.login_write:
        p = safe_abs(d, "--login-write dir")
        guard_writable(p, "--login-write dir", a.force)
        if within(image, p):
            die(f"--login-write {p} contains the image")
        extra_write.append(str(p))

    # Cross-project: no project's writable area may contain another's image.
    mine = [project] + [Path(p) for p in extra_write] + \
           [Path(b["host"]) for b in binds if b["mode"] == "rw"]
    for other in registered_policies(exclude=project):
        for area in writable_areas(other):
            if within(image, area):
                die(f"image {image} is writable by project {other['project']} ({area})")
        for area in mine:
            if within(other["image"]["path"], area):
                die(f"{area} would make {other['project']}'s image writable")

    logs = safe_abs(a.logs_dir, "--logs-dir", must_exist=False)
    others = registered_policies(exclude=project)
    check_logs_dir(logs, mine + [w for o in others for w in writable_areas(o)])
    for other in others:
        if other.get("logs_dir"):
            for area in mine:
                if overlaps(Path(other["logs_dir"]), area):
                    die(f"{area} overlaps {other['project']}'s logs dir {other['logs_dir']}")
    if not logs_readable(logs, DEFAULT_DENY_READ + a.deny_read):
        die(f"--logs-dir {logs} is under a --deny-read path; Claude couldn't read its logs")

    # Slurm client binaries (absolute paths; broker never relies on PATH)
    slurm_bin = {}
    for t in SLURM_TOOLS:
        path = find_tool(t, a.slurm_bin_dir)
        if not path:
            die(f"cannot find `{t}`; load your Slurm module or pass --slurm-bin-dir")
        slurm_bin[t] = str(Path(path).resolve())

    app = a.apptainer or shutil.which("apptainer") or shutil.which("singularity")
    if not app:
        die("cannot find apptainer/singularity; pass --apptainer PATH")
    app = str(Path(app).resolve())
    env_prefix = "SINGULARITYENV_" if "singularity" in Path(app).name else "APPTAINERENV_"
    try:
        h = subprocess.run([app, "exec", "--help"], capture_output=True, text=True, timeout=30)
        if "--no-mount" not in h.stdout:
            warn(f"{app} does not list --no-mount; site bind paths can't be disabled")
    except Exception as e:  # noqa: BLE001
        warn(f"could not run {app}: {e}")

    sinfo = find_tool("sinfo", a.slurm_bin_dir)
    if sinfo:
        try:
            out = subprocess.run([sinfo, "-h", "-o", "%R"], capture_output=True,
                                 text=True, timeout=30).stdout.split()
            for p in a.partition:
                if out and p not in out:
                    warn(f"partition {p!r} not in sinfo output {sorted(set(out))}")
        except Exception:  # noqa: BLE001
            pass

    if a.job_prelude_text is not None:
        prelude = a.job_prelude_text
    else:
        prelude = Path(a.job_prelude).read_text() if a.job_prelude else ""

    for what, v in ([("--account", a.account), ("--qos", a.qos)]
                    + [("--allow-account", x) for x in a.allow_account]
                    + [("--allow-qos", x) for x in a.allow_qos]
                    + [("--deny-account", x) for x in a.deny_account]):
        if v is not None and not jobscript.SLURM_NAME_RE.fullmatch(v):
            die(f"{what} {v!r}: not a valid Slurm name")
    for v in [a.account, *a.allow_account]:
        if v and jobscript.denied_account(v, a.deny_account):
            die(f"account {v!r} is both allowed and denied (--deny-account)")

    try:
        allowed = commands.validate(a.allowed_commands or {})
    except commands.CommandError as e:
        die(str(e))
    for name, spec in allowed.items():
        for ph in spec["placeholders"].values():
            if ph["type"] == "rel_path" and within(HOME, Path(ph["root"])):
                die(f"allowed_commands.{name}: root {ph['root']} contains your home directory")
    try:
        svc = services.validate(a.services, allowed)
    except services.ServiceError as e:
        die(str(e))

    fp = image_fingerprint(image)
    if a.hash_image:
        print(f"hashing {image} ...", file=sys.stderr)
        fp["sha256"] = sha256_file(image)

    pol = {
        "version": POLICY_VERSION,
        "created": int(time.time()),
        "project": str(project),
        "project_mount": mount,
        "logs_dir": str(logs),
        "image": fp,
        "apptainer": {"bin": app, "env_prefix": env_prefix,
                      "no_mount": [x for x in a.no_mount.split(",") if x],
                      "gpu_flag": a.apptainer_gpu_flag, "net_none": a.net_none},
        "binds": binds,
        "job_prelude": prelude,
        "slurm": {"bin": slurm_bin, "account": a.account, "qos": a.qos,
                  "accounts": a.allow_account, "qos_list": a.allow_qos,
                  "deny_accounts": a.deny_account,
                  "partitions": a.partition, "gpu_flag": a.gpu_flag,
                  "max_minutes": a.max_minutes, "max_cpus": a.max_cpus,
                  "max_mem_gb": a.max_mem_gb, "max_gpus": a.max_gpus,
                  "max_active": a.max_active, "max_array": a.max_array},
        "login": {"extra_write": extra_write,
                  "allowed_domains": a.allow_domain,
                  "deny_read": DEFAULT_DENY_READ + a.deny_read},
        "claude": {"auto_submit": a.auto_submit, "auto_cancel": not a.ask_cancel,
                   "project_settings": a.allow_project_settings},
        "allowed_commands": allowed,
        "services": svc,
    }

    cfg.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_ROOT, 0o700)
    os.chmod(cfg, 0o700)
    pol_path.write_text(json.dumps(pol, indent=2) + "\n")
    (cfg / "settings.json").write_text(json.dumps(render.settings(pol), indent=2) + "\n")
    (cfg / "mcp.json").write_text(json.dumps(render.mcp_config(pol, pol_path), indent=2) + "\n")
    (cfg / "context.md").write_text(render.context_md(pol))
    (cfg / "jobs.jsonl").touch()
    logs.mkdir(parents=True, exist_ok=True)
    os.chmod(logs, 0o700)

    if (project / ".claude" / "settings.json").exists() or (project / ".mcp.json").exists():
        msg = ("project has .claude/settings.json or .mcp.json; "
               + ("they WILL be loaded (--allow-project-settings)" if a.allow_project_settings
                  else "they will be ignored by claude-hpc"))
        warn(msg)
    if (cfg / install.MANIFEST).exists():  # keep `install` in sync with the new policy
        man = json.loads((cfg / install.MANIFEST).read_text())
        _install(pol, cfg, man.get("strict_mcp", False), True, man.get("deny_tools", []))

    print(f"initialised {project}\n  config: {cfg}\n  logs:   {logs}\n"
          f"next: claude-slurm check {project} --job --partition {a.partition[0]} --wait 600\n"
          f"then: cd {project} && claude-hpc")


# ---------------------------------------------------------------- helpers
def resolve_project(arg: str | None) -> tuple[Path, Path, dict]:
    start = Path(arg or os.getcwd()).expanduser()
    root = locate(start)
    if not root:
        die(f"{start} is not inside an initialised project (run claude-slurm init)")
    cfg = project_cfg(root)
    return root, cfg, load(cfg / "policy.json")


def cmd_locate(a):
    root = locate(a.path)
    if not root:
        sys.exit(1)
    pol = load(project_cfg(root) / "policy.json")
    sources = "user,project,local" if pol["claude"]["project_settings"] else "user"
    print(root)
    print(project_cfg(root))
    print(sources)


def user_settings_warnings() -> list[str]:
    out = []
    f = HOME / ".claude" / "settings.json"
    if not f.is_file():
        return out
    try:
        s = json.loads(f.read_text())
    except ValueError:
        return [f"{f} is not valid JSON"]
    sb = s.get("sandbox", {})
    if sb.get("excludedCommands"):
        out.append(f"{f}: sandbox.excludedCommands {sb['excludedCommands']} run UNSANDBOXED "
                   "(arrays merge across settings files)")
    net = sb.get("network", {})
    if net.get("allowUnixSockets") or net.get("allowAllUnixSockets"):
        out.append(f"{f}: sandbox.network allows unix sockets (could expose munge)")
    if sb.get("filesystem", {}).get("allowWrite"):
        out.append(f"{f}: sandbox.filesystem.allowWrite {sb['filesystem']['allowWrite']} "
                   "applies to every project")
    if s.get("hooks"):
        out.append(f"{f}: user hooks run outside the sandbox; make sure they are yours")
    return out


def cmd_preflight(a):
    pol = load(Path(a.cfg) / "policy.json")
    errors = []
    if not image_unchanged(pol):
        errors.append(f"image {pol['image']['path']} changed or missing since init "
                      "(if intended: claude-slurm refresh --accept-image)")
    for t in ("bwrap", "socat"):
        if not shutil.which(t):
            errors.append(f"`{t}` not in PATH; Claude Code's sandbox needs it")
    for w in user_settings_warnings():
        warn(w)
    if errors:
        for e in errors:
            print(f"claude-hpc: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------- check
def cmd_check(a):
    root, cfg, pol = resolve_project(a.project)
    ok = True

    def report(cond: bool, msg: str):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + msg)
        ok &= cond

    print(f"== project {root}\n   config  {cfg}")
    report(image_unchanged(pol), f"image unchanged: {pol['image']['path']}")
    if a.hash and pol["image"].get("sha256"):
        report(sha256_file(pol["image"]["path"]) == pol["image"]["sha256"], "image sha256 matches")
    for t in ("bwrap", "socat"):
        report(bool(shutil.which(t)), f"{t} in PATH")
    if shutil.which("bwrap"):
        r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                            "--unshare-all", "true"], capture_output=True, text=True)
        report(r.returncode == 0, "bwrap can create namespaces"
               + ("" if r.returncode == 0 else f": {r.stderr.strip()}"))
    logs = pol.get("logs_dir")
    report(bool(logs), f"logs dir set: {logs or 'MISSING (re-run init --force --logs-dir DIR)'}")
    if logs:
        report(not Path(logs).is_symlink() and os.access(logs, os.W_OK),
               f"logs dir {logs} exists, is not a symlink, and is writable by you")
    report(os.access(pol["apptainer"]["bin"], os.X_OK), f"apptainer: {pol['apptainer']['bin']}")
    for t, p in pol["slurm"]["bin"].items():
        report(os.access(p, os.X_OK), f"{t}: {p}")
    for w in user_settings_warnings():
        print("WARN  " + w)

    if a.service:
        from .services import Services
        svc = Services(pol, cfg, cfg / "policy.json")
        dst = write_probe(root, "service-probe.sh", probes.service_probe(pol))
        log = services.log_path(pol, "cs-probe")
        if log.exists():
            log.unlink()
        svc.stop("cs-probe")
        try:
            svc.start("cs-probe", str(dst.relative_to(root)))
        except services.ServiceError as e:
            report(False, f"start service: {e}")
        else:
            for _ in range(60):
                time.sleep(1)
                if svc.status("cs-probe")["ActiveState"] not in ("active", "activating"):
                    break
            text = log.read_text() if log.exists() else ""
            print(text)
            ok &= "FAIL" not in text and " 0 failed" in text
            svc.stop("cs-probe")
    if a.login_probe:
        dst = write_probe(root, "login-probe.sh", probes.login_probe(pol))
        print(f"\nwrote {dst}\n  start `claude-hpc` in {root} and ask Claude to run "
              "`bash .slurm/login-probe.sh`; every line should be PASS.")

    if a.job:
        from .core import Broker  # needs no MCP deps
        write_probe(root, "job-probe.sh", probes.job_probe(pol))
        b = Broker(cfg / "policy.json")
        part = a.partition or pol["slurm"]["partitions"][0]
        res = b.submit(script=".slurm/job-probe.sh", partition=part, minutes=5,
                       cpus=1, mem_gb=1, gpus=a.gpus, name="cs-probe",
                       env={"CS_PROBE": "1"})
        print(f"\nsubmitted probe job {res['job_id']} -> {res['log']}")
        if a.wait:
            deadline = time.time() + a.wait
            while time.time() < deadline:
                if not any(r[0] == res["job_id"] for r in b._queue_rows()):
                    break
                time.sleep(10)
            log = Path(res["log"].replace("%j", res["job_id"]))
            if log.exists():
                text = log.read_text()
                print(text)
                ok &= "FAIL" not in text and " 0 failed" in text
            else:
                print(f"log {log} not found (job still queued?)")
                ok = False
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- misc
def cmd_refresh(a):
    """Re-render settings/mcp/context from the stored policy (after upgrading the
    install), optionally accepting a changed image."""
    root, cfg, pol = resolve_project(a.project)
    if a.accept_image:
        fp = image_fingerprint(pol["image"]["path"])
        if "sha256" in pol["image"]:
            fp["sha256"] = sha256_file(pol["image"]["path"])
        pol["image"] = fp
        (cfg / "policy.json").write_text(json.dumps(pol, indent=2) + "\n")
        print(f"accepted current image {fp['path']}")
    (cfg / "settings.json").write_text(json.dumps(render.settings(pol), indent=2) + "\n")
    (cfg / "mcp.json").write_text(json.dumps(render.mcp_config(pol, cfg / "policy.json"),
                                             indent=2) + "\n")
    (cfg / "context.md").write_text(render.context_md(pol))
    print(f"refreshed {cfg}")
    for f, keys in policy_file_drift(pol):
        warn(f"{f} differs from the stored policy ({', '.join(keys)}); refresh doesn't read "
             f"it. To apply it: claude-slurm init --config {f} --force")
    if (cfg / install.MANIFEST).exists():
        man = json.loads((cfg / install.MANIFEST).read_text())
        _install(pol, cfg, man.get("strict_mcp", False), True, man.get("deny_tools", []))


def _normalized(values: dict, defaults: dict) -> dict:
    v = {**defaults, **values}
    v["project"] = str(Path(v["project"]).expanduser().resolve())
    try:
        v["allowed_commands"] = commands.validate(v.get("allowed_commands") or {})
        svc = services.validate(v.get("services"), v["allowed_commands"])
        v["services"] = {k: x for k, x in svc.items() if k != "bin"}
    except (commands.CommandError, services.ServiceError):
        pass  # compared as written
    for k in ("deny_read",):  # init adds the defaults to whatever the file lists
        v[k] = [p for p in v.get(k) or [] if p not in DEFAULT_DENY_READ]
    return v


def policy_file_drift(pol: dict) -> list[tuple[Path, list[str]]]:
    """Files in <config>/policies/ for this project whose settings differ from the
    stored policy, with the differing keys."""
    d = CONFIG_ROOT / "policies"
    if not d.is_dir():
        return []
    init = build_parser()[1]
    defaults = {a.dest: a.default for a in init._actions if a.dest != "help"}
    defaults.update(init._defaults)
    tmp = project_cfg(Path(pol["project"])) / ".exported.json"
    tmp.write_text(json.dumps(config.export(pol, DEFAULT_DENY_READ)))
    try:
        stored = _normalized(config.load_config(str(tmp)), defaults)
    finally:
        tmp.unlink()
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            mine = _normalized(config.load_config(str(f)), defaults)
        except (config.ConfigError, KeyError, TypeError):
            continue
        if mine["project"] != pol["project"]:
            continue
        name = {d: k for k, (d, _) in config.SCALARS.items()}
        name.update({d: k for k, d in {**config.LISTS, **config.SECTIONS}.items()})
        keys = sorted(name.get(k, k) for k in set(mine) | set(stored)
                      if k != "config" and mine.get(k) != stored.get(k))
        if keys:
            out.append((f, keys))
    return out


def _install(pol, cfg, strict_mcp, force, deny_tools):
    try:
        notes = install.install(pol, cfg, strict_mcp, force, deny_tools)
    except install.InstallError as e:
        die(str(e))
    root = pol["project"]
    print(f"installed the sandbox in {root}: every new `claude` session started there "
          "is sandboxed")
    for n in notes + install.warnings(Path(root)):
        warn(n)


def cmd_install(a):
    root, cfg, pol = resolve_project(a.project)
    _install(pol, cfg, a.strict_mcp, a.force, a.deny_tool)
    print(f"check: claude-slurm status {root}; then start `claude` there and ask it to run "
          "`bash .slurm/login-probe.sh` (claude-slurm check --login-probe writes it)")


def cmd_uninstall(a):
    root, cfg, pol = resolve_project(a.project)
    try:
        for d in install.uninstall(pol, cfg):
            print(d)
    except install.InstallError as e:
        die(str(e))


def cmd_status(a):
    root, cfg, pol = resolve_project(a.project)
    ok, lines = install.status(pol, cfg)
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def cmd_show(a):
    root, cfg, pol = resolve_project(a.project)
    if a.json:
        print(json.dumps(config.export(pol, DEFAULT_DENY_READ), indent=2))
        return
    print(f"project: {root}\nconfig:  {cfg}")
    print(json.dumps({k: pol[k] for k in ("project_mount", "image", "binds", "slurm",
                                          "login", "claude", "apptainer")}, indent=2))


def cmd_list(a):
    d = CONFIG_ROOT / "projects"
    for p in sorted(d.glob("*/policy.json")) if d.exists() else []:
        try:
            pol = load(p)
            flag = "" if Path(pol["project"]).is_dir() else "  (missing)"
            print(f"{pol['project']}{flag}\n    image {pol['image']['path']}")
        except Exception as e:  # noqa: BLE001
            print(f"{p}: unreadable ({e})")


def cmd_remove(a):
    root, cfg, pol = resolve_project(a.project)
    if (cfg / install.MANIFEST).exists():
        cmd_uninstall(a)
    for u in services.stop_all(cfg, ((pol.get("services") or {}).get("bin") or {}).get("systemctl")):
        print(f"stopped {u}")
    shutil.rmtree(cfg)
    print(f"removed config for {root} ({cfg}); project files untouched")


def build_parser():
    """The top-level parser and its `init` subparser."""
    ap = argparse.ArgumentParser(prog="claude-slurm", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="register a project directory")
    i.add_argument("project", nargs="?", default=".")
    g = i.add_argument_group("container (required: --image)")
    g.add_argument("--image", help="Apptainer .sif every job runs in (required)")
    g.add_argument("--bind", action="append", default=[], metavar="HOST[:CONT][:ro|rw]",
                   help="extra job mount (default ro, same path). Repeatable")
    g.add_argument("--project-mount", default="/work", help="project path inside jobs")
    g.add_argument("--logs-dir", metavar="DIR",
                   help="where Slurm writes job logs. Must be outside the project, every "
                        "writable dir, ~/.claude and claude-slurm's config. Created if missing")
    g.add_argument("--apptainer", help="apptainer binary (default: from PATH)")
    g.add_argument("--no-mount", default="bind-paths,hostfs",
                   help="apptainer --no-mount list (default: bind-paths,hostfs)")
    g.add_argument("--apptainer-gpu-flag", default="--nv", choices=["--nv", "--rocm"])
    g.add_argument("--net-none", action="store_true",
                   help="run jobs with --net --network none (if your site allows it)")
    g.add_argument("--job-prelude", metavar="FILE",
                   help="bash snippet run before apptainer (e.g. module load apptainer)")
    g.add_argument("--hash-image", action="store_true", help="also record sha256")
    s = i.add_argument_group("slurm (required: --partition)")
    s.add_argument("--partition", action="append", default=[],
                   help="allowed partition (required). Repeatable")
    s.add_argument("--account", help="default account")
    s.add_argument("--qos", help="default QOS")
    s.add_argument("--allow-account", action="append", default=[],
                   help="extra account a job may request. Repeatable")
    s.add_argument("--allow-qos", action="append", default=[],
                   help="extra QOS a job may request. Repeatable")
    s.add_argument("--deny-account", action="append", default=[],
                   help="account (or 'name' for every 'name:*') no job may use. Repeatable")
    s.add_argument("--gpu-flag", default="gpus", choices=["gpus", "gres"])
    s.add_argument("--max-minutes", type=int, default=480)
    s.add_argument("--max-cpus", type=int, default=32)
    s.add_argument("--max-mem-gb", type=int, default=128)
    s.add_argument("--max-gpus", type=int, default=4)
    s.add_argument("--max-active", type=int, default=20)
    s.add_argument("--max-array", type=int, default=100)
    s.add_argument("--slurm-bin-dir", help="directory with sbatch etc. (default: PATH)")
    l = i.add_argument_group("login-node sandbox")
    l.add_argument("--login-write", action="append", default=[], metavar="DIR",
                   help="extra dir Claude's shell may write. Repeatable")
    l.add_argument("--allow-domain", action="append", default=[],
                   help="network domain sandboxed commands may reach. Repeatable")
    l.add_argument("--deny-read", action="append", default=[],
                   help="extra path hidden from Claude (defaults include ~/.ssh)")
    c = i.add_argument_group("claude permissions")
    c.add_argument("--auto-submit", action="store_true", help="don't prompt on submit_job")
    c.add_argument("--ask-cancel", action="store_true", help="prompt on cancel/update")
    c.add_argument("--allow-project-settings", action="store_true",
                   help="load the project's .claude/settings*.json (default: ignored)")
    i.add_argument("--force", action="store_true", help="overwrite / relax safety checks")
    i.add_argument("--config", metavar="FILE",
                   help="JSON policy (keys as in `show --json`); explicit flags override it")
    i.set_defaults(fn=cmd_init, job_prelude_text=None, allowed_commands=None, services=None)

    ch = sub.add_parser("check", help="verify setup; optionally run probe job")
    ch.add_argument("project", nargs="?")
    ch.add_argument("--job", action="store_true", help="submit a probe job via the broker")
    ch.add_argument("--partition")
    ch.add_argument("--gpus", type=int, default=0)
    ch.add_argument("--wait", type=int, default=0, metavar="SECONDS")
    ch.add_argument("--service", action="store_true",
                    help="run the sandbox probe as a background service (services.enabled)")
    ch.add_argument("--login-probe", action="store_true",
                    help="write .slurm/login-probe.sh for Claude to run")
    ch.add_argument("--hash", action="store_true", help="verify image sha256 (slow)")
    ch.set_defaults(fn=cmd_check)

    for name, fn, h in (("show", cmd_show, "print a project's policy"),
                        ("remove", cmd_remove, "unregister a project")):
        p = sub.add_parser(name, help=h)
        p.add_argument("project", nargs="?")
        if name == "show":
            p.add_argument("--json", action="store_true",
                           help="print the policy as an `init --config` file")
        p.set_defaults(fn=fn)
    rf = sub.add_parser("refresh", help="re-render config files; --accept-image after updating the .sif")
    rf.add_argument("project", nargs="?")
    rf.add_argument("--accept-image", action="store_true")
    rf.set_defaults(fn=cmd_refresh)
    sub.add_parser("list", help="list registered projects").set_defaults(fn=cmd_list)
    ins = sub.add_parser("install", help="sandbox every plain `claude` session in a project")
    ins.add_argument("project", nargs="?")
    ins.add_argument("--strict-mcp", action="store_true",
                     help="load only the slurm broker: block your other MCP servers "
                          "and claude.ai connectors")
    ins.add_argument("--deny-tool", action="append", default=[], metavar="RULE",
                     help="extra permission deny rule, e.g. mcp__<server> for an "
                          "embedding app's in-process tools (repeatable)")
    ins.add_argument("--force", action="store_true",
                     help="install even if the project isn't its git repository's root")
    ins.set_defaults(fn=cmd_install)
    for name, fn, h in (("uninstall", cmd_uninstall, "undo `install`"),
                        ("status", cmd_status, "is `install` in place and in sync?")):
        p = sub.add_parser(name, help=h)
        p.add_argument("project", nargs="?")
        p.set_defaults(fn=fn)
    lo = sub.add_parser("locate")  # internal, used by claude-hpc
    lo.add_argument("path")
    lo.set_defaults(fn=cmd_locate)
    pf = sub.add_parser("preflight")  # internal, used by claude-hpc
    pf.add_argument("cfg")
    pf.set_defaults(fn=cmd_preflight)
    return ap, i


def main(argv=None):
    ap, i = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == "init" and a.config:
        apply_config(a, i, argv)
    a.fn(a)


def apply_config(a, parser, argv):
    """Fill every init option the command line didn't set from the JSON file."""
    try:
        values = config.load_config(a.config)
    except config.ConfigError as e:
        die(str(e))
    given = explicit_dests(parser, argv)
    for dest, v in values.items():
        if dest == "job_prelude_text" and "job_prelude" in given:
            continue
        if dest not in given:
            setattr(a, dest, v)


def explicit_dests(parser, argv) -> set[str]:
    """Option dests that appear on the init command line."""
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = argv[argv.index("init") + 1:]
    saved = [(act, act.default, act.required) for act in parser._actions]
    try:
        for act, _, _ in saved:
            act.default, act.required = argparse.SUPPRESS, False
        ns, _ = parser.parse_known_args(argv)
    finally:
        for act, d, r in saved:
            act.default, act.required = d, r
    return set(vars(ns)) - set(parser._defaults)


if __name__ == "__main__":
    main()
