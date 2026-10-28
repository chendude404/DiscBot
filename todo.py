"""
To-dos, job applications and reminders: storage (data.json), parsing of the !todo and
!remind commands (plain code, no Claude), and the board shown in #todo. bot.py wires these into Discord.

Item kinds:
    todo      a task on the board; optional due time and pings the user asked for
    job       a job link posted in #jobs; "done" means applied
    reminder  a one-off ping ("!remind stretch 30m"); never shown on the board
Daily tidy-up (tidy()): finished items leave the board after SHOW_DAYS, are deleted after
KEEP_DAYS (their count is kept in "pruned", so the running totals never drop), and everything
is renumbered from 1 so the numbers you type stay small. Jobs applied to this season are kept
until the season ends, so the 📊 Applications tracker (stages.py) can follow them.
"""

import json
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path

import discord

DATA_FILE = Path(__file__).with_name("data.json")
TIDY_AT = time(4, 0)          # daily renumbering, when you're unlikely to be mid-"done 3"
SHOW_DAYS = 7                 # finished items stay visible (Applied / Recent) this long
KEEP_DAYS = 35                # then are deleted from data.json; > a month so "This month" is right
APPLIED_SHOWN = 10
HIDDEN_IDS = 1001             # tidy() numbers finished items and reminders from here
SEASON_START = (7, 1)         # the job-application count restarts every July 1


# ---------------------------------------------------------------- storage

class Store:
    """Everything in one JSON file, rewritten atomically on every change."""

    def __init__(self, path: Path = DATA_FILE):
        self.path = path
        self.data = {"next_id": 1, "items": [], "boards": {}, "chat_model": None,
                     "pruned": {}, "last_tidy": None}     # pruned: guild id -> {kind: count}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def items(self) -> list[dict]:
        return self.data["items"]

    def add(self, kind: str, text: str, **fields) -> dict:
        item = {"id": self.data["next_id"], "kind": kind, "text": text, "category": None,
                "due": None, "remind_at": [], "fired": [], "done": False, "done_at": None,
                "archived": False, "created": datetime.now().astimezone().isoformat(), **fields}
        self.data["next_id"] += 1
        self.items.append(item)
        self.save()
        return item

    def get(self, item_id: int) -> dict | None:
        return next((i for i in self.items if i["id"] == item_id), None)

    def remove(self, item_id: int):
        self.data["items"] = [i for i in self.items if i["id"] != item_id]
        self.save()

    def pruned(self, guild_id) -> dict:
        return self.data["pruned"].setdefault(str(guild_id), {"todo": 0, "job": 0})

    def board_messages(self, channel_id: int) -> list[int]:
        return self.data["boards"].get(str(channel_id)) or []

    def set_board_messages(self, channel_id: int, ids: list[int]):
        self.data["boards"][str(channel_id)] = ids
        self.save()


def dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def stamp(d: datetime, style: str = "f") -> str:
    """Discord timestamp, shown in each viewer's own timezone ("R" = relative, "d" = date)."""
    return f"<t:{int(d.timestamp())}:{style}>"


def season_start(now: datetime) -> datetime:
    """Start of the current job-application season (the most recent July 1)."""
    month, day = SEASON_START
    start = datetime(now.year, month, day, tzinfo=now.tzinfo)
    return start if now >= start else start.replace(year=now.year - 1)


def tidy_due(store: Store, now: datetime) -> bool:
    """True once a day, at or after TIDY_AT (or at startup if that time was missed)."""
    today_mark = datetime.combine(now.date(), TIDY_AT, now.tzinfo)
    last = dt(store.data["last_tidy"])
    return now >= today_mark and (last is None or last < today_mark)


def tidy(store: Store, now: datetime):
    """Daily housekeeping: hide week-old finished items, delete month-old ones (keeping their
    count), then renumber the board from 1 (finished items and reminders from HIDDEN_IDS)."""
    season = season_start(now)
    keep = []
    for item in store.items:
        done_at = dt(item["done_at"]) if item["done"] else None
        this_season_app = item["kind"] == "job" and done_at and done_at >= season
        if done_at and now - done_at > timedelta(days=KEEP_DAYS) and not this_season_app:
            pruned = store.pruned(item.get("guild_id"))
            pruned[item["kind"]] += 1
            if item["kind"] == "job" and done_at >= season:
                if pruned.get("season") != season.date().isoformat():
                    pruned["season"], pruned["season_jobs"] = season.date().isoformat(), 0
                pruned["season_jobs"] += 1
            continue
        if done_at and now - done_at > timedelta(days=SHOW_DAYS):
            item["archived"] = True
        keep.append(item)

    # What's on the board gets 1, 2, 3… in board order, and new items continue from there.
    # Finished items and reminders aren't on the board, so they move out of the way.
    todos, jobs = open_items(keep)
    shown = todos + jobs
    for n, item in enumerate(shown, 1):
        item["id"] = n
    hidden = sorted((i for i in keep if not any(i is s for s in shown)), key=lambda i: i["id"])
    for n, item in enumerate(hidden, HIDDEN_IDS):
        item["id"] = n
    store.data["items"] = shown + hidden
    store.data["next_id"] = len(shown) + 1
    store.data["last_tidy"] = now.isoformat()
    store.save()


