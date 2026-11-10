"""
Job links posted in #jobs. Every link there is treated as a job posting; Claude reads the
URL plus Discord's link preview to fill in title, company, location and deadline.
"""

import json
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import aiohttp

URL_RE = re.compile(r"https?://[^\s<>]+")
# Query keys that only say where you came from, not which job it is (gh_jid, jk, ... are kept).
TRACKING = {"source", "src", "ref", "referrer", "gh_src", "lever-source", "lever-origin",
            "trk", "trackingid", "refid", "share", "si", "fbclid", "gclid", "from_page"}
LOCALE_RE = re.compile(r"[a-z]{2}[-_][a-z]{2}", re.I)          # "/en-US/" path segments


def find_urls(text: str) -> list[str]:
    urls = [u.rstrip(".,;:!?)]>'\"") for u in URL_RE.findall(text)]
    return list(dict.fromkeys(urls))            # de-duplicate, keep order


def _job_path(url: str) -> tuple[str, list[str]]:
    """(host, path segments) with locale segments and anything from "/apply" on removed."""
    parts = urlsplit(url)
    segs = [unquote(s) for s in parts.path.split("/") if s and not LOCALE_RE.fullmatch(s)]
    if "apply" in segs:                               # ".../job/X/apply/..." is job X
        segs = segs[:segs.index("apply")]
    return parts.netloc.lower().removeprefix("www."), segs


def canonical(url: str) -> str:
    """The same posting reached through different links compares equal: no http(s)/www,
    tracking parameters, locale segment, fragment, trailing slash or /apply page."""
    host, segs = _job_path(url)
    if host.endswith("myworkdayjobs.com") and "job" in segs:
        segs = segs[-1:]          # Workday: the last part ends in the req ID; the location part varies
    query = sorted((k, v) for k, v in parse_qsl(urlsplit(url).query)
                   if not k.lower().startswith("utm_") and k.lower() not in TRACKING)
    return f"{host}/{'/'.join(segs)}" + (f"?{urlencode(query)}" if query else "")


# ---------------------------------------------------------------- closed-posting check (no Claude)

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
CLOSED_RE = re.compile(r"no longer (?:available|accepting|open|active)|position has been filled|"
                       r"job (?:is|has been) closed|posting (?:is|has) (?:closed|expired)|job not found",
                       re.I)


def _workday_api(url: str) -> str | None:
    """Workday pages are blank until JavaScript runs; their JSON API answers 404 for removed jobs."""
    host, segs = _job_path(url)
    if not host.endswith("myworkdayjobs.com") or "job" not in segs or segs.index("job") == 0:
        return None
    site, job = segs[segs.index("job") - 1], segs[segs.index("job"):]
    return f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/" + "/".join(job)


async def link_status(session: aiohttp.ClientSession, url: str) -> str | None:
    """"gone" (404/410: the posting was removed), "closed?" (a weaker sign: the page says it's
    closed, or it redirects somewhere without the job's ID), "open", or None when the site
    didn't give a usable answer (blocked, timeout, error); None never closes anything."""
    try:
        async with session.get(_workday_api(url) or url, allow_redirects=True,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status in (404, 410):
                return "gone"
            if r.status != 200:
                return None
            body = await r.text(errors="ignore")
            job_id = max(re.findall(r"\d{5,}", urlsplit(url).path), key=len, default=None)
            if CLOSED_RE.search(body) or (job_id and job_id not in str(r.url)):
                return "closed?"
            return "open"
    except (aiohttp.ClientError, TimeoutError, UnicodeError):
        return None


def preview_text(embed) -> str:
    """The parts of a Discord link preview (discord.Embed) worth showing Claude."""
    bits = [f"Site: {embed.provider.name}" if embed.provider and embed.provider.name else "",
            f"Title: {embed.title}" if embed.title else "",
            f"Description: {embed.description[:800]}" if embed.description else ""]
    return "\n".join(b for b in bits if b)


def job_prompt(url: str, note: str, preview: str) -> str:
    return ("Extract the details of this job posting. You can't open the link; use the URL "
            "itself (it often contains the company, role and location), the link preview, and "
            "the user's note. Reply with ONLY JSON:\n"
            '{"title": "<role>" or null, "company": "<employer>" or null, '
            '"location": "<city/state or country>" or null, '
            '"work_mode": "Remote" | "Hybrid" | "On-site" | null, '
            '"deadline": "YYYY-MM-DD" or null, "pay": "<pay range>" or null}\n'
            "Use null for anything you can't tell. Make titles readable "
            '(e.g. "Quantitative-Developer-Intern_JR104315" -> "Quantitative Developer Intern").\n'
            f"URL: {url}\n"
            + (f"Link preview:\n{preview}\n" if preview else "")
            + (f"User's note: {note}\n" if note else ""))


def parse_job(reply: str) -> dict | None:
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        obj = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    keys = ("title", "company", "location", "work_mode", "deadline", "pay")
    return {k: (str(obj[k]).strip()[:80] if obj.get(k) else None) for k in keys}


def fallback(url: str) -> dict:
    """Used when Claude is unavailable: name the job after the website."""
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return {"title": None, "company": host, "location": None, "work_mode": None,
            "deadline": None, "pay": None}
