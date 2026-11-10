"""
Job hunting: links in #jobs become board items, the always-open job archive and the
applications record in #application-archive, daily link checks and scraping, /jobs import|archive.
The plain-code parts live in lib/jobs/.
"""

import asyncio
from datetime import datetime, time, timedelta

import aiohttp
import discord
from discord import app_commands

from lib import claude_backend, todo
from lib.jobs import archive, links, stages

from . import board, core, decks
from .core import (ARCHIVE_CHANNEL, JOBS_CHANNEL, MAX_SCAN, OLD_ARCHIVE_NAMES, PARSE_MODEL,
    TODO_CHANNEL, TZ, client, job_archive, store, tree)


PING_SCORE = 6                                     # new scraped postings this good get you pinged


async def parse_with_claude(prompt: str, system: str) -> str:
    text, _ = await claude_backend.ask(prompt, system, model=PARSE_MODEL)
    return text


async def import_job(message: discord.Message, url: str) -> dict:
    """One link from #jobs -> a job on the board. Haiku reads the URL and Discord's link preview."""
    preview = next((e for e in message.embeds if e.url and (e.url in url or url in e.url)), None)
    if preview is None and len(links.find_urls(message.content)) == 1 and message.embeds:
        preview = message.embeds[0]
    note = links.URL_RE.sub("", message.content).strip()[:300]
    try:
        info = links.parse_job(await parse_with_claude(
            links.job_prompt(url, note, links.preview_text(preview) if preview else ""),
            "You extract job posting details."))
    except Exception:
        info = None
    info = info or links.fallback(url)
    due = None
    if info["deadline"]:
        try:
            due = datetime.combine(datetime.fromisoformat(info["deadline"]).date(), time(23, 59), TZ)
        except ValueError:
            pass
    board_ch = board.todo_channel(message.guild)
    return board.add_item("job", f"{info['title'] or 'Untitled role'} @ {info['company'] or '?'}",
                    message.guild, board_ch.id if board_ch else None, due, category="Jobs",
                    job={**info, "url": url, "posted": message.jump_url},
                    created=message.created_at.astimezone(TZ).isoformat())


def known_job_urls() -> set[str]:
    """Links already on the board, in jobs.canonical form."""
    return {links.canonical(i["job"]["url"]) for i in store.items if i["kind"] == "job" and i.get("job")}


def my_jobs() -> archive.Mine:
    """Jobs on the board or applied to, to spot the same opportunity under another link."""
    return archive.Mine([(i["job"].get("company"), i["job"].get("title"))
                         for i in store.items if i["kind"] == "job" and i.get("job")])


def forget_my_jobs() -> int:
    """Delete archive postings that are a job you already have; returns how many."""
    if n := job_archive.drop_mine(my_jobs()):
        job_archive.save()
        print(f"archive: dropped {n} postings you already have")
    return n


def archive_channel(guild: discord.Guild) -> discord.TextChannel | None:
    for name in (ARCHIVE_CHANNEL, *OLD_ARCHIVE_NAMES):
        if channel := discord.utils.get(guild.text_channels, name=name):
            return channel
    return None


def application_record(item: dict, guild: discord.Guild) -> str:
    """The one-line record of an applied job in #application-archive, ending with its stages
    (📝 OA 10/30 → 🎤 Interview 11/06) once it has any."""
    j = item.get("job") or {}
    bits = [j.get("company"), j.get("location"), j.get("work_mode"), j.get("pay")]
    if "season_n" not in item:
        # Its number in the season: applications up to and including this one.
        earlier = [i for i in board.guild_items(guild) if (i["done_at"] or "") <= item["done_at"]]
        item["season_n"] = todo.season_jobs(earlier, todo.dt(item["done_at"]), store.pruned(guild.id))
    trail = stages.trail(item)
    return (f"{stages.STAGES[stages.current(item)][0] if stages.current(item) else '✅'} "
            f"**{j.get('title') or 'Untitled role'}**"
            + "".join(f" · {b}" for b in bits if b)
            + f" · applied {todo.stamp(todo.dt(item['done_at']), 'D')}"
            + (f" · [posting](<{j['url']}>)" if j.get("url") else "")
            + f" · #{item['season_n']} this season"
            + (f" · {trail}" if trail else ""))


async def log_application(item: dict, guild: discord.Guild):
    """A job was marked applied: post a permanent one-line record in #application-archive."""
    channel = archive_channel(guild)
    if channel is None or item.get("history_msg"):
        return
    try:
        msg = await channel.send(application_record(item, guild))
    except discord.HTTPException:
        return
    item["history_msg"] = [channel.id, msg.id]
    store.save()


