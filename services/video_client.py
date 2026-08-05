"""
AI short-video generator — the fallback for posts with no usable image.

Wraps the vendored `video/scripts/make_news_video.py` skill (see video/UPSTREAM.md)
as an async, in-process service. When DailyNews would otherwise DROP an article for
lack of a picture, this turns the post into a ~25s narrated motion-news MP4 instead:
Wikimedia open-license stills with Ken Burns motion, an animated chyron, narration,
a ducked music bed, subtitles and the branded outro.

Shape follows services/pollinations_client.py — generate locally, hand bytes to
GcpClient.upload_bytes, post through the normal Gettr path.

Hard rules, all of which the publish agent depends on:
  * generate() NEVER raises. Any failure returns None and the caller falls through
    to its existing behavior (DailyNews drops, EpicFury posts an OG preview).
  * One render at a time, process-wide. DailyNews, EpicFury and every extra channel
    share one interpreter, and two concurrent ffmpeg runs would exhaust a 2-vCPU /
    3.9 GB box. Measured: ~91s wall and ~814 MB peak RSS per video.
  * The work directory is always removed, including on timeout.
  * A render whose assets are not all rights-verified is discarded, not published.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Process-wide: only ever one ffmpeg render in flight across all pipelines.
_RENDER_SLOT = asyncio.Semaphore(1)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "video" / "scripts" / "make_news_video.py"
_BRAND_ROOT = _REPO_ROOT / "video" / "brand"
_WORK_ROOT = _REPO_ROOT / "data" / "videogen"
_PROMPT_PATH = _REPO_ROOT / "prompts" / "video_brief.txt"

_MAX_HEADLINE_WORDS = 8
_MAX_SUBJECTS = 6           # separate Wikimedia searches per video


@dataclass
class VideoResult:
    """A finished, rights-clean MP4 ready to hand to GcpClient.upload_bytes."""
    data: bytes
    duration_s: float
    width: int
    height: int
    headline: str

    @property
    def upload_meta(self) -> dict:
        """Extra Gettr media fields only the generator knows."""
        return {
            "duration": int(round(self.duration_s * 1000)),
            "vid_wid": self.width,
            "vid_hgt": self.height,
        }


def brand_dir_for(slug: str) -> Path:
    """Per-channel brand directory, falling back to DailyNews' branding."""
    candidate = _BRAND_ROOT / slug
    if (candidate / "brand.json").exists():
        return candidate
    return _BRAND_ROOT / "dn"


