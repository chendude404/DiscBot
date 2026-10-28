"""
Private Discord bot backed by your Claude subscription: chat + image collection -> PowerPoint decks.
Only the bot's owner (you) can use it; everyone else is ignored.

Setup:
    pip install discord.py claude-agent-sdk python-dotenv python-pptx pillow tzdata
    claude setup-token          # log in with your Claude Pro/Max plan, copy the token
    # .env file next to this script:
    #   DISCORD_TOKEN=...
    #   CLAUDE_CODE_OAUTH_TOKEN=...
    #   (do NOT set ANTHROPIC_API_KEY; that would bill the API instead of your plan)
    python bot.py

Commands:
    /ask <question>          one-shot question
    @YourBot <message>       conversational chat (attach images and Claude will see them)
    @YourBot pull all pictures from today
                             same as /collect when:today, limited to your own images
                             ("grab the last 10 pics", "collect everyone's images since monday")
    /reset                   clear this channel's chat history
    /collect [topic] [when] [last] [from_user]
                             collect images for a deck. With `when` ("today", "yesterday",
                             "last 3 hours", "since monday", "9/20", "9/18 to 9/20", or any
                             phrase Claude can interpret) and/or `last` (the N most recent
                             images) it first pulls matching images from this channel's
                             history. It keeps collecting new posts until /done.
    /done                    stop collecting and generate a .pptx deck
    /cancel                  stop collecting (even mid-scan) without generating anything
    /help [topic]            browsable FAQ of all commands (also: @YourBot help)

Channels (matched by name):
    #randobot                everything you type is chat with Claude, no @mention needed.
                             Tests, deadlines and events you mention are added to the board.
                             "use opus" / "use haiku" / "use sonnet" switches the chat model;
                             "model" shows the current one.
    #todo                    holds the live board. Plain text becomes an undated to-do;
                             "done 3" / "remove 3" / "undo 3".
    #jobs                    every link you post is added to the board's job list.

Anywhere (parsed by code, no Claude):
    !todo <task> [date] [ping]      e.g. !todo finish resume fri 5pm 1h   (ping: 1h, 30m, 1d,1h,
                                    or "ping" for at the due time)
    !remind <message> <time from now>   e.g. !remind stretch 30m, !remind call mom in 2 hours
    !schedule [fri|9/30|week|list]      today's classes and dated to-dos (or that day / 7 days /
                                        all classes); !schedule add CS 101 mon,wed 10am-11:15am;
                                        !schedule remove S2. The semester lives in my_schedule.py.
    !sig                     SIG Predictions Cup bot status (in #random, #quant, #code; SIG_CHANNELS)
    /todo board              re-post the board at the bottom of #todo
    /todo clear              hide finished items (they still count toward the totals)
    /jobs import [when]      add links already posted in #jobs to the board

Bot permissions needed: Send Messages, Attach Files, Add Reactions, Read Message History,
Embed Links.
Optional .env:
    BOT_TIMEZONE=America/Indianapolis   what "today" means (default shown)
    CLAUDE_MODEL=sonnet                 sonnet | haiku | opus
    OWNER_ID=123...                     who may use the bot (default: whoever owns the bot app)
    CHAT_CHANNEL=randobot  TODO_CHANNEL=todo  JOBS_CHANNEL=jobs   channel names (defaults shown)
To-dos, jobs and reminders are saved in data.json next to this script.
"""

import ast
import asyncio
import base64
import io
import os
import platform
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

import archive
import claude_backend
import faq
import jobs
import schedule
import sig
import todo

from deck import build_deck_under, normalize_image, parse_plan, planning_prompt
from timefilter import claude_prompt, describe, parse_claude_range, parse_when

load_dotenv()
TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Indianapolis"))
MAX_SCAN = 5000                    # max history messages scanned per /collect

MAX_COLLECT = 40                   # images per deck session
MAX_IMAGE_BYTES = 20 * 1024 * 1024 # ignore attachments bigger than this


def environment_note() -> str:
    """Where the bot runs and what it does, for the system prompt. Rebuilt at every start
    from the files' docstrings, so it stays current without hand-editing."""
    here = Path(__file__).resolve().parent
    files = []
    for path in sorted(here.glob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or "(no description)"
        summary = " ".join(doc.strip().split("\n\n")[0].split())   # first paragraph, one line
        files.append(f"- {path.name}: {summary}")
    usage = __doc__.split("Commands:")[1].split("Bot permissions")[0].rstrip()
    return (f"\n\nYou run inside this Discord bot on the owner's {platform.system()} laptop "
            f"({platform.node()}); its code is in {here}. Files:\n" + "\n".join(files) +
            "\n\nWhat the bot does (its code handles these, not you):" + usage)


SYSTEM_PROMPT = (
    "You are a helpful assistant in a Discord server. "
    "Keep answers concise and use Discord-friendly markdown. "
    "You have no tools: you can't run commands, read files, browse the web or see the computer "
    "beyond what is described below. Never write out a tool call or pretend to have run one. "
    "If a request needs that, say so and tell the user what to run or paste."
    + environment_note()
)
# Chat also feeds the to-do board: Claude tags things worth tracking and the bot adds them.
CHAT_PROMPT = SYSTEM_PROMPT + (
    " The user keeps a to-do board. If their latest message tells you about something they need "
    "to do or remember, like a test, deadline, appointment, event or task, or asks you to add "
    "something to their to-do list, reply normally and then add one line per item at the very "
    "end, exactly like: [[todo: <short task> <date> <ping>]]. <date> is optional and must be "
    "one of these forms: today, tonight, tomorrow, fri, next tue, 9/30, sep 30, optionally "
    "followed by a time like 3pm or 14:30 (e.g. 'fri 3pm'). <ping> is optional and only if they "
    "asked to be reminded: 1h, 30m, 1d (how long before), 1d,1h for several, or ping (at the "
    "time). Example: [[todo: chem test next tue]]. Only tag new items from their latest message. "
    "Don't mention these lines or the board unless they ask."
)
TODO_TAG_RE = re.compile(r"\[\[todo:\s*(.+?)\]\]", re.I | re.S)

CHAT_CHANNEL = os.getenv("CHAT_CHANNEL", "randobot")
TODO_CHANNEL = os.getenv("TODO_CHANNEL", "todo")
JOBS_CHANNEL = os.getenv("JOBS_CHANNEL", "jobs")
# Holds both the always-open job archive and the one-line records of jobs you applied to.
ARCHIVE_CHANNEL = os.getenv("ARCHIVE_CHANNEL", "application-archive")
OLD_ARCHIVE_NAMES = ("application-history",)       # its name before the rename, still accepted
PING_SCORE = 6                                     # new scraped postings this good get you pinged

# "done 3", "done 3, 5", "remove #4", "undo 2" typed in #todo
TODO_CMD_RE = re.compile(r"(done|did|finished|complete[d]?|check|applied|x|remove|delete|del|rm|drop"
                         r"|undo|reopen|uncheck)\s+(#?\d+(?:\s*(?:,|and|&)?\s*#?\d+)*)", re.I)
BANG_RE = re.compile(r"!(todo|remind|schedule|done|sig)\b\s*(.*)", re.I | re.S)

MODELS = ("haiku", "sonnet", "opus")
PARSE_MODEL = "haiku"              # reads job links (the only Claude call outside chat)
# "use opus", "switch to haiku", "change model to sonnet", "model opus"; bare "model" shows it
MODEL_RE = re.compile(r"(?:(?:switch|change)(?: the)?(?: chat)? model to|switch to|use|model)\s+"
                      r"(haiku|sonnet|opus)(?: model)?[.!]?|(?:which |what )?model\??", re.I)

HELP_WORDS = {"help", "faq", "commands", "?"}

# "pull all my pictures from today", "grab the last 10 pics", "collect everyone's images since monday"
PULL_RE = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+)*"
    r"(?:pull|collect|grab|get|gather|fetch|find)\s+"
    r"(?:(?:all|every|the|my|of|me|everyone's|everybody's)\s+)*"
    r"(?:(?:last|latest|recent|most recent|newest)\s+(\d+)\s+)?"
    r"(?:(?:my|of)\s+)*"
    r"(?:pictures?|pics?|images?|photos?|screenshots?)\b\s*(.*?)[\s.!?]*$",
    re.I)
