"""
Turn phrases like "today", "last 3 days", "since monday", "on 9/20" into a
(start, end) datetime range. Deterministic for common phrases; bot.py falls
back to Claude for anything this can't parse.
"""

import json
import re
from datetime import date, datetime, timedelta, time

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}


def _midnight(d: date, tz) -> datetime:
    return datetime.combine(d, time.min, tzinfo=tz)


def _parse_date(s: str, today: date) -> date | None:
    s = s.strip().lower()
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if s in (name, name[:3]):
            back = (today.weekday() - i) % 7        # most recent such day, including today
            return today - timedelta(days=back)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s)
        if not m:
            return None
        mo, d = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_when(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """now must be timezone-aware; 'today' etc. are interpreted in its timezone."""
    tz = now.tzinfo
    today = now.date()
    t = re.sub(r"\s+", " ", text.strip().lower())
    t = re.sub(r"^(images?|pics?|photos?|screenshots?) ", "", t)
    t = re.sub(r"^(from|posted|sent) ", "", t)

    if t in ("today", "this day"):
        return _midnight(today, tz), now
    if t == "yesterday":
        return _midnight(today - timedelta(days=1), tz), _midnight(today, tz)
    if t == "this week":                             # weeks start Monday
        return _midnight(today - timedelta(days=today.weekday()), tz), now
    if t == "last week":
        start = today - timedelta(days=today.weekday() + 7)
        return _midnight(start, tz), _midnight(start + timedelta(days=7), tz)
    if t == "this month":
        return _midnight(today.replace(day=1), tz), now

    m = re.fullmatch(r"(?:the )?(?:last|past) (?:(\d+|an?|one) )?(minute|min|hour|hr|day|week)s?", t)
    if m:
        n = m.group(1)
        n = 1 if n in (None, "a", "an", "one") else int(n)
        return now - timedelta(seconds=n * UNITS[m.group(2)]), now

    m = re.fullmatch(r"since (.+)", t)
    if m:
        d = _parse_date(m.group(1), today)
        if d:
            return _midnight(d, tz), now

    m = re.fullmatch(r"(?:on )?(.+)", t)
    if m:
        d = _parse_date(m.group(1), today)
        if d:
            return _midnight(d, tz), min(_midnight(d + timedelta(days=1), tz), now)

    m = re.fullmatch(r"(?:between |from )?(.+?) (?:to|through|until|and|-) (.+)", t)
    if m:
        a, b = _parse_date(m.group(1), today), _parse_date(m.group(2), today)
        if a and b and a <= b:
            return _midnight(a, tz), min(_midnight(b + timedelta(days=1), tz), now)

    return None


def claude_prompt(text: str, now: datetime) -> str:
    return (f"Current local time: {now.isoformat()} ({now.strftime('%A')}).\n"
            f'Convert this time phrase into a range: "{text}"\n'
            'Reply with ONLY JSON: {"start": "<ISO 8601 with offset>", "end": "<ISO 8601 with offset>"} '
            'or {"error": "not a time range"}. Clamp end to the current time.')


def parse_claude_range(reply: str, now: datetime) -> tuple[datetime, datetime] | None:
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        start = datetime.fromisoformat(obj["start"])
        end = datetime.fromisoformat(obj["end"])
    except (ValueError, KeyError, TypeError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=now.tzinfo)
    if end.tzinfo is None:
        end = end.replace(tzinfo=now.tzinfo)
    end = min(end, now)
    return (start, end) if start < end else None


def describe(start: datetime, end: datetime) -> str:
    fmt = "%b %d, %I:%M %p"
    return f"{start.strftime(fmt)} → {end.strftime(fmt)}".replace(" 0", " ")