def reminder_times(due: datetime, offsets: list[timedelta], now: datetime) -> list[str]:
    """When to ping for something due at `due`. Offsets already in the past are dropped; if all
    of them are, ping at the due time itself."""
    times = sorted({due - o for o in offsets if due - o > now})
    if not times and offsets and due > now:
        times = [due]
    return [t.isoformat() for t in times]


# ---------------------------------------------------------------- !todo / !remind parsing (no Claude)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
UNITS = r"(weeks?|wks?|w|days?|d|hours?|hrs?|h|minutes?|mins?|m)"
DURATION_RE = re.compile(rf"(?:\d+\s*{UNITS}\s*(?:and\s*)?)+", re.I)     # "1h30m", "2 hours", "90 min"
UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60}
CLOCK_RE = re.compile(r"(?:\bat\s+)?\b(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm)|\d{1,2}:\d{2})(?![\w/])", re.I)
DEFAULT_TIME = time(9, 0)                   # a date with no time of day means 9:00 AM


def parse_duration(s: str) -> timedelta | None:
    if not DURATION_RE.fullmatch(s.strip()):
        return None
    secs = sum(int(n) * UNIT_SECONDS[u[0].lower()] for n, u in re.findall(rf"(\d+)\s*{UNITS}", s, re.I))
    return timedelta(seconds=secs)


def parse_clock(s: str) -> time | None:
    s = s.strip().lower()
    if s in ("noon", "midnight"):
        return time(12 if s == "noon" else 0)
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if not m or not (m.group(2) or m.group(3)):         # a bare "3" is too ambiguous
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m.group(3) == "pm" else 0)
    return time(h, mi) if h < 24 and mi < 60 else None


