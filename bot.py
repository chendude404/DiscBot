"""
Private Discord bot backed by your Claude subscription: chat, a to-do board, job tracking,
course feeds and image collection -> PowerPoint decks. Only the bot's owner (you) can use it;
everyone else is ignored.

Setup:
    pip install -r requirements.txt
    claude setup-token          # log in with your Claude Pro/Max plan, copy the token
    # .env file next to this script:
    #   DISCORD_TOKEN=...
    #   CLAUDE_CODE_OAUTH_TOKEN=...
    #   (do NOT set ANTHROPIC_API_KEY; that would bill the API instead of your plan)
    py bot.py                   # or scripts/start_bot.ps1 to run it hidden, restarting on crashes

Layout: bot.py starts the bot; discbot/ is the Discord side (one module per feature); lib/ is
plain-code helpers; lms/ is the Brightspace/Canvas feed; scripts/ runs it on Windows; data/
holds your data and the log (gitignored).

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

Gradescope (lib/gradescope.py, plain code): every day at 4 AM, assignments due within 5 days
are added to the #todo board under "Gradescope". Each is added once (remove it and it stays
gone) and its due date follows extensions. Submitting doesn't check it off: "done 3" does.

Announcements (lms/, plain code): every 30 minutes, new announcements from courses you're a
student in on your school's learning platform (LMS=brightspace or canvas in .env) are posted
to #announcements. One edited on the platform is edited in Discord too. Without LMS set and
an #announcements channel it never runs; `py -m lms` checks the setup.

Anywhere (parsed by code, no Claude):
    !todo <task> [date] [ping]      e.g. !todo finish resume fri 5pm 1h   (ping: 1h, 30m, 1d,1h,
                                    or "ping" for at the due time)
    !remind <message> <time from now>   e.g. !remind stretch 30m, !remind call mom in 2 hours
    !schedule [fri|9/30|week|list]      today's classes and dated to-dos (or that day / 7 days /
                                        all classes); !schedule add CS 101 mon,wed 10am-11:15am;
                                        !schedule remove S2. The semester lives in
                                        data/my_schedule.py.
    /todo board              re-post the board at the bottom of #todo
    /todo clear              hide finished items (they still count toward the totals)
    /jobs import [when]      add links already posted in #jobs to the board
    /jobs archive            point to the job archive in #application-archive and redraw it

Job archive (#application-archive): one always-open message with ranked postings in folders
(Promote / Applied / Reject), ⚙️ Filters, and the 📊 Applications tracker (OA → interview →
offer / rejected). See lib/jobs/archive.py and lib/jobs/stages.py.

Bot permissions needed: Send Messages, Attach Files, Add Reactions, Read Message History,
Embed Links.
Optional .env:
    BOT_TIMEZONE=America/Indianapolis   what "today" means (default shown)
    CLAUDE_MODEL=sonnet                 sonnet | haiku | opus
    OWNER_ID=123...                     who may use the bot (default: whoever owns the bot app)
    CHAT_CHANNEL=randobot  TODO_CHANNEL=todo  JOBS_CHANNEL=jobs   channel names (defaults shown)
    ANNOUNCEMENTS_CHANNEL=announcements
    LMS=brightspace | canvas            turns on the announcement feed (see lms/__init__.py)
    GRADESCOPE_EMAIL=...  GRADESCOPE_PASSWORD=...   turns on the Gradescope to-dos
To-dos, jobs and reminders are saved in data/data.json, the job archive in data/archive.json.
"""

import os

from dotenv import load_dotenv

from lib import ROOT

load_dotenv(ROOT / ".env")          # before the features read their settings

import discbot  # noqa: E402  (registers every event handler and command)

if __name__ == "__main__":
    discbot.core.client.run(os.environ["DISCORD_TOKEN"])
