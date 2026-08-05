"""
Config and prompt file editor endpoints.
  GET  /api/config            — return raw config.yaml text
  POST /api/config            — validate + atomic write + reload
  GET  /api/prompts           — list available prompt files
  GET  /api/prompts/{name}    — return prompt file text
  POST /api/prompts/{name}    — write prompt file + reload claude client
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml
from aiohttp import web

from core.config import Config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("config.yaml")
PROMPTS_DIR = Path("prompts")
SOURCES_DIR = Path("sources")

_PIPELINE_SOURCES: dict[str, Path] = {
    "rss":       SOURCES_DIR / "dailynews_sources.md",
    "epicfury":  SOURCES_DIR / "epicfury_sources.md",
}

_REDACTED = "***REDACTED***"
# YAML keys whose values should be masked
_SENSITIVE_KEYS = re.compile(
    r"^\s*(api_key|bot_token|user_token|password_hash|session_secret|smtp_password)\s*:",
    re.IGNORECASE,
)


def _mask_config(text: str) -> str:
    """Replace sensitive values with ***REDACTED*** for display."""
    lines = []
    for line in text.splitlines():
        if _SENSITIVE_KEYS.match(line) and ":" in line:
            key_part = line[: line.index(":") + 1]
            lines.append(f'{key_part} "{_REDACTED}"')
        else:
            lines.append(line)
    return "\n".join(lines)


def _restore_secrets(masked_text: str, original_text: str) -> str:
    """Replace REDACTED placeholders with original values from disk.

    Uses sequential matching: the Nth REDACTED sensitive line in the masked text
    corresponds to the Nth sensitive line in the original. This correctly handles
    configs with multiple identical key names (e.g. multiple api_key: entries).
    """
    # Collect original sensitive lines in document order
    orig_sensitive = [
        l for l in original_text.splitlines()
        if _SENSITIVE_KEYS.match(l) and ":" in l
    ]
    orig_iter = iter(orig_sensitive)

    result = []
    for line in masked_text.splitlines():
        if _REDACTED in line and _SENSITIVE_KEYS.match(line) and ":" in line:
            result.append(next(orig_iter, line))  # fall back to masked line if exhausted
        else:
            result.append(line)
    return "\n".join(result)


def _reload_live_clients(request: web.Request, config_holder) -> None:
    """
    Push freshly loaded config into the long-lived clients that cache it.

    Covers both Gettr accounts (live + editor A/B test), both CDN uploaders — which
    request their upload channel with the account's own Gettr auth, so they need the
    credentials too — and the editor review branch's enabled/prompt settings.
    Adding a `gettr_test` block that was absent at boot still needs a restart: the
    client objects themselves are constructed in main.py.
    """
    cfg = config_holder.current
    targets = [
        ("gettr_client", "reload_config", cfg.gettr),
        ("gcp_client", "reload_config", cfg.gettr),
        ("gettr_test_client", "reload_config", cfg.gettr_test),
        ("gcp_test_client", "reload_config", cfg.gettr_test),
        ("editor_client", "reload_config", cfg.editor_review),
    ]
    for app_key, method, value in targets:
        client = request.app.get(app_key)
        if not client or value is None or not hasattr(client, method):
            continue
        try:
            getattr(client, method)(value)
        except Exception as e:
            logger.warning("%s reload failed: %s", app_key, e)


async def get_config(request: web.Request) -> web.Response:
    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        if request.query.get("raw") != "1":
            text = _mask_config(text)
        return web.Response(text=text, content_type="text/plain")
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))


async def post_config(request: web.Request) -> web.Response:
    body = await request.text()
    config_holder = request.app["config_holder"]
    state = request.app["state"]

    # Restore any REDACTED placeholders with real values from disk
    if _REDACTED in body:
        try:
            original = CONFIG_PATH.read_text(encoding="utf-8")
            body = _restore_secrets(body, original)
        except Exception as e:
            raise web.HTTPInternalServerError(reason=f"Could not restore secrets: {e}")

    # Validate YAML parses
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise web.HTTPBadRequest(reason=f"Invalid YAML: {e}")

    # Validate against Pydantic model
    try:
        Config.model_validate(data)
    except Exception as e:
        raise web.HTTPBadRequest(reason=f"Config validation failed: {e}")

    # Atomic write (write to temp, then rename)
    tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
    try:
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(str(tmp_path), str(CONFIG_PATH))
    except Exception as e:
        raise web.HTTPInternalServerError(reason=f"Write failed: {e}")

    # Hot-reload
    try:
        config_holder.reload()
    except Exception as e:
        logger.warning("Config reload failed after write: %s", e)

    # Propagate credential changes to live clients
    _reload_live_clients(request, config_holder)

    await state.emit({"type": "config_reloaded"})
    logger.info("Config updated and reloaded via dashboard")
    return web.json_response({"ok": True})


# Editor review branch prompts — owned by EditorReviewClient, not ClaudeClient
_EDITOR_PROMPTS = {
    "ai_editor_intake_triage_prompt.md",
    "ai_editor_ccp_exposure_system_prompt.md",
    "unveiled_chinax_style_prompt.md",
}

# Prompts owned by each pipeline type
_PIPELINE_PROMPTS: dict[str, list[str]] = {
    "rss": [
        "post_generation_rules.txt",
        "score_articles.txt",
        "generate_post_system.txt",
        "generate_post_user.txt",
        "verify_post.txt",
        "notion_topical_dedup.txt",
        # Editor review branch (A/B variant → test Gettr account)
        "ai_editor_intake_triage_prompt.md",
        "ai_editor_ccp_exposure_system_prompt.md",
        "unveiled_chinax_style_prompt.md",
        # AI video fallback: chyron headline + Wikimedia media search terms
        "video_brief.txt",
    ],
    "epicfury": [
        "epicfury_score_articles.txt",
        "epicfury_generate_post_system.txt",
        "epicfury_generate_post_user.txt",
        "video_brief.txt",
    ],
}

# Owned by VideoClient, not ClaudeClient or Pipeline
_VIDEO_PROMPTS = {"video_brief.txt"}


def _prompt_names_for(pipeline_type: str) -> list[str]:
    return _PIPELINE_PROMPTS.get(pipeline_type, _PIPELINE_PROMPTS["rss"])


def _prompt_names(request: web.Request) -> list[str]:
    """Per-channel prompt files if the dashboard was built for one, else legacy by pipeline_type."""
    explicit = request.app.get("prompt_files")
    if explicit:
        return explicit
    return _prompt_names_for(request.app.get("pipeline_type", "rss"))


def _sources_path(request: web.Request):
    explicit = request.app.get("sources_path")
    if explicit:
        return Path(explicit)
    return _PIPELINE_SOURCES.get(request.app.get("pipeline_type", "rss"))


async def list_prompts(request: web.Request) -> web.Response:
    try:
        names = _prompt_names(request)
        # Return only files that actually exist on disk
        files = [n for n in names if (PROMPTS_DIR / n).exists()]
        return web.json_response(files)
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))


async def get_prompt(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    # Sanitize: only allow alphanumeric + underscore + dot
    if not all(c.isalnum() or c in "_.-" for c in name):
        raise web.HTTPBadRequest(reason="Invalid prompt name")

    # Scope check: only prompts belonging to this pipeline
    if name not in _prompt_names(request):
        raise web.HTTPForbidden(reason="Prompt not accessible from this dashboard")

    path = PROMPTS_DIR / name
    if not path.exists():
        raise web.HTTPNotFound(reason="Prompt not found")
    return web.Response(text=path.read_text(encoding="utf-8"), content_type="text/plain")


async def post_prompt(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not all(c.isalnum() or c in "_.-" for c in name):
        raise web.HTTPBadRequest(reason="Invalid prompt name")

    # Scope check
    pipeline_type = request.app.get("pipeline_type", "rss")
    if name not in _prompt_names(request):
        raise web.HTTPForbidden(reason="Prompt not accessible from this dashboard")

    body = await request.text()
    path = PROMPTS_DIR / name
    if not path.exists():
        raise web.HTTPNotFound(reason="Prompt not found")

    path.write_text(body, encoding="utf-8")

    # Reload the correct component:
    # - DailyNews prompts live in ClaudeClient (loaded at init / reload_prompts)
    # - EpicFury prompts live in Pipeline as overrides (pipeline.reload_prompts)
    # - verify_post.txt lives in GemmaClient (reload_prompt)
    # - the three editor_*.md prompts live in EditorReviewClient (reload_prompts)
    # - video_brief.txt lives in VideoClient (reload_prompts)
    if name in _VIDEO_PROMPTS:
        video_client = request.app.get("video_client")
        if video_client and hasattr(video_client, "reload_prompts"):
            try:
                video_client.reload_prompts()
            except Exception as e:
                logger.warning("Failed to reload video client prompt: %s", e)
    elif name in _EDITOR_PROMPTS:
        editor_client = request.app.get("editor_client")
        if editor_client and hasattr(editor_client, "reload_prompts"):
            try:
                editor_client.reload_prompts()
            except Exception as e:
                logger.warning("Failed to reload editor client prompts: %s", e)
    elif name == "verify_post.txt":
        gemma_client = request.app.get("gemma_client")
        if gemma_client and hasattr(gemma_client, "reload_prompt"):
            try:
                gemma_client.reload_prompt()
            except Exception as e:
                logger.warning("Failed to reload gemma client prompt: %s", e)
    elif pipeline_type == "epicfury":
        pipeline = request.app.get("pipeline")
        if pipeline and hasattr(pipeline, "reload_prompts"):
            try:
                pipeline.reload_prompts()
            except Exception as e:
                logger.warning("Failed to reload pipeline prompts: %s", e)
    else:
        claude_client = request.app.get("claude_client")
        if claude_client and hasattr(claude_client, "reload_prompts"):
            try:
                claude_client.reload_prompts()
            except Exception as e:
                logger.warning("Failed to reload claude client prompts: %s", e)

    state = request.app["state"]
    await state.emit({"type": "config_reloaded", "detail": f"prompt:{name}"})
    logger.info("Prompt %s updated via %s dashboard", name, pipeline_type)
    return web.json_response({"ok": True})


async def get_sources(request: web.Request) -> web.Response:
    """GET /api/sources — return current sources file content."""
    path = _sources_path(request)
    if not path:
        raise web.HTTPNotFound(reason="No sources file for this pipeline type")
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return web.Response(text=text, content_type="text/plain")
    except Exception as e:
        raise web.HTTPInternalServerError(reason=str(e))


async def post_sources(request: web.Request) -> web.Response:
    """POST /api/sources — write sources file; changes apply on the next pipeline run."""
    pipeline_type = request.app.get("pipeline_type", "rss")
    path = _sources_path(request)
    if not path:
        raise web.HTTPNotFound(reason="No sources file for this pipeline type")
    body = await request.text()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except Exception as e:
        raise web.HTTPInternalServerError(reason=f"Write failed: {e}")

    state = request.app["state"]
    await state.emit({"type": "config_reloaded", "detail": "sources"})
    logger.info("Sources file updated via %s dashboard", pipeline_type)
    return web.json_response({"ok": True})
