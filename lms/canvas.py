"""
Instructure Canvas, through its REST API (https://canvas.instructure.com/doc/api/).

Needs two lines in .env:
    CANVAS_URL=https://canvas.yourschool.edu
    CANVAS_TOKEN=...     Canvas > Account > Settings > "+ New Access Token"

The token can do anything you can, so keep it in .env only. Canvas doesn't report when an
announcement was edited, so an edited one isn't re-posted.
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import aiohttp

from . import Announcement, Course, LMSError

NAME = "Canvas"
FIX_HINT = ("If the access token expired or was deleted, make a new one (Canvas > Account > "
            "Settings > New Access Token) and put it in .env as CANVAS_TOKEN.")


def configured() -> bool:
    return bool(os.getenv("CANVAS_URL") and os.getenv("CANVAS_TOKEN"))


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


class _Markdown(HTMLParser):
    """Just enough HTML -> Discord markdown for announcement bodies: links, bold, italics,
    paragraphs, line breaks and list bullets. Everything else keeps only its text."""

    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.hrefs: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
            self.out.append("[")

    def handle_endtag(self, tag):
        if tag in self.BLOCK:
            self.out.append("\n\n")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a" and self.hrefs:
            href = self.hrefs.pop()
            self.out.append(f"]({href})" if href else "]")

    def handle_data(self, data):
        self.out.append(re.sub(r"\s+", " ", data))

    def text(self) -> str:
        md = "".join(self.out).replace("[]", "")
        md = re.sub(r" *\n *", "\n", md)
        return re.sub(r"\n{3,}", "\n\n", md).strip()


def html_to_markdown(html: str) -> str:
    parser = _Markdown()
    parser.feed(html or "")
    parser.close()
    return parser.text()


class Source:
    def __init__(self, http: aiohttp.ClientSession, base: str):
        self._http = http
        self._base = base
        self._courses: list[Course] | None = None

    async def _get(self, path: str, params: list[tuple[str, str]]) -> list[dict]:
        """GET every page of a list endpoint (Canvas paginates with Link: <...>; rel="next")."""
        url, out = f"{self._base}/api/v1/{path}", []
        params = [*params, ("per_page", "100")]
        while url:
            async with self._http.get(url, params=params) as r:
                if r.status == 401:
                    raise LMSError("Canvas rejected CANVAS_TOKEN (HTTP 401)")
                if r.status != 200:
                    raise LMSError(f"Canvas answered HTTP {r.status} for {path}")
                out += await r.json()
                nxt = r.links.get("next")
                url = str(nxt["url"]) if nxt else None
                params = []                         # the next link already carries them
        return out

    async def courses(self) -> list[Course]:
        """Active courses where you're enrolled as a student, and only those."""
        if self._courses is None:
            found = await self._get("courses", [("enrollment_type", "student"),
                                                ("enrollment_state", "active")])
            self._courses = [Course(str(c["id"]), c.get("name") or "",
                                    c.get("course_code") or c.get("name") or str(c["id"]))
                             for c in found if "id" in c and not c.get("access_restricted_by_date")]
        return self._courses

    async def announcements(self, since: datetime) -> list[Announcement]:
        """Announcements posted after `since` in your student courses, oldest first."""
        courses = {f"course_{c.id}": c for c in await self.courses()}
        if not courses:
            return []
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        found = await self._get("announcements", [
            *(("context_codes[]", code) for code in courses),
            ("start_date", since.astimezone(timezone.utc).strftime(fmt)),
            ("end_date", (now + timedelta(days=1)).strftime(fmt)),   # Canvas defaults to +28 days
            ("active_only", "true")])
        out = []
        for a in found:
            course = courses.get(a.get("context_code"))
            if course is None or not a.get("posted_at"):
                continue
            posted = _parse(a["posted_at"])
            out.append(Announcement(
                id=f"canvas:{a['id']}", course=course, title=a.get("title") or "(untitled)",
                body=html_to_markdown(a.get("message") or ""), url=a.get("html_url") or "",
                posted=posted, edited=posted,
                attachments=[f.get("display_name") or f.get("filename") or "file"
                             for f in a.get("attachments") or []]))
        return sorted(out, key=lambda a: a.posted)


@asynccontextmanager
async def connect():
    if not configured():
        raise LMSError("CANVAS_URL / CANVAS_TOKEN aren't set in .env")
    base = os.environ["CANVAS_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['CANVAS_TOKEN']}"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as http:
        try:
            yield Source(http, base)
        except aiohttp.ClientError as e:
            raise LMSError(f"couldn't reach Canvas: {e!r}") from e
