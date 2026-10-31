"""Telegram bot.

Behaviour:
  * Sending a TikTok link downloads that post to disk/DB. The media is uploaded
    back to the chat only when the "Upload downloaded media to Telegram" toggle
    is on; otherwise the bot replies with the download status only.
  * `/user <username|post_url>` adds a creator to the monitoring list.
  * When the "Send new posts to Telegram" feature is configured (authorized chat
    IDs set), each newly downloaded monitored post is announced to those chats -
    with the video attached when the toggle is on, or just a message + buttons
    when it's off.

The bot token + authorized chat ids are read live from the DB settings (falling
back to env), so they can be configured from the app and applied via `restart`.
Runs in its own thread with its own event loop.
"""

from __future__ import annotations

import asyncio
import re
import functools
import threading
import time
from pathlib import Path

from sqlalchemy import desc, func, select

from ..core.config import settings as cfg
from ..core.database import session_scope
from ..core.events import publish
from ..core import mediainfo
from ..core.logging_config import get_logger
from .. import models, platforms, repositories as repo
from ..models.enums import ActivityType, DownloadStatus, JobStatus, LogSource, PostType
from ..instagram import urls as ig_urls
from ..x import urls as x_urls
from . import x_service
from ..tiktok import urls
from . import (
    cookie_service,
    download_service,
    events_service,
    instagram_service,
    monitor_service,
    settings_service,
    stats_service,
    storage_service,
    user_service,
)

# Not under "telegram": logging_config silences that name to quiet python-telegram-
# bot's own chatter, which silenced this module along with it - every INFO it
# logged, including "bot polling" and which API it connected to, was dropped
# before reaching the file. Hours were spent reading a log that could not
# have shown the answer. A child name like "telegram.lora1" would not help -
# logger levels inherit down the dotted hierarchy - so this sits outside it.
log = get_logger("tgbot")

_thread: threading.Thread | None = None
_app = None
_loop: asyncio.AbstractEventLoop | None = None
_allowed: set[int] = set()
_lifecycle_lock = threading.RLock()

# Every reply is plain text, no markup: Telegram's parsers choke on the
# punctuation that turns up in real post titles, and an unparsed message is worse
# than an unstyled one.
#
# Grouped by what you're trying to do rather than listed flat: the commands are
# only meaningful next to the job they belong to.
HELP = (
    "Here's everything I can do.\n\n"
    "SAVING THINGS\n"
    "Send a TikTok, Instagram or X post link and I'll fetch it. No command needed.\n"
    "That covers Instagram posts, reels, stories and highlights.\n\n"
    "CREATORS\n"
    "Send a TikTok username on its own, like @username, and I'll show you who\n"
    "it is, with buttons to monitor them or save everything they've posted.\n"
    "Send an Instagram or X profile link for the same card and monitoring buttons.\n"
    "Instagram saves posts, highlights and stories. X saves all available media posts.\n\n"
    "/users - who I'm following right now\n"
    "/pause - stop checking for new posts\n"
    "/resume - start checking again\n\n"
    "CHECKING IN\n"
    "/queue - what I'm working on\n"
    "/downloads - what I've saved so far\n"
    "/stats - the numbers at a glance\n"
    "/health - confirm everything is running\n"
)

# Shown to chats outside the approved list. Two variants: one for when guests may
# still download, one for when the bot is fully private. Neither mentions a
# command, because naming something they cannot run only invites them to try it.
GUEST_HELP = (
    "Hi! Send me a TikTok link and I'll grab the video or photos for you.\n\n"
    "You can send as many links as you like, one after another.\n\n"
    "If you need anything beyond that, pass the ID below to whoever runs this bot."
)
GUEST_DENIED = (
    "Hi! This bot is private, so I can't download anything for you at the moment.\n\n"
    "If you know the person who runs it, send them the ID below and they can let you in."
)

# Sent by /start to anyone who can actually download. Someone who can't gets the
# guest reply instead: greeting them with an offer and then refusing the link
# they send would be a worse first impression than saying so up front.
WELCOME = (
    "Let's download our first TikTok!\n"
    "Send me a link and I'll fetch it for you."
)

# Drives Telegram's "/" menu. Registered per approved chat, never globally, so a
# guest typing "/" sees nothing and can't discover the admin surface.
MENU_COMMANDS = [
    ("start", "Say hello"),
    ("ig", "Save something from Instagram"),
    ("users", "Who I'm following"),
    ("queue", "What I'm working on"),
    ("downloads", "What I've saved"),
    ("stats", "The numbers at a glance"),
    ("storage", "Where saved media is kept"),
    ("health", "Check everything is running"),
    ("pause", "Stop checking for new posts"),
    ("resume", "Start checking again"),
    ("help", "Everything I can do"),
]

# Chats whose menu we've populated. Persisted so a restart can still clear the
# menu for someone who has since been removed from the approved list.
_MENU_SCOPES_KEY = "telegram_menu_scopes"

# How many updates the bot may process at once. Enough that a burst of links all
# start immediately, bounded so a flood can't spawn unlimited download threads.
CONCURRENT_UPDATES = 8

# Seconds allowed for pushing a file up. Generous on purpose: the ceiling is a
# 50 MB post on a slow line, and failing that as a timeout is indistinguishable
# to the user from the download itself having gone wrong.
MEDIA_UPLOAD_TIMEOUT = 600.0

# ── progress reporting ──
# A link can take a while, and a chat that sits silent reads as broken. These
# describe what the person asked for, never how it's being done: "downloading"
# and "uploading to Telegram" are our problems, not theirs. For the same reason
# there is one heading for the whole wait rather than one per internal stage.
FETCH_START = "Fetching video, please wait..."
FETCH_WORKING = "Fetching video..."
# Instagram opens on the same line as TikTok. A separate "fetching from
# Instagram" message was the one place the two flows announced themselves
# differently for no reason the person sending the link would care about.
#
# How many files one Instagram request may hand back. A carousel or a
# whole profile can run to hundreds; the rest stay in the library.
IG_SEND_CAP = 10
# Fallback only: shown if a progress source stops reporting after having
# reported. The normal path holds the bar instead of falling back to words.
FETCH_FINALISING = "Almost there, just a moment..."

# Long enough that Telegram never rate-limits the edits, short enough that the
# message visibly moves. Each tick only edits when the rendered text differs, so
# a ten-cell bar tops out at one edit per 10%.
PROGRESS_POLL_SECS = 2.5
PROGRESS_CELLS = 10

# Share of the bar given to fetching the file when the result also has to be
# handed back. From the outside this is one wait, so it gets one bar; the split
# is only about how much of it each half is allowed to fill.
FETCH_SHARE = 85.0
# How fast the remainder creeps once fetching is done, in percent per second.
HANDOVER_CREEP = 2.5

# Shown on the empty-chat screen above Telegram's START button, and on the bot's
# profile. Telegram won't render START without this being worth reading, and a
# blank bot profile looks broken to anyone opening the link for the first time.
# Telegram prints its own "What can this bot do?" heading above this, so the
# line is not repeated here - it would render twice.
BOT_DESCRIPTION = "Download TikTok videos without watermark in HD quality."
BOT_SHORT_DESCRIPTION = "Download TikTok videos without watermark in HD quality."


# ── live config (DB overrides env) ──
def _parse_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
    return out


def live_token(db) -> str:
    stored = repo.Settings.get(db, "bot_token", None)
    return ((stored if stored is not None else cfg.bot_token) or "").strip()


def live_allowed_ids(db) -> set[int]:
    stored = repo.Settings.get(db, "telegram_allowed_ids", None)
    return _parse_ids(stored if stored is not None else cfg.allowed_ids)


def is_enabled() -> bool:
    with session_scope() as db:
        return bool(live_token(db))


def bot_username() -> str:
    """Cached identity from polling initialization; never performs a request."""
    try:
        return _app.bot.username if _app else ""
    except RuntimeError:
        return ""


def is_running() -> bool:
    return not _stopping.is_set() and _thread is not None and _thread.is_alive()


# ── authorization ──
# Two tiers. Approved chats get every command; everyone else can, at most, send
# a TikTok link and receive the file. Instagram is deliberately not offered to
# guests because it downloads through the owner's saved session cookies.


def _approved(chat_id: int) -> bool:
    """Deny by default: an empty list approves nobody.

    An empty list used to mean "everyone", so the protection silently did nothing
    until an ID was added. Locking the owner out isn't a risk worth trading for
    that: approvals are managed from the desktop app, and the guest replies hand
    back the chat ID needed to grant access.

    Read live rather than from the set captured at start(). Approving yourself in
    Settings writes the database, but the running bot went on consulting the copy
    it loaded at launch - so the approval did nothing until the backend happened
    to restart, and every command kept being refused.
    """
    if chat_id in _allowed:
        return True
    try:
        with session_scope() as db:
            live = live_allowed_ids(db)
    except Exception:
        return False
    # Keep the cache in step so the next check costs nothing.
    _allowed.update(live)
    return chat_id in live


def _guests_may_download() -> bool:
    with session_scope() as db:
        return bool(settings_service.get(db, "telegram_public_downloads"))


def _plural(n: int, word: str) -> str:
    """"1 post" / "2 posts". Counts land in these replies constantly and reading
    "1 posts" makes the whole bot feel unfinished."""
    return word if n == 1 else f"{word}s"


