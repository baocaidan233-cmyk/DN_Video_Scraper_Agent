from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseModel):
    log_level: str = "INFO"
    timezone: str = "UTC"


class NotionConfig(BaseModel):
    api_key: str
    rss_database_id: str  # Notion DB with property_rss, property_cookie, in_use fields


class NotionDedupConfig(BaseModel):
    api_key: str
    article_database_id: str  # daily_news DB — human editors' board (send_status, status, post_content, Duplicate, Notes)
    similarity_threshold: float = 0.80
    recent_lookback_hours: int = 24
    # If False, before_publish() only logs what it would have skipped — doesn't actually
    # skip. Keep off until the embedding threshold has been validated against live traffic.
    enforce_recent_skip: bool = False
    gettr_handle: str = "dailynews"
    gettr_crosscheck_interval_minutes: int = 15


class RssConfig(BaseModel):
    filter_feed_hours: int = 2             # keep <= redis.url_hash_ttl_s / 3600
    max_feed_items: int = 10000
    max_feed_per_source: int = 150
    concurrency: int = 10
    fetch_timeout_s: int = 30


class RedisConfig(BaseModel):
    url: str
    url_hash_key_prefix: str = "newsrooms:dailynews_v1:title_hash:"
    url_hash_ttl_s: int = 10800
    post_hash_key_prefix: str = "newsroom:dailynews:post:"
    post_hash_ttl_s: int = 864000
    review_pending_prefix: str = "review:pending:"
    review_queue_key: str = "review:queue"
    publish_queue_key: str = "publish:queue"


class OpenAIConfig(BaseModel):
    api_key: str
    embedding_model: str = "text-embedding-3-small"
    scoring_model: str = "gpt-4o-mini"
    post_gen_model: str = "gpt-4o-mini"
    embedding_batch_size: int = 100
    max_retries: int = 3
    retry_delay_s: float = 2.0


class QdrantConfig(BaseModel):
    url: str
    api_key: str
    collection: str = "dailynews_embeddings"
    vector_size: int = 1536
    within_batch_threshold: float = 0.70
    cross_batch_threshold: float = 0.80
    cross_batch_hours: int = 48


class ClaudeConfig(BaseModel):
    api_key: str
    # max_tokens must cover a whole batch of JSON scores+comments. At 512 with batch_size 10 the
    # response truncated mid-object, _parse_scores failed, and every article in the batch was
    # scored 0.0 — silently dropping entire batches. Do not lower these.
    max_tokens: int = 1500
    batch_size: int = 5
    filter_score_threshold: float = 6.0


class NotionReviewConfig(BaseModel):
    """Notion database used as the article review / approval queue."""
    api_key: str = ""
    review_database_id: str = ""
    poll_interval_s: int = 30  # how often to check for Approved/Rejected decisions


class TelegramConfig(BaseModel):
    bot_token: str = ""
    editor_chat_id: int = 0
    card_queue_maxsize: int = 500
    enabled: bool = True


class GettrConfig(BaseModel):
    api_url: str = "https://gettr.com/api/u/post"
    user_id: str
    user_token: str


class EditorReviewConfig(BaseModel):
    """DailyNews-only editor review branch (A/B against the standard post)."""
    enabled: bool = False
    triage_prompt: str = "prompts/ai_editor_intake_triage_prompt.md"
    ccp_prompt: str = "prompts/ai_editor_ccp_exposure_system_prompt.md"
    style_prompt: str = "prompts/unveiled_chinax_style_prompt.md"
    max_per_run: int = 0            # 0 = every qualifying article
    max_tokens: int = 2000
    model: Optional[str] = None     # None → claude.scoring_model


class GcpConfig(BaseModel):
    resumable_upload_timeout_s: int = 60
    download_timeout_s: int = 30
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    download_proxy_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/media/download"
    download_proxy_api_key: str = "9277311724445fa26f0172a701150da4743bf4b8b0257cf33a39a4c6445204a4"


class AlertsConfig(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_addr: str = ""
    smtp_to_addrs: list[str] = Field(default_factory=list)
    telegram_alert_chat_id: Optional[int] = None


class DashboardConfig(BaseModel):
    enabled: bool = True
    port: int = 8080
    port2: int = 8081          # Epic Fury dashboard
    password_hash: str = ""   # Set via: python3 -m dashboard.setup_password
    session_secret: str = ""  # Auto-generated at startup if empty
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)


class XCredential(BaseModel):
    username: str
    password: str
    email: str
    cookies_file: str = ""


class XConfig(BaseModel):
    tweets_per_account: int = 30
    credentials: list[XCredential] = Field(default_factory=list)


class TwitterApiConfig(BaseModel):
    """twitterapi.io configuration — paid API for reliable X.com scraping."""
    api_key: str = ""
    base_url: str = "https://api.twitterapi.io"
    tweets_per_account: int = 20  # twitterapi.io returns 20 per page


class SocialDataConfig(BaseModel):
    """socialdata.tools configuration — alternative paid API for X.com scraping."""
    api_key: str = ""
    base_url: str = "https://api.socialdata.tools"
    tweets_per_account: int = 20


