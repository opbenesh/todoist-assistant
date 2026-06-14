from __future__ import annotations

import re


def escape_markdown(text: str) -> str:
    """Escape legacy markdown characters (*, _, [, `) while preserving links [text](url)."""
    if not text:
        return ""
    pattern = re.compile(r"(\[[^\]]+\]\([^)]+\))")
    parts = pattern.split(text)
    escaped_parts = []
    for part in parts:
        if part.startswith("[") and part.endswith(")"):
            split_idx = part.find("](")
            link_text = part[1:split_idx]
            link_url = part[split_idx + 2 : -1]
            escaped_parts.append(f"[{_escape_raw(link_text)}]({_escape_raw(link_url)})")
        else:
            escaped_parts.append(_escape_raw(part))
    return "".join(escaped_parts)


def _escape_raw(text: str) -> str:
    text = text.replace("\\", "\\\\")
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("[", "\\[")
    text = text.replace("`", "\\`")
    return text