EVERYONE_RE = re.compile(r"\b(?:(?:from|by)\s+)?(?:everyone|everybody|anyone)(?:'s)?\b", re.I)


def parse_pull(text: str) -> tuple[str | None, int | None, bool] | None:
    """If `text` asks to pull images from history, return (when, last, everyone); else None."""
    m = PULL_RE.match(text.strip())
    if not m:
        return None
    last = min(int(m.group(1)), MAX_COLLECT) if m.group(1) else None
    everyone = bool(EVERYONE_RE.search(text))
    rest = EVERYONE_RE.sub("", m.group(2)).strip()
    rest = re.sub(r"^(?:that\s+|which\s+)?(?:i\s+|i've\s+|i have\s+|were\s+|was\s+)?"
                  r"(?:posted|sent|shared|uploaded)\s*", "", rest, flags=re.I)
    rest = re.sub(r"^(?:in|during|within)\s+", "", rest, flags=re.I).strip()
    return rest or None, last, everyone


LAST_N_RE = re.compile(r"(?:the\s+)?(?:last|latest|recent|newest|most recent)\s+(\d+)"
                       r"(?:\s+(?:pictures?|pics?|images?|photos?|screenshots?))?\b\s*(.*)", re.I)


def split_topic(topic: str) -> tuple[str | None, str | None, int | None]:
    """Typing "/collect last 10" or "/collect today" puts that text in `topic`, the first
    option. Recognize those and return (topic, when, last) as if the right options were used."""
    t = topic.strip()
    if parse_when(t, datetime.now(TZ)):                     # "today", "last 3 hours", ...
        return None, t, None
    m = LAST_N_RE.fullmatch(t)                              # "last 10", "last 10 from today"
    if m:
        return None, m.group(2).strip() or None, min(int(m.group(1)), MAX_COLLECT)
    pull = parse_pull(t)                                    # "pull all pictures from today"
    if pull and (pull[0] or pull[1]):
        return None, pull[0], pull[1]
    return topic, None, None

owner_id: int | None = int(os.environ["OWNER_ID"]) if os.getenv("OWNER_ID") else None


class OwnerOnlyTree(app_commands.CommandTree):
    """Rejects every slash command from anyone but the owner."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == owner_id:
            return True
        await interaction.response.send_message("This is a private bot.", ephemeral=True)
        return False


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = OwnerOnlyTree(client)

# channel_id -> Claude Code session id, so @mention chat remembers earlier messages
chat_sessions: dict[int, str] = {}
chat_locks: dict[int, asyncio.Lock] = {}

# channel_id -> {"topic": str|None, "user_id": int|None,
#                "images": [{"id": int, "data": bytes, "author": str, "ts": datetime}]}
sessions: dict[int, dict] = {}

store = todo.Store()                               # to-dos, jobs, reminders (data.json)
job_archive = archive.Archive()                    # archived and scraped postings (archive.json)
board_locks: dict[int, asyncio.Lock] = {}
background_started = False
catch_up_lock = asyncio.Lock()
CATCH_UP_FIRST = timedelta(hours=12)               # look-back when a channel has no last-seen mark yet
LATE = timedelta(minutes=2)                        # older than this = it came due / was sent while offline


# ---------------------------------------------------------------- helpers

def chunk(text: str, size: int = 1990):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def is_image(att: discord.Attachment) -> bool:
    return (att.content_type or "").startswith("image/") and att.size <= MAX_IMAGE_BYTES


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(jpeg).decode()},
    }


async def ask_claude(content, system: str = SYSTEM_PROMPT) -> str:
    """One-off request with no conversation memory."""
    text, _ = await claude_backend.ask(content, system)
    return text


async def add_images(session: dict, message: discord.Message) -> tuple[int, int]:
    """Download image attachments from a message into the session.
    Returns (added, skipped_for_limit). Skips duplicates and non-matching authors."""
    if session["user_id"] and message.author.id != session["user_id"]:
        return 0, 0
    seen = {img["id"] for img in session["images"]}
    added = skipped = 0
    for att in message.attachments:
        if not is_image(att) or att.id in seen:
            continue
        if len(session["images"]) >= MAX_COLLECT:
            skipped += 1
            continue
        try:
            data = await att.read()
        except discord.HTTPException:
            continue
        session["images"].append({"id": att.id, "data": data,
                                  "author": message.author.display_name,
                                  "ts": message.created_at.astimezone(TZ)})
        added += 1
    return added, skipped


async def resolve_when(text: str) -> tuple[datetime, datetime] | None:
    now = datetime.now(TZ)
    rng = parse_when(text, now)
    if rng:
        return rng
    try:                                             # free-form phrase: let Claude interpret it
        reply = await ask_claude(claude_prompt(text, now),
                                 system="You convert time phrases to date ranges.")
    except Exception:
        return None
    return parse_claude_range(reply, now)


def faq_view(index: int = 0) -> faq.FAQView:
    facts = {"max_images": MAX_COLLECT, "max_scan": MAX_SCAN, "timezone": TZ.key,
             "model": chat_model(), "parse_model": PARSE_MODEL, "chat_channel": CHAT_CHANNEL,
             "todo_channel": TODO_CHANNEL, "jobs_channel": JOBS_CHANNEL,
             "history_channel": ARCHIVE_CHANNEL}
    return faq.FAQView(owner_id, facts, index)


def upload_limit(guild: discord.Guild | None) -> int:
    limit = guild.filesize_limit if guild else 10 * 1024 * 1024
    return limit - 256 * 1024        # headroom for multipart overhead


async def pull_history(channel, session: dict, when: str | None, last: int | None,
                       who: str, about: str, send) -> None:
    """Fill an already-registered collection `session` with images from `channel`'s history
    (`when` range and/or the `last` N images), then report the result with `send(text)`."""
    cid = channel.id
    start = end = None
    if when:
        rng = await resolve_when(when)
        if rng is None:
            sessions.pop(cid, None)
            await send(
                f'I couldn\'t read "{when}" as a time range. Try "today", "yesterday", '
                f'"last 3 hours", "since monday", or "9/18 to 9/20".')
            return
        start, end = rng

    # With `last`, walk newest-first and stop after N images; otherwise take the range in order.
    scanned = skipped = found = 0
    try:
        async for msg in channel.history(after=start, before=end,
                                         oldest_first=not last, limit=MAX_SCAN):
            if sessions.get(cid) is not session:      # /cancel was run mid-scan
                break
            scanned += 1
            if msg.author.bot or not msg.attachments:
                continue
            added, s = await add_images(session, msg)
            found += added
            skipped += s
            if last and found >= last:
                break
    except discord.Forbidden:
        sessions.pop(cid, None)
        await send("I need the **Read Message History** permission "
                   "in this channel to look back in time.")
        return

    if sessions.get(cid) is not session:
        await send("History scan stopped by `/cancel`.")
        return
    if last and found > last:                         # a single message can hold several images
        session["images"].sort(key=lambda im: im["ts"])
        del session["images"][:found - last]

    n = len(session["images"])
    where = f" ({describe(start, end)})" if start else " in the chat history"
    lines = [f"Found **{n}** image{'s' if n != 1 else ''}{who}{where}{about}."]
    if last and n < last:
        lines.append(f"Asked for the last {last}, but only {n} were found.")
    if skipped:
        lines.append(f"Hit the {MAX_COLLECT}-image limit; {skipped} more were left out.")
    if scanned >= MAX_SCAN:
        lines.append(f"Only the {MAX_SCAN} {'most recent' if last else 'first'} messages were checked.")
    lines.append("Post more to add them, run `/done` to build the deck, or `/cancel` to stop.")
    await send("\n".join(lines))


# ---------------------------------------------------------------- to-dos, jobs, reminders

def chat_model() -> str:
    return store.data.get("chat_model") or claude_backend.MODEL


async def parse_with_claude(prompt: str, system: str) -> str:
    text, _ = await claude_backend.ask(prompt, system, model=PARSE_MODEL)
    return text


def todo_channel(guild: discord.Guild | None) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=TODO_CHANNEL) if guild else None


def guild_items(guild: discord.Guild) -> list[dict]:
    return [i for i in store.items if i.get("guild_id") == guild.id]


def add_item(kind: str, text: str, guild: discord.Guild | None, channel_id: int | None,
             due: datetime | None = None, offsets=(), **fields) -> dict:
    now = datetime.now(TZ)
    return store.add(kind, text, guild_id=guild.id if guild else None, channel_id=channel_id,
                     due=due.isoformat() if due else None,
                     remind_at=todo.reminder_times(due, list(offsets), now) if due else [], **fields)


def set_done(item: dict, done: bool = True):
    item["done"] = done
    item["done_at"] = datetime.now(TZ).isoformat() if done else None
    item["archived"] = False
    store.save()


def new_todo(guild: discord.Guild, task: str, due: datetime | None = None, offsets=()) -> dict:
    board = todo_channel(guild)
    return add_item("todo", task.strip()[:200], guild, board.id if board else None, due, offsets)


async def import_job(message: discord.Message, url: str) -> dict:
    """One link from #jobs -> a job on the board. Haiku reads the URL and Discord's link preview."""
    preview = next((e for e in message.embeds if e.url and (e.url in url or url in e.url)), None)
    if preview is None and len(jobs.find_urls(message.content)) == 1 and message.embeds:
        preview = message.embeds[0]
    note = jobs.URL_RE.sub("", message.content).strip()[:300]
    try:
        info = jobs.parse_job(await parse_with_claude(
            jobs.job_prompt(url, note, jobs.preview_text(preview) if preview else ""),
            "You extract job posting details."))
    except Exception:
        info = None
    info = info or jobs.fallback(url)
    due = None
    if info["deadline"]:
        try:
            due = datetime.combine(datetime.fromisoformat(info["deadline"]).date(), time(23, 59), TZ)
        except ValueError:
            pass
    board = todo_channel(message.guild)
    return add_item("job", f"{info['title'] or 'Untitled role'} @ {info['company'] or '?'}",
                    message.guild, board.id if board else None, due, category="Jobs",
                    job={**info, "url": url, "posted": message.jump_url},
                    created=message.created_at.astimezone(TZ).isoformat())


