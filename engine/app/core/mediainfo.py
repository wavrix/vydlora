"""Read a media file's duration from the file itself.

The alternative was asking TikTok, which meant a headless-browser page load per
delivery: seconds of latency, a WAF to get past, and a value that could come back
empty. The file is already on disk by then, and an MP4 states its own duration in
the header - so this is instant, offline, and describes the exact file that was
delivered rather than what the post page claimed.

Pure stdlib on purpose: ffprobe is not bundled with the app, and adding it for
one number would cost far more than parsing the box that holds it.
"""

from __future__ import annotations

import struct
from pathlib import Path

from .logging_config import get_logger

log = get_logger("mediainfo")

# An MP4 is a tree of boxes: [4-byte size][4-byte type][payload]. Duration lives
# in `mvhd` inside `moov`. Only these two need walking into; everything else is
# skipped whole, so the read touches a few hundred bytes of a large file.
_CONTAINERS = {b"moov", b"trak", b"mdia"}


def _walk(fh, end: int) -> float | None:
    while fh.tell() < end:
        header = fh.read(8)
        if len(header) < 8:
            return None
        size, kind = struct.unpack(">I4s", header)

        if size == 1:                      # 64-bit size follows the header
            size = struct.unpack(">Q", fh.read(8))[0]
            body = size - 16
        elif size == 0:                    # extends to end of file
            body = end - fh.tell()
        else:
            body = size - 8
        if body < 0:
            return None

        if kind == b"mvhd":
            version = fh.read(1)[0]
            fh.read(3)                     # flags
            if version == 1:
                fh.read(16)                # 64-bit created/modified
                timescale = struct.unpack(">I", fh.read(4))[0]
                duration = struct.unpack(">Q", fh.read(8))[0]
            else:
                fh.read(8)                 # 32-bit created/modified
                timescale = struct.unpack(">I", fh.read(4))[0]
                duration = struct.unpack(">I", fh.read(4))[0]
            return duration / timescale if timescale else None

        if kind in _CONTAINERS:
            found = _walk(fh, fh.tell() + body)
            if found is not None:
                return found
        else:
            fh.seek(body, 1)
    return None


def duration_seconds(path: Path | str | None) -> float | None:
    """Duration of an MP4 in seconds, or None if it can't be determined.

    Never raises: a missing duration is worth omitting from a caption, never
    worth failing a delivery over.
    """
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return None
        with p.open("rb") as fh:
            return _walk(fh, p.stat().st_size)
    except Exception as exc:
        log.debug("could not read duration of %s: %s", p.name, exc)
        return None
