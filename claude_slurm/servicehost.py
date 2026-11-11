"""Runs one service: serves the broker socket and supervises the sandboxed script.

Started by systemd-run (see services.py). Runs outside the sandbox, from the
installed code, as the user.
"""
from __future__ import annotations

import json
import os
import resource
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import services
from .core import Broker
from .jobscript import RequestError

MAX_REQUEST = 1 << 20


def _log_fd(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def _serve(sock: socket.socket, broker: Broker, pol: dict, log) -> None:
    lock = threading.Lock()
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(30)
            buf, req = b"", None
            try:
                while b"\n" not in buf and len(buf) < MAX_REQUEST:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                req = json.loads(buf.split(b"\n", 1)[0] or b"null")
                with lock:
                    res = {"ok": True, "result": services.handle_request(broker, pol, req)}
            except (RequestError, services.ServiceError, ValueError, TypeError) as e:
                res = {"ok": False, "error": str(e)}
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": f"broker error: {e}"}
            op = req.get("op") if isinstance(req, dict) else None
            log(f"[claude-slurm] request {op}: {'ok' if res['ok'] else res['error']}")
            try:
                conn.sendall(services.encode(res))
            except OSError:
                pass


def main(argv: list[str]) -> int:
    policy_path, name, script, *args = argv
    broker = Broker(policy_path)
    pol = broker.pol
    cfg = Path(policy_path).parent
    if not (pol.get("services") or {}).get("enabled"):
        print("services disabled in policy", file=sys.stderr)
        return 2
    real = services.check_script(pol, script)
    args = services.check_args(args)

    fd = _log_fd(services.log_path(pol, name))

    def log(msg: str) -> None:
        os.write(fd, f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n".encode())

    sock_path = services.socket_path(cfg, name)
    sock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(sock_path.parent, 0o700)
    if sock_path.is_socket() or sock_path.is_symlink():
        sock_path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        sock.bind(str(sock_path))
    finally:
        os.umask(old)
    sock.listen(8)
    threading.Thread(target=_serve, args=(sock, broker, pol, log), daemon=True).start()

    mem = pol["services"].get("memory_max_gb")

    def limits():
        if mem:
            resource.setrlimit(resource.RLIMIT_AS, (mem << 30, mem << 30))

    argv_ = services.bwrap_argv(pol, real, args, sock_path, name)
    log(f"[claude-slurm] start {name}: {real} {' '.join(args)}")
    child = subprocess.Popen(argv_, stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                             preexec_fn=limits, close_fds=True)

    def forward(sig, _frame):
        child.send_signal(sig)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    rc = child.wait()
    log(f"[claude-slurm] {name} exited with {rc}")
    sock.close()
    try:
        sock_path.unlink()
    except OSError:
        pass
    return rc if rc >= 0 else 128 - rc
