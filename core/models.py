from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Article(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Core fields (NewsAPI format, matching n8n Transform node output)
    url: str
    title: str
    description: Optional[str] = None
    author: Optional[str] = None
    published_at: datetime = Field(alias="publishedAt")
    url_to_image: Optional[str] = Field(default=None, alias="urlToImage")
    source: Optional[str] = None
    url_hash: str = ""
    cookie: Optional[str] = None  # download cookie for media

    # Added by SimilarityAgent
    embedding: Optional[list[float]] = None
    is_duplicate: bool = False
    cross_batch_score: float = 0.0
    cross_batch_matched_url: Optional[str] = None

    # Set by XAgent / WebsiteAgent for video content
    has_video: bool = False
    video_url: Optional[str] = None   # primary video URL (first / highest quality)
    video_urls: list[str] = Field(default_factory=list)  # all video URLs (multi-video tweets)

    # Added by ClaudeClient
    body: Optional[str] = None       # cached scraped source body ("" = unusable, don't refetch)
    llm_score: float = 0.0
    llm_comment: str = ""
    llm_post: Optional[str] = None   # Generated Gettr post (60-95 words)

    # Added by EditorReviewClient (editor_review step — DailyNews A/B only).
    # The 3-prompt chain emits a FINISHED post, not a draft: it is posted to the test
    # Gettr account verbatim, never re-run through generate_post (that would strip the
    # editorial voice the chain just applied).
    editor_post: Optional[str] = None

    # Added by GemmaClient (verify_post step — DailyNews only)
    verification_verdict: Optional[str] = None   # PASS / FAIL / REVISE / ERROR
    verification_output: Optional[str] = None    # Full Gemma verification response


class ReviewItem(BaseModel):
    """Article stored in Redis review:pending:<id>"""
    model_config = ConfigDict(populate_by_name=True)

    article_id: str
    url: str
    title: str
    description: Optional[str] = None
    source: Optional[str] = None
    published_at: str  # ISO string for Redis storage
    url_to_image: Optional[str] = None
    url_hash: str = ""
    llm_score: float = 0.0
    llm_comment: str = ""
    post_content: Optional[str] = None   # editor-provided override
    editor_post: Optional[str] = None    # A/B variant posted to the test Gettr account
    telegram_message_id: Optional[int] = None
    media: list[str] = Field(default_factory=list)  # media URLs if any


class PostResult(BaseModel):
    """Result of a Gettr post attempt."""
    article_id: str
    success: bool
    gettr_post_id: Optional[str] = None
    error: Optional[str] = None
    posted_at: Optional[datetime] = None
