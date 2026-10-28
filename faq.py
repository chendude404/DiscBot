"""
Interactive FAQ for the bot's commands: an embed with a topic dropdown and
Prev/Next buttons. bot.py exposes it as /help and as "@YourBot help".

Topic bodies may use {max_images}, {max_scan}, {timezone}, {model}, {parse_model},
{chat_channel}, {todo_channel} and {jobs_channel}; bot.py fills them in so the FAQ always
matches the running configuration.
"""

import discord
from discord import app_commands

# (key, label, emoji, one-line blurb shown in the dropdown, body)
TOPICS = [
    ("overview", "Overview", "📖", "What this bot does and who can use it",
     "This is a private bot backed by Claude. It can:\n"
     "• chat in **#{chat_channel}** (no @mention needed) or anywhere you @mention it\n"
     "• keep a live to-do board in **#{todo_channel}**, with reminders and a running total\n"
     "• add every link you post in **#{jobs_channel}** to the board's job list\n"
     "• answer one-off questions (`/ask`)\n"
     "• collect images posted in a channel and turn them into a PowerPoint deck "
     "(`/collect` → `/done`)\n\n"
     "Only the bot's owner can use it; everyone else is ignored.\n"
     "Use the menu below or **Prev / Next** to browse the commands."),

    ("ask", "/ask", "❓", "Ask Claude a one-off question",
     "**Usage:** `/ask question:<text>`\n\n"
     "Sends a single question to Claude and posts the answer. It has no memory: "
     "each `/ask` starts fresh. For a back-and-forth conversation, @mention the bot instead.\n\n"
     "**Example:** `/ask question: explain CIDR notation in two sentences`"),

    ("randobot", "Chat channel", "🤖", "Chat without @mentions, switch models",
     "Everything you type in **#{chat_channel}** goes to Claude; no @mention needed. It works "
     "like @mention chat (memory, images, `/reset`), plus:\n\n"
     "**To-dos from chat:** mention a test, deadline or event (\"ugh I have a test next "
     "tuesday\") and Claude adds it to the board in #{todo_channel} and tells you.\n\n"
     "**Models:** `use haiku` (fastest, lightest) · `use sonnet` · `use opus` (strongest, "
     "uses the most of your plan). `model` shows the current one: **{model}**. "
     "Job links are always read by {parse_model}.\n\n"
     "`!todo` and `!remind` work here too (see *To-do board*)."),

    ("board", "To-do board", "📋", "How #todo works: add, check off, totals",
     "**#{todo_channel}** holds one live board that the bot keeps at the bottom of the channel: "
     "**📅 Today** (classes and dated to-dos), **To-do**, **Jobs to apply to** and "
     "**Accomplished** (running totals). Jobs you mark applied move to a permanent log in "
     "**#{history_channel}**; `undo` takes them back out.\n\n"
     "**Add (any channel):** `!todo <task> [date] [ping]`\n"
     "`!todo chem test next tue` · `!todo finish resume fri 5pm 1h` · "
     "`!todo dentist 9/30 2pm 1d,30m` · `!todo standup 10am ping`\n"
     "Dates: `today` `tonight` `tomorrow` `fri` `next tue` `9/30` `sep 30` `in 3d`, optionally "
     "with a time (`5pm`, `14:30`, `noon`). Pings: how long before (`1h`, `30m`, `1d`, "
     "`1d,1h`), or `ping` for at the time. No ping unless you add one.\n"
     "Plain text typed in #{todo_channel} becomes a to-do with no date.\n\n"
     "**One-off reminder (any channel):** `!remind <message> <time from now>`, e.g. "
     "`!remind stretch 30m`, `!remind call mom in 2 hours`. It pings in that channel.\n\n"
     "Pings for to-dos go to #{todo_channel}; 🔔 marks items with a ping coming.\n\n"
     "**Update:** `done 3` (or `done 3, 5`) · `undo 3` · `remove 3`. For a job, `done` means applied. "
     "`!done 3` does the same from any channel.\n\n"
     "**Jobs applied this season** counts applications since July 1 and restarts every July 1; "
     "the all-time count keeps going.\n\n"
     "**Housekeeping (every day at 4 AM):** numbers restart from 1 so they stay short; "
     "finished items show for 7 days, then only count in **Accomplished**, which never goes "
     "down. Your typed messages and old pings are cleaned up so the channel stays tidy.\n\n"
     "`/todo board` re-posts the board · `/todo clear` hides finished items right away."),

    ("schedule", "Schedule", "📅", "Weekly classes, !schedule, the agenda",
     "Your weekly classes repeat until the semester ends and skip holidays and breaks. "
     "Together with dated to-dos they fill the board's **📅 Today** section.\n\n"
     "`!schedule` shows today · `!schedule fri` / `!schedule 9/30` shows that day · "
     "`!schedule week` shows 7 days · `!schedule list` shows every class with its number.\n\n"
     "`!schedule add <name> <days> <start>-<end>`: `!schedule add CS 101 mon,wed 10am-11:15am`, "
     "`!schedule add Chem lab TR 2-4:50pm` (days: `mon,wed`, `MWF`, `TR`, `TTh`, `sat`).\n"
     "`!schedule remove S2` deletes one.\n\n"
     "Classes never ping. The semester itself (classes, breaks, end date) lives in "
     "`my_schedule.py`; you'll get a ping in #{todo_channel} when it's time to set up the next one."),

    ("jobs", "Job links", "💼", "How #jobs feeds the board",
     "Every link you post in **#{jobs_channel}** is treated as a job posting. The bot reacts 💼 "
     "and adds it to **Jobs to apply to** in #{todo_channel} with the title, company, location, "
     "pay and deadline when it can tell (read from the link and Discord's preview). 🔁 means "
     "that link is already on the board.\n\n"
     "Jobs are sorted by deadline. Type `done <number>` in #{todo_channel} once you've applied.\n\n"
     "`/jobs import` adds links you posted before this feature existed (optionally only from "
     "a range, e.g. `when: this month`)."),

    ("chat", "@mention chat", "💬", "Talk to Claude with memory; attach images",
     "**Usage:** `@YourBot <message>`\n\n"
     "Starts or continues a conversation in this channel. Claude remembers earlier "
     "messages in the same channel until you run `/reset` or the bot restarts.\n\n"
     "• Attach up to 5 images and Claude will look at them.\n"
     "• Messages are answered one at a time per channel.\n"
     "• `@YourBot help` opens this FAQ.\n\n"
     "**Pulling images by asking:** instead of `/collect`, you can just say\n"
     "`@YourBot pull all pictures from today`\n"
     "`@YourBot grab the last 10 pics`\n"
     "`@YourBot collect everyone's images since monday`\n"
     "It collects only **your** images unless you say *everyone's*. Then run `/done` "
     "for the deck, or `/cancel`."),

    ("reset", "/reset", "🧹", "Clear this channel's chat memory",
     "**Usage:** `/reset`\n\n"
     "Forgets the @mention conversation for the current channel so the next message "
     "starts fresh. It doesn't affect `/collect` sessions or other channels."),

    ("collect", "/collect", "🖼️", "Start gathering images for a slide deck",
     "**Usage:** `/collect [topic] [when] [last] [from_user]`\n\n"
     "Starts collecting images in this channel for a deck. After that, every image you "
     "post here gets a ✅ and is added (up to {max_images}).\n\n"
     "• **topic**: what the deck is about. Optional; Claude infers it otherwise.\n"
     "• **when**: also pull images already in the chat history, e.g. `today` or "
     "`last 3 hours` (see *Time phrases*).\n"
     "• **last**: pull the N most recent images from the chat history, e.g. `last: 20`. "
     "Combine with **when** to take the newest N within that range.\n"
     "• **from_user**: only take images posted by this person.\n\n"
     "Finish with `/done`, or stop with `/cancel`. Only one collection per channel at a time."),

    ("when", "Time phrases", "🕒", "What the `when` option of /collect understands",
     "Times are read in **{timezone}**.\n\n"
     "**Recognized directly:**\n"
     "`today` · `yesterday` · `this week` · `last week` · `this month`\n"
     "`last 3 hours` · `past 30 minutes` · `last day` · `last 2 weeks`\n"
     "`since monday` · `since 9/15` · `on 9/20` · `2026-09-20`\n"
     "`9/18 to 9/20` · `between monday and wednesday`\n\n"
     "Anything else (e.g. `the weekend before last`) is handed to Claude to interpret. "
     "Up to {max_scan} past messages are scanned."),

    ("done", "/done", "📊", "Build the PowerPoint deck",
     "**Usage:** `/done`\n\n"
     "Ends the collection and builds the deck:\n"
     "1. Claude looks at thumbnails of every image and plans the slides "
     "(order, grouping, titles, captions, speaker notes).\n"
     "2. The bot renders a `.pptx`, shrinking images if needed to fit the server's upload limit.\n"
     "3. The file is posted in the channel.\n\n"
     "Images are ordered by when they were posted."),

    ("cancel", "/cancel", "✖️", "Stop collecting (or a history scan) without building anything",
     "**Usage:** `/cancel`\n\n"
     "Discards the images collected so far in this channel. Nothing is posted.\n\n"
     "Also stops a `/collect` that's still scanning the chat history."),

    ("trouble", "Troubleshooting", "🛠️", "Common errors and what to do",
     "**\"Claude isn't logged in\"**: run `claude setup-token` and put the token in "
     "`.env` as `CLAUDE_CODE_OAUTH_TOKEN`, then restart the bot.\n\n"
     "**\"usage limit is reached\"**: your Claude plan's limit was hit; wait for it to reset.\n\n"
     "**Slash commands don't show up**: they're hidden from non-admins, and Discord can take "
     "a minute to show them after the bot starts. Try reloading Discord (Ctrl+R).\n\n"
     "**/collect can't look back in time**: give the bot *Read Message History* in that channel.\n\n"
     "**Chat forgot the conversation**: memory is cleared by `/reset` and by restarting the bot.\n\n"
     "Current model: **{model}**."),
]

