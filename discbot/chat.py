"""
Chat with Claude: #randobot and @mentions (with per-channel memory), /ask, /reset, /help,
and switching the chat model. Things Claude tags as to-dos go on the board. With
LMS=brightspace, chat can also look up your courses, due dates and grades (read-only).
"""

import ast
import asyncio
import platform
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands

import lms
from lib import ROOT, claude_backend, faq, todo
from lib.deck import normalize_image
from lib.jobs import stages

from . import board, core, decks, jobs
from .core import (ANNOUNCE_CHANNEL, ARCHIVE_CHANNEL, CHAT_CHANNEL, JOBS_CHANNEL, MAX_SCAN, MODELS,
    PARSE_MODEL, TODO_CHANNEL, TZ, chat_model, chunk, store, tree)


def environment_note() -> str:
    """Where the bot runs and what it does, for the system prompt. Rebuilt at every start
    from the files' docstrings, so it stays current without hand-editing."""
    files = []
    for path in [ROOT / "bot.py", *sorted(p for d in ("discbot", "lib", "lms") for p in (ROOT / d).rglob("*.py"))]:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or "(no description)"
        summary = " ".join(doc.strip().split("\n\n")[0].split())   # first paragraph, one line
        files.append(f"- {path.relative_to(ROOT).as_posix()}: {summary}")
    entry = ast.get_docstring(ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))) or ""
    usage = entry.split("Commands:")[1].split("Bot permissions")[0].rstrip()
    return (f"\n\nYou run inside this Discord bot on the owner's {platform.system()} laptop "
            f"({platform.node()}); its code is in {ROOT}. Files:\n" + "\n".join(files) +
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


def lms_tools() -> dict:
    """claude_backend.ask kwargs giving chat read-only access to the LMS (Brightspace only),
    or {} when LMS isn't set to a platform that offers chat tools."""
    try:
        p = lms.platform()
    except lms.LMSError:
        return {}
    if p is None or not hasattr(p, "chat_tools"):
        return {}
    return {**p.chat_tools(), "rewrite_tool_output": localize_times}


# "2026-11-13T04:59:00.000Z" -> "Thu 2026-11-12 11:59 PM EST": Brightspace reports UTC, and
# models often slip a day converting it or naming the weekday, so code does it for them.
UTC_STAMP_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)Z\b")


def _local_stamp(m: re.Match) -> str:
    t = datetime.fromisoformat(m.group(1) + "+00:00").astimezone(TZ)
    return f"{t:%a %Y-%m-%d %I:%M %p} {t.tzname()}"


def localize_times(value):
    """Every UTC timestamp in a tool result (any nesting of str/list/dict) in local time."""
    if isinstance(value, str):
        return UTC_STAMP_RE.sub(_local_stamp, value)
    if isinstance(value, list):
        return [localize_times(v) for v in value]
    if isinstance(value, dict):
        return {k: localize_times(v) for k, v in value.items()}
    return value


LMS_TOOLS = lms_tools()
# The weakest model that answers LMS lookups (Haiku got due dates wrong in testing). With a
# weaker chat model, a message that turns out to need a lookup is answered by this one instead.
LMS_MODEL = "sonnet"
LMS_NOTE = (
    f" Exception to having no tools: in this chat you can look things up in the user's "
    f"{lms.name()} (their school's learning platform) with the mcp__{lms.name().lower()}__ "
    "tools, which are read-only: courses, assignments and due dates, calendar, announcements, "
    "grades, course content, syllabus and discussions. Use them when the user asks about their "
    "classes, and answer from what they return; don't use them otherwise. You can't submit, "
    "download or change anything there. If a tool says the sign-in expired, tell the user: "
    + lms.fix_hint() + " Dates and times in these tools' results are already in the user's "
    "time zone and include the weekday: show them as given, without converting. "
    "What you look up with these tools "
    "is not something the user told you about: never add [[todo: ...]] lines for it unless "
    "they ask you to put it on their to-do board."
) if LMS_TOOLS else ""


def lms_now() -> str:
    """Today and the next week with weekdays, appended per message: "what's due this week?"
    needs to know what today is, and models often get the weekday of a date wrong."""
    now = datetime.now(TZ)
    days = ", ".join(f"{now + timedelta(days=i):%a %m/%d}" for i in range(8))
    return f" It is now {now:%A %B %d, %Y %I:%M %p} ({TZ.key}). Next days: {days}."

