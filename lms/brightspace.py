"""
D2L Brightspace, through the brightspace-mcp-server package (an MCP server, run here as a
plain subprocess: the bot calls its tools itself, no Claude involved).

The server does the hard part, signing in to the school's Microsoft SSO with 2FA, and keeps
that session in ~/.d2l-session, shared with any other app that runs the same server (e.g.
Claude Code). Needs Node, and a one-time `npx -y brightspace-mcp-server setup`, which asks
for the school and account. Nothing about either lives in this repo. BRIGHTSPACE_MCP in .env
can replace the command that starts the server.

Each poll starts the server, makes about a dozen tool calls and closes it, so nothing stays
running between polls.

Chat (discbot/chat.py) also hands the same server to Claude through chat_tools(), limited to
CHAT_TOOLS: read-only tools for your own courses. Roster and class emails, the instructor/TA
dropbox tools, downloads and sign-in/sign-out are left out.
"""

import json
import os
import re
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import Announcement, Course, LMSError

NAME = "Brightspace"
# Pinned so an update can't change the tools' output overnight. Bump it on purpose.
VERSION = "5.2.0"
DEFAULT_COMMAND = f"npx -y brightspace-mcp-server@{VERSION}"
FIX_HINT = f"If the sign-in expired, run `{DEFAULT_COMMAND} auth` on the bot's computer."
CALL_TIMEOUT = 90          # seconds per tool call (a sign-in refresh can take a while)


def configured() -> bool:
    return True            # the server's own setup holds everything; a failed poll says so


# What Claude may call in chat: your own courses, assignments, grades and course material.
CHAT_TOOLS = (
    "get_my_courses", "get_upcoming_due_dates", "get_assignments", "get_calendar_events",
    "get_announcements", "get_announcement_files", "get_assignment_files",
    "get_assignment_rubric", "get_my_grades", "get_course_content", "get_syllabus",
    "get_video_transcript", "search_course", "get_discussions", "get_my_groups",
    "get_available_groups",
)
# The rest of this version's tools, hidden from Claude so it doesn't try them and get denied.
HIDDEN_TOOLS = (
    "get_roster", "get_classlist_emails", "get_dropbox_folders", "get_dropbox_submissions",
    "get_dropbox_user_submissions", "get_dropbox_feedback", "get_rubrics_for_object",
    "get_server_info", "download_file", "download_course_files",
    "download_dropbox_submission_file", "sign_in", "sign_out",
)


def _command() -> list[str]:
    return shlex.split(os.getenv("BRIGHTSPACE_MCP") or DEFAULT_COMMAND, posix=os.name != "nt")


def _server() -> StdioServerParameters:
    cmd, *args = _command()
    # The server finds its config and saved session through the user's environment
    # (home folder, LOCALAPPDATA), so it gets the whole thing, not MCP's trimmed default.
    env = {**os.environ, "D2L_NO_UPDATE_CHECK": "1"}
    return StdioServerParameters(command=cmd, args=args, env=env)


def chat_tools() -> dict:
    """claude_backend.ask kwargs: this server, with only CHAT_TOOLS callable."""
    cmd, *args = _command()
    if os.name == "nt" and cmd.lower() in ("npx", "npm"):
        cmd, args = "cmd", ["/c", cmd, *args]      # Claude Code can't start npx.cmd directly
    server = {"type": "stdio", "command": cmd, "args": args,
              "env": {"D2L_NO_UPDATE_CHECK": "1"}}  # added to Claude Code's own environment
    return {"mcp_servers": {"brightspace": server},
            "allowed_tools": [f"mcp__brightspace__{t}" for t in CHAT_TOOLS],
            "disallowed_tools": [f"mcp__brightspace__{t}" for t in HIDDEN_TOOLS]}


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def short_course(c: dict) -> str:
    """"wl.202710.IE.33500.002" -> "IE 335"; courses without a term code keep their name.
    5-digit course numbers ending in 00 are shortened to the usual 3 digits."""
    if m := re.search(r"\.([A-Z]{2,5})\.(\d{3})(\d{2})\.", c.get("code") or ""):
        return f"{m.group(1)} {m.group(2)}{'' if m.group(3) == '00' else m.group(3)}"
    return c.get("name") or str(c.get("id"))


class Source:
    def __init__(self, session: ClientSession):
        self._session = session
        self._courses: list[Course] | None = None
        self.school_url = ""

    async def _call(self, tool: str, **arguments):
        result = await self._session.call_tool(tool, arguments, read_timeout_seconds=CALL_TIMEOUT)
        texts = [c.text for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
        if getattr(result, "isError", False) or not texts:
            raise LMSError(" ".join(texts)[:500] or f"{tool} returned nothing")
        try:
            return json.loads(texts[0])       # array tools put pagination in a second block
        except json.JSONDecodeError:
            raise LMSError(texts[0][:500]) from None

    def _link(self, path: str) -> str:
        """Brightspace returns site-relative links (/d2l/...); make them clickable in Discord."""
        return self.school_url + path if path.startswith("/") else path

    async def courses(self) -> list[Course]:
        """Active courses where your role is Learner, and only those."""
        if self._courses is None:
            info = await self._call("get_server_info")
            self.school_url = (info.get("schoolUrl") or "").rstrip("/")
            now = datetime.now(timezone.utc)
            self._courses = [
                Course(str(c["id"]), c.get("name") or "", short_course(c))
                for c in await self._call("get_my_courses")
                if c.get("role") == "Learner" and c.get("isActive") and c.get("canAccess")
                and not (c.get("endDate") and _parse(c["endDate"]) < now)]
        return self._courses

    async def announcements(self, since: datetime) -> list[Announcement]:
        """Announcements posted or edited after `since` in your learner courses, oldest first."""
        out = []
        for course in await self.courses():           # only ever learner courses
            found = await self._call(
                "get_announcements", courseId=int(course.id), count=50,
                modifiedSince=since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for a in found["announcements"] if isinstance(found, dict) else found:
                posted = _parse(a.get("date") or a["lastModified"])
                out.append(Announcement(
                    id=f"brightspace:{a['id']}", course=course,
                    title=a.get("title") or "(untitled)",
                    body=re.sub(r"\]\((/[^)\s]*)\)", lambda m: f"]({self._link(m.group(1))})",
                                a.get("body") or "").strip(),
                    url=self._link(f"/d2l/le/news/{course.id}/{a['id']}/view"),
                    posted=posted,
                    edited=_parse(a["lastModified"]) if a.get("lastModified") else posted,
                    attachments=[f["fileName"] for f in a.get("attachments") or []]))
        return sorted(out, key=lambda a: a.posted)


def _leaf(e: BaseException) -> BaseException:
    """The MCP client wraps errors in (nested) ExceptionGroups; dig out a lone real one."""
    while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
        e = e.exceptions[0]
    return e


@asynccontextmanager
async def connect():
    started = False
    try:
        with open(os.devnull, "w") as quiet:     # the server logs every call to stderr
            async with stdio_client(_server(), errlog=quiet) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    started = True
                    yield Source(session)
    except Exception as e:
        leaf = _leaf(e)
        if started or isinstance(leaf, LMSError):
            raise leaf from None                 # a failed call or a bug in the caller
        raise LMSError(f"couldn't start the Brightspace server: {leaf!r}") from e
