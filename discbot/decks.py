"""
Slide decks from images: /collect, /done, /cancel, and "@Bot pull all pictures from today".
lib/deck.py plans and renders the .pptx.
"""

import asyncio
import base64
import io
import re
from datetime import datetime

import discord
from discord import app_commands

from lib.deck import build_deck_under, normalize_image, parse_plan, planning_prompt
from lib.timefilter import claude_prompt, describe, parse_claude_range, parse_when

from . import chat
from .core import (MAX_SCAN, TZ, tree)


MAX_COLLECT = 40                   # images per deck session
MAX_IMAGE_BYTES = 20 * 1024 * 1024 # ignore attachments bigger than this

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


# channel_id -> {"topic": str|None, "user_id": int|None,
#                "images": [{"id": int, "data": bytes, "author": str, "ts": datetime}]}
sessions: dict[int, dict] = {}


def is_image(att: discord.Attachment) -> bool:
    return (att.content_type or "").startswith("image/") and att.size <= MAX_IMAGE_BYTES


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg",
                   "data": base64.standard_b64encode(jpeg).decode()},
    }


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
        reply = await chat.ask_claude(claude_prompt(text, now),
                                 system="You convert time phrases to date ranges.")
    except Exception:
        return None
    return parse_claude_range(reply, now)


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
        reply = await chat.ask_claude(content, system="You design clear, well-ordered slide decks.")
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
