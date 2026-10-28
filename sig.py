"""
!sig: a summary of the SIG Predictions Cup trading bot (Documents/SIGCUP) from its /status
endpoint. Plain code, no Claude. Needs SIG_BOT_URL in .env: the bot's control URL, which
contains its secret, so it never goes in a message or the log.
"""

import os
from datetime import datetime, timezone

import aiohttp
import discord

SETTLES = datetime(2026, 11, 4, 17, 0, tzinfo=timezone.utc)
STALE_SECONDS = 180                   # a tick or Kalshi price older than this gets a ⚠️


def channels() -> set[str]:
    """Channel names where !sig works (SIG_CHANNELS in .env, comma-separated)."""
    return {c.strip().lstrip("#").lower() for c in os.getenv("SIG_CHANNELS", "random,quant,code").split(",")}


async def fetch_status() -> dict:
    url = os.getenv("SIG_BOT_URL")
    if not url:
        raise RuntimeError("SIG_BOT_URL isn't set in .env")
    async with aiohttp.ClientSession() as session:
        async with session.get(url.rstrip("/") + "/status", timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                raise RuntimeError(f"the SIG bot answered HTTP {r.status}")
            return await r.json(content_type=None)


def _age(iso: str | None, now: datetime) -> tuple[str, bool]:
    """("37s ago", stale?) for an ISO timestamp."""
    if not iso:
        return "never", True
    secs = (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    text = f"{secs:.0f}s ago" if secs < 120 else f"{secs / 60:.0f} min ago" if secs < 7200 else f"{secs / 3600:.0f} h ago"
    return text, secs > STALE_SECONDS


def _usd(x: float | None) -> str:
    return "?" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.0f}"


def status_embed(s: dict) -> discord.Embed:
    now = datetime.now(timezone.utc)
    tick, tick_stale = _age(s.get("lastTick"), now)
    state = "⏸️ Paused" if s.get("paused") else "🟢 Live" if s.get("live") else "🔴 Not live"
    k = s.get("kalshi") or {}
    mark, ref = k.get("mark") or {}, k.get("markRef") or {}
    prices, prices_stale = _age(k.get("fetchedAt"), now)
    pnl = (s.get("pnlHistory") or [{}])[-1]
    left = SETTLES - now

    lines = [
        f"**{state}** · last tick {tick}{' ⚠️' if tick_stale else ''} · errors {s.get('errors', '?')}",
        f"Equity **{_usd(s.get('equity'))}** · cash {_usd(s.get('balance'))}",
        "",
        f"**Kalshi convergence:** P&L **{_usd(mark.get('total'))}** at SIG prices "
        f"({_usd(mark.get('realized'))} realized) · {_usd(ref.get('total'))} at Kalshi/Poly prices",
        f"{len(k.get('positions') or {})} positions · {_usd(mark.get('deployed'))} deployed"
        + (" · ⛔ **halted** (loss limit)" if k.get("halted") else ""),
        f"Kalshi prices updated {prices}"
        + (" ⚠️ relay may be down" if prices_stale or k.get("error") else ""),
        f"**Market making** (paper): {_usd(pnl.get('mm'))} · **D-lean:** {_usd(pnl.get('lean'))}",
    ]
    if trades := k.get("trades"):
        t = trades[0]
        when, _ = _age(t.get("at"), now)
        lines.append(f"Last trade {when}: {t.get('kind')} · {t.get('side')} {t.get('qty')} "
                     f"{t.get('race')} ({t.get('party')}) @ {t.get('px')}")
    lines.append("")
    lines.append(f"Settles <t:{int(SETTLES.timestamp())}:f> ({left.days}d {left.seconds // 3600}h left)"
                 if left.total_seconds() > 0 else "Tournament settled.")
    return discord.Embed(title="📈 SIG Predictions Cup bot", description="\n".join(lines),
                         color=discord.Color.dark_green() if s.get("live") and not tick_stale
                         else discord.Color.red())
