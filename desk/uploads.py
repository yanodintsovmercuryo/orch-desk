"""Images attached to a reply: stored locally, handed to the orchestrator as file paths."""

import os
import time
import uuid

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads")
MAX_BYTES = 15 * 1024 * 1024
KEEP_DAYS = 30
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def _looks_like(data, ctype):
    # The declared type must match the bytes: nothing but images lands on disk.
    if ctype == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ctype == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if ctype == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if ctype == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def save(data, ctype):
    ctype = (ctype or "").split(";")[0].strip().lower()
    if ctype not in TYPES:
        raise ValueError("only png, jpeg, gif or webp images")
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"image must be 1 byte to {MAX_BYTES // (1024 * 1024)} MB")
    if not _looks_like(data, ctype):
        raise ValueError("file content is not a " + ctype)
    os.makedirs(DIR, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + TYPES[ctype]
    path = os.path.join(DIR, name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def checked(paths):
    """Only files this server stored may be named in a reply."""
    root = os.path.realpath(DIR) + os.sep
    out = []
    for p in paths or []:
        real = os.path.realpath(str(p))
        if not real.startswith(root) or not os.path.isfile(real):
            raise ValueError("unknown attachment")
        out.append(real)
    return out


def prune(days=KEEP_DAYS):
    if not os.path.isdir(DIR):
        return
    cutoff = time.time() - days * 86400
    for name in os.listdir(DIR):
        path = os.path.join(DIR, name)
        if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
            os.remove(path)
