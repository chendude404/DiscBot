"""
Shared state for every feature: settings from .env, the Discord client and its owner-only
command tree, the to-do store (data/data.json) and the job archive (data/archive.json).
Imported first; the feature modules use these names directly. `owner_id` is filled in at
login, so read it as core.owner_id.
"""

import os
from datetime import timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from lib import claude_backend, todo
from lib.jobs import archive


TZ = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Indianapolis"))
MAX_SCAN = 5000                    # max history messages scanned per /collect
CHAT_CHANNEL = os.getenv("CHAT_CHANNEL", "randobot")
TODO_CHANNEL = os.getenv("TODO_CHANNEL", "todo")
JOBS_CHANNEL = os.getenv("JOBS_CHANNEL", "jobs")

# Holds both the always-open job archive and the one-line records of jobs you applied to.
ARCHIVE_CHANNEL = os.getenv("ARCHIVE_CHANNEL", "application-archive")
ANNOUNCE_CHANNEL = os.getenv("ANNOUNCEMENTS_CHANNEL", "announcements")
OLD_ARCHIVE_NAMES = ("application-history",)       # its name before the rename, still accepted
MODELS = ("haiku", "sonnet", "opus")
PARSE_MODEL = "haiku"              # reads job links (the only Claude call outside chat)
HELP_WORDS = {"help", "faq", "commands", "?"}
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
store = todo.Store()                               # to-dos, jobs, reminders (data.json)
job_archive = archive.Archive()                    # archived and scraped postings (archive.json)
LATE = timedelta(minutes=2)                        # older than this = it came due / was sent while offline


def chunk(text: str, size: int = 1990):
    for i in range(0, len(text), size):
        yield text[i : i + size]


def chat_model() -> str:
    return store.data.get("chat_model") or claude_backend.MODEL


async def try_delete(message: discord.Message) -> bool:
    """Delete a user's message; False without the Manage Messages permission."""
    try:
        await message.delete()
        return True
    except discord.HTTPException:
        return False
