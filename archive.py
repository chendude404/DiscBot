"""
The job archive: board jobs you haven't applied to within 7 days, postings whose link closed, and
postings pulled from the Simplify / Pitt CSC lists (internships, or new-grad roles for full-time)
and the CSCareers internship list.
Ranked by fit.py and filtered by the settings on its ⚙️ Filters screen (US only, role type, season,
minimum fit, keyword weights). Shown in an always-open message in #application-archive with
folders, and promoted back to the board with a button.
Plain code, no Claude. Kept in archive.json, apart from data.json, because it holds thousands of
postings and data.json is rewritten on every message.
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp
import discord

import fit
import jobs
import places

ARCHIVE_FILE = Path(__file__).with_name("archive.json")
BOARD_DAYS = 7                         # a board job you haven't applied to moves here after this
# Lists in Simplify's listings.json format: feed -> (url, shown as, role it holds). "intern" and
# "fulltime" are Simplify / Pitt CSC's; "cscareers" (vanshb03) adds internships Simplify misses,
# with shortened links, so it's matched to the others by company + title.
FEEDS = {
    "intern": ("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
               "Simplify", "intern"),
    "cscareers": ("https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
                  "CSCareers", "intern"),
    "fulltime": ("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
                 "Simplify", "fulltime"),
}
TERMS = ["Summer 2027", "Fall 2027", "Winter 2027", "Spring 2027"]    # internship seasons to pick from
ROLES = {"intern": "Internships", "fulltime": "Full-time", "both": "Internships + full-time"}
DEFAULT_SETTINGS = {"us_only": True, "roles": "intern", "term": "Summer 2027", "min_score": 3,
                    "weights": {}, "custom": []}
INTERN_RE = re.compile(r"(?i)\b(?:intern(?:ship)?s?|co-?op|summer (?:analyst|associate)|apprentice(?:ship)?"
                       r"|student|fellow(?:ship)?)\b")
SKIP_CATEGORIES = {"Product"}          # product management roles
SCRAPE_EVERY = timedelta(hours=3)
MAX_AGE = timedelta(days=90)           # a posting is deleted this long after it was posted…
MAX_STAY = timedelta(days=30)          # …or after this long in the archive, whichever comes first
UNDO_REJECTS = 10                      # rejections you can take back with ↩️ Undo reject
PAGE_SIZE = 6                          # 6 postings x 3 buttons keeps a page under Discord's 40 components


def title_key(company: str | None, title: str | None) -> str:
    """"t:<company>|<title>" in letters and digits: the same posting on two lists with different links."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", (x or "").lower())
    return f"t:{norm(company)}|{norm(title)}"


def entry_id(url: str) -> str:
    """Short stable id (fits in a button's custom_id) from the link's canonical form."""
    return hashlib.sha1(jobs.canonical(url).encode()).hexdigest()[:12]


def role_of(e: dict) -> str:
    """"intern" or "fulltime": from the list it came from, else from the title."""
    return e.get("role") or ("intern" if INTERN_RE.search(e.get("title") or "") else "fulltime")


