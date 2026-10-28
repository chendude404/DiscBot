"""
Job links posted in #jobs. Every link there is treated as a job posting; Claude reads the
URL plus Discord's link preview to fill in title, company, location and deadline.
"""

import json
import re
from urllib.parse import urlsplit

URL_RE = re.compile(r"https?://[^\s<>]+")


def find_urls(text: str) -> list[str]:
    urls = [u.rstrip(".,;:!?)]>'\"") for u in URL_RE.findall(text)]
    return list(dict.fromkeys(urls))            # de-duplicate, keep order


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
