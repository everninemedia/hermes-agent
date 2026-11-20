"""Iteration-closeout hygiene for the max-iterations summary path.

Summary requests are text-only by contract (tool routing must not survive into
them), and a failed summary must still leave a sanitized, provider-independent
checkpoint row. Extracted from ``chat_completion_helpers`` so that the facade
stays within its FILE_LINES ratchet; consumers call these through this module.
"""

from __future__ import annotations

import re
from typing import Any

_TOOL_ROUTING_KEYS = ("tools", "tool_choice", "parallel_tool_calls")


def _force_toolless_request(request: dict) -> dict:
    """Return a copy with every tool-routing field removed.

    Responses and Anthropic summaries are deliberately text-only. Responses transports
    normally add ``tool_choice=auto`` and ``parallel_tool_calls=true`` whenever
    the agent has tools.  Removing only ``tools`` leaves an invalid request
    that xAI and other strict gateways reject with HTTP 400.  Keep the coupled
    fields atomic at this final chokepoint, including profile-provided fields
    nested in ``extra_body``.
    """
    sanitized = dict(request)
    for key in _TOOL_ROUTING_KEYS:
        sanitized.pop(key, None)
    extra_body = sanitized.get("extra_body")
    if isinstance(extra_body, dict):
        clean_extra = dict(extra_body)
        for key in _TOOL_ROUTING_KEYS:
            clean_extra.pop(key, None)
        if clean_extra:
            sanitized["extra_body"] = clean_extra
        else:
            sanitized.pop("extra_body", None)
    return sanitized


def _local_iteration_checkpoint(messages: list, max_iterations: int, error: Exception) -> str:
    """Build a useful, provider-independent fallback when summary generation fails."""

    def _visible_text(content: Any) -> str:
        if isinstance(content, str):
            visible = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
            # A truncated provider stream may leave a reasoning block open.
            # Preserve only text before that block; never surface its payload.
            open_think = visible.find("<think>")
            if open_think >= 0:
                visible = visible[:open_think]
            return visible.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in {
                "text",
                "output_text",
            }:
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        # Apply the same filter after joining: reasoning tags can span blocks.
        return _visible_text("\n".join(parts))

    last_progress = ""
    tool_names: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            visible = _visible_text(msg.get("content"))
            if visible:
                last_progress = visible
            for call in msg.get("tool_calls") or []:
                try:
                    name = str((call.get("function") or {}).get("name") or "").strip()
                except AttributeError:
                    name = ""
                if name:
                    tool_names.append(name)
    if len(last_progress) > 2400:
        last_progress = last_progress[-2400:]
    unique_tools = list(dict.fromkeys(tool_names))[-12:]
    parts = [
        f"Iteration limit reached ({max_iterations}). The provider summary failed, "
        "but Hermes preserved the session state.",
    ]
    if last_progress:
        parts.extend(["Last recorded progress:", last_progress])
    if unique_tools:
        parts.append(
            "Recorded tool activity: "
            + ", ".join(unique_tools)
            + f" ({len(tool_names)} calls total)."
        )
    parts.append(
        f"Summary failure class: {type(error).__name__}. Resume this session to continue."
    )
    return "\n\n".join(parts)
