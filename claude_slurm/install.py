"""Install a project's sandbox into the files plain `claude` reads.

claude-hpc passes the sandbox on the command line. `install` writes the same
pieces where every session started in the project picks them up, including
ones launched through the Agent SDK:

  settings.json -> merged into <project>/.claude/settings.local.json
  mcp.json      -> local-scope MCP server (`claude mcp add-json -s local`)
  context.md    -> a marked block in <project>/CLAUDE.local.md

What was added is recorded in <cfg>/install.json, so `uninstall` removes
exactly that and leaves the user's own rules alone.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

from . import render

MANIFEST = "install.json"
BEGIN = "<!-- claude-slurm:begin (managed by `claude-slurm install`; do not edit) -->"
END = "<!-- claude-slurm:end -->"
SERVER = "slurm"
class InstallError(Exception):
    pass


# ---------------------------------------------------------------- what we add
def desired(pol: dict, cfg: Path, strict_mcp: bool = False, deny_tools=()) -> dict:
    root = pol["project"]
    s = _absolutize(render.settings(pol), root)
    mcp = render.mcp_config(pol, cfg / "policy.json")["mcpServers"][SERVER]
    perms = s["permissions"]
    perms["disableBypassPermissionsMode"] = "disable"
    s["sandbox"]["filesystem"]["denyWrite"].append(f"{root}/CLAUDE.local.md")
    if strict_mcp:
        # Like claude-hpc's --strict-mcp-config: only the broker loads.
        s["allowedMcpServers"] = [{"serverCommand": [mcp["command"], *mcp["args"]]}]
        s["disableClaudeAiConnectors"] = True
    # e.g. MCP tools an embedding app registers in-process, which skip
    # allowedMcpServers and run outside the sandbox.
    perms["deny"] += [t for t in deny_tools if t not in perms["deny"]]
    s["env"] = {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
    return {"settings": s, "mcp": mcp, "context": render.context_md(pol)}


def _absolutize(s: dict, root: str) -> dict:
    """`./x` anchors at the session's cwd, which may be a subdirectory."""
    s = copy.deepcopy(s)
    fs = s["sandbox"]["filesystem"]
    fs["denyWrite"] = [root + p[1:] if p.startswith("./") else p for p in fs["denyWrite"]]
    perms = s["permissions"]
    perms["deny"] = [r.replace("(./", f"({render._perm_abs(root)}/", 1) for r in perms["deny"]]
    return s


# ---------------------------------------------------------------- JSON merge
def merge(target: dict, ours: dict, path: tuple = ()) -> list:
    """Merge `ours` into `target` in place; return the ops needed to undo it."""
    ops = []
    for k, v in ours.items():
        p = [*path, k]
        if isinstance(v, (dict, list)):
            if k not in target:
                target[k] = type(v)()
                ops.append({"op": "created", "path": p})
            elif type(target[k]) is not type(v):
                raise InstallError(f"settings key {'.'.join(p)} has an unexpected type")
            if isinstance(v, dict):
                ops += merge(target[k], v, tuple(p))
            else:
                for item in v:
                    if item not in target[k]:
                        target[k].append(item)
                        ops.append({"op": "item", "path": p, "value": item})
        elif k not in target:
            target[k] = v
            ops.append({"op": "scalar", "path": p, "value": v, "absent": True})
        elif target[k] != v:
            ops.append({"op": "scalar", "path": p, "value": v, "absent": False,
                        "prev": target[k]})
            target[k] = v
    return ops


def unmerge(target: dict, ops: list) -> None:
    for o in reversed(ops):
        *parents, key = o["path"]
        node = target
        for k in parents:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict) or key not in node:
            continue
        if o["op"] == "item":
            if isinstance(node[key], list) and o["value"] in node[key]:
                node[key].remove(o["value"])
        elif o["op"] == "scalar":
            if node[key] == o["value"]:      # leave it if the user changed it since
                if o["absent"]:
                    del node[key]
                else:
                    node[key] = o["prev"]
        elif o["op"] == "created" and node[key] in ({}, []):
            del node[key]


def contains(target: dict, ours, path: tuple = ()) -> list[str]:
    """Paths of `ours` that are missing from or differ in `target`."""
    out = []
    for k, v in ours.items():
        name = ".".join((*path, k))
        if k not in target:
            out.append(name)
        elif isinstance(v, dict) and isinstance(target[k], dict):
            out += contains(target[k], v, (*path, k))
        elif isinstance(v, list) and isinstance(target[k], list):
            out += [f"{name}: {json.dumps(i)}" for i in v if i not in target[k]]
        elif target[k] != v:
            out.append(name)
    return out