def known_job_urls() -> set[str]:
    """Links already on the board, in jobs.canonical form."""
    return {jobs.canonical(i["job"]["url"]) for i in store.items if i["kind"] == "job" and i.get("job")}


async def refresh_board(guild: discord.Guild, force: bool = False, deleted: int | None = None):
    """Show the board in #todo. Edited in place while it's still the newest message there;
    otherwise re-posted so it stays at the bottom. `deleted`: a message just removed from the
    channel (discord.py still reports it as the newest), so it doesn't force a re-post."""
    channel = todo_channel(guild)
    if channel is None:
        return
    async with board_locks.setdefault(channel.id, asyncio.Lock()):
        now, items = datetime.now(TZ), guild_items(guild)
        today = schedule.day_lines(store.data.get("schedule", []), items, now.date(), now)
        pages = todo.board_pages(items, now, store.pruned(guild.id), "\n".join(today))
        old = store.board_messages(channel.id)
        newest = channel.last_message_id
        if not force and old and len(old) == len(pages) and newest in (old[-1], deleted):
            try:
                for mid, embeds in zip(old, pages):
                    await channel.get_partial_message(mid).edit(embeds=embeds)
                return
            except discord.NotFound:
                pass
        new = [(await channel.send(embeds=embeds)).id for embeds in pages]
        store.set_board_messages(channel.id, new)
        for mid in old:
            try:
                await channel.get_partial_message(mid).delete()
            except discord.HTTPException:
                pass


def ping_text(item: dict, at: datetime, now: datetime) -> str:
    late = " *(late: I was offline)*" if now - at > timedelta(minutes=10) else ""
    if item["kind"] == "reminder":
        return f"⏰ <@{owner_id}> {item['text']}{late}"
    due = todo.dt(item["due"])
    when = "is due **now**" if due <= at + timedelta(minutes=1) else \
        f"is due {todo.stamp(due, 'R')} ({todo.stamp(due)})"
    j = item.get("job")
    name = f"Apply to **{j['title']}** at **{j['company']}** (<{j['url']}>)" if j else f"**{item['text']}**"
    return f"⏰ <@{owner_id}> `#{item['id']}` {name} {when}{late}"


async def check_reminders(last_check: datetime, now: datetime):
    """Send pings that have come due, and refresh boards where something just became overdue."""
    touched: set[int] = set()
    changed = False
    for item in list(store.items):
        if item["done"]:
            continue
        due = todo.dt(item["due"])
        if due and last_check < due <= now and item.get("guild_id"):
            touched.add(item["guild_id"])                        # show the overdue mark
        pending = [r for r in item["remind_at"] if r not in item["fired"] and todo.dt(r) <= now]
        if not pending:
            continue
        item["fired"] += pending
        changed = True
        # Its time passed while the bot was offline: drop the ping rather than send it late.
        stale = due is not None and due < now - LATE
        if stale:
            print(f"skipped stale ping: {item['text'][:60]!r}")
        channel = None if stale else client.get_channel(item.get("channel_id") or 0)
        if channel is not None:
            try:
                sent = await channel.send(ping_text(item, max(map(todo.dt, pending)), now))
                if item["kind"] != "reminder":        # remembered so it can be cleaned up
                    item.setdefault("pings", []).append([channel.id, sent.id, now.isoformat()])
            except discord.HTTPException:
                pass
        if item["kind"] == "reminder":
            store.items.remove(item)
        elif item.get("guild_id"):
            touched.add(item["guild_id"])                        # bell icon goes away
    if changed:
        store.save()
    for gid in touched:
        if guild := client.get_guild(gid):
            await refresh_board(guild)


async def delete_pings(items: list[dict], older_than: timedelta = timedelta(0)):
    """Remove the bot's ping messages for these items (all of them, or only old ones).
    Deleting its own messages needs no special permission."""
    cutoff = datetime.now(TZ) - older_than
    for item in items:
        keep = []
        for ch_id, msg_id, sent in item.get("pings", []):
            if todo.dt(sent) > cutoff:
                keep.append([ch_id, msg_id, sent])
            elif channel := client.get_channel(ch_id):
                try:
                    await channel.get_partial_message(msg_id).delete()
                except discord.HTTPException:
                    pass
        item["pings"] = keep
    store.save()


def archive_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for name in (ARCHIVE_CHANNEL, *OLD_ARCHIVE_NAMES):
        if channel := discord.utils.get(guild.text_channels, name=name):
            return channel
    return None


