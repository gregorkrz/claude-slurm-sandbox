"""JSON policy files for `claude-slurm init --config` and `show --json`.

The file uses one key per init flag. CLI flags given explicitly override the
file; for repeatable flags the CLI list replaces the file's list. Every value
goes through the same checks as the flags.
"""
from __future__ import annotations

import json
from pathlib import Path

# key -> (argparse dest, kind)
SCALARS = {
    "project": ("project", "path"),
    "project_mount": ("project_mount", "str"),
    "image": ("image", "path"),
    "logs_dir": ("logs_dir", "path"),
    "apptainer": ("apptainer", "path"),
    "apptainer_gpu_flag": ("apptainer_gpu_flag", "str"),
    "net_none": ("net_none", "bool"),
    "hash_image": ("hash_image", "bool"),
    "account": ("account", "str"),
    "qos": ("qos", "str"),
    "gpu_flag": ("gpu_flag", "str"),
    "max_minutes": ("max_minutes", "int"),
    "max_cpus": ("max_cpus", "int"),
    "max_mem_gb": ("max_mem_gb", "int"),
    "max_gpus": ("max_gpus", "int"),
    "max_active": ("max_active", "int"),
    "max_array": ("max_array", "int"),
    "slurm_bin_dir": ("slurm_bin_dir", "path"),
    "auto_submit": ("auto_submit", "bool"),
    "ask_cancel": ("ask_cancel", "bool"),
    "allow_project_settings": ("allow_project_settings", "bool"),
    "job_prelude": ("job_prelude_text", "str"),
}
LISTS = {
    "partitions": "partition",
    "accounts": "allow_account",
    "qos_list": "allow_qos",
    "deny_accounts": "deny_account",
    "binds": "bind",
    "login_write": "login_write",
    "allow_domains": "allow_domain",
    "deny_read": "deny_read",
    "no_mount": "no_mount",
}
# Handled by their own modules; passed through as raw JSON.
SECTIONS = {"allowed_commands": "allowed_commands", "services": "services"}


class ConfigError(Exception):
    pass


def _path(key: str, v) -> str:
    if not isinstance(v, str) or not (v.startswith("/") or v.startswith("~")):
        raise ConfigError(f"{key}: must be an absolute path (or start with ~), got {v!r}")
    return v


def _bind(i: int, b) -> str:
    if isinstance(b, str):
        return b
    if not isinstance(b, dict) or "host" not in b or set(b) - {"host", "cont", "mode"}:
        raise ConfigError(f"binds[{i}]: use a string or {{host, cont?, mode?}}")
    parts = [_path(f"binds[{i}].host", b["host"])]
    for k in ("cont", "mode"):
        if b.get(k) is not None and (not isinstance(b[k], str) or ":" in b[k]):
            raise ConfigError(f"binds[{i}].{k}: bad value {b[k]!r}")
    if ":" in parts[0]:
        raise ConfigError(f"binds[{i}].host: ':' not allowed")
    if b.get("cont"):
        parts.append(b["cont"])
    if b.get("mode"):
        parts.append(b["mode"])
    return ":".join(parts)


def load_config(path: str) -> dict:
    """Return {argparse dest: value} from a JSON policy file."""
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be an object")
    unknown = set(data) - set(SCALARS) - set(LISTS) - set(SECTIONS) - {"$schema", "comment"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)}")

    out = {}
    for key, (dest, kind) in SCALARS.items():
        if key not in data or data[key] is None:
            continue
        v = data[key]
        if kind == "bool" and not isinstance(v, bool):
            raise ConfigError(f"{key}: must be true/false")
        if kind == "int" and (not isinstance(v, int) or isinstance(v, bool)):
            raise ConfigError(f"{key}: must be an integer")
        if kind in ("str", "path") and not isinstance(v, str):
            raise ConfigError(f"{key}: must be a string")
        out[dest] = _path(key, v) if kind == "path" else v
    for key, dest in LISTS.items():
        if key not in data:
            continue
        v = data[key]
        if not isinstance(v, list):
            raise ConfigError(f"{key}: must be a list")
        if key == "binds":
            v = [_bind(i, b) for i, b in enumerate(v)]
        elif not all(isinstance(x, str) for x in v):
            raise ConfigError(f"{key}: must be a list of strings")
        if key in ("login_write",):
            v = [_path(f"{key}[{i}]", x) for i, x in enumerate(v)]
        out[dest] = ",".join(v) if key == "no_mount" else v
    for key, dest in SECTIONS.items():
        if key in data:
            if not isinstance(data[key], dict):
                raise ConfigError(f"{key}: must be an object")
            out[dest] = data[key]
    return out


def export(pol: dict, default_deny_read: list[str]) -> dict:
    """The stored policy as a --config file (round-trips through init)."""
    s, ap, login = pol["slurm"], pol["apptainer"], pol["login"]
    sbatch = s["bin"].get("sbatch")
    out = {
        "project": pol["project"],
        "project_mount": pol["project_mount"],
        "image": pol["image"]["path"],
        "hash_image": "sha256" in pol["image"],
        "logs_dir": pol["logs_dir"],
        "binds": [{"host": b["host"], "cont": b["container"], "mode": b["mode"]}
                  for b in pol["binds"]],
        "job_prelude": pol.get("job_prelude", ""),
        "apptainer": ap["bin"],
        "apptainer_gpu_flag": ap.get("gpu_flag", "--nv"),
        "no_mount": ap.get("no_mount", []),
        "net_none": ap.get("net_none", False),
        "partitions": s["partitions"],
        "account": s.get("account"),
        "accounts": s.get("accounts") or [],
        "qos": s.get("qos"),
        "qos_list": s.get("qos_list") or [],
        "deny_accounts": s.get("deny_accounts") or [],
        "gpu_flag": s.get("gpu_flag", "gpus"),
        **{k: s[k] for k in ("max_minutes", "max_cpus", "max_mem_gb", "max_gpus",
                             "max_active", "max_array")},
        "slurm_bin_dir": str(Path(sbatch).parent) if sbatch else None,
        "login_write": login["extra_write"],
        "allow_domains": login["allowed_domains"],
        "deny_read": [p for p in login["deny_read"] if p not in default_deny_read],
        "auto_submit": pol["claude"]["auto_submit"],
        "ask_cancel": not pol["claude"]["auto_cancel"],
        "allow_project_settings": pol["claude"]["project_settings"],
    }
    if pol.get("allowed_commands"):
        out["allowed_commands"] = pol["allowed_commands"]
    if (pol.get("services") or {}).get("enabled"):
        out["services"] = {k: v for k, v in pol["services"].items() if k != "bin"}
    return {k: v for k, v in out.items() if v is not None}