_DEFAULT_EPICFURY_KEYWORDS = [
    "iran", "iranian", "irgc", "epic fury", "operation epic fury",
    "israel", "centcom", "u.s. military", "us military", "pentagon",
    "airstrike", "strike", "missile", "f-35", "b-2", "carrier",
    "war", "offensive", "military operation", "air campaign",
]


class VideoGenConfig(BaseModel):
    """AI short-video fallback for posts with no usable image (see video/UPSTREAM.md).

    NOTE: `enabled` and `max_24h` are only FIRST-BOOT DEFAULTS. Once
    data/schedule*.json exists, the dashboard values in it win — same rule as
    every other tunable. Read the live values from schedule*.json, never here.
    """
    enabled: bool = False        # dashboard switch default
    max_24h: int = 0             # rolling 24h cap per pipeline; 0 = feature off
    timeout_s: int = 480         # kill the render past this (measured: ~91s typical)
    width: int = 960             # 4:3
    height: int = 720
    brief_model: str = ""        # LLM for the headline/media-query brief; "" = client default


class EpicFuryConfig(BaseModel):
    sources_md_path: str = "sources/epicfury_sources.md"
    filter_feed_hours: int = 2             # keep <= redis_url_hash_ttl_s / 3600
    max_articles_per_website: int = 15
    filter_score_threshold: float = 6.0
    keywords: list[str] = Field(default_factory=lambda: list(_DEFAULT_EPICFURY_KEYWORDS))
    x: XConfig = Field(default_factory=XConfig)
    twitterapi: TwitterApiConfig = Field(default_factory=TwitterApiConfig)
    socialdata: SocialDataConfig = Field(default_factory=SocialDataConfig)
    x_scraper: str = "twitterapi"  # "twitterapi" | "socialdata"
    # Redis key namespacing (all epicfury: prefixed, no collision with RSS pipeline)
    redis_url_hash_prefix: str = "epicfury:title_hash:"
    redis_url_hash_ttl_s: int = 10800
    redis_review_pending_prefix: str = "epicfury:review:pending:"
    redis_review_queue_key: str = "epicfury:review:queue"
    redis_publish_queue_key: str = "epicfury:publish:queue"
    redis_post_hash_prefix: str = "epicfury:post:"
    redis_post_hash_ttl_s: int = 864000
    redis_tg_msg_prefix: str = "ef:tg:msg:"
    # AI video fallback. ChannelConfig.source IS an EpicFuryConfig, so declaring it
    # here covers EpicFury and every config-driven channel in one place.
    video_gen: VideoGenConfig = Field(default_factory=VideoGenConfig)


class ChannelConfig(BaseModel):
    """A fully config-driven, EpicFury-style social-media news channel.

    Each entry in `Config.channels` becomes an independent pipeline (its own X +
    website scraping, scoring/rewrite prompts, dedup namespace, Notion review
    board, Gettr account and dashboard port) — no code changes needed to add one.

    Redis key prefixes are derived from `slug` at runtime (see channel_runtime),
    so channels can never collide as long as their slugs differ. The sub-configs
    are the same models the EpicFury pipeline uses; only new *instances* are made.
    """
    slug: str                       # unique id: lowercase letters/digits only (Python-ident + Redis-safe)
    title: str = ""                 # dashboard header; defaults to slug.title()
    dashboard_port: int             # must be free and unique across channels
    enabled: bool = True            # set false to keep config but not start the channel
    schedule_path: str = ""         # persisted thresholds/pause state; default data/schedule_<slug>.json
    score_prompt: str = ""          # default prompts/<slug>_score_articles.txt
    post_system_prompt: str = ""    # default prompts/<slug>_generate_post_system.txt
    post_user_prompt: str = ""      # default prompts/<slug>_generate_post_user.txt
    video_score_boost: float = 1.0
    source: EpicFuryConfig = Field(default_factory=EpicFuryConfig)  # sources_md_path, keywords, x, ttls, ...
    qdrant: Optional[QdrantConfig] = None      # falls back to shared config.qdrant if omitted
    gettr: GettrConfig                         # the channel's Gettr account
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)  # bot_token still gates pipeline start
    notion_review: NotionReviewConfig = Field(default_factory=NotionReviewConfig)

    # --- derived defaults (used by channel_runtime; never collide across slugs) ---
    @property
    def resolved_title(self) -> str:
        return self.title or self.slug.replace("_", " ").title()

    @property
    def resolved_schedule_path(self) -> str:
        return self.schedule_path or f"data/schedule_{self.slug}.json"

    @property
    def resolved_score_prompt(self) -> str:
        return self.score_prompt or f"prompts/{self.slug}_score_articles.txt"

    @property
    def resolved_post_system_prompt(self) -> str:
        return self.post_system_prompt or f"prompts/{self.slug}_generate_post_system.txt"

    @property
    def resolved_post_user_prompt(self) -> str:
        return self.post_user_prompt or f"prompts/{self.slug}_generate_post_user.txt"

    def redis_keys(self) -> dict:
        """Redis key overrides derived from slug — guarantees per-channel namespacing."""
        return {
            "url_hash_key_prefix":   f"{self.slug}:title_hash:",
            "url_hash_ttl_s":        self.source.redis_url_hash_ttl_s,
            "review_pending_prefix": f"{self.slug}:review:pending:",
            "review_queue_key":      f"{self.slug}:review:queue",
            "publish_queue_key":     f"{self.slug}:publish:queue",
            "post_hash_key_prefix":  f"{self.slug}:post:",
            "post_hash_ttl_s":       self.source.redis_post_hash_ttl_s,
            "tg_msg_prefix":         f"{self.slug}:tg:msg:",
        }