async def update_record(item: dict, guild: discord.Guild):
    """Show an application's current stage on its record in #application-archive."""
    if ref := item.get("history_msg"):
        if channel := client.get_channel(ref[0]):
            try:
                await channel.get_partial_message(ref[1]).edit(content=application_record(item, guild))
            except discord.HTTPException:
                pass                                          # record deleted: the stage still counts


async def set_application_stage(guild: discord.Guild, key: str, stage: str) -> str | None:
    """From the 📊 Applications screen: toggle an application's stage (see stages.toggle) and
    update its record in #application-archive. Returns a short confirmation."""
    item = next((i for i in stages.applications(board.guild_items(guild), datetime.now(TZ))
                 if archive.app_key(i) == key), None)
    if item is None:
        return None
    now_at = stages.toggle(item, stage, datetime.now(TZ))
    store.save()
    await update_record(item, guild)
    title = item["job"].get("title") or "Untitled role"
    if now_at == stage:
        emoji, name = stages.STAGES[stage]
        return f"{emoji} {title} → {name}"
    return f"↩️ {title}: took back {stages.STAGES[stage][1]}"


async def unlog_application(item: dict):
    """Undo of an applied job: take its record back out of #application-archive."""
    if ref := item.pop("history_msg", None):
        if channel := client.get_channel(ref[0]):
            try:
                await channel.get_partial_message(ref[1]).delete()
            except discord.HTTPException:
                pass
        store.save()


def board_jobs() -> list[dict]:
    """Jobs on the board: posted (or promoted) and not applied to yet."""
    return [i for i in store.items if i["kind"] == "job" and not i["done"] and i.get("job")]


async def job_housekeeping():
    """Once a day. Board jobs you haven't applied to within archive.BOARD_DAYS move to the
    archive. Then open jobs whose posting was taken down move there too, marked 🔒: a 404/410
    at once, a weaker sign only if it shows up two days running. Applications that have been
    quiet for stages.AUTO_REJECT_DAYS move to ❌ Rejected."""
    now, moved = datetime.now(TZ), 0
    for item in board_jobs():
        since = datetime.fromisoformat(item["job"].get("promoted") or item["created"])
        if now - since > timedelta(days=archive.BOARD_DAYS):
            store.items.remove(item)
            job_archive.add_board_job(item)
            moved += 1
    async with aiohttp.ClientSession(headers=links.BROWSER) as session:
        for item in board_jobs():
            j = item["job"]
            if j.get("keep_open"):
                continue
            status = await links.link_status(session, j["url"])
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
        rejected = stages.auto_reject(stages.applications(board.guild_items(guild), now), now)
        if rejected:
            store.save()
            print(f"auto-rejected {len(rejected)} quiet applications")
        for item in rejected:
            await update_record(item, guild)
        if moved:
            await board.refresh_board(guild)
        await refresh_archive(guild)


async def scrape_jobs():
    """Every few hours: new Summer 2027 postings from the Simplify / Pitt CSC list go straight
    into the archive, and expired ones are deleted. The only message is a ping when a new
    posting scores PING_SCORE or more."""
    try:
        res = await archive.scrape_simplify(job_archive, known_job_urls(), my_jobs())
        expired = job_archive.expire()
        job_archive.save()
        print(("simplify: unchanged" if res is None else f"simplify: +{len(res[0])} new, "
               f"-{res[1]} no longer listed") + f", {expired} expired")
        if forget_my_jobs() or res or expired:
            for guild in client.guilds:
                await refresh_archive(guild)
        if res:
            await ping_strong_matches(set(res[0]))
    except Exception as e:
        print(f"simplify scrape error: {e!r}")


async def ping_strong_matches(new_ids: set[str]):
    """One message mentioning you, listing the best new postings; it replaces the last one."""
    strong = [r for r in job_archive.shown() if r[0] in new_ids and r[2] >= PING_SCORE]
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
        sent = await channel.send(f"<@{core.owner_id}> 🔔 {len(strong)} strong new match"
                                  f"{'es' if len(strong) > 1 else ''}:\n" + "\n".join(lines) + more
                                  + "\n-# Promote them from the archive above (🆕 folder).",
                                  suppress_embeds=True)
        job_archive.data["ping"] = [channel.id, sent.id]
        job_archive.save()


archive_lock = asyncio.Lock()
archive_bumps: dict[int, asyncio.Task] = {}
BUMP_DELAY = 3                                     # seconds: several new records share one move


