"""
Course announcements from your school's learning platform (LMS), in plain code, no Claude.
Pick the platform with LMS in .env; each one lives in its own module here:

    LMS=brightspace   lms/brightspace.py  D2L Brightspace, through brightspace-mcp-server
    LMS=canvas        lms/canvas.py       Instructure Canvas, through its REST API

Every platform offers the same thing: `async with lms.connect() as src:` then
`await src.courses()` and `await src.announcements(since)`, returning the dataclasses below.
Only courses where you're a student are ever read, so nothing about other students (from
courses you teach, TA or grade) can reach Discord.

Adding a platform: write lms/<name>.py with NAME, FIX_HINT, configured() and connect()
like the existing two, and add it to PLATFORMS.

Run `py -m lms` to check your setup: it lists your courses and the last week of announcements.
"""

import importlib
import os
from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType

PLATFORMS = ("brightspace", "canvas")


class LMSError(RuntimeError):
    pass


@dataclass
class Course:
    id: str
    name: str
    short: str            # "IE 335", what the announcement card shows


@dataclass
class Announcement:
    id: str               # unique across platforms, e.g. "brightspace:2063972"
    course: Course
    title: str
    body: str             # Discord-ready markdown with absolute links
    url: str              # the announcement on the platform's website
    posted: datetime      # timezone-aware
    edited: datetime      # == posted when the platform doesn't report edits
    attachments: list[str] = field(default_factory=list)


def platform() -> ModuleType | None:
    """The module for LMS in .env, or None when it's unset."""
    name = (os.getenv("LMS") or "").strip().lower()
    if not name:
        return None
    if name not in PLATFORMS:
        raise LMSError(f"LMS={name} isn't supported; use one of: {', '.join(PLATFORMS)}")
    return importlib.import_module(f"{__name__}.{name}")


def configured() -> bool:
    try:
        p = platform()
    except LMSError:
        return False
    return bool(p and p.configured())


def connect():
    if (p := platform()) is None:
        raise LMSError("LMS isn't set in .env")
    return p.connect()


def name() -> str:
    p = platform()
    return p.NAME if p else "LMS"


def fix_hint() -> str:
    """What to do when polling keeps failing (usually an expired sign-in or token)."""
    p = platform()
    return p.FIX_HINT if p else "Set LMS in .env."