class VideoClient:
    def __init__(
        self,
        openai_client,
        *,
        timeout_s: int = 480,
        width: int = 960,
        height: int = 720,
        brief_model: Optional[str] = None,
    ) -> None:
        self._openai = openai_client
        self._timeout_s = timeout_s
        self._width = width
        self._height = height
        self._brief_model = brief_model
        self._prompt = ""
        self.reload_prompts()

    # ------------------------------------------------------------------ #
    # Prompt                                                             #
    # ------------------------------------------------------------------ #
    def reload_prompts(self) -> None:
        """Re-read prompts/video_brief.txt (dashboard prompt editor hook)."""
        try:
            self._prompt = _PROMPT_PATH.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("video_brief.txt unreadable (%s) — using title fallback", e)
            self._prompt = ""

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    async def generate(
        self,
        *,
        article_id: str,
        title: str,
        post_content: str,
        brand_slug: str = "dn",
    ) -> Optional[VideoResult]:
        """Render a video for this article. Returns None on ANY failure."""
        if not _SCRIPT.exists():
            logger.error("Video generator missing at %s", _SCRIPT)
            return None

        brief = await self._build_brief(title, post_content)
        workdir = _WORK_ROOT / article_id

        async with _RENDER_SLOT:
            try:
                return await self._render(article_id, post_content, brief,
                                          brand_slug, workdir)
            except Exception as e:
                logger.error("Video generation failed for %s: %s", article_id, e)
                return None
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # Step 1 — the brief (headline + media search terms)                 #
    # ------------------------------------------------------------------ #
    async def _build_brief(self, title: str, post_content: str) -> dict:
        """Ask the LLM for a chyron headline, a media query and card labels.

        Falls back to the article title on any failure — a weaker query means a
        video built mostly from plain graphic cards, never a failed render.
        """
        fallback = {
            "headline": _trim_words(title, _MAX_HEADLINE_WORDS),
            "subjects": [title],
            "labels": [_trim_words(title, 3)],
        }
        if not self._prompt or not self._openai:
            return fallback

        try:
            raw = await self._openai.chat_complete(
                self._prompt,
                f"TITLE: {title}\n\nPOST: {post_content}",
                max_tokens=300,
                temperature=0.2,
                model=self._brief_model,
            )
            data = json.loads(_strip_fences(raw))
            headline = _trim_words(str(data.get("headline") or title),
                                   _MAX_HEADLINE_WORDS)
            subjects = [str(x).strip() for x in (data.get("subjects") or [])
                        if str(x).strip()][:_MAX_SUBJECTS]
            labels = [str(x).strip() for x in (data.get("labels") or []) if str(x).strip()]
            if not headline or not subjects:
                return fallback
            return {"headline": headline, "subjects": subjects,
                    "labels": labels or fallback["labels"]}
        except Exception as e:
            logger.warning("Video brief failed (%s) — falling back to the title", e)
            return fallback

    # ------------------------------------------------------------------ #
    # Step 2 — render                                                    #
    # ------------------------------------------------------------------ #
    async def _render(self, article_id, post_content, brief, brand_slug,
                      workdir: Path) -> Optional[VideoResult]:
        brand = brand_dir_for(brand_slug)
        out_dir = workdir / "deliverables"
        out_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "nice", "-n", "10", sys.executable, str(_SCRIPT),
            "--brand-dir", str(brand),
            "--headline", brief["headline"],
            "--script", post_content,
        ]
        # One --query per subject: each is searched separately (Commons ANDs all
        # terms, so a single conceptual phrase reliably returns nothing).
        for subject in brief["subjects"]:
            cmd += ["--query", subject]
        cmd += [
            "--fallback-labels", ",".join(brief["labels"]),
            "--width", str(self._width),
            "--height", str(self._height),
            "--no-karaoke",
            "--workdir", str(workdir / "work"),
            "--out-dir", str(out_dir),
        ]
        # NOTE: --article-url is deliberately never passed. Source-article photos
        # come back rights_verified=false; without them every asset is Wikimedia-
        # verified or self-produced, so nothing needs human licensing review.

        logger.info("Generating video for %s: %r (subjects: %s)",
                    article_id, brief["headline"], ", ".join(brief["subjects"]))
        rc, stderr = await self._run(cmd)
        if rc != 0:
            logger.error("Video render exited %s for %s: %s",
                         rc, article_id, _tail(stderr))
            return None

        mp4 = out_dir / "final.mp4"
        if not mp4.exists() or mp4.stat().st_size == 0:
            logger.error("Video render produced no output for %s", article_id)
            return None

        if not self._rights_clean(out_dir / "production.json", article_id):
            return None

        duration, width, height = await self._probe(mp4)
        if duration <= 0:
            logger.error("Video for %s has no measurable duration", article_id)
            return None

        data = mp4.read_bytes()
        logger.info("Video ready for %s: %.1fs %dx%d %.1f MB",
                    article_id, duration, width, height, len(data) / 1e6)
        return VideoResult(data=data, duration_s=duration, width=width,
                           height=height, headline=brief["headline"])

    async def _run(self, cmd: list[str]) -> tuple[Optional[int], str]:
        """Run the render, killing the whole process group if it overruns."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,          # own process group, so ffmpeg dies too
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(),
                                               timeout=self._timeout_s)
            return proc.returncode, (stderr or b"").decode("utf-8", "replace")
        except asyncio.TimeoutError:
            logger.error("Video render exceeded %ss — killing process group",
                         self._timeout_s)
            _killpg(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            return None, "timeout"

    @staticmethod
    def _rights_clean(production_json: Path, article_id: str) -> bool:
        """Refuse to publish a render containing any unverified asset.

        Should never trip while --article-url is withheld; it is the backstop
        that keeps that guarantee honest if the flags ever change.
        """
        try:
            scenes = json.loads(production_json.read_text()).get("scenes", [])
        except Exception as e:
            logger.error("Cannot read production.json for %s: %s", article_id, e)
            return False
        bad = [s.get("path") or s.get("type") for s in scenes
               if not s.get("rights_verified")]
        if bad:
            logger.error("Discarding video for %s — unverified assets: %s",
                         article_id, bad)
            return False
        return True

    @staticmethod
    async def _probe(mp4: Path) -> tuple[float, int, int]:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json", str(mp4),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        try:
            info = json.loads(out)
            stream = (info.get("streams") or [{}])[0]
            return (float(info["format"]["duration"]),
                    int(stream.get("width", 0)), int(stream.get("height", 0)))
        except Exception:
            return 0.0, 0, 0


def _killpg(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _trim_words(text: str, limit: int) -> str:
    words = (text or "").split()
    return " ".join(words[:limit]).rstrip(" ,.;:—-")


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start != -1 and end > start else t


def _tail(text: str, limit: int = 600) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text
