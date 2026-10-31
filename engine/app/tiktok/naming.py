"""Windows-safe account folder naming with stylised-Unicode transliteration.

Format produced:  "(nickname) - (@username)"
Ported from monitor.py.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

WINDOWS_UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")
MAX_ACCOUNT_FOLDER_CHARS = 180

_UNICODE_TRANSLITERATION: dict[int, str] = {}


def _build_transliteration_table() -> None:
    styles = [
        (0x1D400, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D41A, True, 0x1D7CE),
        (0x1D434, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D44E, False, None),
        (0x1D468, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D482, False, None),
        (0x1D49C, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D4B6, False, None),
        (0x1D4D0, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D4EA, False, None),
        (0x1D504, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D51E, False, None),
        (0x1D538, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D552, True, 0x1D7D8),
        (0x1D56C, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D586, False, None),
        (0x1D5A0, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D5BA, True, 0x1D7E2),
        (0x1D5D4, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D5EE, True, 0x1D7EC),
        (0x1D608, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D622, False, None),
        (0x1D63C, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D656, False, None),
        (0x1D670, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", 0x1D68A, True, 0x1D7F6),
    ]
    for upper_start, letters, lower_start, has_digits, digit_start in styles:
        for i, ch in enumerate(letters):
            _UNICODE_TRANSLITERATION[upper_start + i] = ch
            _UNICODE_TRANSLITERATION[lower_start + i] = ch.lower()
        if has_digits and digit_start is not None:
            for d in range(10):
                _UNICODE_TRANSLITERATION[digit_start + d] = str(d)
    for cp in range(0xFF01, 0xFF5F):
        ascii_cp = cp - 0xFEE0
        if 0x21 <= ascii_cp <= 0x7E:
            _UNICODE_TRANSLITERATION[cp] = chr(ascii_cp)
    for i in range(26):
        _UNICODE_TRANSLITERATION[0x24B6 + i] = chr(ord("A") + i)
    for i in range(26):
        _UNICODE_TRANSLITERATION[0x24D0 + i] = chr(ord("a") + i)
    for i in range(20):
        _UNICODE_TRANSLITERATION[0x2460 + i] = str(i + 1)
    _UNICODE_TRANSLITERATION.update(
        {0x2070: "0", 0x00B9: "1", 0x00B2: "2", 0x00B3: "3", 0x2074: "4",
         0x2075: "5", 0x2076: "6", 0x2077: "7", 0x2078: "8", 0x2079: "9"}
    )
    for i in range(10):
        _UNICODE_TRANSLITERATION[0x2080 + i] = str(i)


_build_transliteration_table()


def _transliterate(text: str) -> str:
    return "".join(_UNICODE_TRANSLITERATION.get(ord(ch), ch) for ch in text)


def unicode_to_plain(value: str) -> str:
    text = _transliterate(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", errors="ignore").decode("ascii")
    return "".join(ch for ch in text if ch.isprintable())


def _is_emoji(char: str) -> bool:
    cp = ord(char)
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x1F1E6 <= cp <= 0x1F1FF
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or cp in {0x200D, 0xFE0E, 0xFE0F}
    )


def clean_folder_label(value: Optional[str]) -> str:
    if value is None:
        return ""
    cleaned = "".join(
        ch for ch in str(value)
        if not _is_emoji(ch) and not unicodedata.category(ch).startswith("C")
    )
    cleaned = WINDOWS_UNSAFE_CHARS_RE.sub(" ", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned)
    return cleaned.strip(" .")


def _has_alnum(value: str) -> bool:
    return any(ch.isalnum() for ch in value)


def _nickname_for_folder(nickname: str, username: str) -> str:
    if not nickname:
        return clean_folder_label(username)
    label = clean_folder_label(unicode_to_plain(nickname))
    return label if _has_alnum(label) else clean_folder_label(username)


def account_folder_name(nickname: str, username: str) -> str:
    """Build "(nickname) - (@username)", capped to MAX_ACCOUNT_FOLDER_CHARS."""
    username_label = clean_folder_label(username) or "account"
    if not _has_alnum(username_label):
        username_label = "account"

    nickname_label = _nickname_for_folder(nickname or username, username)
    if not _has_alnum(nickname_label):
        nickname_label = username_label

    suffix = f" - (@{username_label})"
    max_nick = max(1, MAX_ACCOUNT_FOLDER_CHARS - len(suffix))
    nickname_label = nickname_label[:max_nick].rstrip(" .")
    return f"{nickname_label}{suffix}"