KEYS = [t[0] for t in TOPICS]
CHOICES = [app_commands.Choice(name=label, value=key) for key, label, *_ in TOPICS]


def index_of(key: str | None) -> int:
    return KEYS.index(key) if key in KEYS else 0


class FAQView(discord.ui.View):
    """Browsable FAQ. Only `owner_id` can operate the controls."""

    def __init__(self, owner_id: int, facts: dict, index: int = 0, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.facts = facts
        self.index = index
        self.message: discord.Message | None = None
        self._refresh()

    def embed(self) -> discord.Embed:
        key, label, emoji, _, body = TOPICS[self.index]
        e = discord.Embed(title=f"{emoji} {label}", description=body.format(**self.facts),
                          color=discord.Color.blurple())
        e.set_footer(text=f"Topic {self.index + 1} of {len(TOPICS)} · /help topic:<name> jumps straight to one")
        return e

    def _refresh(self):
        self.pick.options = [
            discord.SelectOption(label=label, value=key, emoji=emoji,
                                 description=blurb[:100], default=(i == self.index))
            for i, (key, label, emoji, blurb, _) in enumerate(TOPICS)
        ]
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index == len(TOPICS) - 1

    async def _show(self, interaction: discord.Interaction, index: int):
        self.index = index
        self._refresh()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This is a private bot.", ephemeral=True)
        return False

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    @discord.ui.select(placeholder="Jump to a topic…", row=0)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await self._show(interaction, index_of(select.values[0]))

    @discord.ui.button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, max(self.index - 1, 0))

    @discord.ui.button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show(interaction, min(self.index + 1, len(TOPICS) - 1))