async def log_application(item: dict, guild: discord.Guild):
    """A job was marked applied: post a permanent one-line record in #application-archive."""
    channel = archive_channel(guild)
    if channel is None or item.get("history_msg"):
        return
    j = item.get("job") or {}
    bits = [j.get("company"), j.get("location"), j.get("work_mode"), j.get("pay")]
    # Its number in the season: applications up to and including this one.
    earlier = [i for i in guild_items(guild) if (i["done_at"] or "") <= item["done_at"]]
    n = todo.season_jobs(earlier, todo.dt(item["done_at"]), store.pruned(guild.id))
    text = (f"✅ **{j.get('title') or 'Untitled role'}**"
            + "".join(f" · {b}" for b in bits if b)
            + f" · applied {todo.stamp(todo.dt(item['done_at']), 'D')}"
            + (f" · [posting](<{j['url']}>)" if j.get("url") else "")
            + f" · #{n} this season")
    try:
        msg = await channel.send(text)
    except discord.HTTPException:
        return
    item["history_msg"] = [channel.id, msg.id]
    store.save()


async def unlog_application(item: dict):
    """Undo of an applied job: take its record back out of #application-archive."""
    if ref := item.pop("history_msg", None):
        if channel := client.get_channel(ref[0]):
            try:
                await channel.get_partial_message(ref[1]).delete()
            except discord.HTTPException:
                pass
        store.save()


async def try_delete(message: discord.Message) -> bool:
    """Delete a user's message; False without the Manage Messages permission."""
    try:
        await message.delete()
        return True
    except discord.HTTPException:
        return False


def board_jobs() -> list[dict]:
    """Jobs on the board: posted (or promoted) and not applied to yet."""
    return [i for i in store.items if i["kind"] == "job" and not i["done"] and i.get("job")]


async def job_housekeeping():
    """Once a day. Board jobs you haven't applied to within archive.BOARD_DAYS move to the
    archive. Then open jobs whose posting was taken down move there too, marked 🔒: a 404/410
    at once, a weaker sign only if it shows up two days running."""
    now, moved = datetime.now(TZ), 0
    for item in board_jobs():
        since = datetime.fromisoformat(item["job"].get("promoted") or item["created"])
        if now - since > timedelta(days=archive.BOARD_DAYS):
            store.items.remove(item)
            job_archive.add_board_job(item)
            moved += 1
    async with aiohttp.ClientSession(headers=jobs.BROWSER) as session:
        for item in board_jobs():
            j = item["job"]
            if j.get("keep_open"):
                continue
            status = await jobs.link_status(session, j["url"])
            if status is None:
                continue                                      # no usable answer: try again tomorrow
            j["strikes"] = j.get("strikes", 0) + 1 if status == "closed?" else 0
            if status == "gone" or j["strikes"] >= 2:
                j["closed"] = now.isoformat()
                store.items.remove(item)
                job_archive.add_board_job(item, closed=True)
                moved += 1
                print(f"job link closed ({status}): {item['text']!r}")
            await asyncio.sleep(1)                            # go easy on the job sites
    expired = job_archive.expire()
    store.save()
    job_archive.save()
    print(f"job housekeeping: {moved} moved to the archive, {expired} expired")
    for guild in client.guilds:
        if moved:
            await refresh_board(guild)
        await refresh_archive(guild)


async def scrape_jobs():
    """Every few hours: new Summer 2027 postings from the Simplify / Pitt CSC list go straight
    into the archive, and expired ones are deleted. The only message is a ping when a new
    posting scores PING_SCORE or more."""
    try:
        res = await archive.scrape_simplify(job_archive, known_job_urls())
        expired = job_archive.expire()
        job_archive.save()
        print(("simplify: unchanged" if res is None else f"simplify: +{len(res[0])} new, "
               f"-{res[1]} no longer listed") + f", {expired} expired")
        if res or expired:
            for guild in client.guilds:
                await refresh_archive(guild)
        if res:
            await ping_strong_matches(set(res[0]))
    except Exception as e:
        print(f"simplify scrape error: {e!r}")


async def ping_strong_matches(new_ids: set[str]):
    """One message mentioning you, listing the best new postings; it replaces the last one."""
    strong = [r for r in job_archive.scored() if r[0] in new_ids and r[2] >= PING_SCORE]
    for guild in client.guilds:
        channel = archive_channel(guild)
        if not strong or channel is None:
            continue
        lines = [f"**{score:+d}** [{(e['title'] or 'Untitled role')[:70]}]({e['url']}) · "
                 f"**{e['company']}** · {(e['location'] or '?')[:40]}" for _, e, score, _ in strong[:5]]
        more = f"\n…and {len(strong) - 5} more" if len(strong) > 5 else ""
        if old := job_archive.data.get("ping"):
            try:
                await channel.get_partial_message(old[1]).delete()
            except discord.HTTPException:
                pass
        sent = await channel.send(f"<@{owner_id}> 🔔 {len(strong)} strong new match"
                                  f"{'es' if len(strong) > 1 else ''}:\n" + "\n".join(lines) + more
                                  + "\n-# Promote them from the archive above (🆕 folder).",
                                  suppress_embeds=True)
        job_archive.data["ping"] = [channel.id, sent.id]
        job_archive.save()


archive_lock = asyncio.Lock()


async def refresh_archive(guild: discord.Guild, note: str = ""):
    """Redraw the always-open archive message in #application-archive (posting and pinning it
    if it's missing). It keeps showing the folder and page you left it on."""
    async with archive_lock:
        channel = archive_channel(guild)
        if channel is None:
            print(f"no #{ARCHIVE_CHANNEL} channel for the job archive")
            return
        view = archive.ArchiveView(job_archive, lambda eid: promote_from_archive(guild, eid, redraw=False),
                                   owner_id, note)
        ref = job_archive.data.get("message")
        if ref and ref[0] == channel.id:
            try:
                await channel.get_partial_message(ref[1]).edit(view=view)
                client.add_view(view, message_id=ref[1])     # buttons keep working after restarts
                return
            except discord.NotFound:
                pass                                          # deleted: post a new one
        message = await channel.send(view=view)
        client.add_view(view, message_id=message.id)
        job_archive.data["message"] = [channel.id, message.id]
        job_archive.save()
        try:
            await message.pin()                               # easy to find above the records
        except discord.HTTPException:
            pass


async def promote_from_archive(guild: discord.Guild, eid: str, redraw: bool = True) -> str | None:
    """Move an archived posting to the board (back as the same item if it came from there).
    `redraw`: also redraw the archive message (its's own buttons redraw it themselves)."""
    e = job_archive.entries.pop(eid, None)
    if e is None:
        return None
    if item := e.get("item"):
        if item["job"].pop("closed", None):
            item["job"]["keep_open"] = True               # you said it's open: stop checking it
        item["job"].pop("strikes", None)
        item["id"] = store.data["next_id"]
        store.data["next_id"] += 1
        store.items.append(item)
    else:
        board = todo_channel(guild)
        item = add_item("job", f"{e['title'] or 'Untitled role'} @ {e['company'] or '?'}", guild,
                        board.id if board else None, category="Jobs",
                        job={"title": e["title"], "company": e["company"], "location": e["location"],
                             "work_mode": None, "deadline": None, "pay": None, "url": e["url"],
                             "posted": None, "source": e["source"]})
    item["job"]["promoted"] = datetime.now(TZ).isoformat()   # its 7 days start over
    store.save()
    job_archive.handle(e["url"])
    job_archive.save()
    await refresh_board(guild)
    note = f"⬆️ Moved to the board: {todo.label(item)}"
    if redraw:
        await refresh_archive(guild, note)
    return note


