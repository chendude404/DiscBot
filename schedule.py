"""
Weekly class schedule and the board's 📅 agenda. Plain code, no Claude.

The semester lives in my_schedule.py (gitignored, so your timetable stays off GitHub; without it
the bot uses my_schedule_example.py). On startup, if SEMESTER or CLASSES changed since last time,
CLASSES are loaded into data.json (replacing what was there), so `!schedule add` /
`!schedule remove` then work on everything alike. Classes repeat weekly until SEMESTER_END and
skip HOLIDAYS. For a new semester: edit SEMESTER, CLASSES, HOLIDAYS, SEMESTER_END,
NEXT_SEMESTER_REMINDER in my_schedule.py and restart.
"""

import re
from datetime import date, datetime, time, timedelta

import todo

# ---------------------------------------------------------------- the semester (my_schedule.py)

try:
    from my_schedule import CLASSES, HOLIDAYS, NEXT_SEMESTER_REMINDER, SEMESTER, SEMESTER_END
except ImportError:
    from my_schedule_example import CLASSES, HOLIDAYS, NEXT_SEMESTER_REMINDER, SEMESTER, SEMESTER_END


# ---------------------------------------------------------------- parsing

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Registrar-style letters (MWF, TR, TTh); two-letter forms first so "th" isn't read as t + h.
COMPACT_RE = re.compile(r"th|tu|sa|su|m|t|w|r|f|s|u")
COMPACT = {"m": 0, "t": 1, "tu": 1, "w": 2, "r": 3, "th": 3, "f": 4, "s": 5, "sa": 5, "u": 6, "su": 6}


def parse_days(token: str) -> list[int] | None:
    """"mon,wed", "tue/thu", "monday", "MWF", "TR", "TTh" -> weekday numbers (Monday = 0)."""
    parts = [p for p in re.split(r"[,/+&]", token.lower()) if p]
    named = [next((i for i, n in enumerate(DAY_NAMES)
                   if len(p) >= 2 and (n.startswith(p) or p.startswith(n))), COMPACT.get(p)) for p in parts]
    if parts and None not in named:
        return sorted(set(named))
    if len(parts) == 1 and re.fullmatch(f"(?:{COMPACT_RE.pattern})+", parts[0]):
        return sorted({COMPACT[c] for c in COMPACT_RE.findall(parts[0])})
    return None


def parse_time_range(token: str) -> tuple[time, time] | None:
    """"10am-11:15am", "10-11:15am", "2-4:50pm", "14:00-15:15" -> (start, end)."""
    m = re.fullmatch(r"(.+?)\s*-\s*(.+)", token.lower())
    if not m:
        return None
    a, b = m.groups()
    end = todo.parse_clock(b)
    mer = re.search(r"(am|pm)$", b)
    if mer and not re.search(r"(am|pm)$", a):
        # "10-11:15am": the start borrows the end's am/pm, unless that puts it after the end
        # ("11-12:15pm" means 11am).
        start = todo.parse_clock(a + mer.group(1))
        if start and end and start >= end:
            start = todo.parse_clock(a + ("am" if mer.group(1) == "pm" else "pm"))
    else:
        start = todo.parse_clock(a)
    return (start, end) if start and end and end > start else None


def parse_add(args: str) -> dict | str:
    """"<name> <days> <start>-<end>" -> class dict, or an error."""
    words = args.split()
    if len(words) >= 3 and (rng := parse_time_range(words[-1])) and (days := parse_days(words[-2])):
        return {"name": " ".join(words[:-2])[:60], "days": days,
                "start": rng[0].strftime("%H:%M"), "end": rng[1].strftime("%H:%M")}
    return ("Usage: `!schedule add <name> <days> <start>-<end>`, e.g. "
            "`!schedule add CS 101 mon,wed 10am-11:15am` or `!schedule add Chem lab R 2-4:50pm`")