class Archive:
    """{"entries": {id: entry}, "handled": {id: when}, "settings": {...}, "etags": {feed: etag},
    "last_scrape": ..., "message": [channel id, message id], "folder": ..., "page": ...}.
    `handled` remembers postings you promoted, applied to or removed, or that expired, so the
    scraper never brings them back. `message`/`folder`/`page`: the always-open message in
    #application-archive and what it's showing ("settings"/"keywords" are its setup screens)."""

    def __init__(self, path: Path = ARCHIVE_FILE):
        self.path = path
        self.data = {"entries": {}, "handled": {}, "etags": {}, "last_scrape": None,
                     "message": None, "folder": None, "page": 0}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        if "settings" not in self.data:            # first run with settings: re-read the lists so
            self.data["settings"] = {}             # postings already here get their season and places
            self.rescrape()
        self.data.pop("etag", None)                # replaced by per-list "etags"
        self.data["settings"] = {**DEFAULT_SETTINGS, **self.data["settings"]}

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def rescrape(self):
        """After a settings change that affects what's imported: re-read the lists on the bot's
        next loop (within ~20 s)."""
        self.data["etags"], self.data["last_scrape"] = {}, None

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @property
    def entries(self) -> dict[str, dict]:
        return self.data["entries"]

    def has(self, url: str) -> bool:
        return entry_id(url) in self.entries

    def handle(self, url: str, company: str | None = None, title: str | None = None):
        """You promoted, applied to, rejected or removed this posting, or it expired: keep every
        list from re-adding it (by its link, and by company + title for lists with other links)."""
        when = datetime.now(timezone.utc).isoformat()
        self.data["handled"][entry_id(url)] = when
        if title:
            self.data["handled"][title_key(company, title)] = when

    def reject(self, eid: str) -> str | None:
        """You don't want this posting: delete it, and keep the lists from re-adding it. The last
        UNDO_REJECTS rejections are kept for undo_reject. Returns a short confirmation."""
        e = self.entries.pop(eid, None)
        if e is None:
            return None
        self.handle(e["url"], e.get("company"), e.get("title"))
        stack = self.data.setdefault("rejected", [])
        stack.append({"id": eid, "entry": e})
        del stack[:-UNDO_REJECTS]
        self.save()
        return f"✖ Rejected: {e.get('title') or 'Untitled role'} · {e.get('company') or '?'}"

    def undo_reject(self) -> str | None:
        """Put back the most recently rejected posting (and let the lists see it again)."""
        stack = self.data.get("rejected") or []
        if not stack:
            return None
        r = stack.pop()
        e = self.entries[r["id"]] = r["entry"]
        self.data["handled"].pop(r["id"], None)
        self.data["handled"].pop(title_key(e.get("company"), e.get("title")), None)
        self.save()
        return f"↩️ Restored: {e.get('title') or 'Untitled role'} · {e.get('company') or '?'}"

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
            e = self.entries.pop(eid)
            self.handle(e["url"], e.get("company"), e.get("title"))    # still listed? don't re-add it
        return len(old)

    def scored(self) -> list[tuple[str, dict, int, list[str]]]:
        """Every posting as (id, entry, score, matched), best fit first, newest first on ties."""
        kws = fit.keywords(self.settings)
        rows = [(eid, e, *fit.score(e, e.get("desc", ""), kws)) for eid, e in self.entries.items()]
        rows.sort(key=lambda r: (r[2], r[1].get("posted") or ""), reverse=True)
        return rows

    def wanted(self, row: tuple[str, dict, int, list[str]]) -> bool:
        """Does a posting pass the ⚙️ Filters? A posting with no location passes "US only"."""
        _, e, score, _ = row
        s, role = self.settings, role_of(e)
        where = " · ".join(e.get("places") or []) or e.get("location")
        return ((not s["us_only"] or places.is_us(where) is not False)
                and s["roles"] in (role, "both")
                and (role != "intern" or e["source"] != "Simplify" or s["term"] in e.get("terms", [s["term"]]))
                and score >= s["min_score"])

    def shown(self) -> list[tuple[str, dict, int, list[str]]]:
        """The postings the archive shows: scored, filtered, best fit first."""
        return [r for r in self.scored() if self.wanted(r)]


def in_folder(folder: str, row: tuple[str, dict, int, list[str]], now: datetime) -> bool:
    """Is this posting in this folder? Closed postings only appear in the 🔒 folder."""
    _, e, _, matched = row
    if (folder == "closed") != bool(e.get("closed")):
        return False
    if folder == "board":
        return e["source"] == "board"
    if folder == "new":
        return now - datetime.fromisoformat(e["posted"]) <= timedelta(days=3)
    if folder.startswith("grp:"):
        boosts = {m.split(" ", 1)[1] for m in matched if not m.startswith("-")}
        group = next((labels for key, _, _, labels in GROUPS if key == folder), None)
        return bool(boosts & group) if group is not None else bool(boosts - GROUPED)
    return True


# ---------------------------------------------------------------- Simplify / Pitt CSC list

def _desc(listing: dict) -> str:
    """Extra text fit.py scores: the list's category, and the degrees it accepts (a posting open
    to Bachelor's students counts as Bachelor's; one only for Master's/PhD as grad-only)."""
    degrees = listing.get("degrees") or []
    deg = ("Bachelor's" if "Bachelor's" in degrees
           else "graduate students only" if set(degrees) & {"Master's", "PhD", "MBA"} else "")
    return f"{listing.get('category') or ''} {deg}".strip()


