"""
Routes every message you send to the feature that handles it.
"""

import re

import discord

from lib.jobs import links

from . import board, chat, core, decks, jobs, lifecycle
from .core import (CHAT_CHANNEL, HELP_WORDS, JOBS_CHANNEL, TODO_CHANNEL, client)


@client.event
async def on_message(message: discord.Message):
    if message.guild:
        lifecycle.mark_seen(message)
        if message.channel == jobs.archive_channel(message.guild):
            jobs.bump_archive(message)
    await handle_message(message)


async def handle_message(message: discord.Message, chat_batch: list | None = None):
    """Route one message. With `chat_batch` (catching up after downtime), chat messages are
    collected there instead of answered, so they can share one reply."""
    if message.author.bot or message.author.id != core.owner_id:
        return

    if bang := board.BANG_RE.match(message.content.strip()):
        print(f"#{getattr(message.channel, 'name', 'dm')} {message.content.strip()[:80]!r} -> !{bang.group(1).lower()}")
        await board.handle_bang(message, bang.group(1).lower(), bang.group(2))
        return
    name = getattr(message.channel, "name", None) if message.guild else None
    if name == TODO_CHANNEL:
        await board.handle_todo_channel(message)
        return
    if name == JOBS_CHANNEL and links.find_urls(message.content):
        await jobs.handle_jobs_channel(message)
        return

    images = [a for a in message.attachments if decks.is_image(a)]

    # Image collection for an active /collect session.
    session = decks.sessions.get(message.channel.id)
    if session is not None and images:
        took, skipped = await decks.add_images(session, message)
        if took:
            await message.add_reaction("✅")
        if skipped:
            await message.reply(f"Limit is {decks.MAX_COLLECT} images per deck; skipped {skipped}.",
                                mention_author=False)

    # Conversational chat: every message in #randobot, or @mentions anywhere else.
    if client.user not in message.mentions and name != CHAT_CHANNEL:
        return
    text = re.sub(rf"<@!?{client.user.id}>", "", message.content).strip()
    if not text and not images:
        return
    if not images:
        if text.lower() in HELP_WORDS:
            view = chat.faq_view()
            view.message = await message.reply(embed=view.embed(), view=view, mention_author=False)
            return
        if m := chat.MODEL_RE.fullmatch(text):
            await chat.chat_model_command(message, m.group(1))
            return
        if pull := decks.parse_pull(text):
            await decks.chat_pull(message, *pull)
            return

    if chat_batch is not None:
        chat_batch.append((message, text, images))
        return
    await chat.chat_reply(message, text, images)
