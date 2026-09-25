"""Screenshot folders named in orchestrator messages: list, serve and reveal, read only."""

import os
import re
import subprocess

from . import state

SCREENS = os.path.expanduser("~/screens")
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
PAIR_RE = re.compile(r"^(?P<base>.+?)-(?P<side>before|after)$")


def roots():
    return [os.path.realpath(SCREENS), os.path.realpath(state.state_root())]


def allowed(path):
    """Only files under the screens folder or the orchestrator state are ever read."""
    real = os.path.realpath(os.path.expanduser(path or ""))
    return real if any(real == r or real.startswith(r + os.sep) for r in roots()) else ""


def image(path):
    real = allowed(path)
    ext = os.path.splitext(real)[1].lower()
    if not real or ext not in IMAGE_EXT or not os.path.isfile(real):
        raise ValueError("не картинка из разрешённой папки")
    with open(real, "rb") as f:
        return f.read(), IMAGE_EXT[ext]


def listing(path):
    """Images of a folder (or of a file's folder) grouped into before/after pairs, in name order."""
    real = allowed(path)
    if not real or not os.path.exists(real):
        raise ValueError("папка не найдена или не разрешена")
    folder = real if os.path.isdir(real) else os.path.dirname(real)
    groups, order = {}, []
    for name in sorted(os.listdir(folder)):
        stem, ext = os.path.splitext(name)
        if ext.lower() not in IMAGE_EXT:
            continue
        m = PAIR_RE.match(stem)
        base, side = (m.group("base"), m.group("side")) if m else (stem, "after")
        if base not in groups:
            groups[base] = {"name": base}
            order.append(base)
        groups[base][side] = os.path.join(folder, name)
    items = [groups[b] for b in order]
    start = 0
    if real != folder:
        start = next((i for i, g in enumerate(items) if real in (g.get("before"), g.get("after"))), 0)
    return {"folder": folder, "items": items, "start": start}


def reveal(path):
    real = allowed(path)
    if not real or not os.path.exists(real):
        raise ValueError("путь не найден или не разрешён")
    subprocess.run(["open", "-R", real], check=True, timeout=10)
    return {"ok": True}