async def reminder_loop():
    """Every 20 s: send due pings. At midnight: move the agenda to the new day.
    Once a day at 4 AM: tidy and renumber the boards, and archive stale or closed jobs.
    Every few hours: pull new postings into the archive."""
    last = datetime.now(TZ)
    while True:
        now = datetime.now(TZ)
        try:
            if now.time() >= todo.TIDY_AT and store.data.get("last_link_check") != now.date().isoformat():
                store.data["last_link_check"] = now.date().isoformat()
                asyncio.create_task(job_housekeeping())
            scraped = job_archive.data.get("last_scrape")
            if scraped is None or now - datetime.fromisoformat(scraped) > archive.SCRAPE_EVERY:
                job_archive.data["last_scrape"] = now.isoformat()  # set now so it isn't started twice
                asyncio.create_task(scrape_jobs())
            if todo.tidy_due(store, now):
                await delete_pings(store.items, older_than=timedelta(days=1))
                todo.tidy(store, now)
                for guild in client.guilds:
                    await refresh_board(guild)
            elif now.date() != last.date():
                for guild in client.guilds:
                    await refresh_board(guild)
            await check_reminders(last, now)
        except Exception as e:                                   # keep the loop alive
            print(f"reminder loop error: {e!r}")
        last = now
        await asyncio.sleep(20)


# ---------------------------------------------------------------- lifecycle

@client.event
async def on_ready():
    global owner_id
    if owner_id is None:
        app = await client.application_info()
        owner_id = app.owner.id
    # Hide the commands from members without admin rights (the check above is the real lock).
    for cmd in tree.get_commands():
        cmd.default_permissions = discord.Permissions(administrator=True)
    await tree.sync()
    print(f"Logged in as {client.user}; responding only to user {owner_id}")
    global background_started
    if not background_started:                   # on_ready also fires after reconnects
        background_started = True
        if schedule.load_semester(store):
            print(f"Loaded {schedule.SEMESTER}: {len(store.data['schedule'])} classes")
        remind_at = schedule.NEXT_SEMESTER_REMINDER.replace(tzinfo=TZ)
        if store.data.get("next_semester_reminder") != schedule.SEMESTER and remind_at > datetime.now(TZ):
            store.data["next_semester_reminder"] = schedule.SEMESTER    # set it up once per semester
            for guild in client.guilds:
                if board := todo_channel(guild):
                    add_item("reminder", "📚 Time to add next semester's classes: edit my_schedule.py "
                             "(or use `!schedule add`).", guild, board.id, remind_at, [timedelta(0)])
        asyncio.create_task(reminder_loop())
        for guild in client.guilds:
            # Applied jobs not yet in #application-archive (e.g. from before it existed), oldest first.
            for item in sorted((i for i in guild_items(guild) if i["kind"] == "job" and i["done"]),
                               key=lambda i: i["done_at"] or ""):
                await log_application(item, guild)
            await refresh_board(guild)
        if n := job_archive.expire():                 # catch up on the 90/30-day rule
            job_archive.save()
            print(f"archive: {n} expired")
        for guild in client.guilds:
            await refresh_archive(guild)
    # On every connect, including reconnects after a dropped connection: Discord doesn't replay
    # messages sent while the bot was away, so read them from history.
    for guild in client.guilds:
        await catch_up(guild)


def mark_seen(message: discord.Message):
    """Remember the newest message in a watched channel, so catch_up starts after it."""
    seen = store.data.setdefault("last_seen", {})
    if message.id > seen.get(str(message.channel.id), 0):
        seen[str(message.channel.id)] = message.id
        store.save()


def wants_bot(message: discord.Message) -> bool:
    """Would the bot act on this message? Everything in #todo/#jobs/#randobot; elsewhere only
    !commands and @mentions."""
    return (message.channel.name in (TODO_CHANNEL, JOBS_CHANNEL, CHAT_CHANNEL)
            or bool(BANG_RE.match(message.content.strip())) or client.user in message.mentions)


async def catch_up(guild: discord.Guild):
    """Handle what you posted while the bot was offline, in every channel it can read: messages
    after the last one it saw, minus any it already replied or reacted to. Missed chats get one
    combined answer, to-dos/reminders whose time has passed are dropped (see handle_bang), the
    board is refreshed, and a summary is posted in #randobot."""
    async with catch_up_lock:
        until = discord.utils.utcnow()                # anything newer arrives through on_message
        seen = store.data.setdefault("last_seen", {})
        caught = {}
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            last = seen.get(str(channel.id))
            after = discord.Object(last) if last else until - CATCH_UP_FIRST
            history = [m async for m in channel.history(after=after, before=until, oldest_first=True,
                                                        limit=MAX_SCAN)]
            answered = {m.reference.message_id for m in history
                        if m.author == client.user and m.reference}
            missed = [m for m in history if m.author.id == owner_id and m.id not in answered
                      and not any(r.me for r in m.reactions) and wants_bot(m)]
            batch = []
            for m in missed:
                print(f"catch-up #{channel.name} {m.content[:80]!r}")
                try:
                    await handle_message(m, chat_batch=batch)
                except Exception as e:                # one bad message shouldn't stop the rest
                    print(f"catch-up error: {e!r}")
            if batch:
                await answer_missed_chats(batch)
            if history:
                mark_seen(history[-1])
            if missed:
                caught[channel] = len(missed)
        if caught:
            await refresh_board(guild)
            if chat := discord.utils.get(guild.text_channels, name=CHAT_CHANNEL):
                await chat.send("🔄 Back online. Caught up on what I missed: "
                                + ", ".join(f"{c.mention} ({n})" for c, n in caught.items()))


async def answer_missed_chats(batch: list[tuple[discord.Message, str, list]]):
    """Chats sent while the bot was offline: one Claude call answers them together."""
    if len(batch) == 1:
        await chat_reply(*batch[0])
        return
    lines = [f"[{m.created_at.astimezone(TZ):%a %I:%M %p}] {text or '(sent an image)'}"
             for m, text, _ in batch]
    text = ("You were offline and missed these messages (oldest first). Answer them together:\n"
            + "\n".join(lines))
    await chat_reply(batch[-1][0], text, [a for _, _, images in batch for a in images])


# ---------------------------------------------------------------- chat commands

@tree.command(name="ask", description="Ask Claude a one-off question")
async def ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await ask_claude(question)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}")
        return
    for part in chunk(answer or "(empty response)"):
        await interaction.followup.send(part)


@tree.command(name="reset", description="Clear Claude's chat history for this channel")
async def reset(interaction: discord.Interaction):
    chat_sessions.pop(interaction.channel_id, None)
    await interaction.response.send_message("History cleared.", ephemeral=True)


@tree.command(name="help", description="Browse an FAQ of the bot's commands")
@app_commands.describe(topic="Jump straight to one topic")
@app_commands.choices(topic=faq.CHOICES)
async def help_cmd(interaction: discord.Interaction, topic: app_commands.Choice[str] | None = None):
    view = faq_view(faq.index_of(topic.value if topic else None))
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
    view.message = await interaction.original_response()


# ---------------------------------------------------------------- deck commands

@tree.command(name="collect", description="Collect images in this channel for a slide deck")
@app_commands.describe(
    topic="What the deck is about (optional; Claude will infer it otherwise)",
    when='Pull past images from chat history, e.g. "today", "yesterday", "last 3 hours", "9/20"',
    last="Pull the N most recent images from chat history (combine with `when` to limit the range)",
    from_user="Only collect images posted by this person",
)
async def collect(interaction: discord.Interaction, topic: str | None = None,
                  when: str | None = None,
                  last: app_commands.Range[int, 1, MAX_COLLECT] | None = None,
                  from_user: discord.Member | None = None):
    if topic and not when and not last:
        topic, when, last = split_topic(topic)
    cid = interaction.channel_id
    if cid in sessions:
        n = len(sessions[cid]["images"])
        await interaction.response.send_message(
            f"Already collecting here ({n} images so far). Use `/done` or `/cancel`.",
            ephemeral=True)
        return

    session = {"topic": topic, "user_id": from_user.id if from_user else None, "images": []}
    who = f" from **{from_user.display_name}**" if from_user else ""
    about = f" about **{topic}**" if topic else ""

    if not when and not last:
        sessions[cid] = session
        await interaction.response.send_message(
            f"Collecting images{who}{about}. Post them in this channel (up to {MAX_COLLECT}), "
            f"then run `/done` to build the deck.\n"
            f"Tip: use the `when` or `last` option to also pull images already in the chat.")
        return

    # Register right away so /cancel can stop the scan and a second /collect is refused.
    sessions[cid] = session
    await interaction.response.defer(thinking=True)
    await pull_history(interaction.channel, session, when, last, who, about,
                       interaction.followup.send)


