"""
Talks to Claude through the Claude Agent SDK, authenticated with your Claude
Pro/Max subscription instead of an API key.

Auth (one time): run `claude setup-token`, then put the token it prints in .env as
    CLAUDE_CODE_OAUTH_TOKEN=...
Do NOT also set ANTHROPIC_API_KEY, or Claude Code will bill the API key instead.

Every call runs Claude Code with its built-in tools disabled and no user/project settings
or MCP servers loaded, so it behaves like a plain chat model. A caller can hand it MCP
servers plus the exact tools it may call (chat does this for Brightspace); then it gets up
to TOOL_TURNS turns to look things up before answering.
"""

import os
import tempfile
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

MODEL = os.getenv("CLAUDE_MODEL", "sonnet")        # "sonnet", "haiku", or "opus"
TOOL_TURNS = 12            # turns allowed when MCP tools are given (each tool call uses one)

# Isolated working dir so Claude Code never picks up a CLAUDE.md or project settings.
WORKDIR = Path(tempfile.gettempdir()) / "discord-claude-bot"
WORKDIR.mkdir(exist_ok=True)


class ClaudeError(RuntimeError):
    pass


def _output_hook(rewrite: Callable[[Any], Any]):
    """PostToolUse hook that passes every tool result through `rewrite` before Claude sees it."""
    async def hook(input_data, tool_use_id, context):
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "updatedToolOutput": rewrite(input_data["tool_response"])}}
    return hook


def _options(system: str, resume: str | None, model: str | None, mcp_servers: dict | None,
             allowed_tools: list[str], disallowed_tools: list[str],
             rewrite_tool_output: Callable[[Any], Any] | None) -> ClaudeAgentOptions:
    hooks = ({"PostToolUse": [HookMatcher(hooks=[_output_hook(rewrite_tool_output)])]}
             if rewrite_tool_output else None)
    return ClaudeAgentOptions(
        hooks=hooks,
        system_prompt=system,
        model=model or MODEL,
        tools=[],                  # no Bash/Read/Edit/web: text and images in, text out
        setting_sources=[],        # ignore ~/.claude settings and CLAUDE.md files
        mcp_servers=mcp_servers or {},
        strict_mcp_config=True,    # only these servers, never ones from your Claude config
        allowed_tools=allowed_tools,   # anything else is denied (no one to approve it)
        disallowed_tools=disallowed_tools,  # removed from what Claude sees at all
        max_turns=TOOL_TURNS if mcp_servers else 1,
        env={"MCP_TIMEOUT": "60000"} if mcp_servers else {},  # npx can be slow to start
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
              resume: str | None = None, model: str | None = None,
              mcp_servers: dict | None = None, allowed_tools: list[str] | None = None,
              disallowed_tools: list[str] | None = None,
              rewrite_tool_output: Callable[[Any], Any] | None = None,
              tool_model: str | None = None) -> tuple[str, str | None]:
    """Send one user message (a string, or a list of text/image content blocks).
    Pass `resume` (a session id from an earlier call) to continue that conversation, and
    `model` to override CLAUDE_MODEL for this call. `mcp_servers` ({name: stdio config})
    with `allowed_tools` (full names like "mcp__brightspace__get_assignments") lets Claude
    call those tools and nothing else; `disallowed_tools` hides tools from it entirely;
    `rewrite_tool_output` edits each tool result before Claude reads it.
    `tool_model` answers with that model instead whenever Claude reaches for a tool: the
    message first runs on `model` in a fork of the session, and if it calls a tool, that run
    is stopped and thrown away and the message is answered again on `tool_model`.
    Returns (reply_text, session_id)."""
    options = _options(system, resume, model, mcp_servers, list(allowed_tools or []),
                       list(disallowed_tools or []), rewrite_tool_output)
    if tool_model and mcp_servers and tool_model != options.model:
        # A fork, so the stopped run never lands in the conversation the user continues.
        answer = await _run(content, replace(options, fork_session=bool(resume)),
                            stop_on_tool=True)
        if answer is not None:
            return answer
        options = replace(options, model=tool_model)
    return await _run(content, options)


async def _run(content, options: ClaudeAgentOptions,
               stop_on_tool: bool = False) -> tuple[str, str | None] | None:
    """One query. With stop_on_tool, returns None (and stops Claude) at its first tool call."""
    texts, session_id, result = [], None, None
    try:
        async with aclosing(query(prompt=_single(content), options=options)) as messages:
            async for msg in messages:
                if isinstance(msg, AssistantMessage):
                    if any(isinstance(b, ToolUseBlock) for b in msg.content):
                        if stop_on_tool:
                            return None
                        texts = []     # "let me check..." before a tool call isn't the answer
                    else:
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
    if "max_turns" in low:
        return (f"That took more than {TOOL_TURNS} lookups, so I stopped. "
                "Try asking about one course or a narrower question.")
    if "login" in low or "auth" in low or "401" in low or "token" in low:
        return ("Claude isn't logged in. Run `claude setup-token` and put the token in .env "
                "as CLAUDE_CODE_OAUTH_TOKEN.")
    if "limit" in low or "429" in low:
        return "Your Claude subscription's usage limit is reached; try again after it resets."
    return detail[:500]