# ---------------------------------------------------------------- files
def _read_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
    except ValueError as e:
        raise InstallError(f"{p} is not valid JSON ({e}); fix it first") from None
    if not isinstance(d, dict):
        raise InstallError(f"{p} is not a JSON object")
    return d


def _write(p: Path, text: str) -> None:
    if p.is_symlink():
        raise InstallError(f"{p} is a symlink; refusing to write through it")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.cs-tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def _strip_block(text: str) -> str:
    if BEGIN not in text:
        return text
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1] if END in rest else ""
    return head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")


def _with_block(text: str, ctx: str) -> str:
    base = _strip_block(text).rstrip("\n")
    return (base + "\n\n" if base else "") + f"{BEGIN}\n{ctx.rstrip()}\n{END}\n"


def git_root(d: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def _ignored(root: Path, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel]).returncode == 0


def _tracked(root: Path, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", rel],
                          capture_output=True).returncode == 0


def _exclude_file(root: Path) -> Path:
    r = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
                       capture_output=True, text=True, check=True)
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else root / p


# ---------------------------------------------------------------- MCP (local scope)
def _claude() -> str:
    return os.environ.get("CLAUDE_BIN", "claude")


def _claude_json() -> Path:
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) / ".claude.json" if d else Path.home() / ".claude.json"


def mcp_get(root: Path) -> dict | None:
    """The local-scope `slurm` server for root (`claude mcp` keys it by git root)."""
    try:
        d = json.loads(_claude_json().read_text())
    except (OSError, ValueError):
        return None
    key = str(git_root(root) or root)
    return ((d.get("projects") or {}).get(key) or {}).get("mcpServers", {}).get(SERVER)


def _mcp(root: Path, *args: str) -> None:
    r = subprocess.run([_claude(), "mcp", *args], cwd=root, capture_output=True, text=True)
    if r.returncode:
        raise InstallError(f"`claude mcp {args[0]}` failed: {(r.stderr or r.stdout).strip()}")


# ---------------------------------------------------------------- commands
def install(pol: dict, cfg: Path, strict_mcp: bool = False, force: bool = False,
            deny_tools=()) -> list[str]:
    root = Path(pol["project"])
    notes = []
    groot = git_root(root)
    if groot and groot != root and not force:
        raise InstallError(
            f"{root} is inside the git repository {groot}. Claude Code reads "
            ".claude/settings.local.json and local MCP servers at the repository root, so "
            "install there instead (init that directory), or pass --force")
    man_path = cfg / MANIFEST
    old = _read_json(man_path)
    want = desired(pol, cfg, strict_mcp, deny_tools)

    sfile = root / ".claude" / "settings.local.json"
    settings = _read_json(sfile)
    unmerge(settings, old.get("settings_ops", []))
    ops = merge(settings, want["settings"])
    _write(sfile, json.dumps(settings, indent=2) + "\n")

    cur = mcp_get(root)
    if cur is not None and cur != want["mcp"]:
        if not old.get("mcp"):
            raise InstallError(f"a local MCP server named {SERVER!r} already exists for {root}; "
                               f"remove it first (`claude mcp remove {SERVER} -s local`)")
        _mcp(root, "remove", SERVER, "-s", "local")
        cur = None
    if cur is None:
        _mcp(root, "add-json", SERVER, json.dumps(want["mcp"]), "-s", "local")
    mcp_ours = bool(old.get("mcp")) or cur is None

    md = root / "CLAUDE.local.md"
    md_created = old.get("md_created", not md.exists())
    _write(md, _with_block(md.read_text() if md.exists() else "", want["context"]))

    excluded = list(old.get("excluded", []))
    if groot:
        exf = _exclude_file(root)
        for rel in (".claude/settings.local.json", "CLAUDE.local.md"):
            if _tracked(root, rel):
                notes.append(f"{rel} is tracked by git: Claude Code then waits for workspace "
                             "trust before applying its allow rules, and the sandbox block "
                             "could be committed")
            elif not _ignored(root, rel):
                exf.parent.mkdir(parents=True, exist_ok=True)
                with open(exf, "a") as f:
                    f.write(f"/{rel}\n")
                excluded.append(f"/{rel}")

    man = {"project": str(root), "settings_ops": ops, "mcp": mcp_ours,
           "md_created": md_created, "excluded": excluded,
           "strict_mcp": strict_mcp, "deny_tools": list(deny_tools), "desired": want}
    man_path.write_text(json.dumps(man, indent=2) + "\n")
    return notes


