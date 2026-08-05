"""
DashboardState — shared mutable state between pipeline loops and SSE clients.

All methods are called from within the same asyncio event loop, so no locking
is needed for attribute access.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class DashboardLogHandler(logging.Handler):
    """Routes Python log records to the dashboard SSE Logs tab.

    Attach to the root logger after DashboardState objects are created:
        handler = DashboardLogHandler([state, state_ef])
        handler._loop = asyncio.get_running_loop()
        logging.getLogger().addHandler(handler)

    Routing rules:
      - Own modules (agents/core/services/utils/dashboard/__main__): INFO and above
      - Third-party libraries (httpx, aiohttp, etc.): ERROR only to suppress noise
    """

    _OWN_PREFIXES = ('agents.', 'core.', 'services.', 'utils.', 'dashboard.', '__main__')

    def __init__(self, states: list) -> None:
        super().__init__(level=logging.INFO)
        self._states: list = list(states)
        self._loop = None  # set to asyncio.get_running_loop() in main()
        self.setFormatter(logging.Formatter('%(name)s — %(message)s'))

    def add_state(self, state) -> None:
        if state not in self._states:
            self._states.append(state)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            is_own = record.name.startswith(self._OWN_PREFIXES)
            if not is_own and record.levelno < logging.ERROR:
                return
            msg = self.format(record)
            event = {"type": "log", "level": record.levelname, "msg": msg}
            loop = self._loop
            if loop and loop.is_running():
                for state in self._states:
                    try:
                        loop.call_soon_threadsafe(state.emit_nowait, event)
                    except RuntimeError:
                        pass
            else:
                for state in self._states:
                    try:
                        state.emit_nowait(event)
                    except Exception:
                        pass
        except Exception:
            pass

logger = logging.getLogger(__name__)

class DashboardState:
    def __init__(
        self,
        schedule_path: str = "data/schedule.json",
        default_filter_score: float = 6.0,
        default_within_batch: float = 0.70,
        default_cross_batch: float = 0.80,
        default_notion_dedup: float = 0.80,
        default_video_gen_enabled: bool = False,
        default_video_gen_max_24h: int = 0,
    ) -> None:
        self._schedule_path = Path(schedule_path)

        # SSE subscriber queues (one per connected browser tab)
        self.sse_subscribers: list[asyncio.Queue] = []

        # Ring buffer of log lines for the log-tail panel
        self.log_ring: deque[dict] = deque(maxlen=1000)

        # Schedule state — loaded from disk, persisted on change
        saved = self._load_schedule()
        self.rss_interval_s: int = saved.get("rss_interval_s", 600)
        self.publish_interval_s: int = saved.get("publish_interval_s", 3600)
        self.rss_paused: bool = saved.get("rss_paused", False)
        self.publish_paused: bool = saved.get("publish_paused", False)
        self.filter_score_threshold: float = saved.get("filter_score_threshold", default_filter_score)
        self.within_batch_threshold: float = saved.get("within_batch_threshold", default_within_batch)
        self.cross_batch_threshold: float = saved.get("cross_batch_threshold", default_cross_batch)
        self.notion_dedup_threshold: float = saved.get("notion_dedup_threshold", default_notion_dedup)
        self.autopilot: bool = saved.get("autopilot", False)
        self.verify_enabled: bool = saved.get("verify_enabled", True)
        self.x_scraper: str = saved.get("x_scraper", "twitterapi")
        # AI video fallback for posts with no usable image
        self.video_gen_enabled: bool = saved.get("video_gen_enabled", default_video_gen_enabled)
        self.video_gen_max_24h: int = saved.get("video_gen_max_24h", default_video_gen_max_24h)

        # Persist immediately so all fields (including new ones with defaults) are on disk.
        # This migrates old 4-field schedule files to the current 8-field format.
        self.save_schedule()

        # Cancellation events
        self.rss_cancel_event: asyncio.Event = asyncio.Event()
        self.publish_cancel_event: asyncio.Event = asyncio.Event()

        # Live counters (updated by heartbeat loop from Redis)
        self.review_queue_len: int = 0
        self.publish_queue_len: int = 0
        self.next_rss_in_s: int = 0
        self.next_publish_in_s: int = 0

        # In-memory sessions: token -> expiry_timestamp (unix seconds)
        self.sessions: dict[str, float] = {}

        # Current active run IDs
        self._active_runs: set[str] = set()

    def _load_schedule(self) -> dict:
        try:
            return json.loads(self._schedule_path.read_text())
        except Exception:
            return {}

    def save_schedule(self) -> None:
        try:
            self._schedule_path.parent.mkdir(parents=True, exist_ok=True)
            self._schedule_path.write_text(json.dumps({
                "rss_interval_s": self.rss_interval_s,
                "publish_interval_s": self.publish_interval_s,
                "rss_paused": self.rss_paused,
                "publish_paused": self.publish_paused,
                "filter_score_threshold": self.filter_score_threshold,
                "within_batch_threshold": self.within_batch_threshold,
                "cross_batch_threshold": self.cross_batch_threshold,
                "notion_dedup_threshold": self.notion_dedup_threshold,
                "autopilot": self.autopilot,
                "verify_enabled": self.verify_enabled,
                "x_scraper": self.x_scraper,
                "video_gen_enabled": self.video_gen_enabled,
                "video_gen_max_24h": self.video_gen_max_24h,
            }))
        except Exception as e:
            logger.warning("Failed to persist schedule: %s", e)

    # ------------------------------------------------------------------ #
    # SSE broadcast                                                        #
    # ------------------------------------------------------------------ #

    async def emit(self, event: dict[str, Any]) -> None:
        """Broadcast a JSON event to all connected SSE clients."""
        if "ts" not in event:
            event["ts"] = datetime.now(timezone.utc).isoformat()

        # Also capture log events into the ring buffer
        if event.get("type") == "log":
            self.log_ring.append(event)

        data = json.dumps(event)
        dead: list[asyncio.Queue] = []
        for q in self.sse_subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self.sse_subscribers.remove(q)
            except ValueError:
                pass

    def emit_nowait(self, event: dict[str, Any]) -> None:
        """Synchronous (non-blocking) emit — safe to call from logging processors."""
        if "ts" not in event:
            event["ts"] = datetime.now(timezone.utc).isoformat()
        if event.get("type") == "log":
            self.log_ring.append(event)

        data = json.dumps(event)
        dead: list[asyncio.Queue] = []
        for q in self.sse_subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self.sse_subscribers.remove(q)
            except ValueError:
                pass

    # ------------------------------------------------------------------ #
    # Session helpers                                                      #
    # ------------------------------------------------------------------ #

    def add_session(self, token: str, ttl_s: int = 86400) -> None:
        import time
        self.sessions[token] = time.time() + ttl_s

    def validate_session(self, token: str) -> bool:
        import time
        from dashboard.auth import SESSION_TTL
        now = time.time()
        expiry = self.sessions.get(token)
        if expiry is None or now > expiry:
            self.sessions.pop(token, None)
            return False
        # Slide the idle window on each successful request
        self.sessions[token] = now + SESSION_TTL
        return True

    def remove_session(self, token: str) -> None:
        self.sessions.pop(token, None)
