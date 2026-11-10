"""`py -m lms`: check the LMS setup by listing your courses and the last week of announcements."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

import lms


async def main():
    load_dotenv()
    if not lms.configured():
        sys.exit(f"Not set up. Set LMS in .env to one of: {', '.join(lms.PLATFORMS)}, plus what "
                 "that platform's module (lms/<name>.py) asks for.")
    async with lms.connect() as src:
        courses = await src.courses()
        print(f"{lms.name()}: {len(courses)} course(s) where you're a student:")
        for c in courses:
            print(f"  {c.id:>10}  {c.short:<14} {c.name}")
        print("\nAnnouncements from the last 7 days:")
        for a in await src.announcements(datetime.now(timezone.utc) - timedelta(days=7)):
            print(f"  {a.posted.astimezone():%a %b %d %H:%M}  {a.course.short}: {a.title}"
                  f"  ({len(a.attachments)} attachment(s))")


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