def load_semester(store: todo.Store) -> bool:
    """Put the hardcoded CLASSES into data.json whenever SEMESTER or CLASSES change (this
    replaces classes added or removed with !schedule). True if it did."""
    key = f"{SEMESTER}|{CLASSES!r}"
    if store.data.get("semester") == key:
        return False
    blocks = []
    for n, (name, days, start, end) in enumerate(CLASSES, 1):
        d, rng = parse_days(days), parse_time_range(f"{start}-{end}")
        if d is None or rng is None:
            raise ValueError(f"my_schedule.py: can't read {name!r} ({days} {start}-{end})")
        blocks.append({"id": n, "name": name, "days": d,
                       "start": rng[0].strftime("%H:%M"), "end": rng[1].strftime("%H:%M")})
    store.data.update(semester=key, schedule=blocks, schedule_next_id=len(blocks) + 1)
    store.save()
    return True


# ---------------------------------------------------------------- what happens on a day

def holiday(day: date) -> str | None:
    return next((name for first, last, name in HOLIDAYS if first <= day <= last), None)


def classes_on(blocks: list[dict], day: date, tz) -> list[tuple[datetime, datetime, dict]]:
    if day > SEMESTER_END or holiday(day):
        return []
    out = []
    for b in blocks:
        if day.weekday() in b["days"]:
            start = datetime.combine(day, time.fromisoformat(b["start"]), tz)
            out.append((start, datetime.combine(day, time.fromisoformat(b["end"]), tz), b))
    return sorted(out, key=lambda x: x[0])


def day_lines(blocks: list[dict], items: list[dict], day: date, now: datetime) -> list[str]:
    """Everything on one day, by time: classes, dated to-dos, application deadlines."""
    rows = [(s, f"{todo.stamp(s, 't')} **{b['name']}** (until {todo.stamp(e, 't')})")
            for s, e, b in classes_on(blocks, day, now.tzinfo)]
    for i in items:
        due = todo.dt(i["due"])
        if i["kind"] in ("todo", "job") and not i["done"] and due and due.astimezone(now.tzinfo).date() == day:
            bell = " 🔔" if any(r not in i["fired"] for r in i["remind_at"]) else ""
            what = f"apply by: {i['job']['title'] or i['text']}" if i["kind"] == "job" else i["text"]
            rows.append((due, f"{todo.stamp(due, 't')} `#{i['id']}` {what}{bell}"))
    rows.sort(key=lambda r: r[0])
    lines = [line for _, line in rows]
    if name := holiday(day):
        lines.insert(0, f"🎉 No classes ({name})")
    return lines


def day_header(day: date, today: date) -> str:
    label = "Today" if day == today else "Tomorrow" if day == today + timedelta(days=1) else None
    return f"**{label + ' · ' if label else ''}{day:%a} {day.month}/{day.day}**"


def agenda(blocks: list[dict], items: list[dict], now: datetime, days: int = 7) -> str:
    """The board's 📅 section: the next `days` days, skipping empty ones."""
    out = []
    for n in range(days):
        day = now.date() + timedelta(days=n)
        if lines := day_lines(blocks, items, day, now):
            out.append(day_header(day, now.date()))
            out += [f"> {line}" for line in lines]
    return "\n".join(out) or "*Nothing scheduled in the next week.*"


def list_blocks(blocks: list[dict]) -> str:
    if not blocks:
        return "No classes yet. Add one with `!schedule add CS 101 mon,wed 10am-11:15am`."
    lines = []
    for b in sorted(blocks, key=lambda b: (b["days"][0], b["start"])):
        days = ", ".join(DAY_NAMES[d].title() for d in b["days"])
        s, e = (time.fromisoformat(b[k]).strftime("%I:%M %p").lstrip("0") for k in ("start", "end"))
        lines.append(f"`S{b['id']}` **{b['name']}** · {days} · {s}–{e}")
    return "\n".join(lines)