def _progress_bar(pct: float) -> str:
    filled = max(0, min(PROGRESS_CELLS, int(pct // (100 / PROGRESS_CELLS))))
    return "▓" * filled + "░" * (PROGRESS_CELLS - filled)


def _progress_text(pct: float | None, elapsed: float, seen_any: bool, base: str) -> str:
    """What the chat should be showing right now.

    `pct` is None either before the job has started or after it has finished, so
    the two cases are told apart by whether a percentage was ever seen: falling
    back to the opening line after showing 60% would look like the whole thing
    had restarted."""
    if pct is None:
        return FETCH_FINALISING if seen_any else f"{base} ({int(elapsed)}s)"
    return f"{FETCH_WORKING}\n\n{_progress_bar(pct)}  {int(pct)}%"


async def _keep_posted(status, task, progress=None, base="") -> None:
    """Keep `status` moving until `task` finishes.

    `progress` is an optional blocking callable returning a percentage; without
    one the message still ticks on `base`, so a slow job never looks like a stuck
    one. `base` is the caller's own opening line, so a flow that isn't fetching a
    TikTok video doesn't get relabelled as one.

    `base` may be a callable, read fresh on every tick. A profile download runs
    for minutes and sends nothing back, so its line counts what it has saved so
    far rather than repeating the same sentence until it finishes.
    """
    base = base or FETCH_START
    last = ""
    seen_any = False
    started = time.monotonic()

    while True:
        try:
            # Wakes on whichever comes first: the work finishing, or the next
            # tick. shield() keeps the task alive when the timeout fires.
            await asyncio.wait_for(asyncio.shield(task), timeout=PROGRESS_POLL_SECS)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            return  # the caller awaits the task properly and handles the error

        pct = None
        if progress is not None:
            try:
                pct = await asyncio.to_thread(progress)
            except Exception:
                pct = None
        if pct is not None:
            seen_any = True

        line = base() if callable(base) else base
        text = _progress_text(pct, time.monotonic() - started, seen_any, line)
        if text == last:
            continue
        last = text
        try:
            await status.edit_text(text)
        except Exception:
            # Rate limited, or edited to identical text by a race. Not worth
            # failing a download over; the next tick tries again.
            pass


def _job_progress(user_id: int, video_id: str) -> float | None:
    """Live percentage for a post still being worked on, else None."""
    with session_scope() as db:
        job = repo.Queue.active_for_video(db, user_id, video_id)
        return float(job.progress) if job else None


def _combined_progress(phase: dict, user_id: int, video_id: str, share: float) -> float | None:
    """One percentage spanning the whole job.

    Fetching the file is measurable and owns `share` of the bar. What happens
    after reports nothing we can read, so the remainder creeps on a timer. The
    point is that the bar never stalls or restarts partway, not that the last
    stretch is precise.
    """
    if phase["stage"] == "handover":
        crept = (time.monotonic() - phase["since"]) * HANDOVER_CREEP
        return min(99.0, share + crept)

    raw = _job_progress(user_id, video_id)
    if raw is None:
        # Between the fetch finishing and the handover starting: hold the bar
        # where it is rather than dropping out of it for a tick.
        return share if phase["seen"] else None
    phase["seen"] = True
    return raw * share / 100.0


def _guest_reply(chat_id: int) -> str:
    """Always ends with the chat's own id. That's the whole onboarding path now
    that an empty list approves nobody: message the bot, read the id back, paste
    it into Settings. It also lets a guest ask to be approved."""
    base = GUEST_HELP if _guests_may_download() else GUEST_DENIED
    return f"{base}\n\nYour chat ID: {chat_id}"


# Chats whose menu this process has already revoked, so an unapproved chat costs
# one API call rather than one per message.
_menu_cleared: set[int] = set()


async def _hide_menu(chat_id: int) -> None:
    """Make sure an unapproved chat has no command menu.

    The startup sweep can only revoke chats it still has a record of. Anything it
    misses - approvals changed while the bot was down, or the records themselves
    wiped - is caught here, the first time that chat speaks to the bot.
    """
    if chat_id in _allowed or chat_id in _menu_cleared or _app is None:
        return
    _menu_cleared.add(chat_id)

    from telegram import BotCommandScopeChat

    try:
        await _app.bot.set_my_commands([], scope=BotCommandScopeChat(chat_id))
        log.info("Cleared the command menu for unapproved chat %s", chat_id)
    except Exception as exc:
        log.debug("Could not clear the command menu for %s: %s", chat_id, exc)


def _display_name(user) -> str:
    """What to call this person. Their @username when they have one, otherwise
    the name they set - Telegram usernames are optional, so a display name is
    often all there is."""
    if user is None:
        return ""
    if getattr(user, "username", None):
        return user.username
    parts = [p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p]
    return " ".join(parts).strip()


# ── post caption ──
# Telegram renders this with HTML parse mode: <b> for the values that matter and
# <blockquote> for the caption, so the post's own words stay visually separate
# from our summary of it.
#
def _compact(n: int | None) -> str:
    """1.2K / 3.4M, the way the numbers appear under a post."""
    if n is None:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "K"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def _duration_text(seconds) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return f"{total // 60}:{total % 60:02d}"


def _escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _creator_button(creator: dict | None):
    """A single button under the post that opens the creator's TikTok profile.

    Named after the person rather than "TikTok Profile": the caption already
    says what the post is, so the button only has to say whose it is."""
    if not creator or not creator.get("username"):
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    label = (creator.get("nickname") or creator["username"]).strip()
    link = platforms.profile_url(creator.get("platform") or platforms.TIKTOK,
                                 creator["username"])
    if not link:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=link)]])


LINE_BREAK = chr(10)


# Telegram caps a media caption at 1024 characters and rejects the send when it
# is longer. TikTok titles top out around 130 so this never bit; Instagram
# descriptions run to 2200, which would have failed every delivery carrying a
# long one.
CAPTION_LIMIT = 1024
DESCRIPTION_LIMIT = 600


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _post_details(media_path, post_type, meta: dict) -> str:
    """The last line: what this is, how long, how big.

    Reads like the TikTok one ("video 0:15") with the extra facts Instagram
    actually supplies appended, rather than a differently shaped block.
    """
    kind = {
        "story": "Story",
        "highlight": "Highlight",
    }.get(str(meta.get("type") or "").lower())
    if kind and meta.get("highlight_title"):
        # The line above already named the highlight, so this one says what the
        # media is instead of repeating the word.
        kind = ""
    if not kind:
        kind = "photo" if post_type == PostType.image else "video"

    bits = [kind]
    if post_type != PostType.image:
        duration = _duration_text(mediainfo.duration_seconds(media_path))
        if duration:
            bits.append(duration)
    width, height = meta.get("width"), meta.get("height")
    if width and height:
        bits.append(f"{width}x{height}")
    return "  ".join(bits)


def _post_facts(meta: dict) -> str:
    """Likes, date, and where in a carousel this sits. Empty when unknown."""
    facts: list[str] = []
    likes = meta.get("likes")
    if isinstance(likes, int) and likes > 0:
        facts.append(f"{likes:,} {_plural(likes, 'like')}")
    num, count = meta.get("num"), meta.get("count")
    if isinstance(num, int) and isinstance(count, int) and count > 1:
        facts.append(f"{num} of {count}")
    date = str(meta.get("date") or "").strip()
    if date:
        # gallery-dl writes "2026-09-01 12:00:00"; only the day is worth saying.
        facts.append(date.split(" ")[0])
    return "  ·  ".join(facts)


def build_post_caption(media_path, post_type, handle: str = "",
                       nickname: str = "", title: str = "",
                       platform: str = platforms.TIKTOK, meta: dict | None = None) -> str:
    """The message that travels with a delivered post.

    Who made it, what they said, and what it is. The name links to their
    profile, so the sentence carries the navigation.

    `meta` is whatever the platform supplied about the post. TikTok passes none
    and its caption is unchanged; Instagram passes gallery-dl's sidecar, which
    adds the handle, the story or highlight label, the dimensions, the likes and
    the date. Every part is dropped when it is missing rather than shown empty.

    No emoji. The TikTok engagement figures were dropped because reading them
    cost a headless-browser page load per delivery; Instagram's arrive in the
    metadata that comes with the download, so they cost nothing.
    """
    meta = meta or {}
    lines: list[str] = []

    name = (nickname or handle or "").strip()
    bare = (handle or "").strip().lstrip("@")
    if name:
        label = f"<b>{_escape(name)}</b>"
        link = platforms.profile_url(platform, handle)
        if link:
            label = f'<a href="{link}">{label}</a>'
        lines.append(label)
        # Instagram only, and only when the display name is a real name:
        # repeating "@nasa" under "nasa" is a wasted line. TikTok's caption is
        # the format everything else matches, so it is left exactly as it was -
        # its handle is already in the link on the name.
        if (platform != platforms.TIKTOK and bare
                and name.lower() != bare.lower()):
            lines.append(f"@{_escape(bare)}")

    highlight = str(meta.get("highlight_title") or "").strip()
    if highlight:
        lines.append(f"Highlight: {_escape(_clip(highlight, 80))}")

    caption = _clip(title, DESCRIPTION_LIMIT)
    if caption:
        lines.append(f"<blockquote>{_escape(caption)}</blockquote>")

    lines.append(_post_details(media_path, post_type, meta))

    facts = _post_facts(meta)
    if facts:
        lines.append(facts)

    text = LINE_BREAK.join(lines)
    if len(text) > CAPTION_LIMIT:
        # The description is the only part that can be long, so it is what gets
        # cut rather than losing the creator or the type.
        keep = max(80, DESCRIPTION_LIMIT - (len(text) - CAPTION_LIMIT) - 8)
        return build_post_caption(media_path, post_type, handle, nickname,
                                  _clip(title, keep), platform, meta)[:CAPTION_LIMIT]
    return text




async def _backfill_chat_names(bot) -> None:
    """Fill in the sender for chats recorded before their name was captured.

    The message recorder only names rows from now on, which would have left every
    existing chat reading "Unknown" until it happened to write again - and a chat
    that has gone quiet would have stayed unknown forever. Telegram will simply
    tell us: getChat works for any chat the bot has spoken to, so the names are
    recoverable rather than lost.

    Runs at startup, after the command menu. One call per unnamed chat, and once
    named a chat is never asked about again.
    """
    try:
        with session_scope() as db:
            rows = db.execute(
                select(models.TelegramMessage.chat_id)
                .where(models.TelegramMessage.username.is_(None))
                .distinct()
            ).all()
            unnamed = [r[0] for r in rows if r[0] is not None]
    except Exception as exc:
        log.debug("could not list unnamed chats: %s", exc)
        return

    if not unnamed:
        return

    named = 0
    for chat_id in unnamed:
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as exc:
            # Blocked the bot, deleted account, or never really a private chat.
            log.debug("getChat(%s) failed: %s", chat_id, exc)
            continue

        name = _display_name(chat)
        if not name:
            continue
        try:
            with session_scope() as db:
                db.query(models.TelegramMessage).filter(
                    models.TelegramMessage.chat_id == chat_id,
                    models.TelegramMessage.username.is_(None),
                ).update({models.TelegramMessage.username: name}, synchronize_session=False)
                db.commit()
            named += 1
        except Exception as exc:
            log.debug("could not store name for %s: %s", chat_id, exc)

    if named:
        log.info("Resolved the sender for %d Telegram chat(s)", named)


async def _record_update(update, ctx) -> None:
    """Note every incoming message, and who sent it.

    Rows were previously written from only two places - the TikTok and Instagram
    download paths - and neither passed a username, so the column was always
    NULL and Settings showed "Unknown" for everybody. Anyone who only ever ran
    commands left no record at all and so never appeared in the list.

    Registered in group -1, which runs before the command handlers and fires
    whether or not any of them claims the update, so there is exactly one place
    that decides what "seen" means.
    """
    msg = getattr(update, "message", None)
    chat = getattr(update, "effective_chat", None)
    if msg is None or chat is None:
        return

    text = (msg.text or "").strip()
    command = text.split()[0].lstrip("/").split("@")[0] if text.startswith("/") else None
    try:
        with session_scope() as db:
            db.add(models.TelegramMessage(
                chat_id=chat.id,
                username=_display_name(getattr(update, "effective_user", None)) or None,
                text=text[:2000],
                command=command,
                handled=True,
            ))
            db.commit()
    except Exception as exc:   # bookkeeping must never drop a message
        log.debug("could not record telegram message: %s", exc)


