"""
Talks to Claude through the Claude Agent SDK, authenticated with your Claude
Pro/Max subscription instead of an API key.

Auth (one time): run `claude setup-token`, then put the token it prints in .env as
    CLAUDE_CODE_OAUTH_TOKEN=...
Do NOT also set ANTHROPIC_API_KEY, or Claude Code will bill the API key instead.

Every call runs Claude Code with all tools disabled and no user/project settings
loaded, so it behaves like a plain chat model.
"""

import os
import tempfile
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

MODEL = os.getenv("CLAUDE_MODEL", "sonnet")        # "sonnet", "haiku", or "opus"

# Isolated working dir so Claude Code never picks up a CLAUDE.md or project settings.
WORKDIR = Path(tempfile.gettempdir()) / "discord-claude-bot"
WORKDIR.mkdir(exist_ok=True)


class ClaudeError(RuntimeError):
    pass


def _options(system: str, resume: str | None, model: str | None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=system,
        model=model or MODEL,
        tools=[],                  # no Bash/Read/Edit/web: text and images in, text out
        setting_sources=[],        # ignore ~/.claude settings and CLAUDE.md files
        max_turns=1,
        cwd=str(WORKDIR),
        resume=resume,
    )


async def _single(content):
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "default",
    }


async def ask(content: str | list[dict], system: str,
              resume: str | None = None, model: str | None = None) -> tuple[str, str | None]:
    """Send one user message (a string, or a list of text/image content blocks).
    Pass `resume` (a session id from an earlier call) to continue that conversation, and
    `model` to override CLAUDE_MODEL for this call. Returns (reply_text, session_id)."""
    texts, session_id, result = [], None, None
    try:
        async for msg in query(prompt=_single(content), options=_options(system, resume, model)):
            if isinstance(msg, AssistantMessage):
                texts += [b.text for b in msg.content if isinstance(b, TextBlock)]
            elif isinstance(msg, ResultMessage):
                result, session_id = msg, msg.session_id
    except Exception as e:  # CLI missing, not logged in, process crashed, ...
        raise ClaudeError(_friendly(str(e))) from e

    if result is not None and result.is_error:
        detail = result.result or "; ".join(result.errors or []) or result.subtype
        raise ClaudeError(_friendly(detail))
    text = "".join(texts).strip() or (result.result or "" if result else "").strip()
    return text, session_id


def _friendly(detail: str) -> str:
    low = detail.lower()
    if "login" in low or "auth" in low or "401" in low or "token" in low:
        return ("Claude isn't logged in. Run `claude setup-token` and put the token in .env "
                "as CLAUDE_CODE_OAUTH_TOKEN.")
    if "limit" in low or "429" in low:
        return "Your Claude subscription's usage limit is reached; try again after it resets."
    return detail[:500]