@tree.command(name="cancel", description="Stop collecting images (or a history scan) without building a deck")
async def cancel(interaction: discord.Interaction):
    session = sessions.pop(interaction.channel_id, None)
    if session is None:
        await interaction.response.send_message("Nothing is being collected here.", ephemeral=True)
    else:
        n = len(session["images"])
        await interaction.response.send_message(
            f"Collection cancelled; {n} image{'s' if n != 1 else ''} discarded.")


@tree.command(name="done", description="Build a PowerPoint deck from the collected images")
async def done(interaction: discord.Interaction):
    session = sessions.pop(interaction.channel_id, None)
    if session is None:
        await interaction.response.send_message("Nothing is being collected here. Start with `/collect`.",
                                                ephemeral=True)
        return
    items = sorted(session["images"], key=lambda im: im["ts"])
    raw = [im["data"] for im in items]
    if not raw:
        await interaction.response.send_message("No images were posted, so there's nothing to build.")
        return

    await interaction.response.defer(thinking=True)

    # 1. Small thumbnails for Claude to plan from (cheap on tokens).
    thumbs = await asyncio.to_thread(lambda: [normalize_image(b, 768, 75) for b in raw])
    content = []
    for i, t in enumerate(thumbs):
        if t is None:
            continue
        posted = items[i]["ts"].strftime("%a %b %d, %I:%M %p")
        content.append({"type": "text", "text": f"Image {i} (posted by {items[i]['author']}, {posted}):"})
        content.append(image_block(t[0]))
    if not content:
        await interaction.followup.send("None of those files could be opened as images.")
        return
    content.append({"type": "text", "text": planning_prompt(len(raw), session["topic"])})

    # 2. Claude plans the deck.
    try:
        reply = await ask_claude(content, system="You design clear, well-ordered slide decks.")
    except Exception as e:
        await interaction.followup.send(f"Claude request failed: {e}")
        return
    plan = parse_plan(reply, len(raw))
    for s in plan["slides"]:                     # drop images that failed to decode
        s["images"] = [i for i in s["images"] if thumbs[i] is not None]
    plan["slides"] = [s for s in plan["slides"] if s["images"]]
    if not plan["slides"]:
        plan["slides"] = [{"title": "", "caption": "", "images": [i], "notes": ""}
                          for i, t in enumerate(thumbs) if t is not None]

    # 3. Render the .pptx off the event loop, shrinking images until it fits.
    data = await asyncio.to_thread(build_deck_under, plan, raw, upload_limit(interaction.guild))
    if data is None:
        await interaction.followup.send("The deck came out too large to upload here. "
                                        "Try fewer images.")
        return

    # 4. Deliver.
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in plan["title"]).strip()
    fname = (safe.replace(" ", "_") or "deck")[:60] + ".pptx"
    file = discord.File(fp=io.BytesIO(data), filename=fname)
    await interaction.followup.send(
        f"**{plan['title']}**: {len(plan['slides']) + 1} slides from {len(raw)} images.",
        file=file)


# ---------------------------------------------------------------- board commands

todo_group = app_commands.Group(name="todo", description="The to-do board in #todo")
jobs_group = app_commands.Group(name="jobs", description="Job links from #jobs")


@todo_group.command(name="board", description="Re-post the to-do board at the bottom of #todo")
async def todo_board(interaction: discord.Interaction):
    channel = todo_channel(interaction.guild)
    if channel is None:
        await interaction.response.send_message(f"Create a #{TODO_CHANNEL} channel first.", ephemeral=True)
        return
    await interaction.response.send_message(f"Board posted in {channel.mention}.", ephemeral=True)
    await refresh_board(interaction.guild, force=True)


@todo_group.command(name="clear", description="Hide finished items (they still count toward your totals)")
async def todo_clear(interaction: discord.Interaction):
    n = 0
    for item in guild_items(interaction.guild) if interaction.guild else []:
        if item["done"] and not item["archived"]:
            item["archived"] = True
            n += 1
    store.save()
    await interaction.response.send_message(f"Hid {n} finished item{'s' if n != 1 else ''}.", ephemeral=True)
    await refresh_board(interaction.guild)