async def _ignore(chat_id: int) -> None:
    """The default for an unapproved chat: say nothing at all.

    The bot used to answer every guest message with a refusal and their chat ID.
    That made it a bot anyone could talk to - it confirmed it was alive, invited
    a reply, and made refusals into a conversation. Silence is both the quieter
    and the safer behaviour, and the owner still learns a chat ID from Settings
    and the Overview, which list every chat that has messaged.

    The one exception is handled by the caller: a guest who sends a TikTok link
    gets an answer, because that is a real request rather than chatter.

    The menu is still revoked here, which costs one API call per new chat.
    """
    await _hide_menu(chat_id)


async def _refuse(update, chat_id: int) -> None:
    """Turn down a request we understood but won't serve. Reserved for a guest
    who sent a TikTok link while guest downloads are off - the only case where an
    unapproved chat is answered at all."""
    await _hide_menu(chat_id)
    await update.message.reply_text(_guest_reply(chat_id))


def _approved_only(handler):
    """Gate a command handler. Applied at registration so a new command can't be
    added without a decision about who may run it."""

    @functools.wraps(handler)
    async def guarded(update, ctx):
        if not _approved(update.effective_chat.id):
            await _ignore(update.effective_chat.id)
            return
        await handler(update, ctx)

    return guarded


# ── command menu ──
# Telegram renders the "/" menu from commands registered per scope. Registering
# only against BotCommandScopeChat for approved chats, plus clearing the default
# scope, is what keeps the admin commands invisible to everyone else.


async def _apply_command_menu(bot) -> None:
    from telegram import (
        BotCommand,
        BotCommandScopeChat,
        BotCommandScopeDefault,
        MenuButtonCommands,
    )

    commands = [BotCommand(name, description) for name, description in MENU_COMMANDS]

    # Profile copy. Applied here rather than set once by hand in BotFather so the
    # text lives in source and survives a token swap.
    try:
        await bot.set_my_description(description=BOT_DESCRIPTION)
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
    except Exception as exc:
        log.warning("Could not set the bot description: %s", exc)

    # Nothing globally: a guest's "/" menu must come up empty.
    try:
        await bot.set_my_commands([], scope=BotCommandScopeDefault())
    except Exception as exc:
        log.warning("Could not clear the default command menu: %s", exc)

    # Telegram won't tell us which chats we've given a menu to, so the set has to
    # be reconstructed. The persisted list alone isn't enough: it lives in
    # settings, and anything that clears those (a reset, a fresh database) strands
    # every menu already out there, leaving the full command list on display to
    # someone who is no longer approved. Every chat that has ever messaged the bot
    # is the durable source, so revocation is driven from that as well.
    with session_scope() as db:
        previous = _parse_ids(repo.Settings.get(db, _MENU_SCOPES_KEY, "") or "")
        seen = {
            row[0]
            for row in db.query(models.TelegramMessage.chat_id).distinct().all()
            if row[0] is not None
        }

    # Revoke first, so a chat that lost its approval loses its menu in the same
    # pass that grants the others.
    for chat_id in (previous | seen) - _allowed:
        try:
            await bot.set_my_commands([], scope=BotCommandScopeChat(chat_id))
        except Exception as exc:
            log.warning("Could not clear the command menu for %s: %s", chat_id, exc)

    applied: set[int] = set()
    for chat_id in _allowed:
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id))
            await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
            applied.add(chat_id)
        except Exception as exc:
            # A chat that has never messaged the bot can't be addressed yet; it
            # picks up its menu on the next restart after it does.
            log.warning("Could not set the command menu for %s: %s", chat_id, exc)

    with session_scope() as db:
        repo.Settings.set(db, _MENU_SCOPES_KEY, ",".join(str(c) for c in sorted(applied)))
        db.commit()

    log.info("Command menu applied to %d approved chat(s).", len(applied))


# ── command handlers ──
async def _cmd_start(update, ctx):
    """The same greeting for everyone who can actually download, approved or not.
    What separates the two tiers is the command menu, not the welcome.

    Someone who can't download at all is told so here instead: inviting them to
    send their first link and then turning it down reads far worse than being
    straight with them."""
    chat_id = update.effective_chat.id
    if not _approved(chat_id):
        # Silent for guests, including /start. Answering it announced the bot to
        # anyone who found it; a link is the only thing worth replying to.
        await _ignore(chat_id)
        return
    # Plain text. The banner belongs on the empty-chat screen above Telegram's
    # START button, which is set through BotFather rather than the Bot API, so
    # attaching it here would only duplicate it inside the conversation.
    await update.message.reply_text(WELCOME)


async def _cmd_help(update, ctx):
    """The command reference, for approved chats only. A guest gets nothing:
    naming commands they cannot run only invites them to try, and answering at
    all confirms the bot is listening."""
    chat_id = update.effective_chat.id
    if not _approved(chat_id):
        await _ignore(chat_id)
        return
    await update.message.reply_text(HELP)


# ── creator card ──
#
# Sending a username is the whole interaction now: the bot answers with who that
# is and two buttons. Commands for the same jobs are gone - a slash command has
# to be remembered and typed correctly, while a button is simply there, already
# carrying the creator it applies to.
#
# The handle rides in the callback payload rather than in server-side state, so
# a button still works after the bot restarts. Telegram caps that payload at 64
# bytes, which a TikTok handle (24 max) fits inside comfortably.
# TikTok handles are 2-24 characters of letters, digits, underscore and dot.
# Matching the real rule rather than "anything without spaces" keeps ordinary
# chatter from being mistaken for a lookup.
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9._]{2,24})$")


def _as_handle(text: str) -> str:
    """The handle in this message, or "" if it is not one."""
    match = _HANDLE_RE.match((text or "").strip())
    if not match:
        return ""
    handle = match.group(1)
    # A word of only dots and underscores is not a handle.
    return handle if any(c.isalnum() for c in handle) else ""


CB_MONITOR = "mon"
CB_UNMONITOR = "unmon"
CB_DOWNLOAD = "dl"
# Instagram profile card. Separate from CB_DOWNLOAD so a TikTok handle and an
# Instagram handle that happen to be spelled the same cannot be confused.
CB_IG_DOWNLOAD = "igdl"
CB_IG_MONITOR = "igmon"
CB_IG_UNMONITOR = "igunmon"
CB_X_MONITOR = "xmon"
CB_X_UNMONITOR = "xunmon"
CB_X_DOWNLOAD = "xdl"
_x_profile_tasks: dict[str, asyncio.Task] = {}


def _creator_buttons(handle: str, monitored: bool, platform: str = platforms.TIKTOK):
    """The card's actions, reflecting the state it is in.

    The monitoring button toggles rather than going dead once a creator is
    followed: that is what carries the old /unfollow, and a button that only
    ever adds would leave no way to undo a mistyped handle from Telegram.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    monitor, unmonitor, download = (
        (CB_IG_MONITOR, CB_IG_UNMONITOR, CB_IG_DOWNLOAD)
        if platform == platforms.INSTAGRAM else
        (CB_X_MONITOR, CB_X_UNMONITOR, CB_X_DOWNLOAD) if platform == platforms.X else
        (CB_MONITOR, CB_UNMONITOR, CB_DOWNLOAD)
    )

    if monitored:
        first = InlineKeyboardButton("Stop Monitoring",
                                     callback_data=f"{unmonitor}:{handle}")
    else:
        first = InlineKeyboardButton("Add to Monitoring",
                                     callback_data=f"{monitor}:{handle}")
    return InlineKeyboardMarkup([[
        first,
        InlineKeyboardButton("Download", callback_data=f"{download}:{handle}"),
    ]])


def _lookup_creator(handle: str) -> dict:
    """Resolve a handle to the facts the card shows. Runs off the event loop."""
    with session_scope() as db:
        existing = repo.Users.by_username(db, handle, platform=platforms.TIKTOK)
        cookies = cookie_service.active_cookies_path(db)
        timeout = int(settings_service.get(db, "request_timeout"))
        max_posts = int(settings_service.get(db, "max_posts_per_scan"))
        monitored = bool(existing and existing.monitored)
        try:
            result = user_service.resolve_account(handle, cookies, max_posts, timeout)
            meta = result.metadata
        except Exception as exc:
            log.info("creator lookup failed for @%s: %s", handle, exc)
            meta = None

        if meta is None or not (meta.username or meta.account_id):
            # An account we already follow still deserves an answer when TikTok
            # is being difficult - the card falls back to what is on file.
            if existing:
                return {
                    "username": existing.username, "nickname": existing.nickname or existing.username,
                    "uid": existing.tiktok_uid or "unknown", "avatar": existing.avatar_url,
                    "monitored": monitored, "stale": True,
                }
            return {"error": (
                f"I couldn't find @{handle} on TikTok. Check the spelling, or the "
                "account may be private or gone."
            )}
        return {
            "username": meta.username or handle,
            "nickname": meta.nickname or meta.username or handle,
            "uid": meta.account_id or (existing.tiktok_uid if existing else "") or "unknown",
            "avatar": meta.avatar_url or (existing.avatar_url if existing else None),
            "followers": meta.followers,
            "posts": meta.total_posts,
            "monitored": monitored,
        }


def _creator_caption(info: dict, platform: str = platforms.TIKTOK) -> str:
    name = _escape(info["nickname"])
    handle = _escape(info["username"])
    link = _escape(platforms.profile_url(platform, info["username"]))
    lines = [
        f'<b><a href="{link}">{name}</a></b>',
        f"@{handle}",
        f"UID {_escape(str(info.get('uid') or 'unknown'))}",
    ]
    if info.get("stale"):
        lines.append("")
        lines.append(f"{platforms.display_name(platform)} didn't answer just now, so this is what I have on file.")
    return LINE_BREAK.join(lines)


async def _send_creator_card(message, handle: str, platform: str = platforms.TIKTOK) -> None:
    """Answer a bare username with the creator and the two actions."""
    status = await message.reply_text(f"Looking up @{handle}...")
    try:
        lookup = _x_profile if platform == platforms.X else _ig_profile if platform == platforms.INSTAGRAM else _lookup_creator
        info = await asyncio.to_thread(lookup, handle)
    except Exception as exc:
        # The lookup touches TikTok, the database and the cookie store, so it has
        # several ways to fail. Whatever went wrong, the person is left staring
        # at "Looking up..." unless this says otherwise.
        log.error("creator lookup crashed for @%s: %s", handle, exc, exc_info=True)
        await status.edit_text(
            f"Something went wrong looking up @{handle}. Try again in a moment."
        )
        return
    if info.get("error"):
        await status.edit_text(info["error"])
        return

    caption = _creator_caption(info, platform)
    keyboard = _creator_buttons(info["username"], info["monitored"], platform)
    avatar = info.get("avatar")
    if avatar:
        try:
            # The photo replaces the placeholder rather than adding to it, so the
            # chat is left with one message per lookup.
            await message.reply_photo(photo=avatar, caption=caption,
                                      parse_mode="HTML", reply_markup=keyboard)
            await status.delete()
            return
        except Exception as exc:
            log.debug("avatar send failed for @%s: %s", handle, exc)
    await status.edit_text(caption, parse_mode="HTML", reply_markup=keyboard,
                           disable_web_page_preview=True)


async def _on_button(update, ctx):
    """Act on a creator-card button.

    Buttons are answered for approved chats only, exactly like the commands they
    replace - a card cannot reach a guest, but a stale one in an old chat must
    not become a way around that.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = (query.data or "")
    action, _, handle = data.partition(":")
    handle = handle.strip()

    if not _approved(chat_id):
        # Answer the query so the client stops spinning, but say nothing that
        # confirms what the button would have done.
        await query.answer()
        return
    if not handle:
        await query.answer("I lost track of which creator that was.")
        return

    platform = (platforms.X if action in (CB_X_MONITOR, CB_X_UNMONITOR, CB_X_DOWNLOAD) else
                platforms.INSTAGRAM if action in (CB_IG_MONITOR, CB_IG_UNMONITOR, CB_IG_DOWNLOAD) else platforms.TIKTOK)
    if action in (CB_X_MONITOR, CB_X_UNMONITOR):
        monitored = action == CB_X_MONITOR
        await query.answer("Adding to monitoring..." if monitored else "Stopping monitoring...")
        result = await asyncio.to_thread(_button_ig_monitor, handle, monitored, platforms.X)
    elif action == CB_X_DOWNLOAD:
        log.info("X download button received for @%s", handle)
        key = handle.lower()
        pending = _x_profile_tasks.get(key)
        active = pending and not pending.done() and pending.get_loop() is asyncio.get_running_loop()
        try:
            await query.answer("Already downloading this creator." if active else "Starting the download...")
        except Exception as exc:
            # Expired callback acknowledgements must not discard the request.
            log.debug("X download button acknowledgement failed: %s", exc)
        if active:
            return
        task = asyncio.create_task(_run_ig_profile(chat_id, handle, query.message, platforms.X))
        _x_profile_tasks[key] = task
        def finished(done):
            if _x_profile_tasks.get(key) is done:
                _x_profile_tasks.pop(key, None)
        task.add_done_callback(finished)
        return
    elif action in (CB_IG_MONITOR, CB_IG_UNMONITOR):
        monitored = action == CB_IG_MONITOR
        await query.answer("Adding to monitoring..." if monitored else "Stopping monitoring...")
        result = await asyncio.to_thread(_button_ig_monitor, handle, monitored)
    elif action == CB_MONITOR:
        await query.answer("Adding to monitoring...")
        result = await asyncio.to_thread(_button_monitor, handle)
    elif action == CB_UNMONITOR:
        await query.answer("Stopping monitoring...")
        result = await asyncio.to_thread(_button_unmonitor, handle)
    elif action == CB_IG_DOWNLOAD:
        await query.answer("Starting the download...")
        await _run_ig_button(query, chat_id, handle)
        return
    elif action == CB_DOWNLOAD:
        await query.answer("Starting the download...")
        result = await asyncio.to_thread(_button_download, chat_id, handle)
    else:
        await query.answer()
        return

    # The card keeps its picture; only the buttons change, so the message does
    # not jump as the state updates.
    try:
        await query.edit_message_reply_markup(
            reply_markup=_creator_buttons(handle, result.get("monitored", False), platform)
        )
    except Exception:
        pass
    await ctx.bot.send_message(chat_id=chat_id, text=result["text"])

    if result.get("job_ids"):
        asyncio.create_task(_watch_bulk(chat_id, handle, result["job_ids"]))


