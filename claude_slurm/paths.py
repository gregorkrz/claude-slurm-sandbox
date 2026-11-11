"""Path helpers shared by the CLI, the broker and the launcher."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# Installed code lives here (copied by install.sh, never inside a project).
PREFIX = Path(__file__).resolve().parent.parent

# Per-project policies live here. Must stay outside every writable area.
CONFIG_ROOT = Path(
    os.environ.get("CLAUDE_SLURM_CONFIG", "~/.config/claude-slurm")
).expanduser().resolve()

HOME = Path.home().resolve()

# Paths end up in sbatch options and Apptainer --bind specs, where ':' ','
# and whitespace have meaning. Keep them boring.
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+@=-]*$")


def within(p: Path | str, root: Path | str) -> bool:
    """True if p == root or p is below root."""
    p, root = Path(p), Path(root)
    return p == root or root in p.parents


def overlaps(a: Path | str, b: Path | str) -> bool:
    return within(a, b) or within(b, a)


def project_id(root: Path) -> str:
    slug = re.sub(r"[^\w.-]", "_", root.name)[:32] or "root"
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    return f"{slug}-{digest}"


def project_cfg(root: Path) -> Path:
    return CONFIG_ROOT / "projects" / project_id(root)


def locate(start: Path | str) -> Path | None:
    """Find the registered project containing `start` (walks up)."""
    p = Path(start).resolve()
    for cand in (p, *p.parents):
        if (project_cfg(cand) / "policy.json").is_file():
            return cand
    return None


def install_info() -> dict:
    f = PREFIX / "install.json"
    if f.is_file():
        return json.loads(f.read_text())
    return {}


def protected_paths() -> list[Path]:
    """Things neither Claude's shell, its file tools, nor jobs may write."""
    out = [CONFIG_ROOT, PREFIX]
    for link in install_info().get("links", []):
        out.append(Path(link))
    return out