async def _fetch(feed: str, etag: str | None) -> tuple[list | None, str | None]:
    """(listings, new ETag) from one list, or (None, etag) if it hasn't changed."""
    async with aiohttp.ClientSession() as session:
        async with session.get(FEEDS[feed][0], headers={"If-None-Match": etag} if etag else {},
                               timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status == 304:
                return None, etag
            r.raise_for_status()
            raw, new_etag = await r.read(), r.headers.get("ETag")
    return await asyncio.to_thread(json.loads, raw), new_etag     # ~13 MB: parse off the event loop


def _in_season(feed: str, x: dict, term: str) -> bool:
    """Is a listing for your internship season? Simplify lists terms ("Summer 2027"); CSCareers'
    list is all 2027 and gives a season ("Summer", "Spring/Summer")."""
    if FEEDS[feed][2] != "intern":
        return True
    if feed == "cscareers":
        return term.split()[0] in (x.get("season") or "")
    return term in (x.get("terms") or [])


def _feed_of(e: dict) -> str | None:
    """Which list a stored posting came from (postings stored before "feed" existed: by role)."""
    return e.get("feed") or (e.get("role", "intern") if e["source"] == "Simplify" else None)


async def scrape_simplify(archive: Archive, known: set[str]) -> tuple[list[str], int] | None:
    """Sync the archive with the lists your role setting asks for (internships: Simplify, then
    CSCareers; full-time: Simplify's New Grad list): add new active postings (internships only
    for your season), refresh ones already here, and drop ones a list no longer has as active.
    A posting another list already gave is skipped (same company + title). `known`: canonical
    links already on the board. Returns (ids added, number removed), or None if no list changed
    since last time (checked by ETag)."""
    s, now = archive.settings, datetime.now(timezone.utc)
    feeds = [f for f, (_, _, role) in FEEDS.items() if s["roles"] in (role, "both")]
    archive.data["last_scrape"] = now.isoformat()
    added, removed, changed = [], 0, False
    entries, handled = archive.entries, archive.data["handled"]
    for feed in feeds:
        url, source, role = FEEDS[feed]
        listings, archive.data["etags"][feed] = await _fetch(feed, archive.data["etags"].get(feed))
        if listings is None:
            continue
        changed = True
        active = {}
        for x in listings:
            if (x.get("active") and x.get("is_visible", True) and x.get("url")
                    and x.get("category") not in SKIP_CATEGORIES and _in_season(feed, x, s["term"])):
                active[entry_id(x["url"])] = x
        gone = [eid for eid, e in entries.items() if _feed_of(e) == feed and eid not in active]
        for eid in gone:
            del entries[eid]
        removed += len(gone)
        elsewhere = {title_key(e.get("company"), e.get("title")) for eid, e in entries.items()
                     if _feed_of(e) != feed}
        for eid, x in active.items():
            fresh = {"title": x.get("title"), "company": x.get("company_name"),
                     "location": " · ".join(x.get("locations") or [])[:80], "places": x.get("locations") or [],
                     "desc": _desc(x), "role": role, "feed": feed,
                     "terms": x.get("terms") or ([s["term"]] if feed == "cscareers" else [])}
            if eid in entries:
                entries[eid].update(fresh)                    # keeps when it was found
                continue
            tkey = title_key(fresh["company"], fresh["title"])
            if eid in handled or tkey in handled or tkey in elsewhere or jobs.canonical(x["url"]) in known:
                continue
            posted = datetime.fromtimestamp(x.get("date_posted") or 0, timezone.utc)
            if now - posted > MAX_AGE:
                continue                                      # would expire right away
            entries[eid] = {**fresh, "url": x["url"], "source": source, "posted": posted.isoformat(),
                            "found": now.isoformat(), "closed": None, "item": None}
            elsewhere.add(tkey)
            added.append(eid)
    archive.save()
    return (added, removed) if changed else None


# ---------------------------------------------------------------- the archive message

FIXED_FOLDERS = [("all", "🗂️", "Everything"), ("new", "🆕", "Posted in the last 3 days"),
                 ("board", "📌", "From my board"), ("closed", "🔒", "Closed")]


# Keyword folders: a posting is in one when it matches any of the group's keywords (as a boost).
# Keywords you add in 🔑 Keywords go in ⭐ My keywords; Bachelor's only counts toward fit.
GROUPS = [
    ("grp:quant", "📈", "Quant & trading", {"Quant", "Trading", "Market making / options", "Finance"}),
    ("grp:software", "💻", "Software", {"Software engineering", "C++", "Cloud / infra"}),
    ("grp:data", "🤖", "Data & AI", {"Data science", "ML / AI", "Research"}),
    ("grp:hardware", "🔌", "Hardware & ECE", {"Embedded", "Hardware", "Signals / wireless"}),
]
GROUPED = set().union(*(labels for *_, labels in GROUPS)) | {"Bachelor's"}


def folders(settings: dict) -> list[tuple[str, str, str]]:
    """(key, emoji, name): Everything and New, the keyword groups, ⭐ My keywords if you added
    any, then 📌 From my board and 🔒 Closed (the home screen hides empty ones)."""
    mine = [("grp:mine", "⭐", "My keywords")] if settings.get("custom") else []
    return (FIXED_FOLDERS[:2] + [(key, emoji, name) for key, emoji, name, _ in GROUPS] + mine
            + FIXED_FOLDERS[2:])


def _button(label: str, custom_id: str, callback, style=discord.ButtonStyle.secondary,
            emoji: str | None = None, disabled: bool = False) -> discord.ui.Button:
    b = discord.ui.Button(label=label[:80], custom_id=custom_id[:100], style=style, emoji=emoji,
                          disabled=disabled)
    b.callback = callback
    return b


class KeywordModal(discord.ui.Modal, title="Add a keyword"):
    name = discord.ui.TextInput(label="Name", placeholder="Robotics", max_length=30)
    words = discord.ui.TextInput(label="Words that match it (comma-separated)", max_length=200,
                                 placeholder="robotics, robot, autonomous")
    weight = discord.ui.TextInput(label="Weight: 3 (strong), 1, or -2 (push down)", default="1", max_length=2)

    def __init__(self, view: "ArchiveView"):
        super().__init__()
        self.archive_view = view

    async def on_submit(self, interaction: discord.Interaction):
        words = [w.strip() for w in self.words.value.split(",") if w.strip()]
        try:
            weight = int(self.weight.value.strip().replace("+", ""))
        except ValueError:
            weight = 0
        if not words or weight not in fit.WEIGHTS[:3]:
            await interaction.response.send_message("Give at least one word and a weight of 3, 1 or -2.",
                                                    ephemeral=True)
            return
        custom = self.archive_view.archive.settings["custom"]
        custom[:] = [c for c in custom if c["label"] != self.name.value.strip()]
        custom.append({"label": self.name.value.strip(), "words": words, "weight": weight})
        self.archive_view.archive.save()
        await interaction.response.edit_message(view=self.archive_view.redraw(f"➕ Added keyword {self.name.value.strip()}"))


class ArchiveView(discord.ui.LayoutView):
    """The always-open message in #application-archive. Screens, by the saved "folder": none =
    the folders as buttons; a folder = its postings best fit first, PAGE_SIZE at a time, each
    with ⬆️ Promote / ✅ Applied / ✖ Reject; "settings" = ⚙️ Filters; "keywords" = 🔑 keyword
    weights. `take(entry_id, applied)` moves a posting to the board (or straight to your applied
    records) and returns a short confirmation (None if it's gone). Which
    screen and page it shows is saved in archive.json, so a restart reopens the same place.
    Only `owner_id` can press anything."""

    def __init__(self, archive: Archive, take: Callable[[str, bool], Awaitable[str | None]],
                 owner_id: int | None, note: str = ""):
        super().__init__(timeout=None)
        self.archive, self.take, self.owner_id = archive, take, owner_id
        screen = archive.data.get("folder")
        container = discord.ui.Container(accent_colour=discord.Colour.gold())
        if screen == "settings":
            self._settings(container, note)
        elif screen == "keywords":
            self._keywords(container, note)
        else:
            rows, now = archive.shown(), datetime.now(timezone.utc)
            names = {key: f"{emoji} {name}" for key, emoji, name in folders(archive.settings)}
            if screen in names:
                self._folder(container, names[screen], [r for r in rows if in_folder(screen, r, now)], note)
            else:
                self._home(container, rows, now, note)
        self.add_item(container)

    # ------------------------------------------------ screens

    def _home(self, container, rows, now, note):
        s = self.archive.settings
        summary = (f"{ROLES[s['roles']]}"
                   + (f" ({s['term']})" if s["roles"] != "fulltime" else "")
                   + f" · {'US only' if s['us_only'] else 'anywhere'} · fit {s['min_score']:+d} or better")
        container.add_item(discord.ui.TextDisplay(
            f"## 💼 Job archive\n**{sum(not r[1].get('closed') for r in rows)}** postings match your filters "
            f"(of {len(self.archive.entries)} stored) · pick a folder\n-# {summary}"
            + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        buttons = []
        for key, emoji, name in folders(s):
            n = sum(in_folder(key, r, now) for r in rows)
            if n or key == "all":
                buttons.append(_button(f"{name} · {n}", f"arcf:{key}", self._go(key), emoji=emoji,
                                       style=discord.ButtonStyle.primary if key == "all"
                                       else discord.ButtonStyle.secondary))
        for i in range(0, min(len(buttons), 20), 4):          # up to 5 rows of folders
            container.add_item(discord.ui.ActionRow(*buttons[i:i + 4]))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            "-# Unapplied board jobs land here after 7 days; new postings every 3 h (Simplify / Pitt CSC, CSCareers). "
            "Postings are deleted 90 days after posting or 30 days here."))
        container.add_item(discord.ui.ActionRow(
            _button("Filters", "arcn:settings", self._go("settings"), emoji="⚙️")))

    def _folder(self, container, title, rows, note):
        pages = max(1, -(-len(rows) // PAGE_SIZE))
        page = min(self.archive.data.get("page", 0), pages - 1)
        container.add_item(discord.ui.TextDisplay(
            f"## {title}\n{len(rows)} postings · best fit first" + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        for eid, e, score, matched in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            name = (e.get("title") or "Untitled role")[:80].replace("[", "(").replace("]", ")")
            keys = ", ".join(m.split(" ", 1)[1] for m in matched) or "no keywords"
            text = (f"**{score:+d}** [{name}]({e['url']}) · **{(e.get('company') or '?')[:40]}**\n"
                    f"-# {(e.get('location') or '?')[:60]} · {keys} · "
                    f"{'from your board' if e['source'] == 'board' else e['source']} "
                    f"{(e.get('posted') or '')[:10]}")
            container.add_item(discord.ui.TextDisplay(text))
            container.add_item(discord.ui.ActionRow(
                _button("Reopen" if e.get("closed") else "Promote", f"arc:{eid}", self._taker(eid, False),
                        style=discord.ButtonStyle.primary, emoji="⬆️"),
                _button("Applied", f"arca:{eid}", self._taker(eid, True),
                        style=discord.ButtonStyle.success, emoji="✅"),
                _button("Reject", f"arcr:{eid}", self._rejecter(eid),
                        style=discord.ButtonStyle.danger, emoji="✖️")))
        if not rows:
            container.add_item(discord.ui.TextDisplay("*Nothing here.*"))
        container.add_item(discord.ui.Separator())
        folder = self.archive.data["folder"]
        nav = [_button("Folders", "arcn:home", self._go(None), emoji="⬅️"),
               _button("◀", "arcn:prev", self._go(folder, page - 1), disabled=page == 0),
               _button(f"Page {page + 1}/{pages}", "arcn:page", self._go(folder, page), disabled=True),
               _button("▶", "arcn:next", self._go(folder, page + 1), disabled=page >= pages - 1)]
        if self.archive.data.get("rejected"):
            nav.append(_button("Undo reject", "arcn:undo", self._undo, emoji="↩️"))
        container.add_item(discord.ui.ActionRow(*nav))

    def _settings(self, container, note):
        s = self.archive.settings
        shown, total = len(self.archive.shown()), len(self.archive.entries)
        container.add_item(discord.ui.TextDisplay(
            f"## ⚙️ Filters\nShowing **{shown}** of {total} stored postings. Tap a button to change it."
            "\n-# Role type and season also decide what's imported: internships come from Simplify's "
            "and CSCareers' lists, full-time from Simplify's New Grad list."
            + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.ActionRow(
            _button("US only" if s["us_only"] else "Anywhere", "arcs:us", self._set("us"),
                    emoji="📍", style=discord.ButtonStyle.primary),
            _button(ROLES[s["roles"]], "arcs:roles", self._set("roles"), emoji="💼",
                    style=discord.ButtonStyle.primary),
            _button(s["term"], "arcs:term", self._set("term"), emoji="📅", style=discord.ButtonStyle.primary,
                    disabled=s["roles"] == "fulltime")))
        container.add_item(discord.ui.ActionRow(
            _button("−", "arcs:min-", self._set("min-"), disabled=s["min_score"] <= -4),
            _button(f"Minimum fit {s['min_score']:+d}", "arcs:min", self._set("noop"), disabled=True),
            _button("+", "arcs:min+", self._set("min+"), disabled=s["min_score"] >= 10)))
        container.add_item(discord.ui.Separator())
        on = sum(1 for _, w, _, _ in fit.keywords(s) if w)
        container.add_item(discord.ui.ActionRow(
            _button("Folders", "arcn:home", self._go(None), emoji="⬅️"),
            _button(f"Keywords ({on} on)", "arcn:keywords", self._go("keywords"), emoji="🔑")))

    def _keywords(self, container, note):
        kws = fit.keywords(self.archive.settings)
        container.add_item(discord.ui.TextDisplay(
            "## 🔑 Keywords\nTap one to change its weight: +3 → +1 → −2 → off. A posting's fit is the "
            "sum of the keywords in its title, company and listing details."
            + (f"\n{note}" if note else "")))
        container.add_item(discord.ui.Separator())
        buttons = []
        for i, (label, weight, _, _) in enumerate(kws[:fit.MAX_KEYWORDS]):
            style = (discord.ButtonStyle.success if weight > 0 else discord.ButtonStyle.danger if weight < 0
                     else discord.ButtonStyle.secondary)
            buttons.append(_button(f"{label} {weight:+d}" if weight else f"{label} · off", f"arck:{i}",
                                   self._cycle(label), style=style))
        for i in range(0, len(buttons), 4):
            container.add_item(discord.ui.ActionRow(*buttons[i:i + 4]))
        container.add_item(discord.ui.Separator())
        add = discord.ui.Button(label="Add keyword", emoji="➕", custom_id="arcn:addkw",
                                disabled=len(kws) >= fit.MAX_KEYWORDS)
        add.callback = self._add_keyword
        container.add_item(discord.ui.ActionRow(
            _button("Filters", "arcn:settings", self._go("settings"), emoji="⬅️"), add))

    # ------------------------------------------------ actions

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This archive is private.", ephemeral=True)
        return False

    def redraw(self, note: str = "") -> "ArchiveView":
        return ArchiveView(self.archive, self.take, self.owner_id, note)

    def _go(self, screen: str | None, page: int = 0):
        async def callback(interaction: discord.Interaction):
            self.archive.data["folder"], self.archive.data["page"] = screen, max(0, page)
            self.archive.save()
            await interaction.response.edit_message(view=self.redraw())
        return callback

    def _set(self, what: str):
        async def callback(interaction: discord.Interaction):
            s = self.archive.settings
            if what == "us":
                s["us_only"] = not s["us_only"]
            elif what == "roles":
                order = list(ROLES)
                s["roles"] = order[(order.index(s["roles"]) + 1) % len(order)]
                self.archive.rescrape()
            elif what == "term":
                s["term"] = TERMS[(TERMS.index(s["term"]) + 1) % len(TERMS)] if s["term"] in TERMS else TERMS[0]
                self.archive.rescrape()
            elif what in ("min-", "min+"):
                s["min_score"] += 1 if what == "min+" else -1
            self.archive.save()
            note = "-# New postings for this setting are being imported now." if what in ("roles", "term") else ""
            await interaction.response.edit_message(view=self.redraw(note))
        return callback

    def _cycle(self, label: str):
        async def callback(interaction: discord.Interaction):
            s = self.archive.settings
            current = next(w for l, w, _, _ in fit.keywords(s) if l == label)
            nxt = fit.WEIGHTS[(fit.WEIGHTS.index(current) + 1) % len(fit.WEIGHTS)] if current in fit.WEIGHTS else 0
            custom = next((c for c in s["custom"] if c["label"] == label), None)
            if custom:
                custom["weight"] = nxt
            else:
                s["weights"][label] = nxt
            self.archive.save()
            await interaction.response.edit_message(view=self.redraw())
        return callback

    async def _add_keyword(self, interaction: discord.Interaction):
        await interaction.response.send_modal(KeywordModal(self))

    def _taker(self, eid: str, applied: bool):
        async def callback(interaction: discord.Interaction):
            note = await self.take(eid, applied) or "That posting is no longer in the archive."
            await interaction.response.edit_message(view=self.redraw(note))
        return callback

    async def _undo(self, interaction: discord.Interaction):
        note = self.archive.undo_reject() or "Nothing to undo."
        await interaction.response.edit_message(view=self.redraw(note))

    def _rejecter(self, eid: str):
        async def callback(interaction: discord.Interaction):
            note = self.archive.reject(eid) or "That posting is no longer in the archive."
            await interaction.response.edit_message(view=self.redraw(note))
        return callback
