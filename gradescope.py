"""
Gradescope access through the gradescopeapi library (https://github.com/nyuoss/gradescope-api).
Plain code, no Claude. Needs GRADESCOPE_EMAIL and GRADESCOPE_PASSWORD in .env. If your school
signs in through SSO, set a Gradescope password first (gradescope.com > Account Settings, or
"Forgot password" on the login page) — the library can only do email + password logins.

The library is synchronous (requests), so the async helpers run it in a worker thread to keep
the bot's event loop free. One logged-in session is kept and reused; if a call fails (e.g. the
session expired), it logs in again once and retries.

Run `py gradescope.py` to check the credentials: it prints your courses and upcoming deadlines.
"""

import asyncio
import os
import re
import threading
from datetime import datetime, timezone

from gradescopeapi.classes.assignments import Assignment
from gradescopeapi.classes.connection import GSConnection
from gradescopeapi.classes.courses import Course

_conn: GSConnection | None = None
_lock = threading.Lock()  # the requests session isn't safe to share across threads at once


def _login() -> GSConnection:
    email, password = os.getenv("GRADESCOPE_EMAIL"), os.getenv("GRADESCOPE_PASSWORD")
    if not email or not password:
        raise RuntimeError("GRADESCOPE_EMAIL / GRADESCOPE_PASSWORD aren't set in .env")
    conn = GSConnection()
    try:
        conn.login(email, password)
    except ValueError:
        raise RuntimeError("Gradescope rejected the email/password in .env") from None
    return conn


def _call(fn):
    """Run fn(account) with a logged-in session, logging in again once if it fails."""
    global _conn
    with _lock:
        if _conn is None:
            _conn = _login()
        try:
            return fn(_conn.account)
        except Exception:
            _conn = _login()
            return fn(_conn.account)


def student_courses_sync() -> dict[str, Course]:
    """{course_id: Course} for courses you're enrolled in as a student."""
    return _call(lambda a: a.get_courses().get("student", {}))


def assignments_sync(course_id: str) -> list[Assignment]:
    return _call(lambda a: a.get_assignments(course_id))


async def student_courses() -> dict[str, Course]:
    return await asyncio.to_thread(student_courses_sync)


async def assignments(course_id: str) -> list[Assignment]:
    return await asyncio.to_thread(assignments_sync, course_id)


_TERM_ORDER = {"spring": 0, "summer": 1, "fall": 2, "winter": 3}


def _term_key(c: Course) -> tuple[int, int]:
    year = int(c.year) if str(c.year).isdigit() else 0
    return year, _TERM_ORDER.get(str(c.semester).lower(), -1)


def current_term(courses: dict[str, Course]) -> dict[str, Course]:
    """Only the courses from the latest semester (old courses have no upcoming work)."""
    if not courses:
        return {}
    latest = max(_term_key(c) for c in courses.values())
    return {cid: c for cid, c in courses.items() if _term_key(c) == latest}


def _aware(d: datetime) -> datetime:
    """Gradescope dates carry an offset; treat any that don't as UTC so comparisons work."""
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def configured() -> bool:
    return bool(os.getenv("GRADESCOPE_EMAIL") and os.getenv("GRADESCOPE_PASSWORD"))


def short_course(c: Course) -> str:
    """"wl.202710.ECE.36200.001" / "ECE 30100 - Lin" / "ECE302 MWF Section 3" -> "ECE 362" etc.
    Purdue's 5-digit numbers ending in 00 are shortened to the usual 3 digits."""
    for text in (c.name, c.full_name):
        if m := re.search(r"\b([A-Z]{2,5})[ .]?(\d{3})(\d{2})?\b", text or ""):
            num = m.group(2) + (m.group(3) if m.group(3) not in (None, "00") else "")
            return f"{m.group(1)} {num}"
    return c.name or c.full_name or "?"


def due(a: Assignment) -> datetime | None:
    return _aware(a.due_date) if a.due_date else None


async def term_assignments() -> list[tuple[str, Course, Assignment]]:
    """(course id, course, assignment) for every assignment in this term's student courses."""
    out = []
    for cid, course in current_term(await student_courses()).items():
        out += [(cid, course, a) for a in await assignments(cid)]
    return out


async def upcoming() -> list[tuple[Course, Assignment]]:
    """Every not-yet-due assignment across this term's student courses, soonest first."""
    now = datetime.now(timezone.utc)
    out = [(c, a) for _, c, a in await term_assignments() if a.due_date and due(a) > now]
    return sorted(out, key=lambda ca: due(ca[1]))


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    courses = student_courses_sync()
    print(f"Logged in. {len(courses)} student course(s):")
    for cid, c in courses.items():
        print(f"  {cid}  {c.name} — {c.full_name} ({c.semester} {c.year})")
    print("\nUpcoming:")
    for course, a in asyncio.run(upcoming()):
        print(f"  {a.due_date:%a %b %d %H:%M}  {short_course(course)}: {a.name}  [{a.submissions_status}]")