def parse_day(s: str, today) -> "date | None":
    s = s.strip().lower()
    if s in ("today", "tonight"):
        return today
    if s in ("tomorrow", "tmrw", "tmr"):
        return today + timedelta(days=1)
    if m := re.fullmatch(r"(next |this )?([a-z]+)", s):
        for i, name in enumerate(WEEKDAYS):
            if m.group(2) in (name, name[:3], name[:4]):
                ahead = (i - today.weekday()) % 7
                return today + timedelta(days=7 if ahead == 0 and m.group(1) == "next " else ahead)
        if m.group(1) is None:
            return None
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s):
        mo, d = int(m.group(1)), int(m.group(2))
        y = int(m.group(3) or today.year)
        y += 2000 if y < 100 else 0
    elif (m := re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?", s)) and m.group(1) in MONTHS:
        mo, d, y = MONTHS.index(m.group(1)) + 1, int(m.group(2)), today.year
    else:
        return None
    try:
        return today.replace(year=y, month=mo, day=d)
    except ValueError:
        return None


def parse_point(text: str, now: datetime) -> datetime | None:
    """"fri 5pm", "tomorrow", "9/30 2:30pm", "sep 30", "next tue", "3pm", "in 2h" -> datetime."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    if t.startswith("in ") and (d := parse_duration(t[3:])):
        return now + d
    clock = None
    if cm := CLOCK_RE.search(t):
        if (clock := parse_clock(cm.group(1))) is None:
            return None
        t = (t[:cm.start()] + " " + t[cm.end():]).strip()
    t = re.sub(r"^(?:on|at) |,$", "", t).strip()
    if not t:                                   # time only: the next time that clock comes up
        if clock is None:
            return None
        when = datetime.combine(now.date(), clock, now.tzinfo)
        return when if when > now else when + timedelta(days=1)
    day = parse_day(t, now.date())
    if day is None:
        return None
    when = datetime.combine(day, clock or (time(20) if t == "tonight" else DEFAULT_TIME), now.tzinfo)
    if when <= now and day == now.date() and t not in ("today", "tonight"):
        when += timedelta(days=7)               # "fri 9am" said on Friday afternoon
    return when


def fmt_offset(td: timedelta) -> str:
    """timedelta(hours=1, minutes=30) -> "1h 30m"."""
    mins, out = int(td.total_seconds() // 60), []
    for size, unit in ((10080, "w"), (1440, "d"), (60, "h"), (1, "m")):
        n, mins = divmod(mins, size)
        if n:
            out.append(f"{n}{unit}")
    return " ".join(out) or "0m"


def parse_pings(token: str) -> list[timedelta] | None:
    """"ping" (at the due time), "1h", "30m", "1d,1h" (several) -> offsets before the due time."""
    if token.lower() == "ping":
        return [timedelta(0)]
    parts = [parse_duration(p) for p in token.split(",")]
    return None if None in parts else sorted(set(parts), reverse=True)


def parse_todo_command(args: str, now: datetime) -> tuple[str, datetime | None, list[timedelta]] | str:
    """"!todo <task> [date] [ping]", read from the end. Returns (task, due, ping offsets) or an error."""
    words = args.split()
    offsets: list[timedelta] = []
    if len(words) > 1 and words[-2].lower() != "in" and (p := parse_pings(words[-1])) is not None:
        offsets = p
        words.pop()
        if len(words) > 1 and words[-1].lower() == "ping":          # "... fri 5pm ping 1h"
            words.pop()
    due = None
    for n in range(min(5, len(words) - 1), 0, -1):                  # longest date at the end
        if due := parse_point(" ".join(words[-n:]), now):
            del words[-n:]
            break
    while len(words) > 1 and words[-1].lower() in ("at", "on", "by", "due", "for", "before"):
        words.pop()
    if not words:
        return "Usage: `!todo <task> [date] [ping]`, e.g. `!todo finish resume fri 5pm 1h`"
    if offsets and due is None:
        return ("A ping needs a date to count back from, e.g. `!todo stretch today 5pm 30m`. "
                "For a ping some time from now, use `!remind stretch 30m`.")
    return " ".join(words)[:200], due, offsets


def parse_remind_command(args: str, now: datetime) -> tuple[str, datetime] | str:
    """"!remind <message> <time from now>" -> (message, when), or an error."""
    m = re.fullmatch(rf"(.+?)\s+(?:in\s+)?({DURATION_RE.pattern})", args.strip(), re.I)
    d = parse_duration(m.group(2)) if m else None
    if not d:
        return "Usage: `!remind <message> <time from now>`, e.g. `!remind stretch 30m` or `!remind call mom 2h`"
    return m.group(1).strip()[:300], now + d


# ---------------------------------------------------------------- the board

MAX_DESC = 4000          # per embed (Discord allows 4096)
MAX_MESSAGE = 5800       # all embeds in one message (Discord allows 6000)


def _due_key(item: dict):
    d = dt(item["due"])
    return (d.timestamp() if d else float("inf"), item["id"])


def _fit(lines: list[str], empty: str) -> str:
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) + 1 > MAX_DESC - 40:
            out.append(f"*…and {len(lines) - i} more*")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out) or empty


def _due_text(item: dict, now: datetime, label: str = "due") -> str:
    d = dt(item["due"])
    if not d:
        return ""
    warn = " ⚠️ **overdue**" if d < now else ""
    return f" · {label} {stamp(d)} ({stamp(d, 'R')}){warn}"


def label(item: dict) -> str:
    """Short name for confirmations: "#3 Finish resume · due <date>"."""
    d = dt(item["due"])
    return f"`#{item['id']}` **{item['text']}**" + (f" · due {stamp(d)}" if d else "")


def todo_line(item: dict, now: datetime) -> str:
    bell = " 🔔" if any(r not in item["fired"] for r in item["remind_at"]) else ""
    return f"`#{item['id']}` {item['text']}{_due_text(item, now)}{bell}"


def job_line(item: dict, now: datetime) -> str:
    j = item.get("job") or {}
    title = j.get("title") or "Untitled role"
    if item["done"]:
        return f"`#{item['id']}` ~~{title}~~ · {j.get('company') or '?'} · applied {stamp(dt(item['done_at']), 'd')}"
    bits = [f"**{j['company']}**" if j.get("company") else None, j.get("location"),
            j.get("work_mode"), j.get("pay")]
    extra = "".join(f" · {b}" for b in bits if b)
    return f"`#{item['id']}` [{title}]({j.get('url', '')}){extra}{_due_text(item, now, 'apply by')}"


def accomplished(items: list[dict], now: datetime, pruned: dict) -> str:
    """Running totals. `pruned` counts finished items already deleted by tidy()."""
    done = [i for i in items if i["kind"] in ("todo", "job") and i["done"] and i["done_at"]]
    week = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min, now.tzinfo)
    month = datetime.combine(now.date().replace(day=1), time.min, now.tzinfo)
    n_week = sum(dt(i["done_at"]) >= week for i in done)
    n_month = sum(dt(i["done_at"]) >= month for i in done)
    n_jobs = sum(i["kind"] == "job" for i in done) + pruned.get("job", 0)
    n_todos = sum(i["kind"] == "todo" for i in done) + pruned.get("todo", 0)
    season = season_start(now)
    lines = [f"**All time: {n_todos + n_jobs}** · This week: {n_week} · This month: {n_month}",
             f"To-dos done: {n_todos} · Jobs applied: {n_jobs} all time",
             f"💼 **Jobs applied this season: {season_jobs(items, now, pruned)}** "
             f"(since {season:%b} {season.day}, {season.year}; restarts every July 1)"]
    recent = sorted((i for i in done if not i["archived"]), key=lambda i: i["done_at"], reverse=True)[:3]
    if recent:
        names = [("Applied: " if i["kind"] == "job" else "") + i["text"] for i in recent]
        lines.append("Recent: " + " · ".join(f"✔ {n[:60]} ({stamp(dt(i['done_at']), 'R')})"
                                              for n, i in zip(names, recent)))
    return "\n".join(lines)


def season_jobs(items: list[dict], now: datetime, pruned: dict) -> int:
    """Jobs marked applied since the season started (July 1), including ones tidy() deleted."""
    season = season_start(now)
    n = sum(i["kind"] == "job" and i["done"] and i["done_at"] and dt(i["done_at"]) >= season
            for i in items)
    if pruned.get("season") == season.date().isoformat():
        n += pruned.get("season_jobs", 0)
    return n


def open_items(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(to-dos by category then due date, jobs by deadline), unfinished only."""
    todos = [i for i in items if i["kind"] == "todo" and not i["done"]]
    todos.sort(key=lambda i: ((i["category"] or "").lower(), *_due_key(i)))
    jobs = sorted((i for i in items if i["kind"] == "job" and not i["done"]), key=_due_key)
    return todos, jobs


def applied_items(items: list[dict]) -> list[dict]:
    """Jobs shown under Applied: the most recent APPLIED_SHOWN that aren't hidden yet."""
    return sorted((i for i in items if i["kind"] == "job" and i["done"] and not i["archived"]),
                  key=lambda i: i["done_at"] or "", reverse=True)[:APPLIED_SHOWN]


def board_pages(items: list[dict], now: datetime, pruned: dict,
                agenda: str | None = None) -> list[list[discord.Embed]]:
    """The board as embeds, packed into as few messages as Discord's size limits allow.
    `agenda` (today's lines from schedule.day_lines) becomes the 📅 section at the top.
    Applied jobs aren't shown here; they're logged in #application-archive."""
    todos, jobs = open_items(items)

    lines, cat = [], object()
    headers = any(i["category"] for i in todos)
    for item in todos:
        if headers and item["category"] != cat:
            cat = item["category"]
            lines.append(f"__**{cat or 'General'}**__")
        lines.append(todo_line(item, now))
    embeds = [
        discord.Embed(title=f"📋 To-do ({len(todos)})", color=discord.Color.blurple(),
                      description=_fit(lines, "*Nothing to do. Type a task in this channel to add one.*")),
        discord.Embed(title=f"💼 Jobs to apply to ({len(jobs)})", color=discord.Color.gold(),
                      description=_fit([job_line(i, now) for i in jobs], "*No open applications. Post job links in #jobs.*")),
    ]
    embeds[-1].set_footer(text="Unapplied jobs move to #application-archive after 7 days")
    if agenda is not None:
        embeds.insert(0, discord.Embed(title=f"📅 Today · {now:%a} {now.month}/{now.day}",
                                       color=discord.Color.teal(),
                                       description=_fit(agenda.split("\n") if agenda else [],
                                                        "*Nothing scheduled today.*")))
    embeds.append(discord.Embed(title="🏆 Accomplished", color=discord.Color.green(),
                                description=accomplished(items, now, pruned)))
    embeds[-1].set_footer(text='!todo <task> [date] [ping] · "done 3" checks off #3 · '
                               '"undo 3" / "remove 3" · numbers reset daily at 4 AM')

    pages, page = [], []
    for e in embeds:
        if page and sum(len(x) for x in page) + len(e) > MAX_MESSAGE:
            pages.append(page)
            page = []
        page.append(e)
    return pages + [page]
