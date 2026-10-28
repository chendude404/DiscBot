"""
The job archive: board jobs you haven't applied to within 7 days, postings whose link closed, and
Summer 2027 internships pulled from the Simplify / Pitt CSC list. Ranked by fit.py, shown in
an always-open message in #application-archive with folders, and promoted back to the board with a button.
Plain code, no Claude. Kept in archive.json, apart from data.json, because it holds thousands of
postings and data.json is rewritten on every message.
"""

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import discord

import fit
import jobs

ARCHIVE_FILE = Path(__file__).with_name("archive.json")
BOARD_DAYS = 7                         # a board job you haven't applied to moves here after this
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/"
                ".github/scripts/listings.json")
SIMPLIFY_TERM = "Summer 2027"
SKIP_CATEGORIES = {"Product"}          # product management roles
SCRAPE_EVERY = timedelta(hours=3)
MAX_AGE = timedelta(days=90)           # a posting is deleted this long after it was posted…
MAX_STAY = timedelta(days=30)          # …or after this long in the archive, whichever comes first
PAGE_SIZE = 8                          # 8 rows keeps a page under Discord's 40-component limit


def entry_id(url: str) -> str:
    """Short stable id (fits in a button's custom_id) from the link's canonical form."""
    return hashlib.sha1(jobs.canonical(url).encode()).hexdigest()[:12]


class Archive:
    """{"entries": {id: entry}, "handled": {id: when}, "etag": ..., "last_scrape": ...,
    "message": [channel id, message id], "folder": ..., "page": ...}. `handled` remembers postings
    you promoted, applied to or removed, or that expired, so the scraper never brings them back.
    `message`/`folder`/`page`: the always-open message in #application-archive and what it's showing."""

    def __init__(self, path: Path = ARCHIVE_FILE):
        self.path = path
        self.data = {"entries": {}, "handled": {}, "etag": None, "last_scrape": None,
                     "message": None, "folder": None, "page": 0}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def entries(self) -> dict[str, dict]:
        return self.data["entries"]

    def has(self, url: str) -> bool:
        return entry_id(url) in self.entries

    def handle(self, url: str):
        """You promoted, applied to or removed this posting: keep the scraper from re-adding it."""
        self.data["handled"][entry_id(url)] = datetime.now(timezone.utc).isoformat()

    def add_board_job(self, item: dict, closed: bool = False):
        """Move a board job (its data.json item) here; promoting it puts the same item back."""
        j = item["job"]
        self.entries[entry_id(j["url"])] = {
            "title": j.get("title"), "company": j.get("company"), "location": j.get("location"),
            "url": j["url"], "source": "board", "posted": item["created"],
            "found": datetime.now(timezone.utc).isoformat(), "desc": "",
            "closed": j.get("closed") if closed else None, "item": item}

    def expire(self) -> int:
        """Delete postings 90 days after they were posted or 30 days after they came here,
        whichever is first. Returns how many went."""
        now = datetime.now(timezone.utc)
        old = [eid for eid, e in self.entries.items()
               if now - datetime.fromisoformat(e["posted"]) > MAX_AGE
               or now - datetime.fromisoformat(e["found"]) > MAX_STAY]
        for eid in old:
            del self.entries[eid]
            self.data["handled"][eid] = now.isoformat()        # still listed? don't re-add it
        return len(old)

    def scored(self) -> list[tuple[str, dict, int, list[str]]]:
        """Every posting as (id, entry, score, matched), best fit first, newest first on ties."""
        rows = [(eid, e, *fit.score(e, e.get("desc", ""))) for eid, e in self.entries.items()]
        rows.sort(key=lambda r: (r[2], r[1].get("posted") or ""), reverse=True)
        return rows


def in_folder(folder: str, row: tuple[str, dict, int, list[str]], now: datetime) -> bool:
    """Is this posting in this folder? Closed postings only appear in the 🔒 folder."""
    _, e, _, matched = row
    if (folder == "closed") != bool(e.get("closed")):
        return False
    if folder == "board":
        return e["source"] == "board"
    if folder == "new":
        return now - datetime.fromisoformat(e["posted"]) <= timedelta(days=3)
    if folder.startswith("kw:"):
        return any(m.endswith(" " + folder[3:]) for m in matched)
    return True