class GemmaConfig(BaseModel):
    api_key: str = ""                      # Google AI Studio API key
    model: str = "gemma-4-31b-it"         # Gemma 4 model name on Google AI Studio
    max_tokens: int = 4096                 # must fit the verdict line + a full revised post
    enabled: bool = True


class ImageGenConfig(BaseModel):
    pollinations_api_key: str = ""   # Free key from https://auth.pollinations.ai
    enabled: bool = True


class MetadataApiConfig(BaseModel):
    # urlmeta.org — HTTP Basic auth: base64("apikey:") as Authorization header value
    urlmeta_api_key: str = ""
    # YouTube Data API v3
    youtube_api_key: str = "AIzaSyCKmo1qVzsjMelYL1ZurAaA3ZnSaVk8pBY"
    # Self-hosted metadata API (fallback when urlmeta has no image)
    self_hosted_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/website/metadata"
    self_hosted_api_key: str = "9277311724445fa26f0172a701150da4743bf4b8b0257cf33a39a4c6445204a4"
    # Self-hosted URL resolver — resolves redirect chains including Google News (mirrors n8n 'get final url')
    url_resolver_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/url/final"
    # Self-hosted article extractor — mirrors n8n 'scape article' node (extract-premium)
    extract_premium_url: str = "http://n8n-svr.gettr.fyi:7771/api/v1/article/extract-premium"
    # Domains that must not have a preview (mirrors n8n Redis cfg_domains_wo_preview)
    no_preview_domains: list[str] = Field(default_factory=list)


class Config(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    notion: NotionConfig
    rss: RssConfig = Field(default_factory=RssConfig)
    redis: RedisConfig
    openai: OpenAIConfig
    qdrant: QdrantConfig
    notion_dedup: Optional[NotionDedupConfig] = None   # Stage-3 Notion dedup config
    qdrant_notion: Optional[QdrantConfig] = None       # Qdrant collection for Stage-3 Notion dedup
    qdrant_epicfury: Optional[QdrantConfig] = None  # separate Qdrant API for Epic Fury
    claude: ClaudeConfig
    telegram: TelegramConfig
    notion_review: NotionReviewConfig = Field(default_factory=NotionReviewConfig)
    notion_review_epicfury: NotionReviewConfig = Field(default_factory=NotionReviewConfig)
    gettr: GettrConfig
    # DailyNews A/B: editor-revised posts go to this account, standard posts to `gettr`.
    gettr_test: Optional[GettrConfig] = None
    editor_review: EditorReviewConfig = Field(default_factory=EditorReviewConfig)
    gcp: GcpConfig = Field(default_factory=GcpConfig)
    gemma: GemmaConfig = Field(default_factory=GemmaConfig)
    image_gen: ImageGenConfig = Field(default_factory=ImageGenConfig)
    video_gen: VideoGenConfig = Field(default_factory=VideoGenConfig)  # DailyNews
    metadata_api: MetadataApiConfig = Field(default_factory=MetadataApiConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    epicfury: Optional[EpicFuryConfig] = None
    gettr_epicfury: Optional[GettrConfig] = None
    telegram_epicfury: Optional[TelegramConfig] = None
    # Fully config-driven extra channels — add entries here, no code changes needed.
    channels: list[ChannelConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_channels(self) -> "Config":
        """Fail fast on channel misconfiguration (duplicate slug / port clash)."""
        slugs, ports = set(), {self.dashboard.port, self.dashboard.port2}
        for ch in self.channels:
            if not ch.slug.isalnum() or not ch.slug.islower():
                raise ValueError(f"channel slug {ch.slug!r} must be lowercase alphanumeric")
            if ch.slug in slugs:
                raise ValueError(f"duplicate channel slug {ch.slug!r}")
            slugs.add(ch.slug)
            if ch.dashboard_port in ports:
                raise ValueError(
                    f"channel {ch.slug!r} dashboard_port {ch.dashboard_port} clashes with another port"
                )
            ports.add(ch.dashboard_port)
        return self


def load_config(path: str | Path = "config.yaml") -> Config:
    """Load configuration from a YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return Config.model_validate(data)


class ConfigHolder:
    """Mutable wrapper around Config that supports hot-reload."""

    def __init__(self, path: str | Path = "config.yaml") -> None:
        self._path = Path(path)
        self.current: Config = load_config(self._path)

    def reload(self) -> None:
        self.current = load_config(self._path)