def _button_monitor(handle: str) -> dict:
    """Add to monitoring. Same work the /user command did, minus the typing."""
    try:
        with session_scope() as db:
            user = user_service.add(db, handle, source="telegram", monitored=True)
            return {
                "monitored": True,
                "text": (f"Now following {user.nickname} (@{user.username}). "
                         "I'll save new posts as they go up."),
            }
    except Exception as exc:
        log.error("monitor button failed for @%s: %s", handle, exc)

    # The add can raise after the creator was written - a duplicate key the
    # retry resolved, or a follow-up lookup that failed. Check before reporting
    # a failure that did not happen.
    try:
        with session_scope() as db:
            saved = repo.Users.by_username(db, handle, platform=platforms.TIKTOK)
            if saved is not None and saved.monitored:
                return {
                    "monitored": True,
                    "text": (f"Now following {saved.nickname} (@{saved.username}). "
                             "I'll save new posts as they go up."),
                }
    except Exception:
        pass
    return {"monitored": False,
            "text": "I couldn't add that creator. Give it another go in a moment."}


def _button_unmonitor(handle: str) -> dict:
    """Stop monitoring. Carries what /unfollow used to do.

    Only monitoring stops; saved files stay. That is said out loud because
    "stop" on a button is ambiguous enough to worry about.
    """
    try:
        with session_scope() as db:
            user = repo.Users.by_username(db, handle, platform=platforms.TIKTOK)
            if not user:
                return {"monitored": False, "text": f"I'm not following @{handle}."}
            name = user.username
            user_service.delete(db, user.id)
            return {"monitored": False,
                    "text": (f"Stopped following @{name}. "
                             "Files already saved are untouched.")}
    except Exception as exc:
        log.error("unmonitor button failed for @%s: %s", handle, exc)
        return {"monitored": True,
                "text": "I couldn't stop that one. Give it another go in a moment."}


def _button_download(chat_id: int, handle: str) -> dict:
    """Download the creator's back catalogue - the old `/download @x all`."""
    with session_scope() as db:
        user = repo.Users.by_username(db, handle, platform=platforms.TIKTOK)
        if not user:
            try:
                user = user_service.add(db, handle, source="telegram",
                                        baseline=False, monitored=False)
            except Exception as exc:
                log.error("download button add failed for @%s: %s", handle, exc)
                user = None
        if not user:
            return {"monitored": False,
                    "text": "I couldn't find that creator on TikTok."}

        monitored = bool(user.monitored)
        result = monitor_service.download_all_posts(db, user)
        queued, total = result.get("queued", 0), result.get("total", 0)
        events_service.emit(
            db, ActivityType.telegram_received,
            f"Telegram download button for @{user.username}: queued {queued}/{total}",
            user_id=user.id, log_source=LogSource.telegram,
        )
        if total == 0:
            return {"monitored": monitored,
                    "text": f"@{user.username} doesn't seem to have any posts I can reach."}
        if queued == 0:
            return {"monitored": monitored,
                    "text": f"Nothing new for @{user.username}. I already have all {total}."}
        return {
            "monitored": monitored,
            "job_ids": _bulk_job_ids(db, user.id),
            "text": (f"Started on @{user.username}: {queued} of {total} posts to go. "
                     "I'll let you know when they're all saved."),
        }


def _bulk_job_ids(db, user_id):
    return list(
        db.scalars(
            select(models.QueueJob.id).where(
                models.QueueJob.user_id == user_id,
                models.QueueJob.status.in_([JobStatus.pending, JobStatus.processing, JobStatus.paused]),
            )
        ).all()
    )


def _batch_status(job_ids):
    """Tally a queued batch: (completed, failed, still_active)."""
    with session_scope() as db:
        completed = failed = active = 0
        for jid in job_ids:
            j = db.get(models.QueueJob, jid)
            if j is None or j.status == JobStatus.completed:
                completed += 1
            elif j.status in (JobStatus.failed, JobStatus.cancelled):
                failed += 1
            else:
                active += 1
        return completed, failed, active


async def _watch_bulk(chat_id, username, job_ids):
    # Poll until the batch drains, then report. Capped at ~2h so a paused or
    # stuck queue can never leave this coroutine running forever.
    for _ in range(240):  # 240 * 30s
        await asyncio.sleep(30)
        try:
            done, failed, active = await asyncio.to_thread(_batch_status, job_ids)
        except Exception as exc:
            log.error("/download watch error: %s", exc)
            return
        if active == 0:
            msg = f"All done with @{username}. Saved {done} {_plural(done, 'post')}"
            msg += f", {failed} didn't work." if failed else "."
            try:
                await _app.bot.send_message(chat_id, msg)
            except Exception as exc:
                log.error("/download completion send failed: %s", exc)
            return


async def _cmd_storage(update, ctx):
    """Where saved media actually lives.

    /stats reports the size of the library, which stopped meaning "space used on
    the machine" once downloads began moving to MEGA. This says which of the two
    is holding things, and how much room is left.
    """
    def work() -> str:
        with session_scope() as db:
            from . import mega_service

            state = mega_service.state(db)
            if not state["installed"]:
                return "Saving to this machine. MEGA storage is not set up."
            if not state["connected"]:
                return "Saving to this machine. No MEGA account is connected."
            used, total = state["storage_used"], state["storage_total"]
            where = ("Saving to MEGA." if state["enabled"]
                     else "Saving to this machine. MEGA is connected but turned off.")
            lines = [where, "", f"Account: {state['account']}", f"Folder: {state['folder']}"]
            if total:
                pct = used / total * 100
                lines.append(f"Used: {_human_bytes(used)} of {_human_bytes(total)} ({pct:.0f}%)")
                lines.append(f"Free: {_human_bytes(total - used)}")
            return "\n".join(lines)

    await update.message.reply_text(await asyncio.to_thread(work))


