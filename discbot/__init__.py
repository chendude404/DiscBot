"""
The Discord side of the bot, one module per feature. Importing the package registers every
event handler and slash command on core.client / core.tree; bot.py then runs the client.

    core.py       settings, client, command tree, store, job archive (shared by all)
    lifecycle.py  on_ready, catch-up after downtime, the background loop
    messages.py   routes each message you send to its feature
    chat.py       Claude chat, /ask, /reset, /help
    decks.py      /collect, /done, /cancel: images -> PowerPoint
    board.py      #todo board, !todo !remind !done !schedule, reminder pings
    jobs.py       #jobs, job archive, applications
    feeds.py      Gradescope deadlines, Brightspace/Canvas announcements
"""

from . import core  # noqa: F401  (first: everything else builds on it)
from . import board, chat, decks, feeds, jobs, lifecycle, messages  # noqa: F401