def bump_archive(message: discord.Message):
    """Something new was posted in #application-archive: move the archive message back below it
    in a few seconds (one move for a burst of records)."""
    ref = job_archive.data.get("message")
    if ref and message.id == ref[1]:
        return                                        # the archive message itself
    task = archive_bumps.get(message.guild.id)
    if task and not task.done():
        return

    async def later():
        await asyncio.sleep(BUMP_DELAY)
        await refresh_archive(message.guild)
    archive_bumps[message.guild.id] = asyncio.create_task(later())


async def refresh_archive(guild: discord.Guild, note: str = ""):
    """Redraw the always-open archive message in #application-archive. Edited in place while it's
    the newest message there; otherwise posted again at the bottom (the old one is deleted), like
    the #todo board. It keeps showing the folder and page you left it on."""
    async with archive_lock:
        channel = archive_channel(guild)
        if channel is None:
            print(f"no #{ARCHIVE_CHANNEL} channel for the job archive")
            return
        def take(eid: str, applied: bool):
            return promote_from_archive(guild, eid, redraw=False, applied=applied)

        def apps() -> list[dict]:
            return stages.applications(board.guild_items(guild), datetime.now(TZ))

        def stage(key: str, to: str):
            return set_application_stage(guild, key, to)
        view = archive.ArchiveView(job_archive, take, core.owner_id, note, apps, stage)
        ref = job_archive.data.get("message")
        here = ref and ref[0] == channel.id
        if here and channel.last_message_id == ref[1]:
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
        if here:
            try:
                await channel.get_partial_message(ref[1]).delete()
            except discord.HTTPException:
                pass


async def promote_from_archive(guild: discord.Guild, eid: str, redraw: bool = True,
                               applied: bool = False) -> str | None:
    """Move an archived posting to the board (back as the same item if it came from there), or
    with `applied`, straight to your applied records in #application-archive without showing it
    on the board. `redraw`: also redraw the archive message (its own buttons redraw it themselves)."""
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
        board_ch = board.todo_channel(guild)
        item = board.add_item("job", f"{e['title'] or 'Untitled role'} @ {e['company'] or '?'}", guild,
                        board_ch.id if board_ch else None, category="Jobs",
                        job={"title": e["title"], "company": e["company"], "location": e["location"],
                             "work_mode": None, "deadline": None, "pay": None, "url": e["url"],
                             "posted": None, "source": e["source"]})
    item["job"]["promoted"] = datetime.now(TZ).isoformat()   # its 7 days start over
    if applied:
        board.set_done(item)
        await log_application(item, guild)
    store.save()
    job_archive.handle(e["url"], e.get("company"), e.get("title"))
    job_archive.save()
    await board.refresh_board(guild)
    note = (f"✅ Marked applied: {todo.label(item)}" if applied
            else f"⬆️ Moved to the board: {todo.label(item)}")
    if redraw:
        await refresh_archive(guild, note)
    return note


jobs_group = app_commands.Group(name="jobs", description="Job links from #jobs")


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
        rng = await decks.resolve_when(when)
        if rng is None:
            await interaction.followup.send(f'I couldn\'t read "{when}" as a time range.')
            return
        start, end = rng

    known, found = known_job_urls(), []
    async for msg in channel.history(after=start, before=end, oldest_first=True, limit=MAX_SCAN):
        if msg.author.id == core.owner_id:
            for url in links.find_urls(msg.content):
                if links.canonical(url) not in known and not job_archive.has(url):
                    known.add(links.canonical(url))
                    found.append((msg, url))
    if not found:
        await interaction.followup.send("No new job links found; everything is already on the board.")
        return
    await interaction.followup.send(f"Adding {len(found)} job links (a few seconds each)…")
    for msg, url in found:
        await import_job(msg, url)
    await board.refresh_board(guild)
    board_ch = board.todo_channel(guild)
    await interaction.followup.send(f"Added {len(found)} jobs to {board_ch.mention if board_ch else 'the board'}. "
                                    f'Mark ones you already applied to with "done <number>" in #{TODO_CHANNEL}.')


@jobs_group.command(name="archive", description="Point to the job archive in #application-archive (and redraw it)")
async def jobs_archive(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message("Use this in the server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await refresh_archive(interaction.guild)
    channel = archive_channel(interaction.guild)
    await interaction.followup.send(f"The archive is at the bottom of {channel.mention}." if channel
                                    else f"There's no #{ARCHIVE_CHANNEL} channel for it.", ephemeral=True)


tree.add_command(jobs_group)


async def handle_jobs_channel(message: discord.Message):
    """#jobs: every new link becomes a job on the board."""
    urls = [u for u in links.find_urls(message.content) if links.canonical(u) not in known_job_urls()]
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
    await board.refresh_board(message.guild)
    if forget_my_jobs():                                      # the same job from the archive
        await refresh_archive(message.guild)