# "use opus", "switch to haiku", "change model to sonnet", "model opus"; bare "model" shows it
MODEL_RE = re.compile(r"(?:(?:switch|change)(?: the)?(?: chat)? model to|switch to|use|model)\s+"
                      r"(haiku|sonnet|opus)(?: model)?[.!]?|(?:which |what )?model\??", re.I)

# channel_id -> Claude Code session id, so @mention chat remembers earlier messages
chat_sessions: dict[int, str] = {}
chat_locks: dict[int, asyncio.Lock] = {}


async def ask_claude(content, system: str = SYSTEM_PROMPT) -> str:
    """One-off request with no conversation memory."""
    text, _ = await claude_backend.ask(content, system)
    return text


def faq_view(index: int = 0) -> faq.FAQView:
    facts = {"max_images": decks.MAX_COLLECT, "max_scan": MAX_SCAN, "timezone": TZ.key,
             "model": chat_model(), "parse_model": PARSE_MODEL, "chat_channel": CHAT_CHANNEL,
             "todo_channel": TODO_CHANNEL, "jobs_channel": JOBS_CHANNEL,
             "archive_channel": ARCHIVE_CHANNEL, "ping_score": jobs.PING_SCORE,
             "announce_channel": ANNOUNCE_CHANNEL, "lms": lms.name(),
             "no_reply_days": stages.NO_REPLY_DAYS, "auto_reject_days": stages.AUTO_REJECT_DAYS}
    return faq.FAQView(core.owner_id, facts, index)


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


async def chat_model_command(message: discord.Message, choice: str | None):
    if choice:
        store.data["chat_model"] = choice.lower()
        store.save()
        await message.reply(f"Chat now uses **{choice.lower()}**. To-dos and job links stay on "
                            f"{PARSE_MODEL}.", mention_author=False)
    else:
        await message.reply(f"Chat uses **{chat_model()}**. Switch with "
                            + " / ".join(f"`use {m}`" for m in MODELS) + ".", mention_author=False)


async def chat_reply(message: discord.Message, text: str, images: list[discord.Attachment]):
    """Answer a chat message with Claude, continuing this channel's conversation."""
    blocks = []
    for att in images[:5]:
        try:
            norm = normalize_image(await att.read(), 1024, 80)
        except discord.HTTPException:
            norm = None
        if norm:
            blocks.append(decks.image_block(norm[0]))
    blocks.append({"type": "text", "text": text or "(sent an image)"})

    # One message at a time per channel, so each reply continues the right session.
    lock = chat_locks.setdefault(message.channel.id, asyncio.Lock())
    async with lock, message.channel.typing():
        system, model, tools = CHAT_PROMPT, chat_model(), LMS_TOOLS
        if LMS_TOOLS:
            system += LMS_NOTE + lms_now()
            if MODELS.index(model) < MODELS.index(LMS_MODEL):   # haiku: lookups go to sonnet
                tools = {**LMS_TOOLS, "tool_model": LMS_MODEL}
        try:
            answer, sid = await claude_backend.ask(
                blocks, system, resume=chat_sessions.get(message.channel.id),
                model=model, **tools)
        except Exception as e:
            await message.reply(f"Error: {e}")
            return
        if sid:
            chat_sessions[message.channel.id] = sid

        # Things Claude tagged as worth tracking go on the board.
        tags = TODO_TAG_RE.findall(answer or "")
        answer = TODO_TAG_RE.sub("", answer or "").strip()
        board_ch = board.todo_channel(message.guild)
        if tags and board_ch:
            now = datetime.now(TZ)
            added = []
            for tag in tags[:5]:
                res = todo.parse_todo_command(tag, now)
                added.append(board.new_todo(message.guild, *res) if not isinstance(res, str)
                             else board.new_todo(message.guild, tag))
            answer += "\n\n" + "\n".join(f"📋 Added to {board_ch.mention}: {todo.label(i)}" for i in added)
            await board.refresh_board(message.guild)

    for i, part in enumerate(chunk(answer or "(empty response)")):
        if i == 0:
            await message.reply(part, mention_author=False)
        else:
            await message.channel.send(part)
