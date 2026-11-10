"""
The to-do board in #todo: plain text and "done 3" / "undo 3" / "remove 3" there, !todo,
!remind, !done and !schedule anywhere, reminder pings, and /todo board|clear.
"""

import asyncio
import re
from datetime import datetime, timedelta

import discord
from discord import app_commands

from lib import faq, schedule, todo

from . import chat, core, jobs
from .core import (HELP_WORDS, LATE, TODO_CHANNEL, TZ, client, job_archive, store, tree,
    try_delete)


# "done 3", "done 3, 5", "remove #4", "undo 2" typed in #todo
TODO_CMD_RE = re.compile(r"(done|did|finished|complete[d]?|check|applied|x|remove|delete|del|rm|drop"
                         r"|undo|reopen|uncheck)\s+(#?\d+(?:\s*(?:,|and|&)?\s*#?\d+)*)", re.I)
BANG_RE = re.compile(r"!(todo|remind|schedule|done)\b\s*(.*)", re.I | re.S)
board_locks: dict[int, asyncio.Lock] = {}


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
    board_ch = todo_channel(guild)
    return add_item("todo", task.strip()[:200], guild, board_ch.id if board_ch else None, due, offsets)


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
        return f"⏰ <@{core.owner_id}> {item['text']}{late}"
    due = todo.dt(item["due"])
    when = "is due **now**" if due <= at + timedelta(minutes=1) else \
        f"is due {todo.stamp(due, 'R')} ({todo.stamp(due)})"
    j = item.get("job")
    name = f"Apply to **{j['title']}** at **{j['company']}** (<{j['url']}>)" if j else f"**{item['text']}**"
    return f"⏰ <@{core.owner_id}> `#{item['id']}` {name} {when}{late}"


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


todo_group = app_commands.Group(name="todo", description="The to-do board in #todo")


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


tree.add_command(todo_group)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Your reaction on a !schedule answer deletes the answer and the !schedule message.
    Works from the reply link alone, so it also works for answers from before a restart."""
    if payload.user_id != core.owner_id or payload.message_author_id != client.user.id:
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
        view = chat.faq_view(faq.index_of("board"))
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
            job_archive.handle(item["job"]["url"], item["job"].get("company"), item["job"].get("title"))
            job_archive.save()
        if verb in ("remove", "delete", "del", "rm", "drop"):
            await jobs.unlog_application(item)
            store.remove(item["id"])
        else:
            set_done(item, not undo)
            if item["kind"] == "job":
                await (jobs.unlog_application(item) if undo else jobs.log_application(item, guild))
    if not undo and any(i.get("job") for i in items) and jobs.forget_my_jobs():
        await jobs.refresh_archive(guild)
    return items, sorted(set(ids) - {i["id"] for i in items})


async def tidy_up(message: discord.Message):
    """After a change made from #todo: delete the typed message (the board now shows it) and
    refresh the board; without Manage Messages, react ✅ instead."""
    if await try_delete(message):
        await refresh_board(message.guild, deleted=message.id)
    else:
        await message.add_reaction("✅")
        await refresh_board(message.guild)


async def handle_bang(message: discord.Message, command: str, args: str):
    """!todo <task> [date] [ping], !remind <message> <time from now>, !schedule …, in any channel.
    Dates are read relative to when the message was sent. If it was sent while the bot was offline
    and its time has already passed, it's skipped without a reply."""
    now = message.created_at.astimezone(TZ)
    late = datetime.now(TZ) - now > LATE
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
    board_ch = todo_channel(message.guild)
    if board_ch is None:
        await message.reply(f"Create a #{TODO_CHANNEL} channel for the board first.", mention_author=False)
        return
    item = new_todo(message.guild, *res)
    if message.channel.id == board_ch.id:
        await tidy_up(message)
        return
    pings = ", ".join("at the time" if o == timedelta(0) else f"{todo.fmt_offset(o)} before" for o in res[2])
    await message.reply(f"📋 Added to {board_ch.mention}: {todo.label(item)}"
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
