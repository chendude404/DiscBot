"""
Where each job application stands: applied → 📝 OA → 🎤 Interview → 🎉 Offer, or ❌ Rejected.
Plain code, no Claude. The archive's 📊 Applications screen sets stages; the history is kept on
the applied job's item as item["job"]["stages"] = [[stage, ISO time], ...] (last = current).
An application that hasn't moved in NO_REPLY_DAYS counts as 👻 no reply (worked out, not stored).
"""

from datetime import datetime, timedelta

import todo

STAGES ={"oa": ("📝", "OA"), "interview": ("🎤", "Interview"), "offer": ("🎉", "Offer"),
          "rejected": ("❌", "Rejected")}
ORDER = ["oa", "interview", "offer"]           # how far it got, for the funnel
NO_REPLY_DAYS = 30


def applications(items: list[dict], now: datetime) -> list[dict]:
    """Jobs marked applied this season (since July 1), newest first. tidy() keeps these."""
    season = todo.season_start(now)
    return sorted((i for i in items if i["kind"] == "job" and i["done"] and i.get("job")
                   and i["done_at"] and todo.dt(i["done_at"]) >= season),
                  key=lambda i: i["done_at"], reverse=True)


def history(item: dict) -> list[list[str]]:
    return (item.get("job") or {}).get("stages") or []


def current(item: dict) -> str | None:
    """The latest stage, or None while it's only applied."""
    h = history(item)
    return h[-1][0] if h else None


def last_change(item: dict) -> datetime:
    h = history(item)
    return datetime.fromisoformat(h[-1][1] if h else item["done_at"])


def reached(item: dict, stage: str) -> bool:
    """Did it ever get to `stage` (or further)? An interview means the OA, if any, was passed."""
    if stage == "rejected":
        return current(item) == "rejected"
    later = ORDER[ORDER.index(stage):]
    return any(s in later for s, _ in history(item))


def no_reply(item: dict, now: datetime) -> bool:
    """Still waiting (not an offer or a rejection) and nothing has happened for NO_REPLY_DAYS."""
    return (current(item) not in ("offer", "rejected")
            and now - last_change(item) > timedelta(days=NO_REPLY_DAYS))


def toggle(item: dict, stage: str, now: datetime) -> str | None:
    """Tap a stage: it becomes the current one. Tapping the current stage again takes it back
    off (the one before is current again). Returns the new current stage."""
    h = item["job"].setdefault("stages", [])
    if h and h[-1][0] == stage:
        h.pop()
    else:
        h.append([stage, now.isoformat()])
    return current(item)


def funnel(apps: list[dict], now: datetime) -> str:
    """"Applied 42 → 📝 OA 7 → 🎤 Interview 3 → 🎉 Offer 1 · ❌ 12 · 👻 19"."""
    steps = [f"Applied **{len(apps)}**"] + [
        f"{STAGES[s][0]} {STAGES[s][1]} **{sum(reached(a, s) for a in apps)}**" for s in ORDER]
    return (" → ".join(steps)
            + f" · ❌ **{sum(reached(a, 'rejected') for a in apps)}**"
            + f" · 👻 no reply **{sum(no_reply(a, now) for a in apps)}**")


def trail(item: dict) -> str:
    """Its stages so far: "📝 OA 10/30 → 🎤 Interview 11/06" ("" while only applied)."""
    return " → ".join(f"{STAGES[s][0]} {STAGES[s][1]} {datetime.fromisoformat(t):%m/%d}"
                      for s, t in history(item))


def label(item: dict, now: datetime) -> str:
    """trail() for the tracker, plus 👻 if it went quiet, or ⏳ if nothing has happened yet."""
    text = trail(item)
    if no_reply(item, now):
        text += (" · " if text else "") + f"👻 no reply in {NO_REPLY_DAYS}+ days"
    return text or "⏳ waiting to hear back"
