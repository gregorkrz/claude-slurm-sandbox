"""Policy file: what one project's Claude is allowed to do on Slurm."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath

POLICY_VERSION = 1
TAG = "claude-sbx"

# Container paths a bind may not land on (exact) or under (prefix).
RESERVED_EXACT = {"/", "/opt", "/home", "/mnt", "/srv"}
RESERVED_PREFIX = ("/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin",
                   "/lib", "/lib64", "/var", "/run", "/tmp", "/.singularity.d",
                   "/boot", "/root")


class PolicyError(Exception):
    pass


def load(path: Path | str) -> dict:
    path = Path(path)
    pol = json.loads(path.read_text())
    if pol.get("version") != POLICY_VERSION:
        raise PolicyError(f"{path}: unsupported policy version {pol.get('version')}")
    return pol


def image_fingerprint(path: Path | str) -> dict:
    st = os.stat(path)
    return {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ino": st.st_ino}


def image_unchanged(pol: dict) -> bool:
    rec = pol["image"]
    try:
        cur = image_fingerprint(rec["path"])
    except FileNotFoundError:
        return False
    return all(cur[k] == rec[k] for k in ("size", "mtime_ns", "ino"))


def sha256_file(path: Path | str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def check_container_path(p: str) -> str:
    pp = PurePosixPath(p)
    if not pp.is_absolute() or ".." in pp.parts:
        raise PolicyError(f"container path must be absolute and normalized: {p}")
    s = str(pp)
    if s in RESERVED_EXACT or any(s == r or s.startswith(r + "/") for r in RESERVED_PREFIX):
        raise PolicyError(f"container path {s} would shadow a system directory")
    return s


def container_overlap(a: str, b: str) -> bool:
    a, b = PurePosixPath(a), PurePosixPath(b)
    return a == b or a in b.parents or b in a.parents