@jobs_group.command(name="import", description="Add job links you already posted in #jobs to the board")
@app_commands.describe(when='Only links posted in this range, e.g. "this month", "since 9/1" (default: all)')
async def jobs_import(interaction: discord.Interaction, when: str | None = None):
    guild = interaction.guild
    channel = discord.utils.get(guild.text_channels, name=JOBS_CHANNEL) if guild else None
    if channel is None:
        await interaction.response.send_message(f"There's no #{JOBS_CHANNEL} channel here.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    start = end = None
    if when:
        rng = await resolve_when(when)
        if rng is None:
            await interaction.followup.send(f'I couldn\'t read "{when}" as a time range.')
            return
        start, end = rng

    known, found = known_job_urls(), []
    async for msg in channel.history(after=start, before=end, oldest_first=True, limit=MAX_SCAN):
        if msg.author.id == owner_id:
            for url in jobs.find_urls(msg.content):
                if jobs.canonical(url) not in known and not job_archive.has(url):
                    known.add(jobs.canonical(url))
                    found.append((msg, url))
    if not found:
        await interaction.followup.send("No new job links found; everything is already on the board.")
        return
    await interaction.followup.send(f"Adding {len(found)} job links (a few seconds each)…")
    for msg, url in found:
        await import_job(msg, url)
    await refresh_board(guild)
    board = todo_channel(guild)
    await interaction.followup.send(f"Added {len(found)} jobs to {board.mention if board else 'the board'}. "
                                    f'Mark ones you already applied to with "done <number>" in #{TODO_CHANNEL}.')


@jobs_group.command(name="archive", description="Point to the job archive in #application-archive (and redraw it)")
async def jobs_archive(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await refresh_archive(interaction.guild)
    channel = archive_channel(interaction.guild)
    await interaction.followup.send(f"The archive is pinned in {channel.mention}." if channel
                                    else f"There's no #{ARCHIVE_CHANNEL} channel for it.", ephemeral=True)


tree.add_command(todo_group)
tree.add_command(jobs_group)


# ---------------------------------------------------------------- messages

@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Your reaction on a !schedule answer deletes the answer and the !schedule message.
    Works from the reply link alone, so it also works for answers from before a restart."""
    if payload.user_id != owner_id or payload.message_author_id != client.user.id:
        return
    channel = client.get_channel(payload.channel_id)
    if channel is None:
        return
    try:
        answer = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        return
    if not answer.reference or not answer.reference.message_id:
        return
    try:
        original = await channel.fetch_message(answer.reference.message_id)
    except discord.NotFound:
        original = None                           # already gone; still clear the answer
    except discord.HTTPException:
        return
    is_schedule = (original.content.strip().lower().startswith("!schedule") if original is not None
                   else bool(answer.embeds) and answer.embeds[0].color == discord.Color.teal())
    if not is_schedule:
        return                                    # some other reply: leave it alone
    if original is not None:
        await try_delete(original)
    await try_delete(answer)


async def handle_todo_channel(message: discord.Message):
    """#todo: "done 3" / "undo 3" / "remove 3", or any other text becomes a new to-do."""
    guild, text = message.guild, message.content.strip()
    if not text:
        return
    if text.lower() in HELP_WORDS:
        view = faq_view(faq.index_of("board"))
        view.message = await message.reply(embed=view.embed(), view=view, mention_author=False)
        return
    m = TODO_CMD_RE.fullmatch(text.rstrip(".!"))
    if not m and re.fullmatch(r"(done|remove|delete|del|rm|undo|reopen)\b\s*(?:#.*|\d.*)?", text, re.I):
        # Looks like a command but isn't one ("remove", "remove #3 pls"): don't make it a to-do.
        print(f"#todo {text!r} -> hint")
        await message.channel.send('Use a number from the board: `done 3`, `remove 3, 5`, `undo 3`.',
                                   delete_after=15)
        await tidy_up(message)
        return
    refresh_only = text.lower() in ("board", "refresh", "list", "show")
    print(f"#todo {text!r} -> {'command' if m else 'refresh' if refresh_only else 'new to-do'}")
    if m:
        _, missing = await apply_command(guild, m.group(1).lower(), m.group(2))
        if missing:
            await message.channel.send(f"There's no item {', '.join(f'#{n}' for n in missing)}.",
                                       delete_after=15)
    elif text.lower() not in ("board", "refresh", "list", "show"):
        new_todo(guild, text)                         # plain text: an undated to-do
    await tidy_up(message)


async def apply_command(guild: discord.Guild, verb: str, numbers: str) -> tuple[list[dict], list[int]]:
    """done / undo / remove on the board numbers in `numbers` ("3", "3, 5").
    Returns (items changed, numbers that don't exist). Used by #todo and !done."""
    ids = [int(n) for n in re.findall(r"\d+", numbers)]
    items = [i for i in map(store.get, ids)
             if i and i.get("guild_id") == guild.id and i["kind"] != "reminder"]
    undo = verb in ("undo", "reopen", "uncheck")
    if not undo:
        await delete_pings(items)                     # its reminders are no longer needed
    for item in items:
        if item.get("job") and not undo:                  # removed or applied: don't re-scrape it
            job_archive.handle(item["job"]["url"])
            job_archive.save()
        if verb in ("remove", "delete", "del", "rm", "drop"):
            await unlog_application(item)
            store.remove(item["id"])
        else:
            set_done(item, not undo)
            if item["kind"] == "job":
                await (unlog_application(item) if undo else log_application(item, guild))
    return items, sorted(set(ids) - {i["id"] for i in items})


async def tidy_up(message: discord.Message):
    """After a change made from #todo: delete the typed message (the board now shows it) and
    refresh the board; without Manage Messages, react ✅ instead."""
    if await try_delete(message):
        await refresh_board(message.guild, deleted=message.id)
    else:
        await message.add_reaction("✅")
        await refresh_board(message.guild)


async def handle_jobs_channel(message: discord.Message):
    """#jobs: every new link becomes a job on the board."""
    urls = [u for u in jobs.find_urls(message.content) if jobs.canonical(u) not in known_job_urls()]
    archived = [u for u in urls if job_archive.has(u)]
    for url in archived:                                      # already found: bring it to the board
        await promote_from_archive(message.guild, archive.entry_id(url))
    new = [u for u in urls if u not in archived]
    if not new:
        await message.add_reaction("💼" if archived else "🔁")   # 🔁: already on the board
        return
    async with message.channel.typing():
        await asyncio.sleep(2)                                # let Discord build the link preview
        try:
            message = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            pass
        for url in new:
            await import_job(message, url)
    await message.add_reaction("💼")
    await refresh_board(message.guild)


async def handle_bang(message: discord.Message, command: str, args: str):
    """!todo <task> [date] [ping], !remind <message> <time from now>, !schedule …, in any channel.
    Dates are read relative to when the message was sent. If it was sent while the bot was offline
    and its time has already passed, it's skipped without a reply."""
    now = message.created_at.astimezone(TZ)
    late = datetime.now(TZ) - now > LATE
    if command == "sig":
        if late:                                      # a status from then is gone; ask again
            return
        if getattr(message.channel, "name", "").lower() not in sig.channels():
            await message.reply("`!sig` works in " + ", ".join(f"#{c}" for c in sorted(sig.channels())) + ".",
                                mention_author=False, delete_after=15)
            return
        async with message.channel.typing():
            try:
                embed = sig.status_embed(await sig.fetch_status())
            except Exception as e:                    # never echo aiohttp's text: it has the secret URL
                why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
                await message.reply(f"Couldn't reach the SIG bot ({why}).", mention_author=False)
                return
        await message.reply(embed=embed, mention_author=False)
        return
    if command == "schedule":
        is_edit = re.match(r"(add|remove|delete|del|rm)\b", args.strip(), re.I)
        if late and now.date() < datetime.now(TZ).date() and not is_edit:   # an old day's view
            print(f"catch-up: skipped a !schedule view from {now:%m/%d}")
            return
        await schedule_command(message, args.strip(), now)
        return
    if command == "done":                             # "!done 3" = "done 3", from any channel
        if not message.guild or not re.search(r"\d", args):
            await message.reply("Usage: `!done 3` or `!done 3, 5` (numbers from the board).",
                                mention_author=False)
            return
        items, missing = await apply_command(message.guild, "done", args)
        note = f"There's no item {', '.join(f'#{n}' for n in missing)}." if missing else ""
        if message.channel == todo_channel(message.guild):
            if note:
                await message.channel.send(note, delete_after=15)
            await tidy_up(message)
            return
        if not items:
            # Nothing was checked off: the note and your !done both clear after 15 s.
            await message.reply(note, mention_author=False, delete_after=15)
            await message.delete(delay=15)            # needs Manage Messages; ignored otherwise
            return
        lines = [f"✅ Checked off `#{i['id']}` **{i['text']}**" for i in items] + ([note] if note else [])
        await message.reply("\n".join(lines), mention_author=False)
        await refresh_board(message.guild)
        return
    if command == "remind":
        res = todo.parse_remind_command(args, now)
        if isinstance(res, str):
            await message.reply(res, mention_author=False)
            return
        text, at = res
        if late and at <= datetime.now(TZ):
            print(f"catch-up: skipped !remind, its time passed: {text!r}")
            return
        add_item("reminder", text, message.guild, message.channel.id, at, [timedelta(0)])
        await message.reply(f"⏰ I'll remind you {todo.stamp(at, 'R')} ({todo.stamp(at, 't')}): {text}",
                            mention_author=False)
        return

    res = todo.parse_todo_command(args, now)
    if isinstance(res, str):
        await message.reply(res, mention_author=False)
        return
    if late and res[1] and res[1] <= datetime.now(TZ):
        print(f"catch-up: skipped !todo, its time passed: {res[0]!r}")
        return
    board = todo_channel(message.guild)
    if board is None:
        await message.reply(f"Create a #{TODO_CHANNEL} channel for the board first.", mention_author=False)
        return
    item = new_todo(message.guild, *res)
    if message.channel.id == board.id:
        await tidy_up(message)
        return
    pings = ", ".join("at the time" if o == timedelta(0) else f"{todo.fmt_offset(o)} before" for o in res[2])
    await message.reply(f"📋 Added to {board.mention}: {todo.label(item)}"
                        + (f" · 🔔 {pings}" if pings else ""), mention_author=False)
    await refresh_board(message.guild)


async def schedule_command(message: discord.Message, args: str, now: datetime):
    """!schedule [day|week|list] · !schedule add <name> <days> <start>-<end> · !schedule remove S2"""
    blocks = store.data.setdefault("schedule", [])
    items = guild_items(message.guild) if message.guild else []
    word, _, rest = args.partition(" ")
    word = word.lower()
    changed = False

    if word == "add":
        res = schedule.parse_add(rest)
        if isinstance(res, str):
            await message.reply(res, mention_author=False)
            return
        res["id"] = store.data.get("schedule_next_id", len(blocks) + 1)
        store.data["schedule_next_id"] = res["id"] + 1
        blocks.append(res)
        title, body, changed = "📅 Added", schedule.list_blocks([res]), True
    elif word in ("remove", "delete", "del", "rm"):
        ids = {int(n) for n in re.findall(r"\d+", rest)}
        gone = [b for b in blocks if b["id"] in ids]
        if not gone:
            await message.reply(f"No class {rest.strip() or '?'}. See `!schedule list` for the numbers.",
                                mention_author=False)
            return
        store.data["schedule"] = [b for b in blocks if b["id"] not in ids]
        title, body, changed = "📅 Removed", schedule.list_blocks(gone), True
    elif word in ("list", "all", "classes"):
        title, body = f"📅 Weekly classes (until {schedule.SEMESTER_END:%b} {schedule.SEMESTER_END.day})", \
            schedule.list_blocks(blocks)
    elif word == "week":
        title, body = "📅 Next 7 days", schedule.agenda(blocks, items, now)
    else:
        day = now.date() if not args else todo.parse_day(args, now.date())
        if day is None:
            await message.reply("Usage: `!schedule` (today) · `!schedule fri` / `9/30` / `week` / `list` · "
                                "`!schedule add CS 101 mon,wed 10am-11:15am` · `!schedule remove S2`",
                                mention_author=False)
            return
        title = schedule.day_header(day, now.date()).strip("*")
        body = "\n".join(schedule.day_lines(blocks, items, day, now)) or "*Nothing scheduled.*"

    if changed:
        store.save()
    embed = discord.Embed(title=title, description=body[:4000], color=discord.Color.teal())
    # React to the answer (🗑️ is there as a hint) to delete it and the !schedule message;
    # see on_raw_reaction_add.
    answer = await message.reply(embed=embed, mention_author=False)
    try:
        await answer.add_reaction("🗑️")
    except discord.HTTPException:
        pass
    if changed and message.guild:
        await refresh_board(message.guild)


async def chat_model_command(message: discord.Message, choice: str | None):
    if choice:
        store.data["chat_model"] = choice.lower()
        store.save()
        await message.reply(f"Chat now uses **{choice.lower()}**. To-dos and job links stay on "
                            f"{PARSE_MODEL}.", mention_author=False)
    else:
        await message.reply(f"Chat uses **{chat_model()}**. Switch with "
                            + " / ".join(f"`use {m}`" for m in MODELS) + ".", mention_author=False)


async def chat_pull(message: discord.Message, when: str | None, last: int | None, everyone: bool):
    """Handle "@Bot pull all pictures from today": like /collect with `when`/`last`,
    limited to the owner's own images unless they asked for everyone's."""
    async def send(text):
        await message.reply(text, mention_author=False)

    cid = message.channel.id
    if cid in sessions:
        await send(f"Already collecting here ({len(sessions[cid]['images'])} images so far). "
                   f"Use `/done` or `/cancel` first.")
        return
    if not when and not last:
        await send('From when? For example: "pull all my pictures from today", '
                   '"grab the last 10 pics", or "collect everyone\'s images since monday".')
        return
    session = {"topic": None, "user_id": None if everyone else message.author.id, "images": []}
    who = "" if everyone else f" from **{message.author.display_name}**"
    sessions[cid] = session
    async with message.channel.typing():
        await pull_history(message.channel, session, when, last, who, "", send)


@client.event
async def on_message(message: discord.Message):
    if message.guild:
        mark_seen(message)
    await handle_message(message)


async def handle_message(message: discord.Message, chat_batch: list | None = None):
    """Route one message. With `chat_batch` (catching up after downtime), chat messages are
    collected there instead of answered, so they can share one reply."""
    if message.author.bot or message.author.id != owner_id:
        return

    if bang := BANG_RE.match(message.content.strip()):
        print(f"#{getattr(message.channel, 'name', 'dm')} {message.content.strip()[:80]!r} -> !{bang.group(1).lower()}")
        await handle_bang(message, bang.group(1).lower(), bang.group(2))
        return
    name = getattr(message.channel, "name", None) if message.guild else None
    if name == TODO_CHANNEL:
        await handle_todo_channel(message)
        return
    if name == JOBS_CHANNEL and jobs.find_urls(message.content):
        await handle_jobs_channel(message)
        return

    images = [a for a in message.attachments if is_image(a)]

    # Image collection for an active /collect session.
    session = sessions.get(message.channel.id)
    if session is not None and images:
        took, skipped = await add_images(session, message)
        if took:
            await message.add_reaction("✅")
        if skipped:
            await message.reply(f"Limit is {MAX_COLLECT} images per deck; skipped {skipped}.",
                                mention_author=False)

    # Conversational chat: every message in #randobot, or @mentions anywhere else.
    if client.user not in message.mentions and name != CHAT_CHANNEL:
        return
    text = re.sub(rf"<@!?{client.user.id}>", "", message.content).strip()
    if not text and not images:
        return
    if not images:
        if text.lower() in HELP_WORDS:
            view = faq_view()
            view.message = await message.reply(embed=view.embed(), view=view, mention_author=False)
            return
        if m := MODEL_RE.fullmatch(text):
            await chat_model_command(message, m.group(1))
            return
        if pull := parse_pull(text):
            await chat_pull(message, *pull)
            return

    if chat_batch is not None:
        chat_batch.append((message, text, images))
        return
    await chat_reply(message, text, images)


async def chat_reply(message: discord.Message, text: str, images: list[discord.Attachment]):
    """Answer a chat message with Claude, continuing this channel's conversation."""
    blocks = []
    for att in images[:5]:
        try:
            norm = normalize_image(await att.read(), 1024, 80)
        except discord.HTTPException:
            norm = None
        if norm:
            blocks.append(image_block(norm[0]))
    blocks.append({"type": "text", "text": text or "(sent an image)"})

    # One message at a time per channel, so each reply continues the right session.
    lock = chat_locks.setdefault(message.channel.id, asyncio.Lock())
    async with lock, message.channel.typing():
        try:
            answer, sid = await claude_backend.ask(
                blocks, CHAT_PROMPT, resume=chat_sessions.get(message.channel.id),
                model=chat_model())
        except Exception as e:
            await message.reply(f"Error: {e}")
            return
        if sid:
            chat_sessions[message.channel.id] = sid

        # Things Claude tagged as worth tracking go on the board.
        tags = TODO_TAG_RE.findall(answer or "")
        answer = TODO_TAG_RE.sub("", answer or "").strip()
        board = todo_channel(message.guild)
        if tags and board:
            now = datetime.now(TZ)
            added = []
            for tag in tags[:5]:
                res = todo.parse_todo_command(tag, now)
                added.append(new_todo(message.guild, *res) if not isinstance(res, str)
                             else new_todo(message.guild, tag))
            answer += "\n\n" + "\n".join(f"📋 Added to {board.mention}: {todo.label(i)}" for i in added)
            await refresh_board(message.guild)

    for i, part in enumerate(chunk(answer or "(empty response)")):
        if i == 0:
            await message.reply(part, mention_author=False)
        else:
            await message.channel.send(part)


if __name__ == "__main__":
    client.run(os.environ["DISCORD_TOKEN"])