def _human_bytes(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if step < 1024 or unit == "TB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{n} B"


async def _cmd_stats(update, ctx):
    def work():
        with session_scope() as db:
            o = stats_service.overview(db)
            return (
                "Here's where things stand.\n\n"
                f"Following: {o.monitored_users} {_plural(o.monitored_users, 'creator')}\n"
                f"Saved: {o.total_downloads} {_plural(o.total_downloads, 'post')}"
                f" ({o.downloads_today} today)\n"
                f"Waiting: {o.queue_size}\n"
                f"Storage used: {o.storage_used_human}\n"
                f"Success rate: {o.success_rate}%"
            )
    await update.message.reply_text(await asyncio.to_thread(work))


async def _cmd_users(update, ctx):
    def work():
        with session_scope() as db:
            n = repo.Users.count(db)
            if n == 0:
                return "Not following anyone yet. Add someone with /user @username"
            return f"Following {n} {_plural(n, 'creator')}."
    await update.message.reply_text(await asyncio.to_thread(work))


async def _cmd_downloads(update, ctx):
    def work():
        with session_scope() as db:
            o = stats_service.overview(db)
            return (
                f"{o.total_downloads} {_plural(o.total_downloads, 'post')} saved, "
                f"{o.downloads_today} of them today.\n"
                f"Taking up {o.storage_used_human}."
            )
    await update.message.reply_text(await asyncio.to_thread(work))


async def _cmd_queue(update, ctx):
    def work():
        with session_scope() as db:
            c = repo.Queue.counts(db)
            busy = c.get("processing", 0)
            waiting = c.get("pending", 0)
            if not busy and not waiting:
                return "Nothing in the queue. All caught up."
            return (
                f"Downloading now: {busy}\n"
                f"Waiting: {waiting}\n"
                f"Finished: {c.get('completed', 0)}\n"
                f"Didn't work: {c.get('failed', 0)}"
            )
    await update.message.reply_text(await asyncio.to_thread(work))


async def _cmd_health(update, ctx):
    def work():
        with session_scope() as db:
            h = stats_service.health(db)
            hours, rem = divmod(int(h.uptime_seconds), 3600)
            up = f"{hours}h {rem // 60}m" if hours else f"{rem // 60}m"
            headline = "Everything looks good." if h.status == "healthy" else f"Status: {h.status}"
            return (
                f"{headline}\n\n"
                f"Database: {h.database}\n"
                f"Queue: {h.queue}\n"
                f"Downloaders: {h.workers_online} of {h.workers_total} ready\n"
                f"Running for: {up}"
            )
    await update.message.reply_text(await asyncio.to_thread(work))


async def _cmd_pause(update, ctx):
    def work():
        with session_scope() as db:
            settings_service.update(db, {"monitoring_paused": True})
            events_service.emit(db, ActivityType.telegram_received, "Monitoring paused via Telegram", log_source=LogSource.telegram)
    await asyncio.to_thread(work)
    await update.message.reply_text(
        "Paused. I'll stop checking for new posts until you send /resume."
    )


async def _cmd_resume(update, ctx):
    def work():
        with session_scope() as db:
            settings_service.update(db, {"monitoring_paused": False})
            events_service.emit(db, ActivityType.telegram_received, "Monitoring resumed via Telegram", log_source=LogSource.telegram)
    await asyncio.to_thread(work)
    await update.message.reply_text("Back on. I'll watch for new posts again.")


# ── Instagram profile card ──
#
# Both platforms use the same card renderer and platform-specific lookups.
def _ig_user(db, handle: str):
    """The Instagram creator row for this handle, or None.

    Scoped to the platform on purpose. repo.Users.by_username matches on the
    handle alone, so an Instagram account spelled the same as one of the
    monitored TikTok creators would have returned the TikTok row and put that
    creator's name on an Instagram post.
    """
    handle = (handle or "").lstrip("@").strip("/")
    if not handle:
        return None
    return db.scalar(
        select(models.User).where(
            models.User.platform == platforms.INSTAGRAM,
            func.lower(models.User.username) == handle.lower(),
        )
    )


def _x_profile(handle: str) -> dict:
    with session_scope() as db:
        user = x_service.find_creator(db, handle)
        profile, reason = x_service.probe_creator(db, handle)
        if reason == "not-found" and not user:
            return {"error": f"I couldn't find @{handle} on X. Check the username."}
        profile = profile or {}
        return {"username": profile.get("username") or handle,
                "nickname": profile.get("fullname") or (user.nickname if user else handle),
                "uid": profile.get("owner_id") or (user.platform_uid if user else None) or "unknown",
                "avatar": profile.get("avatar") or (user.avatar_url if user else None),
                "monitored": bool(user and user.monitored), "stale": bool(user and not profile)}


def announce_x_download(db, record, entry):
    if not (is_running() and _app is not None and _loop is not None):
        return
    chats = live_allowed_ids(db)
    if not chats:
        return
    user = repo.Users.by_username(db, entry["username"], platform=platforms.X)
    message = build_new_post_message(entry["username"], user.nickname if user else "", entry.get("title") or "", platforms.X)
    post_url = x_urls.status_url(entry["video_id"].split("_", 1)[0])
    with_media = bool(settings_service.get(db, "telegram_send_new_posts"))
    def prepare():
        with session_scope() as work:
            items, _ = _ig_items(work, [entry], 1, platforms.X)
            return items
    async def deliver():
        try:
            items = await asyncio.to_thread(prepare) if with_media else []
            if items:
                for item in items:
                    await _send_file(chats, Path(item["path"]), item["post_type"], item["caption"],
                                     reply_markup=_new_post_keyboard(entry["username"], post_url))
            else:
                await _announce_new_post(chats, entry["username"], message, post_url, None, record.post_type, False)
        except Exception as exc:
            log.error("X announcement failed: %s", exc)
    asyncio.run_coroutine_threadsafe(deliver(), _loop)


def _ig_profile(handle: str) -> dict:
    handle = (handle or "").lstrip("@").strip("/")
    with session_scope() as db:
        user = _ig_user(db, handle)
        profile, reason = instagram_service.probe_creator(db, handle)
        if not profile and not user and reason in ("not-found", "no-profile"):
            return {"error": f"I couldn't find @{handle} on Instagram. Check the spelling, or the account may be private or gone."}
        if not profile and reason.startswith("mismatch:"):
            return {"error": f"Instagram resolved @{handle} to @{reason.split(':', 1)[1]}. Send that profile instead."}
        profile = profile or {}
        return {
            "username": profile.get("username") or handle,
            "nickname": profile.get("fullname") or (user.nickname if user else None) or handle,
            "uid": profile.get("owner_id") or (user.platform_uid if user else None) or "unknown",
            "avatar": profile.get("avatar") or (user.avatar_url if user else None),
            "monitored": bool(user and user.monitored),
            "stale": bool(user and not profile),
        }


def _ig_caption(info: dict) -> str:
    return _creator_caption(info, platforms.INSTAGRAM)


def _ig_buttons(handle: str, monitored: bool = False):
    return _creator_buttons(handle, monitored, platforms.INSTAGRAM)


async def _send_ig_card(message, handle: str) -> None:
    await _send_creator_card(message, handle, platforms.INSTAGRAM)


def _button_ig_monitor(handle: str, monitored: bool, platform: str = platforms.INSTAGRAM) -> dict:
    try:
        with session_scope() as db:
            service = x_service if platform == platforms.X else instagram_service
            user = service.set_monitoring(db, handle, monitored)
            events_service.emit(
                db, ActivityType.user_added if monitored else ActivityType.user_removed,
                f"{platforms.display_name(platform)} monitoring {'started' if monitored else 'stopped'} for @{user.username}",
                user_id=user.id, log_source=LogSource.telegram,
            )
            text = (f"Now following {user.nickname} (@{user.username}). I'll save new posts as they go up."
                    if monitored else f"Stopped following @{user.username}. Files already saved are untouched.")
            return {"monitored": monitored, "text": text}
    except Exception as exc:
        log.error("Instagram monitoring button failed for @%s: %s", handle, exc)
        with session_scope() as db:
            saved = x_service.find_creator(db, handle) if platform == platforms.X else _ig_user(db, handle)
            return {"monitored": bool(saved and saved.monitored),
                    "text": "I couldn't change monitoring for that creator. Give it another go in a moment."}

async def _run_ig_button(query, chat_id: int, handle: str) -> None:
    """The card's Download button: stories, highlights and posts for one account.

    Started as its own task so the button returns at once, the way the TikTok
    bulk download does. The card is left alone rather than edited into a
    progress bar, because it is what the person presses again later.
    """
    asyncio.create_task(_run_ig_profile(chat_id, handle, query.message))

def _ig_items(db, files: list[dict], limit: int, platform: str = platforms.INSTAGRAM) -> tuple[list[dict], int]:
    """Turn recorded downloads into things ready to send.

    The caption and the profile button are built here, from the same helpers the
    TikTok path uses, so a delivered Instagram post reads identically to a
    delivered TikTok one.
    """
    items: list[dict] = []
    skipped = 0
    for f in files[:limit]:
        # `materialize` is what makes this work with MEGA storage on: the local
        # copy is deleted after upload, so the recorded path usually points at
        # nothing. The TikTok path has always gone through it; Instagram sent
        # the raw path and silently delivered nothing but "nothing to send".
        media = None
        if f.get("download_id"):
            d = db.get(models.Download, f["download_id"])
            if d is not None:
                media = storage_service.materialize(db, d)
        if media is None:
            media = Path(f["path"])
        if not media.exists():
            log.warning("instagram: no media on disk or in MEGA for %s", f.get("video_id"))
            continue
        if oversize(media):
            # Same call the TikTok path makes: the media is in the library
            # either way, it just cannot come back through Telegram.
            skipped += 1
            storage_service.release_temporary_media(media)
            continue
        username = f.get("username") or ""
        user = repo.Users.by_username(db, username, platform=platform)
        nickname = (user.nickname if user else "") or username
        items.append({
            "path": str(media),
            "post_type": f["post_type"],
            "caption": build_post_caption(
                media, f["post_type"], handle=username, nickname=nickname,
                title=f.get("title") or "", platform=platform,
                meta=f.get("meta") or {},
            ),
            "creator": {
                "username": username,
                "nickname": nickname,
                "platform": platform,
            },
        })
    return items, skipped


def _ig_work(chat_id: int, target: str, *, profile: bool = False, on_ready=None, platform: str = platforms.INSTAGRAM, on_progress=None) -> dict:
    """Download an Instagram target and prepare what to send back.

    `profile` runs stories, highlights and posts for one account; otherwise
    `target` is a single post, story or highlight URL.

    Only a link the person actually sent comes back through Telegram. A profile
    download is a library job, exactly as a TikTok bulk download is.
    """
    with session_scope() as db:
        service = x_service if platform == platforms.X else instagram_service
        platform_urls = x_urls if platform == platforms.X else ig_urls
        site = platforms.display_name(platform)
        # Not recorded here: _record_update already logged this message, with the
        # sender's name attached. Adding a second row double-counted it.
        upload = bool(settings_service.get(db, "telegram_upload_link_downloads")) and not profile
        streaming = on_ready is not None and upload
        state = {"sent": 0, "skipped": 0}

        def on_item(entry: dict) -> None:
            if state["sent"] >= IG_SEND_CAP:
                return
            ready, skipped = _ig_items(db, [entry], 1, platform)
            state["skipped"] += skipped
            if ready:
                state["sent"] += 1
                on_ready(ready[0])

        if profile:
            handle = platform_urls.extract_username(target) or target
            options = {"on_progress": on_progress} if platform == platforms.X else {}
            result = service.download_profile(db, handle, source="telegram", **options)
        else:
            result = service.download_url(
                db, target, source="telegram", on_item=on_item if streaming else None)

        if not result.get("ok"):
            return {"ok": False,
                    "text": f"That {site} link didn't work out: {result.get('error')}"}

        count = result["count"]
        events_service.emit(
            db, ActivityType.telegram_received,
            f"Telegram {site} download: {count} item(s)",
            log_source=LogSource.telegram,
        )
        creators = ", ".join(f"@{c}" for c in result["creators"]) or site

        if streaming:
            items, skipped = [], state["skipped"]
        else:
            items, skipped = _ig_items(db, result.get("files") or [], IG_SEND_CAP, platform) if upload else ([], 0)

        # The per-part breakdown, because "stories, highlights and posts" is what
        # the button promised and a bare total would not show that stories were
        # empty. The names are already plural, so a count of one needs the
        # singular back: "1 stories" reads as unfinished.
        singular = {"stories": "story", "highlights": "highlight", "posts": "post"}
        parts = result.get("parts") or {}
        detail = ", ".join(
            f"{v} {singular.get(k, k) if v == 1 else k}" for k, v in parts.items() if v
        )

        summary = f"Saved {count} {_plural(count, 'item')} from {creators}"
        summary += f" ({detail})." if detail else "."
        if not profile:
            if skipped:
                summary += (f" {skipped} too large for Telegram's "
                            f"{upload_limit_text(db)} limit, saved to the library.")
            shown = state["sent"] if streaming else len(items)
            if upload and len(result.get("files") or []) > shown and shown >= IG_SEND_CAP:
                summary += f" Sent the first {IG_SEND_CAP}; the rest are in the library."

        if result.get("errors") or result.get("warning"):
            summary += " Some media could not be saved. Try Download again to complete it."
        return {"ok": True, "text": summary, "items": items, "count": count,
                "detail": detail, "creators": result["creators"],
                "errors": result.get("errors") or ({"media": result["warning"]} if result.get("warning") else {})}


async def _run_ig_profile(chat_id: int, handle: str, message, platform: str = platforms.INSTAGRAM) -> None:
    """Download a whole Instagram profile, the way a TikTok bulk download runs.

    X updates the opening message with its saved-item count. Profile media stays
    in storage instead of flooding the chat with hundreds of attachments.
    """
    status = None
    try:
        status = await message.reply_text(
            (f"Downloading all available media from @{handle}…"
             if platform == platforms.X else f"Started on @{handle}. I'll let you know when everything's saved.")
        )
    except Exception as exc:
        log.warning("Profile download start message failed: %s", exc)
    try:
        options = {}
        state = {"count": 0}
        if platform == platforms.X:
            loop = asyncio.get_running_loop()
            def progress(data):
                try:
                    loop.call_soon_threadsafe(state.update, data)
                except RuntimeError:
                    pass  # The bot may be stopping; saving media can still finish.
            options["on_progress"] = progress
        work = asyncio.create_task(asyncio.to_thread(
            _ig_work, chat_id, platforms.profile_url(platform, handle), profile=True, platform=platform, **options))
        previous = ""
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(work), timeout=10)
                break
            except asyncio.TimeoutError:
                if work.done():
                    raise  # A worker timeout is a failure, not another timer tick.
                text = f"Downloading @{handle} · {state['count']} saved"
                if platform == platforms.X and status and text != previous:
                    try:
                        await status.edit_text(text)
                        previous = text
                    except Exception:
                        pass
    except Exception as exc:
        log.error("instagram profile download failed for @%s: %s", handle, exc)
        result = {"ok": False,
                  "text": f"Something went wrong saving @{handle}. Try again in a moment."}

    if not result.get("ok"):
        text = result.get("text") or f"Couldn't save @{handle}."
    elif not result.get("count"):
        text = (f"No new items were saved for @{handle}." if result.get("errors")
                else f"Nothing new for @{handle}. I already have everything I can reach.")
    else:
        count = result["count"]
        opening = "Finished" if result.get("errors") else "All done with"
        text = f"{opening} @{handle}. Saved {count} {_plural(count, 'item')}"
        text += f" ({result['detail']})." if result.get("detail") else "."

    if result.get("ok") and result.get("errors"):
        missing = "some X posts" if platform == platforms.X else ", ".join(result["errors"])
        text += f" Couldn't finish {missing}. Try Download again to complete them."

    try:
        await _app.bot.send_message(chat_id, text)
    except Exception as exc:
        log.error("instagram profile completion send failed: %s", exc)


