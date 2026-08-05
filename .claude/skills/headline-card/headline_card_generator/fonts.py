"""Cross-platform CJK font auto-detection.

Tries a short list of common macOS/Linux system font locations and returns
the first one that actually exists on disk. Pass font_path/font_index
explicitly to skip auto-detection and use your own font file instead.
"""

from __future__ import annotations

from pathlib import Path

# (path, ttc font-collection index) — first one that exists wins.
_FONT_CANDIDATES = [
    ("/System/Library/AssetsV2/com_apple_MobileAsset_Font8/4a418d1fa4860652a3241e8ee457806c8557fc64.asset/AssetData/Yuanti.ttc", 2),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 2),
]


def resolve_font(font_path: str | None = None, font_index: int = 0) -> tuple[str, int]:
    if font_path:
        return font_path, font_index
    for path, index in _FONT_CANDIDATES:
        if Path(path).exists():
            return path, index
    raise RuntimeError(
        "No CJK font found. Pass font_path explicitly (e.g. a .ttf/.ttc/.otf "
        "file bundled with your project), or on Ubuntu/Debian install one: "
        "sudo apt-get install fonts-noto-cjk "
        "(then it'll auto-detect at /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc)."
    )
