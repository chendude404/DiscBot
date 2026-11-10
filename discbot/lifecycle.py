"""
Startup and the background loop: on_ready, catching up on messages sent while the bot was
offline, and the 20-second loop that sends pings and starts the daily and periodic jobs.
"""

import asyncio
from datetime import datetime, timedelta

import discord

import lms
from lib import gradescope, schedule, todo
from lib.jobs import archive

from . import board, chat, core, feeds, jobs, messages
from .core import (CHAT_CHANNEL, JOBS_CHANNEL, MAX_SCAN, TODO_CHANNEL, TZ, client, job_archive,
    store, tree)


background_started = False
catch_up_lock = asyncio.Lock()
CATCH_UP_FIRST = timedelta(hours=12)               # look-back when a channel has no last-seen mark yet


async def reminder_loop():
    """Every 20 s: send due pings. At midnight: move the agenda to the new day.
    Once a day at 4 AM: tidy and renumber the boards, and archive stale or closed jobs.
    Every few hours: pull new postings into the archive. Daily at 4 AM: Gradescope deadlines.
    Every 30 min, with LMS set and an #announcements channel: new course announcements."""
    last = datetime.now(TZ)
    while True:
        now = datetime.now(TZ)
        try:
            if now.time() >= todo.TIDY_AT and store.data.get("last_link_check") != now.date().isoformat():
                store.data["last_link_check"] = now.date().isoformat()
                asyncio.create_task(jobs.job_housekeeping())
            scraped = job_archive.data.get("last_scrape")
            if scraped is None or now - datetime.fromisoformat(scraped) > archive.SCRAPE_EVERY:
                job_archive.data["last_scrape"] = now.isoformat()  # set now so it isn't started twice
                asyncio.create_task(jobs.scrape_jobs())
            if (gradescope.configured() and now.time() >= todo.TIDY_AT
                    and store.data.get("last_gradescope") != now.date().isoformat()):
                store.data["last_gradescope"] = now.date().isoformat()  # once a day
                asyncio.create_task(feeds.sync_gradescope())
            polled = store.data.get("last_announcements")
            if (lms.configured() and feeds.announcement_channels()
                    and (polled is None or now - datetime.fromisoformat(polled) >= feeds.ANNOUNCE_EVERY)):
                store.data["last_announcements"] = now.isoformat()  # set now so it isn't started twice
                asyncio.create_task(feeds.sync_announcements())
            if todo.tidy_due(store, now):
                await board.delete_pings(store.items, older_than=timedelta(days=1))
                todo.tidy(store, now)
                for guild in client.guilds:
                    await board.refresh_board(guild)
            elif now.date() != last.date():
                for guild in client.guilds:
                    await board.refresh_board(guild)
            await board.check_reminders(last, now)
        except Exception as e:                                   # keep the loop alive
            print(f"reminder loop error: {e!r}")
        last = now
        await asyncio.sleep(20)


@client.event
async def on_ready():
    if core.owner_id is None:
        app = await client.application_info()
        core.owner_id = app.owner.id
    # Hide the commands from members without admin rights (the check above is the real lock).
    for cmd in tree.get_commands():
        cmd.default_permissions = discord.Permissions(administrator=True)
    await tree.sync()
    print(f"Logged in as {client.user}; responding only to user {core.owner_id}")
    global background_started
    if not background_started:                   # on_ready also fires after reconnects
        background_started = True
        if schedule.load_semester(store):
            print(f"Loaded {schedule.SEMESTER}: {len(store.data['schedule'])} classes")
        remind_at = schedule.NEXT_SEMESTER_REMINDER.replace(tzinfo=TZ)
        if store.data.get("next_semester_reminder") != schedule.SEMESTER and remind_at > datetime.now(TZ):
            store.data["next_semester_reminder"] = schedule.SEMESTER    # set it up once per semester
            for guild in client.guilds:
                if board_ch := board.todo_channel(guild):
                    board.add_item("reminder", "📚 Time to add next semester's classes: edit data/my_schedule.py "
                             "(or use `!schedule add`).", guild, board_ch.id, remind_at, [timedelta(0)])
        asyncio.create_task(reminder_loop())
        for guild in client.guilds:
            # Applied jobs not yet in #application-archive (e.g. from before it existed), oldest first.
            for item in sorted((i for i in board.guild_items(guild) if i["kind"] == "job" and i["done"]),
                               key=lambda i: i["done_at"] or ""):
                await jobs.log_application(item, guild)
            await board.refresh_board(guild)
        if n := job_archive.expire():                 # catch up on the 90/30-day rule
            job_archive.save()
            print(f"archive: {n} expired")
        jobs.forget_my_jobs()
        for guild in client.guilds:
            await jobs.refresh_archive(guild)
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
            or bool(board.BANG_RE.match(message.content.strip())) or client.user in message.mentions)


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
            missed = [m for m in history if m.author.id == core.owner_id and m.id not in answered
                      and not any(r.me for r in m.reactions) and wants_bot(m)]
            batch = []
            for m in missed:
                print(f"catch-up #{channel.name} {m.content[:80]!r}")
                try:
                    await messages.handle_message(m, chat_batch=batch)
                except Exception as e:                # one bad message shouldn't stop the rest
                    print(f"catch-up error: {e!r}")
            if batch:
                await chat.answer_missed_chats(batch)
            if history:
                mark_seen(history[-1])
            if missed:
                caught[channel] = len(missed)
        if caught:
            await board.refresh_board(guild)
            if chat_ch := discord.utils.get(guild.text_channels, name=CHAT_CHANNEL):
                await chat_ch.send("🔄 Back online. Caught up on what I missed: "
                                + ", ".join(f"{c.mention} ({n})" for c, n in caught.items()))