async def _run_ig_download(chat_id: int, target: str, status, platform: str = platforms.INSTAGRAM) -> None:
    """Fetch a single Instagram link and hand the media back as it arrives.

    Mirrors the TikTok link flow: the same opening line, the same ticking bar,
    the same captions and profile button, and each file sent the moment it
    lands rather than after the whole run.
    """
    loop = asyncio.get_running_loop()
    ready: asyncio.Queue = asyncio.Queue()
    done = object()

    def on_ready(item: dict) -> None:
        # Called from the download thread, so the hand-off goes through the
        # loop rather than touching the queue directly.
        loop.call_soon_threadsafe(ready.put_nowait, item)

    async def fetch() -> dict:
        try:
            return await asyncio.to_thread(_ig_work, chat_id, target, on_ready=on_ready, platform=platform)
        finally:
            loop.call_soon_threadsafe(ready.put_nowait, done)

    async def send_as_they_arrive() -> int:
        delivered = 0
        while True:
            item = await ready.get()
            if item is done:
                return delivered
            try:
                await _send_file(
                    {chat_id}, Path(item["path"]), item["post_type"],
                    item["caption"], reply_markup=_creator_button(item["creator"]),
                )
                delivered += 1
            except Exception as exc:
                log.error("instagram upload failed: %s", exc)

    work = asyncio.create_task(fetch())
    sender = asyncio.create_task(send_as_they_arrive())
    try:
        await _keep_posted(status, work, base=FETCH_START)
        result = await work
        # `fetch` pushes the sentinel from its own finally, so the sender always
        # finishes even when the download raised. Awaiting it on both paths is
        # what stops a pending task being destroyed underneath us.
        delivered = await sender

        # Anything not streamed (the upload toggle is off) still goes out here.
        for item in result.get("items") or []:
            try:
                await _send_file(
                    {chat_id}, Path(item["path"]), item["post_type"],
                    item["caption"], reply_markup=_creator_button(item["creator"]),
                )
                delivered += 1
            except Exception as exc:
                log.error("instagram upload failed: %s", exc)

        # One post delivered whole is answered by the post itself, exactly as a
        # TikTok link is: the bar is cleared and the media is left to speak.
        if delivered == 1 and result.get("count", 0) <= 1 and not result.get("errors"):
            try:
                await status.delete()
            except Exception:
                pass
            return
        await status.edit_text(result["text"])
    except Exception as exc:
        log.error("telegram instagram error: %s", exc)
        try:
            await sender
        except Exception:
            pass
        await status.edit_text(f"That {platforms.display_name(platform)} link didn't work out. It may be private or removed.")










async def _cmd_ig(update, ctx):
    chat_id = update.effective_chat.id
    raw = " ".join(ctx.args).strip() if ctx.args else ""
    if not raw:
        await update.message.reply_text(
            "Send me an Instagram link or a username, like this:\n\n"
            "/ig <link to a post or reel>\n"
            "/ig @username"
        )
        return
    # A bare handle → that profile's recent posts.
    target = raw if ig_urls.is_instagram_url(raw) else ig_urls.profile_url(raw)
    if ig_urls.kind(target) == ig_urls.PROFILE:
        handle = ig_urls.extract_username(target) or raw.lstrip("@")
        await _run_ig_profile(chat_id, handle, update.message)
        return
    status = await update.message.reply_text(FETCH_START)
    await _run_ig_download(chat_id, target, status)


async def _on_message(update, ctx):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # Guests reach only the TikTok path below, and only while the setting allows
    # it. Instagram is withheld deliberately: it downloads through the owner's
    # saved session cookies, so lending it out would lend out their account.
    approved = _approved(chat_id)
    if not approved:
        # A TikTok link is the single thing an unapproved chat gets an answer to.
        # Anything else - chatter, a command, a link for another site - is met
        # with silence, so the bot never announces itself to someone who simply
        # found it.
        if not urls.is_tiktok_url(text):
            await _ignore(chat_id)
            return
        if not _guests_may_download():
            await _refuse(update, chat_id)
            return
        await _hide_menu(chat_id)

    if approved and x_urls.is_x_url(text):
        if x_urls.is_profile_url(text):
            await _send_creator_card(update.message, x_urls.extract_username(text), platforms.X)
        else:
            status = await update.message.reply_text(FETCH_START)
            await _run_ig_download(chat_id, text, status, platforms.X)
        return
    if approved and ig_urls.is_instagram_url(text):
        # A profile link is an invitation, not an instruction: it opens the card
        # so the download is a decision, the same way a TikTok handle does.
        # A post, story or highlight is a specific thing to fetch, so it is
        # fetched, on the same opening line and bar as a TikTok link.
        if ig_urls.kind(text) == ig_urls.PROFILE:
            await _send_ig_card(update.message, ig_urls.extract_username(text))
            return
        status = await update.message.reply_text(FETCH_START)
        await _run_ig_download(chat_id, text, status)
        return
    if not urls.is_tiktok_url(text):
        # A bare username is the main way in now: it opens the creator card,
        # which carries the actions that used to need /user and /download.
        handle = _as_handle(text)
        if handle:
            await _send_creator_card(update.message, handle)
            return
        # Only approved chats reach this: a guest with anything other than a
        # TikTok link was already answered above, without naming any command.
        await update.message.reply_text(
            "Send me a TikTok username or an Instagram or X profile link to see who it is and what I can do with "
            "them, or a link to save it.\n\n/help for the full list."
        )
        return

    # Resolving and acknowledging run together: the work does not depend on the
    # reply, and awaiting it first would put a Telegram round-trip in front of
    # every download for nothing.
    resolving = asyncio.create_task(asyncio.to_thread(_resolve_link, chat_id, text))
    status = await update.message.reply_text(FETCH_START)

    try:
        prep = await resolving
    except Exception as exc:
        log.error("telegram link resolve failed: %s", exc)
        await status.edit_text("Something went wrong on my end. Please try that again.")
        return
    if prep.get("error"):
        await status.edit_text(prep["error"])
        return

    # Resolution is what produces the queue job, so progress can only be reported
    # once it has finished.
    downloading = asyncio.create_task(asyncio.to_thread(_download_link, prep, approved))
    phase = {"stage": "fetch", "seen": False, "since": 0.0}

    async def fetch_and_hand_over() -> dict:
        """Both halves under one task, so one progress bar covers the lot."""
        result = await downloading
        if result.get("upload"):
            phase["stage"] = "handover"
            phase["since"] = time.monotonic()
            jid = result.get("job_id")
            # The job is already `completed` in the database by now, so this
            # stage exists only as a live event. The app's Queue page shows it as
            # its own step rather than jumping straight to done while the file is
            # still on its way.
            if jid:
                publish("queue_job", {"id": jid, "status": "sending", "progress": 100})
            try:
                await _send_file(
                    {chat_id}, Path(result["file_path"]), result["post_type"],
                    result["caption"], reply_markup=_creator_button(result.get("creator")),
                )
                result["delivered"] = True
            except Exception as exc:
                log.error("link-download upload failed: %s", exc)
            if jid:
                publish("queue_job", {
                    "id": jid,
                    "status": "completed" if result.get("delivered") else "failed",
                    "progress": 100,
                })
        return result

    whole = asyncio.create_task(fetch_and_hand_over())
    # Nothing to hand back means fetching is the whole job, so it gets the whole bar.
    share = FETCH_SHARE if prep.get("upload") else 100.0
    await _keep_posted(
        status, whole,
        lambda: _combined_progress(phase, prep["user_id"], prep["video_id"], share),
    )

    try:
        result = await whole
        if result.get("delivered"):
            # The video is the answer. Clearing the progress message leaves the
            # chat holding just that, rather than a bar and a line restating what
            # the file already shows.
            try:
                await status.delete()
            except Exception:
                pass
        else:
            await status.edit_text(result["text"])
    except Exception as exc:
        log.error("telegram message error: %s", exc)
        await status.edit_text("Something went wrong on my end. Please try that again.")


