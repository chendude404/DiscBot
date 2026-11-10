"""
Outside sources that feed the bot: Gradescope deadlines onto the board (daily) and course
announcements from Brightspace or Canvas into #announcements (every 30 minutes).
"""

from datetime import datetime, timedelta

import discord

import lms
from lib import gradescope

from . import board, core
from .core import (ANNOUNCE_CHANNEL, TZ, client, store)


GRADESCOPE_LEAD_DAYS = 5     # an assignment goes on the board this many days before its due date


async def sync_gradescope():
    """Daily at 4 AM: Gradescope assignments due within GRADESCOPE_LEAD_DAYS (by calendar day)
    become to-dos, submitted or not; you check them off yourself. "gradescope_seen" remembers
    every one added, so one you remove doesn't come back. Board items follow due-date changes."""
    try:
        found = await gradescope.term_assignments()
    except Exception as e:
        print(f"gradescope: {e!r}")
        return
    now = datetime.now(TZ)
    seen = store.data.setdefault("gradescope_seen", {})     # "guild:course:assignment" -> due
    touched, added = set(), 0
    for cid, course, a in found:
        key = f"{cid}:{a.assignment_id or a.name}"
        due = gradescope.due(a)
        due = due.astimezone(TZ) if due else None
        for guild in client.guilds:
            item = next((i for i in board.guild_items(guild) if i.get("gradescope") == key), None)
            if item:
                if due and not item["done"] and item["due"] != due.isoformat():
                    item["due"] = due.isoformat()            # extension or moved deadline
                    touched.add(guild)
                continue
            gkey = f"{guild.id}:{key}"
            board_ch = board.todo_channel(guild)
            if (gkey in seen or due is None or board_ch is None or due <= now
                    or (due.date() - now.date()).days > GRADESCOPE_LEAD_DAYS):
                continue
            board.add_item("todo", f"{gradescope.short_course(course)} · {a.name}"[:200], guild, board_ch.id,
                     due, category="Gradescope", gradescope=key)
            seen[gkey] = due.isoformat()
            touched.add(guild)
            added += 1
    for gkey, d in list(seen.items()):                      # forget long-past assignments
        if datetime.fromisoformat(d) < now - timedelta(days=60):
            del seen[gkey]
    store.save()
    print(f"gradescope: {len(found)} assignments checked, {added} added to the board")
    for guild in touched:
        await board.refresh_board(guild)


ANNOUNCE_EVERY = timedelta(minutes=30)
ANNOUNCE_BACKFILL = timedelta(days=3)      # the first poll also posts this much history
ANNOUNCE_WARN_AFTER = 3                    # failed polls in a row before saying so in the channel
ANNOUNCE_COLOR = 0xE0A526


def announcement_channels() -> list[discord.TextChannel]:
    return [c for g in client.guilds if (c := discord.utils.get(g.text_channels, name=ANNOUNCE_CHANNEL))]


def announcement_embed(a: lms.Announcement) -> discord.Embed:
    e = discord.Embed(title=a.title[:256], url=a.url or None, color=ANNOUNCE_COLOR, timestamp=a.posted,
                      description=a.body[:4000] + ("…" if len(a.body) > 4000 else ""))
    e.set_author(name=a.course.short)
    if a.attachments:
        e.add_field(name="📎 Attachments", value="\n".join(a.attachments)[:1024], inline=False)
    e.set_footer(text=lms.name() + (" · edited" if a.edited - a.posted > timedelta(minutes=10) else ""))
    return e


async def sync_announcements():
    """Every 30 min: post new announcements from your student courses (lms/, LMS in .env) in
    #announcements, oldest first. One edited on the platform is edited in Discord too. State
    lives in data.json under "announcements": when the last poll worked, which message is which."""
    channels = announcement_channels()
    state = store.data.setdefault("announcements", {"last_ok": None, "posted": {}, "fails": 0})
    now = datetime.now(TZ)
    since = (datetime.fromisoformat(state["last_ok"]) - timedelta(hours=1) if state["last_ok"]
             else now - ANNOUNCE_BACKFILL)
    try:
        async with lms.connect() as src:
            found = await src.announcements(since)
        new = edited = 0
        for a in found:
            seen, stamp = state["posted"].get(a.id), a.edited.isoformat()
            if seen and seen["mod"] == stamp:
                continue
            embed = announcement_embed(a)
            if seen:                                       # edited on the platform
                for ch_id, msg_id in seen["msgs"]:
                    if ch := client.get_channel(ch_id):
                        try:
                            await ch.get_partial_message(msg_id).edit(embed=embed)
                        except discord.HTTPException:
                            pass                           # deleted in Discord: leave it
                seen["mod"] = stamp
                edited += 1
            else:
                msgs = [[ch.id, (await ch.send(embed=embed)).id] for ch in channels]
                state["posted"][a.id] = {"mod": stamp, "msgs": msgs, "at": now.isoformat()}
                new += 1
    except Exception as e:
        state["fails"] += 1
        store.save()
        print(f"announcements: {e!r}")
        if state["fails"] == ANNOUNCE_WARN_AFTER:          # once per outage, not every poll
            for ch in channels:
                await ch.send(f"<@{core.owner_id}> ⚠️ The {lms.name()} feed has failed {state['fails']} times "
                              f"in a row and is paused until it works again. {lms.fix_hint()}\n"
                              f"-# {str(e)[:300]}")
        return
    state.update(last_ok=now.isoformat(), fails=0)
    for key, p in list(state["posted"].items()):          # forget announcements from long ago
        if datetime.fromisoformat(p["at"]) < now - timedelta(days=120):
            del state["posted"][key]
    store.save()
    print(f"announcements: {len(found)} checked, {new} posted, {edited} edited")
