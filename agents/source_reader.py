"""
Source Reader — parses epicfury_sources.md into (x_handles, website_urls).
Re-reads the file on every call so live edits take effect without restart.
"""

from __future__ import annotations


def parse_sources(md_path: str) -> tuple[list[str], list[str]]:
    """
    Parse a Markdown sources file into (x_handles, website_urls).

    Format:
      @handle     → X account handle (with or without @)
      http(s)://  → website URL
      #           → comment, ignored
      blank lines → ignored
    """
    x_handles: list[str] = []
    website_urls: list[str] = []

    try:
        with open(md_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("@"):
                    handle = line.lstrip("@").strip()
                    if handle:
                        x_handles.append(handle)
                elif line.startswith("http://") or line.startswith("https://"):
                    website_urls.append(line)
    except FileNotFoundError:
        pass  # Return empty lists if file not found yet

    return x_handles, website_urls