def uninstall(pol: dict, cfg: Path) -> list[str]:
    root = Path(pol["project"])
    man_path = cfg / MANIFEST
    man = _read_json(man_path)
    if not man:
        return [f"{root}: not installed"]
    done = []
    sfile = root / ".claude" / "settings.local.json"
    if sfile.exists():
        settings = _read_json(sfile)
        unmerge(settings, man.get("settings_ops", []))
        if settings:
            _write(sfile, json.dumps(settings, indent=2) + "\n")
        else:
            sfile.unlink()
        done.append(f"removed our entries from {sfile}")
    if man.get("mcp") and mcp_get(root) is not None:
        _mcp(root, "remove", SERVER, "-s", "local")
        done.append(f"removed local MCP server {SERVER!r}")
    md = root / "CLAUDE.local.md"
    if md.exists() and not md.is_symlink():
        rest = _strip_block(md.read_text())
        if rest.strip() or not man.get("md_created"):
            _write(md, rest)
        else:
            md.unlink()
        done.append(f"removed the claude-slurm block from {md}")
    if man.get("excluded") and git_root(root):
        exf = _exclude_file(root)
        if exf.exists():
            lines = exf.read_text().splitlines()
            for e in man["excluded"]:
                if e in lines:
                    lines.remove(e)
            exf.write_text("\n".join(lines) + ("\n" if lines else ""))
            done.append(f"removed {', '.join(man['excluded'])} from {exf}")
    man_path.unlink()
    return done


def status(pol: dict, cfg: Path) -> tuple[bool, list[str]]:
    """(installed and in sync, report lines)."""
    root = Path(pol["project"])
    man = _read_json(cfg / MANIFEST)
    if not man:
        return False, [f"not installed in {root}"]
    out, ok = [], True

    def line(cond: bool, msg: str):
        nonlocal ok
        out.append(("PASS  " if cond else "FAIL  ") + msg)
        ok &= cond

    want = desired(pol, cfg, man.get("strict_mcp", False), man.get("deny_tools", []))
    line(man.get("desired") == want, "installed copy matches the current policy"
         + ("" if man.get("desired") == want else " (run `claude-slurm install` again)"))
    sfile = root / ".claude" / "settings.local.json"
    try:
        missing = contains(_read_json(sfile), want["settings"])
    except InstallError as e:
        missing = [str(e)]
    line(not missing, f"{sfile} has every sandbox entry"
         + ("" if not missing else ": missing " + "; ".join(missing[:5])))
    line(mcp_get(root) == want["mcp"], f"local MCP server {SERVER!r} registered")
    md = root / "CLAUDE.local.md"
    line(md.exists() and BEGIN in md.read_text(), f"{md} has the claude-slurm block")
    out += ["WARN  " + w for w in warnings(root)]
    return ok, out


def warnings(root: Path) -> list[str]:
    """Things that load in plain `claude` but not in claude-hpc."""
    out = []
    f = root / ".claude" / "settings.json"
    if f.exists():
        try:
            s = json.loads(f.read_text())
        except ValueError:
            s = {}
            out.append(f"{f} is not valid JSON")
        sb = s.get("sandbox") or {}
        risky = [k for k, v in (("sandbox.excludedCommands", sb.get("excludedCommands")),
                                ("sandbox.filesystem.allowWrite",
                                 (sb.get("filesystem") or {}).get("allowWrite")),
                                ("sandbox.network.allowUnixSockets",
                                 (sb.get("network") or {}).get("allowUnixSockets")),
                                ("sandbox.network.allowAllUnixSockets",
                                 (sb.get("network") or {}).get("allowAllUnixSockets")),
                                ("allowedMcpServers", s.get("allowedMcpServers")),
                                ("hooks", s.get("hooks"))) if v]
        out.append(f"{f} loads in plain claude" + (f" and sets {', '.join(risky)}, which "
                                                    "loosen the sandbox" if risky else ""))
    u = Path.home() / ".claude" / "settings.json"
    try:
        us = json.loads(u.read_text()) if u.exists() else {}
    except ValueError:
        us = {}
    if us.get("allowedMcpServers"):
        out.append(f"{u} sets allowedMcpServers; allowlists merge, so those servers also "
                   "load in this project, outside the sandbox")
    if not git_root(root):
        out.append(f"{root} is not a git repository: sessions started in a subdirectory "
                   "won't load the sandbox settings. Start them at the project root")
    return out