# ---------------------------------------------------------------- Simplify / Pitt CSC list

def _desc(listing: dict) -> str:
    """Extra text fit.py scores: the list's category, and the degrees it accepts (a posting open
    to Bachelor's students counts as Bachelor's; one only for Master's/PhD as grad-only)."""
    degrees = listing.get("degrees") or []
    deg = ("Bachelor's" if "Bachelor's" in degrees
           else "graduate students only" if set(degrees) & {"Master's", "PhD", "MBA"} else "")
    return f"{listing.get('category') or ''} {deg}".strip()


async def scrape_simplify(archive: Archive, known: set[str]) -> tuple[list[str], int] | None:
    """Add new active Summer 2027 postings from the Simplify / Pitt CSC list and drop ones it no
    longer lists as active. `known`: canonical links already on the board. Returns (ids added,
    number removed), or None if the list hasn't changed since last time (checked with its ETag)."""
    headers = {"If-None-Match": archive.data["etag"]} if archive.data.get("etag") else {}
    async with aiohttp.ClientSession() as session:
        async with session.get(SIMPLIFY_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as r:
            archive.data["last_scrape"] = datetime.now(timezone.utc).isoformat()
            if r.status == 304:
                archive.save()
                return None
            r.raise_for_status()
            raw = await r.read()
            etag = r.headers.get("ETag")
    listings = await asyncio.to_thread(json.loads, raw)       # ~13 MB: parse off the event loop

    active = {}
    for x in listings:
        if (x.get("active") and x.get("is_visible") and SIMPLIFY_TERM in (x.get("terms") or [])
                and x.get("category") not in SKIP_CATEGORIES and x.get("url")):
            active[entry_id(x["url"])] = x
    entries, handled = archive.entries, archive.data["handled"]
    removed = [eid for eid, e in entries.items() if e["source"] == "Simplify" and eid not in active]
    for eid in removed:
        del entries[eid]
    added = []
    for eid, x in active.items():
        if eid in entries or eid in handled or jobs.canonical(x["url"]) in known:
            continue
        posted = datetime.fromtimestamp(x.get("date_posted") or 0, timezone.utc)
        if datetime.now(timezone.utc) - posted > MAX_AGE:
            continue                                          # would expire right away
        posted = posted.isoformat()
        entries[eid] = {"title": x.get("title"), "company": x.get("company_name"),
                        "location": " · ".join(x.get("locations") or [])[:80], "url": x["url"],
                        "source": "Simplify", "posted": posted,
                        "found": datetime.now(timezone.utc).isoformat(), "desc": _desc(x),
                        "closed": None, "item": None}
        added.append(eid)
    archive.data["etag"] = etag
    archive.save()
    return added, len(removed)


# ---------------------------------------------------------------- the archive message

FOLDERS = ([("all", "🗂️", "Everything"), ("new", "🆕", "Posted in the last 3 days"),
            ("board", "📌", "From my board"), ("closed", "🔒", "Closed")]
           + [(f"kw:{label}", "📁", label) for label, weight, _, _ in fit.KEYWORDS if weight > 0])
FOLDER_NAMES = {key: f"{emoji} {name}" for key, emoji, name in FOLDERS}


class ArchiveView(discord.ui.LayoutView):
    """The always-open message in #application-archive. With no folder open it shows the folders as
    buttons; inside a folder, its postings best fit first, PAGE_SIZE at a time, each with a
    ⬆️ Promote button. `promote(entry_id)` moves a posting to the board and returns a short
    confirmation (None if it's gone). Which folder and page it shows is saved in archive.json,
    so a restart reopens the same place. Only `owner_id` can press anything."""

    def __init__(self, archive: Archive, promote: Callable[[str], Awaitable[str | None]],
                 owner_id: int | None, note: str = ""):
        super().__init__(timeout=None)
        self.archive, self.promote, self.owner_id = archive, promote, owner_id
        rows, now = archive.scored(), datetime.now(timezone.utc)
        folder = archive.data.get("folder")
        container = discord.ui.Container(accent_colour=discord.Colour.gold())
        if folder not in FOLDER_NAMES:
            self._folders(container, rows, now, note)
        else:
            self._folder(container, folder, [r for r in rows if in_folder(folder, r, now)], note)
        self.add_item(container)

    def _folders(self, container: discord.ui.Container, rows: list, now: datetime, note: str):
        open_rows = [r for r in rows if not r[1].get("closed")]
        container.add_item(discord.ui.TextDisplay(
            f"## 💼 Job archive\n{len(open_rows)} open postings · pick a folder"
            + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        buttons = []
        for key, emoji, name in FOLDERS:
            n = sum(in_folder(key, r, now) for r in rows)
            if n or key == "all":
                button = discord.ui.Button(label=f"{name} · {n}"[:80], emoji=emoji,
                                           style=discord.ButtonStyle.primary if key == "all"
                                           else discord.ButtonStyle.secondary,
                                           custom_id=f"arcf:{key}"[:100])
                button.callback = self._go(key)
                buttons.append(button)
        for i in range(0, len(buttons), 4):
            container.add_item(discord.ui.ActionRow(*buttons[i:i + 4]))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# Unapplied board jobs land here after 7 days; new Summer 2027 postings every 3 h "
            "(Simplify / Pitt CSC). Postings are deleted 90 days after posting or 30 days here."))

    def _folder(self, container: discord.ui.Container, folder: str, rows: list, note: str):
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(self.archive.data.get("page", 0), pages - 1)
        container.add_item(discord.ui.TextDisplay(
            f"## {FOLDER_NAMES[folder]}\n{len(rows)} postings · best fit first"
            + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        for eid, e, score, matched in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            title = (e.get("title") or "Untitled role")[:80].replace("[", "(").replace("]", ")")
            keys = ", ".join(m.split(" ", 1)[1] for m in matched) or "no keywords"
            text = (f"**{score:+d}** [{title}]({e['url']}) · **{(e.get('company') or '?')[:40]}**\n"
                    f"-# {(e.get('location') or '?')[:60]} · {keys} · "
                    f"{'from your board' if e['source'] == 'board' else e['source']} "
                    f"{(e.get('posted') or '')[:10]}")
            button = discord.ui.Button(label="Reopen" if e.get("closed") else "Promote", emoji="⬆️",
                                       style=discord.ButtonStyle.success, custom_id=f"arc:{eid}")
            button.callback = self._promoter(eid)
            container.add_item(discord.ui.Section(discord.ui.TextDisplay(text), accessory=button))
        if not rows:
            container.add_item(discord.ui.TextDisplay("*Nothing here.*"))
        container.add_item(discord.ui.Separator())
        back = discord.ui.Button(label="Folders", emoji="⬅️", custom_id="arcn:home")
        back.callback = self._go(None)
        prev = discord.ui.Button(label="◀", disabled=page == 0, custom_id="arcn:prev")
        prev.callback = self._go(folder, page - 1)
        where = discord.ui.Button(label=f"Page {page + 1}/{pages}", disabled=True, custom_id="arcn:page")
        nxt = discord.ui.Button(label="▶", disabled=page >= pages - 1, custom_id="arcn:next")
        nxt.callback = self._go(folder, page + 1)
        container.add_item(discord.ui.ActionRow(back, prev, where, nxt))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This archive is private.", ephemeral=True)
        return False

    def _redraw(self, note: str = "") -> "ArchiveView":
        return ArchiveView(self.archive, self.promote, self.owner_id, note)

    def _go(self, folder: str | None, page: int = 0):
        async def callback(interaction: discord.Interaction):
            self.archive.data["folder"], self.archive.data["page"] = folder, max(0, page)
            self.archive.save()
            await interaction.response.edit_message(view=self._redraw())
        return callback

    def _promoter(self, eid: str):
        async def callback(interaction: discord.Interaction):
            note = await self.promote(eid) or "That posting is no longer in the archive."
            await interaction.response.edit_message(view=self._redraw(note))
        return callback