def _resolve_link(chat_id: int, text: str) -> dict:
    """Identify the post and its creator.

    Split from the download so the queue job exists before progress reporting
    starts. Fast: one lookup, and the creator is only created from the post's own
    metadata rather than a full profile fetch.

    The message itself is not recorded here - _record_update already did, with
    the sender's name, which this path never had."""
    with session_scope() as db:
        handle, video_id, card = urls.identify(text)
        user, post = user_service.adhoc_for_link(db, text, handle, source="telegram", post=card)
        if not user:
            return {"error": "I couldn't work out which creator that link belongs to."}
        return {
            "url": text,
            "user_id": user.id,
            "username": user.username,
            "nickname": user.nickname,
            "video_id": video_id,
            "title": post.title,
            # Carried so the download can be attributed to whoever asked for it.
            "chat_id": chat_id,
            # Read up front purely to size the bar: whether there's a handover
            # decides how much of it fetching is allowed to fill.
            "upload": bool(settings_service.get(db, "telegram_upload_link_downloads")),
        }


def _download_link(prep: dict, approved: bool) -> dict:
    """Fetch the post resolved by `_resolve_link` and build the closing reply."""
    with session_scope() as db:
        user = repo.Users.get(db, prep["user_id"])
        if not user:
            return {"text": "I couldn't work out which creator that link belongs to."}

        ok = download_service.download_from_url(
            db, prep["url"], prep["video_id"], user, source="telegram",
            title=prep["title"], chat_id=prep.get("chat_id"),
        )
        events_service.emit(
            db, ActivityType.telegram_received,
            f"Telegram download for @{user.username}",
            user_id=user.id, log_source=LogSource.telegram,
        )
        if not ok:
            return {
                "text": "I couldn't save that one. The post may be private, "
                        "removed, or region locked."
            }

        upload = bool(settings_service.get(db, "telegram_upload_link_downloads"))
        d = db.scalar(
            select(models.Download)
            .where(models.Download.user_id == user.id, models.Download.status == DownloadStatus.completed)
            .order_by(desc(models.Download.id))
        )
        # `materialize` covers both storage modes: the local file when there
        # is one, otherwise the copy in MEGA (usually still cached from the
        # upload that just happened, so this is a lookup, not a transfer).
        media = storage_service.materialize(db, d) if upload and d else None
        if upload and media and media.exists() and oversize(media):
            # Saying so beats a send that quietly never arrives: the post is in
            # the library either way, it just can't come back through Telegram.
            # The limit is read rather than fixed, so a self-hosted Bot API
            # server reports its own 2 GB ceiling instead of the cloud's 50 MB.
            storage_service.release_temporary_media(media)
            return {"text": f"The file is too large. Telegram has a {upload_limit_text(db)} file size limit."}

        if upload and media and media.exists():
            # No closing text: the file itself is the answer, and its caption
            # already names the creator. `text` is only the fallback for a send
            # that fails, so the chat isn't left on a half-finished bar.
            job = repo.Queue.latest_for_video(db, user.id, prep["video_id"])
            return {
                "text": "I saved it, but couldn't send the file here.",
                "upload": True,
                "file_path": str(media),
                "post_type": d.post_type,
                "caption": build_post_caption(
                    media, d.post_type,
                    handle=user.username, nickname=user.nickname or user.username,
                    title=d.title or "",
                ),
                # Drives the profile button under the delivered post.
                "creator": {
                    "username": user.username,
                    "nickname": user.nickname or user.username,
                },
                # Lets the app's Queue page show the hand-over as its own stage,
                # which happens after the job itself is already marked complete.
                "job_id": job.id if job else None,
            }

        # Nothing is being sent back, so this message is the only confirmation
        # there is and has to stay.
        text_reply = "Saved!"
        # Offered only to chats that can actually run /user; to a guest it would
        # just be an invitation to hit a refusal.
        if approved:
            text_reply += f"\n\nWant every new post from them? /user @{user.username}"
        return {"text": text_reply}


# ── outbound: push a downloaded file to chats ──
# Telegram's hosted Bot API refuses any bot upload above 50 MB, whatever the file
# is. A self-hosted Bot API server (telegram-bot-api, run with your own api_id /
# api_hash) lifts that to 2000 MB - it is the only way to send a larger post back
# through the bot, and it is why the limit is derived rather than hardcoded.
#
# Point `telegram_api_base_url` at the local server to switch: the bot then talks
# to it instead of api.telegram.org and the cap follows automatically.
CLOUD_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024
LOCAL_UPLOAD_LIMIT_BYTES = 2000 * 1024 * 1024

# Kept as a module constant for the paths that only need the conservative value.
UPLOAD_LIMIT_BYTES = CLOUD_UPLOAD_LIMIT_BYTES


def api_base_url(db=None) -> str:
    """Base URL of the Bot API in use. Empty means Telegram's hosted one."""
    def _read(session) -> str:
        return (settings_service.get(session, "telegram_api_base_url") or "").strip()

    if db is not None:
        return _read(db)
    try:
        with session_scope() as own:
            return _read(own)
    except Exception as exc:
        # Silently returning "" here sent the bot to the hosted API without a
        # word about why, which is indistinguishable from the setting being
        # unset - and cost a long hunt when it mattered.
        log.warning("could not read telegram_api_base_url (%s); using the hosted API", exc)
        return ""


def upload_limit_bytes(db=None) -> int:
    """How large a file this bot may send, given where its API lives."""
    return LOCAL_UPLOAD_LIMIT_BYTES if api_base_url(db) else CLOUD_UPLOAD_LIMIT_BYTES


def upload_limit_text(db=None) -> str:
    """The limit as it should read in a message: "50 MB" / "2 GB"."""
    limit = upload_limit_bytes(db)
    gb = limit / 1024 / 1024 / 1024
    if gb >= 1:
        return f"{gb:.0f} GB" if abs(gb - round(gb)) < 0.05 else f"{gb:.1f} GB"
    return f"{limit // 1024 // 1024} MB"

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def attachments(path: Path) -> list[Path]:
    """The files to actually send for one download.

    A video post is a single file. A photo post is a *folder* of images, and
    `file_path` points at the folder - which `open("rb")` cannot read, so every
    photo post silently failed to send.
    """
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(
            p for p in path.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
        )
    return []


def oversize(path: Path) -> bool:
    """True when nothing here can be handed to Telegram."""
    files = attachments(path)
    limit = upload_limit_bytes()
    return bool(files) and all(f.stat().st_size > limit for f in files)


async def _send_file(chat_ids: set[int], path: Path, post_type: PostType, caption: str, reply_markup=None) -> int:
    try:
        return await _send_file_contents(chat_ids, path, post_type, caption, reply_markup)
    finally:
        storage_service.release_temporary_media(path)


async def _send_file_contents(chat_ids: set[int], path: Path, post_type: PostType, caption: str, reply_markup=None) -> int:
    files = attachments(path)
    if not files:
        log.error("nothing to send at %s", path)
        return 0

    limit = upload_limit_bytes()
    too_big = [f for f in files if f.stat().st_size > limit]
    for f in too_big:
        log.error(
            "%s is %.1f MB; this bot's API caps uploads at %s",
            f.name, f.stat().st_size / 1024 / 1024, upload_limit_text(),
        )
    files = [f for f in files if f not in too_big]
    if not files:
        return 0

    sent = 0
    for chat_id in chat_ids:
        delivered = 0
        for index, item in enumerate(files):
            try:
                with item.open("rb") as fh:
                    # Always as a document, video included. sendVideo hands the
                    # file to Telegram's transcoder, which re-encodes it and
                    # hands back something smaller than what was downloaded; a
                    # document is stored and delivered byte for byte.
                    await _app.bot.send_document(
                        chat_id,
                        document=fh,
                        # The media's own name on disk, never anything built from
                        # the caption or title. Sent without one, Telegram names
                        # the file after the caption, which turns a post's whole
                        # description into the filename.
                        filename=item.name,
                        # Only the first carries the caption and buttons:
                        # repeating them under every image of a carousel is noise.
                        caption=caption if index == 0 else None,
                        # The caption is built with <b>/<blockquote>; without
                        # this Telegram would print the tags verbatim.
                        parse_mode="HTML" if index == 0 and caption else None,
                        # Telegram sniffs the type of an uploaded document and
                        # will still present a video as a playable message unless
                        # this says otherwise. Only meaningful for video.
                        disable_content_type_detection=(post_type == PostType.video),
                        reply_markup=reply_markup if index == 0 else None,
                    )
                delivered += 1
            except Exception as exc:  # one bad file shouldn't abort the rest
                log.error("send to %s failed (%s): %s", chat_id, item.name, exc)
        if delivered:
            sent += 1
    return sent


def upload_download(db, download_id: int) -> int:
    """Send a completed download's file to every authorized chat. Returns the
    number of chats it reached. Raises ValueError on a configuration problem."""
    if not (is_running() and _app is not None and _loop is not None):
        raise ValueError("Telegram bot is not running. Add a bot token in Settings first.")
    chats = live_allowed_ids(db)
    if not chats:
        raise ValueError("No authorized chats configured (set Allowed IDs in Settings).")

    d = db.get(models.Download, download_id)
    if not d or not (d.file_path or d.remote_path):
        raise ValueError("Download has no file on disk.")
    # Fetches it back out of MEGA when that is where it lives.
    path = storage_service.materialize(db, d)
    if not path or not path.exists():
        raise ValueError("File not found on disk.")

    caption = f"@{d.user.username} · {d.title or d.video_id}" if d.user else (d.title or d.video_id)

    # Same live-only stage the bot's own hand-over reports, so sending from the
    # app shows up on the Queue page rather than looking like nothing happened.
    job = repo.Queue.latest_for_video(db, d.user_id, d.video_id) if d.user_id else None
    if job:
        publish("queue_job", {"id": job.id, "status": "sending", "progress": 100})
    try:
        fut = asyncio.run_coroutine_threadsafe(_send_file(chats, path, d.post_type, caption), _loop)
        sent = fut.result(timeout=180)
    except Exception:
        if job:
            publish("queue_job", {"id": job.id, "status": "failed", "progress": 100})
        raise
    if job:
        publish("queue_job", {
            "id": job.id,
            "status": "completed" if sent else "failed",
            "progress": 100,
        })
    return sent


