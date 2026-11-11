"""Allowed commands: fixed argv templates the broker runs outside the sandbox.

The policy fixes every argument. Claude supplies only the values of typed
placeholders, which are checked here before substitution. There is no shell,
and a placeholder can't add or remove arguments.

Placeholder types:
  rel_path  relative path under `root`, checked after resolving symlinks.
            Options: root (required), kind any|file|dir (default any).
  enum      one of `values`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TOKEN_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
REL_RE = re.compile(r"^[A-Za-z0-9._+@=/-]{1,512}$")
OUTPUT_TAIL = 4000


class CommandError(ValueError):
    pass


# ---------------------------------------------------------------- init time
def validate(specs: dict) -> dict:
    """Check `allowed_commands` from a --config file and return the stored form."""
    out = {}
    for name, spec in specs.items():
        where = f"allowed_commands.{name}"
        if not NAME_RE.fullmatch(name):
            raise CommandError(f"{where}: name must match {NAME_RE.pattern}")
        if not isinstance(spec, dict):
            raise CommandError(f"{where}: must be an object")
        extra = set(spec) - {"argv", "cwd", "placeholders", "timeout", "ask",
                             "min_interval_s", "description"}
        if extra:
            raise CommandError(f"{where}: unknown keys {sorted(extra)}")

        argv = spec.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
            raise CommandError(f"{where}.argv: must be a non-empty list of strings")
        if TOKEN_RE.search(argv[0]):
            raise CommandError(f"{where}.argv[0]: the program can't be a placeholder")
        prog = argv[0] if argv[0].startswith("/") else shutil.which(argv[0], path="/usr/bin:/bin")
        if not prog or not os.access(prog, os.X_OK):
            raise CommandError(f"{where}.argv[0]: {argv[0]} is not an executable")
        prog = str(Path(prog).resolve())

        phs = spec.get("placeholders") or {}
        if not isinstance(phs, dict):
            raise CommandError(f"{where}.placeholders: must be an object")
        used = {m for a in argv[1:] for m in TOKEN_RE.findall(a)}
        for a in argv[1:]:
            if "{" in TOKEN_RE.sub("", a) or "}" in TOKEN_RE.sub("", a):
                raise CommandError(f"{where}.argv: stray brace in {a!r}")
        if used != set(phs):
            raise CommandError(f"{where}: placeholders used {sorted(used)} "
                               f"but declared {sorted(phs)}")
        stored_ph = {p: _validate_placeholder(f"{where}.placeholders.{p}", d)
                     for p, d in phs.items()}

        cwd = spec.get("cwd", "/")
        if not isinstance(cwd, str) or not cwd.startswith("/") or not Path(cwd).is_dir():
            raise CommandError(f"{where}.cwd: must be an existing absolute directory")
        timeout = spec.get("timeout", 300)
        interval = spec.get("min_interval_s", 0)
        for k, v, lo, hi in (("timeout", timeout, 1, 3600), ("min_interval_s", interval, 0, 86400)):
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise CommandError(f"{where}.{k}: integer in [{lo}, {hi}]")
        ask = spec.get("ask", True)
        if not isinstance(ask, bool):
            raise CommandError(f"{where}.ask: must be true/false")
        out[name] = {"argv": [prog, *argv[1:]], "cwd": str(Path(cwd).resolve()),
                     "placeholders": stored_ph, "timeout": timeout, "ask": ask,
                     "min_interval_s": interval,
                     "description": str(spec.get("description", ""))}
    return out


def _validate_placeholder(where: str, d) -> dict:
    if not isinstance(d, dict) or d.get("type") not in ("rel_path", "enum"):
        raise CommandError(f"{where}: needs type rel_path or enum")
    if d["type"] == "rel_path":
        if set(d) - {"type", "root", "kind"}:
            raise CommandError(f"{where}: rel_path takes root and kind")
        root = d.get("root")
        if not isinstance(root, str) or not root.startswith("/") or not Path(root).is_dir():
            raise CommandError(f"{where}.root: must be an existing absolute directory")
        kind = d.get("kind", "any")
        if kind not in ("any", "file", "dir"):
            raise CommandError(f"{where}.kind: any, file or dir")
        return {"type": "rel_path", "root": str(Path(root).resolve()), "kind": kind}
    vals = d.get("values")
    if set(d) - {"type", "values"} or not (isinstance(vals, list) and vals
                                           and all(isinstance(v, str) and REL_RE.fullmatch(v)
                                                   and not v.startswith("-") for v in vals)):
        raise CommandError(f"{where}: enum needs values, a list of plain strings")
    return {"type": "enum", "values": vals}


# ---------------------------------------------------------------- run time
def check_value(name: str, ph: dict, value) -> str:
    if not isinstance(value, str):
        raise CommandError(f"{name}: must be a string")
    if ph["type"] == "enum":
        if value not in ph["values"]:
            raise CommandError(f"{name}: must be one of {ph['values']}")
        return value
    rel = value.rstrip("/")
    if not REL_RE.fullmatch(rel or "x") or rel.startswith("/"):
        raise CommandError(f"{name}: relative path of [A-Za-z0-9._+@=/-] only")
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith("-") for p in parts):
        raise CommandError(f"{name}: no empty, '.', '..' or '-'-leading components")
    root = Path(ph["root"])
    real = Path(os.path.realpath(root / rel))
    if real != root and root not in real.parents:
        raise CommandError(f"{name}: {rel} resolves outside {root}")
    if not real.exists():
        raise CommandError(f"{name}: {rel} does not exist under {root}")
    if ph["kind"] == "file" and not real.is_file():
        raise CommandError(f"{name}: {rel} is not a file")
    if ph["kind"] == "dir" and not real.is_dir():
        raise CommandError(f"{name}: {rel} is not a directory")
    # Pass the resolved path so the command never walks a symlink we checked.
    return str(real.relative_to(root)) if real != root else "."


def build_argv(spec: dict, params) -> list[str]:
    if not isinstance(params, dict):
        raise CommandError("params must be an object")
    want = set(spec["placeholders"])
    if set(params) != want:
        raise CommandError(f"params must be exactly {sorted(want)}")
    vals = {k: check_value(k, spec["placeholders"][k], v) for k, v in params.items()}
    return [spec["argv"][0], *(TOKEN_RE.sub(lambda m: vals[m.group(1)], a)
                               for a in spec["argv"][1:])]


def _env() -> dict:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for k in ("HOME", "USER", "LOGNAME", "SSH_AUTH_SOCK"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def run(pol: dict, name: str, params, logs_dir: Path) -> dict:
    specs = pol.get("allowed_commands") or {}
    if name not in specs:
        raise CommandError(f"no allowed command {name!r}; have {sorted(specs)}")
    spec = specs[name]
    argv = build_argv(spec, params)

    state_dir = logs_dir / "commands"
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = state_dir / f"{name}.last"
    if spec["min_interval_s"] and stamp.exists():
        wait = spec["min_interval_s"] - (time.time() - stamp.stat().st_mtime)
        if wait > 0:
            raise CommandError(f"{name}: rate limited, retry in {int(wait) + 1}s")
    stamp.touch()

    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=spec["cwd"], env=_env(), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=spec["timeout"],
                           start_new_session=True)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = -1, _text(e.stdout), _text(e.stderr) + f"\ntimed out after {spec['timeout']}s"
    res = {"command": name, "rc": rc, "seconds": round(time.time() - t0, 1),
           "stdout": out[-OUTPUT_TAIL:], "stderr": err[-OUTPUT_TAIL:]}
    with open(logs_dir / "commands.log", "a") as f:
        f.write(json.dumps({"time": int(t0), "params": params, "argv": argv, **res}) + "\n")
    return res


def _text(b) -> str:
    if b is None:
        return ""
    return b.decode(errors="replace") if isinstance(b, bytes) else b


def describe(pol: dict) -> dict:
    return {n: {"description": s["description"],
                "params": {p: ({"type": "rel_path", "under": d["root"], "kind": d["kind"]}
                               if d["type"] == "rel_path" else d)
                           for p, d in s["placeholders"].items()},
                "argv": s["argv"]}
            for n, s in (pol.get("allowed_commands") or {}).items()}
