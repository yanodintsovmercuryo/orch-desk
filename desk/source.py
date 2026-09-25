"""Source files named in orchestrator messages: read with a line in focus, open in Zed."""

import os
import subprocess

from . import state

HOME = os.path.expanduser("~")
MAX_BYTES = 2 * 1024 * 1024


def roots():
    return [os.path.realpath(p) for p in (
        os.path.join(HOME, "go", "src", "github.com", "MercuryoPro"),
        os.path.join(HOME, "orca", "workspaces"),
        os.path.join(HOME, "screens"),
        state.state_root(),
    )]


def allowed(path):
    """Only files inside the owner's repositories, worktrees and orchestrator state."""
    real = os.path.realpath(os.path.expanduser(path or ""))
    return real if any(real.startswith(r + os.sep) for r in roots()) else ""


def read(path, line=0):
    real = allowed(path)
    if not real or not os.path.isfile(real):
        raise ValueError("файл не найден или вне разрешённых папок")
    if os.path.getsize(real) > MAX_BYTES:
        raise ValueError("файл слишком большой для просмотра")
    with open(real, "rb") as f:
        data = f.read()
    if b"\0" in data[:4096]:
        raise ValueError("это не текстовый файл")
    lines = data.decode("utf-8", errors="replace").splitlines()
    return {"path": real, "lines": lines, "line": max(0, min(int(line or 0), len(lines)))}


def open_in_zed(path, line=0):
    real = allowed(path)
    if not real or not os.path.exists(real):
        raise ValueError("файл не найден или вне разрешённых папок")
    target = f"{real}:{int(line)}" if line else real
    subprocess.run(["zed", target], check=True, timeout=15)
    return {"ok": True}