# ── outbound: announce a newly downloaded monitored post ──
def _configure_api(builder):
    """Point the bot at a self-hosted Bot API server when one is configured.

    Telegram's hosted API caps bot uploads at 50 MB and there is no way around
    it. Running telegram-bot-api yourself (with an api_id/api_hash from
    my.telegram.org) raises that to 2000 MB, and `local_mode` lets the server
    read files off disk instead of them being uploaded twice.

    Left unset, nothing changes: the bot talks to api.telegram.org exactly as
    before.
    """
    base = api_base_url()
    # Unconditional, so the log always answers "which API is this bot on?" - the
    # question that took a rebuild each time it was left to inference.
    log.info("Telegram API: %s (uploads up to %s)", base or "api.telegram.org (hosted)", upload_limit_text())
    if not base:
        return builder

    base = base.rstrip("/")
    log.info("Telegram: using local Bot API at %s (uploads up to %s)", base, upload_limit_text())
    try:
        return (
            builder
            .base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
            .local_mode(True)
        )
    except Exception as exc:
        # A bad URL must not stop the bot starting - fall back to the cloud API.
        log.error("local Bot API config rejected (%s); using the hosted API", exc)
        return builder


def build_new_post_message(username: str, nickname: str = "", caption: str = "", platform: str = platforms.TIKTOK) -> str:
    """The announcement for a newly downloaded monitored post.

    Just the creator and what they wrote. The video above it already says a post
    arrived, so a sentence saying so again is words the reader has to step over;
    the name carries the link to their profile and the View button carries the
    link to the post. The caption is dropped entirely when there is none - an
    empty quote block reads as a rendering fault."""
    handle = (username or "").lstrip("@")
    name = (nickname or handle).strip()
    lines = [f'<a href="{_escape(platforms.profile_url(platform, handle))}"><b>{_escape(name)}</b></a>']
    text = (caption or "").strip()
    if text:
        lines.append(f"<blockquote>{_escape(text)}</blockquote>")
    return "\n".join(lines)


def _new_post_keyboard(username: str, post_url: str):
    """One button. The profile link now lives on the creator's name in the
    message itself, so a second button for it would just be the same jump twice."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[InlineKeyboardButton("View", url=post_url)]])


async def _announce_new_post(chats, username, message, post_url, file_path, post_type, with_media):
    kb = _new_post_keyboard(username, post_url)
    path = Path(file_path) if file_path else None
    can_attach = with_media and path is not None and path.is_file() and post_type == PostType.video

    if can_attach:
        # Goes through the shared sender rather than calling the API directly, so
        # this can't drift from how every other attachment is delivered. It used
        # to send a bare video with no filename, which left Telegram naming the
        # file after the caption.
        await _send_file(set(chats), path, post_type, message, reply_markup=kb)
        return

    for chat_id in chats:
        try:
            # HTML: the creator's name in the message is a profile link.
            await _app.bot.send_message(
                chat_id, text=message, reply_markup=kb,
                parse_mode="HTML", disable_web_page_preview=True,
            )
        except Exception as exc:
            log.error("new-post announce to %s failed: %s", chat_id, exc)


def announce_new_post(db, *, username: str, post_url: str, file_path: str | None,
                      post_type: PostType, nickname: str = "", caption: str = "") -> None:
    """Announce a new monitored post to the authorized chats. The video is
    attached when the "Send new posts to Telegram" toggle is on; otherwise only
    the message + buttons are sent. No-op when the bot isn't running or no
    authorized chat IDs are configured (the feature is opt-in via Allowed IDs)."""
    if not (is_running() and _app is not None and _loop is not None):
        return
    chats = live_allowed_ids(db)
    if not chats:
        return
    with_media = bool(settings_service.get(db, "telegram_send_new_posts"))
    message = build_new_post_message(username, nickname=nickname, caption=caption)
    # Fire-and-forget: schedule the send on the bot's event loop and return
    # immediately so a (potentially slow) video upload never blocks the download
    # worker thread. The coroutine logs its own per-chat errors.
    try:
        asyncio.run_coroutine_threadsafe(
            _announce_new_post(chats, username, message, post_url, file_path, post_type, with_media), _loop
        )
    except Exception as exc:
        log.error("announce_new_post scheduling failed: %s", exc)


async def _on_error(update, ctx) -> None:
    """Last line of defence for an exception in any handler.

    Without one registered, python-telegram-bot logs the traceback and stops
    there - the chat gets no reply at all, which reads as the bot having
    ignored the message. A NameError shipped in the creator card behaved
    exactly that way. This says something happened, and says it once.
    """
    log.error("unhandled Telegram error: %s", ctx.error, exc_info=ctx.error)

    chat = getattr(update, "effective_chat", None)
    if chat is None or not _approved(chat.id):
        return
    try:
        await ctx.bot.send_message(
            chat_id=chat.id,
            text="Something went wrong handling that. Try again in a moment.",
        )
    except Exception:
        # The reply itself failing is not worth a second traceback: the real
        # error is already logged above.
        pass


# ── lifecycle ──
def _run(token: str) -> None:
    from telegram import Update
    from telegram.ext import (
        ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler,
        TypeHandler, filters,
    )

    global _app, _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    # post_init runs once the bot is up, which is the earliest point the menu can
    # be pushed to Telegram. Re-runs on every restart(), so saving a changed
    # approved list re-applies it.
    async def _post_init(app):
        if _stopping.is_set():
            app.stop_running()
            return
        await _apply_command_menu(app.bot)
        if _stopping.is_set():
            app.stop_running()
            return
        await _backfill_chat_names(app.bot)

    # Updates are handled one at a time by default, so a second link sat idle
    # until the first had finished downloading. Handling them concurrently means
    # each message starts processing the moment it arrives; the download itself
    # already runs off the event loop in a worker thread.
    _app = (
        _configure_api(
            ApplicationBuilder()
            .token(token)
            .post_init(_post_init)
            .concurrent_updates(CONCURRENT_UPDATES)
            # Uploads get their own budget. The default is 20s, which a 40 MB post
            # only beats on a fast connection; past that the send failed as a
            # timeout even though nothing was wrong with the file.
            .media_write_timeout(MEDIA_UPLOAD_TIMEOUT)
        ).build()
    )
    # Group -1: runs before everything else, for every update, so the record of
    # who has used the bot doesn't depend on which handler ends up claiming it.
    _app.add_handler(TypeHandler(Update, _record_update), group=-1)
    _app.add_handler(CommandHandler("start", _cmd_start))
    _app.add_handler(CommandHandler("help", _cmd_help))
    _app.add_handler(CommandHandler("ig", _approved_only(_cmd_ig)))
    _app.add_handler(CommandHandler("stats", _approved_only(_cmd_stats)))
    _app.add_handler(CommandHandler("users", _approved_only(_cmd_users)))
    _app.add_handler(CommandHandler("downloads", _approved_only(_cmd_downloads)))
    _app.add_handler(CommandHandler("queue", _approved_only(_cmd_queue)))
    _app.add_handler(CommandHandler("health", _approved_only(_cmd_health)))
    _app.add_handler(CommandHandler("pause", _approved_only(_cmd_pause)))
    _app.add_handler(CommandHandler("resume", _approved_only(_cmd_resume)))
    _app.add_handler(CommandHandler("storage", _approved_only(_cmd_storage)))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))
    # Registered last and unfiltered: the card is the only thing that
    # produces buttons, and _on_button does its own approval check.
    _app.add_handler(CallbackQueryHandler(_on_button))
    _app.add_error_handler(_on_error)
    log.info("Telegram bot polling...")
    _app.run_polling(drop_pending_updates=True, stop_signals=None, close_loop=False)


def start() -> None:
    with _lifecycle_lock:
        _start_locked()


def _start_locked() -> None:
    global _thread, _allowed
    # Saving settings, restoring cloud data and startup may all request this.
    # Never clear the stop signal or replace a still-live polling thread.
    if _thread is not None and _thread.is_alive():
        return
    # Forget which chats we've already revoked: the approved list may have
    # changed since, so every chat gets re-evaluated on its next message.
    _menu_cleared.clear()
    _stopping.clear()
    with session_scope() as db:
        token = live_token(db)
        _allowed = live_allowed_ids(db)
        from .cloud_sync_service import sync
        if not sync.can_automate(db):
            log.info("Telegram polling runs on the account's other device.")
            return
    if not token:
        log.info("Telegram bot disabled (no bot token).")
        return
    _thread = threading.Thread(target=_run_guarded, args=(token,), name="telegram-bot", daemon=True)
    _thread.start()


# Longest gap between restart attempts. Telegram answers a second poller with
# `Conflict`, and the loser dies; a few minutes is long enough for whatever is
# holding the other connection to let go, and short enough that the bot comes
# back without anyone noticing.
_RESTART_BACKOFF_START = 5.0
_RESTART_BACKOFF_MAX = 300.0

# Set while a stop is intentional, so the supervisor below can tell "we asked it
# to stop" from "it fell over".
_stopping = threading.Event()


def _run_guarded(token: str) -> None:
    """Run the bot, and bring it back when it falls over.

    Polling ends for reasons that have nothing to do with configuration: a
    dropped connection, a Telegram 5xx, or `Conflict` when something else claims
    the same token. This used to log the exception and let the thread die, so
    the bot went quiet until the whole app was restarted - which is exactly what
    "the bot sometimes stops responding" was.

    Restarts are backed off, and an intentional stop breaks the loop rather than
    racing it back to life.
    """
    backoff = _RESTART_BACKOFF_START
    while not _stopping.is_set():
        try:
            _run(token)
            # run_polling returned. Clean when we asked for it; otherwise the
            # updater gave up on its own and the bot is now deaf.
            if _stopping.is_set():
                break
            log.warning("Telegram polling ended unexpectedly; restarting in %.0fs", backoff)
        except Exception as exc:
            if _stopping.is_set():
                break
            log.exception("Telegram bot crashed (%s); restarting in %.0fs", exc, backoff)
        finally:
            _shutdown_loop()

        if _stopping.wait(backoff):
            break
        backoff = min(backoff * 2, _RESTART_BACKOFF_MAX)
    log.info("Telegram bot thread finished.")


def _shutdown_loop() -> None:
    """Close the event loop `_run` created, so a restart does not leak one."""
    global _app, _loop
    loop, _loop, _app = _loop, None, None
    if loop is not None:
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass


def stop() -> None:
    with _lifecycle_lock:
        _stop_locked()


def _stop_locked() -> None:
    global _app, _loop, _thread
    _stopping.set()
    # The bot's finally block may clear the globals during this call.
    app, loop = _app, _loop
    if app is not None and loop is not None:
        try:
            # stop_running() uses get_running_loop(): it MUST execute on the
            # bot's thread. Application.stop() alone leaves its Updater polling.
            loop.call_soon_threadsafe(app.stop_running)
        except RuntimeError:
            pass  # The owning thread is already closing its loop.
    if _thread is not None:
        _thread.join(timeout=15)
        if _thread.is_alive():
            log.warning("Telegram is still shutting down; a second connection will not be started.")
            return
    _app = None
    _loop = None
    _thread = None


def restart() -> None:
    """Apply a changed token / allowed-ids by cycling the bot thread."""
    with _lifecycle_lock:
        _stop_locked()
        _start_locked()
