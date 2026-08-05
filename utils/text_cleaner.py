"""
Text cleaning utilities faithful to the n8n clean_post_data.json sub-workflow.

Removes ASCII control characters U+0000–U+001F EXCEPT:
  - \t (U+0009) horizontal tab
  - \n (U+000A) line feed (newline)
  - \r (U+000D) carriage return

This preserves emojis, symbols, and line breaks exactly as the n8n Code node does.
"""

import re

# Match control chars U+0000-U+001F, excluding tab (\x09), LF (\x0A), CR (\x0D)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def clean_control_chars(text: str | None) -> str:
    """Remove control characters from text, preserving emojis and line breaks."""
    if not text:
        return ""
    return _CONTROL_CHAR_RE.sub("", text)


def clean_fields(**kwargs: str | None) -> dict[str, str]:
    """Clean multiple fields at once. Returns dict of cleaned values."""
    return {k: clean_control_chars(v) for k, v in kwargs.items()}
