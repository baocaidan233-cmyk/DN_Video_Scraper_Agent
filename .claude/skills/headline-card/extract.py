"""Extract the visual subject of a headline for image search.

Given a headline (and optional description), asks an LLM to decide whether the
story centers on one identifiable **person** (→ search that person's photo) or
on a non-person entity like a company, country, weapon system, or event
(→ search those **keywords** instead). Returns both plus a single recommended
search `query`.

Used by generate.py when `--auto` is set, so a photo card can be attempted
instead of always falling back to a plain card. Degrades safely: any failure
(no key, no credits, network/parse error) returns an empty subject and the
caller falls back to a plain card.

Provider: defaults to OpenAI `gpt-4o-mini` — the model this project actually
uses for scoring/post-gen. Set provider="anthropic" (or HEADLINE_CARD_PROVIDER
=anthropic) to use claude-haiku-4-5-20251001 instead.

Key precedence: explicit api_key arg > provider env var (OPENAI_API_KEY /
ANTHROPIC_API_KEY) > ./config.yaml (openai.api_key / claude.api_key), the last
as a convenience when run from the project root.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional, TypedDict

_DEFAULT_MODELS = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5-20251001"}
_ENV_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_CFG_PATHS = {"openai": ("openai", "api_key"), "anthropic": ("claude", "api_key")}

_SYSTEM = (
    "You pick the best image-search subject for a news headline. "
    "Return ONLY a JSON object, no prose, no code fence.\n\n"
    "Schema:\n"
    '{"person": "<full name of the SINGLE central identifiable individual the '
    'story is about, else empty string>",\n'
    ' "keywords": ["<1-3 concrete, visual search terms when there is no single '
    'person: company, country, org, place, weapon system, event>"],\n'
    ' "query": "<the single best image-search string: the person name if '
    'present, otherwise the strongest keyword phrase>",\n'
    ' "subject_type": "person|org|place|event|other"}\n\n'
    "Rules:\n"
    "- person is non-empty ONLY when the story clearly centers on one named "
    "individual (a leader, official, CEO). A country or company is NOT a person.\n"
    "- Prefer concrete, depictable terms over abstractions.\n"
    "- query is never empty; fall back to the main subject of the headline."
)


class Subject(TypedDict):
    person: str
    keywords: list[str]
    query: str
    subject_type: str


_EMPTY: Subject = {"person": "", "keywords": [], "query": "", "subject_type": "other"}


def _resolve_key(provider: str, api_key: Optional[str]) -> Optional[str]:
    if api_key:
        return api_key
    env = os.environ.get(_ENV_KEYS[provider])
    if env:
        return env
    cfg = Path("config.yaml")
    if cfg.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text()) or {}
            section, field = _CFG_PATHS[provider]
            return (data.get(section) or {}).get(field) or None
        except Exception:
            return None
    return None


def _parse(text: str) -> Subject:
    """Pull the JSON object out of the reply, tolerating a code fence."""
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        t = t[4:] if t.lower().startswith("json") else t
        t = t.split("```")[0]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        return dict(_EMPTY)
    obj = json.loads(t[start : end + 1])
    kws = obj.get("keywords") or []
    if isinstance(kws, str):
        kws = [kws]
    return {
        "person": (obj.get("person") or "").strip(),
        "keywords": [str(k).strip() for k in kws if str(k).strip()][:3],
        "query": (obj.get("query") or "").strip(),
        "subject_type": (obj.get("subject_type") or "other").strip(),
    }


def _call_openai(key: str, model: str, user: str) -> str:
    from openai import OpenAI
    r = OpenAI(api_key=key).chat.completions.create(
        model=model, max_tokens=200, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": user}],
    )
    return r.choices[0].message.content


def _call_anthropic(key: str, model: str, user: str) -> str:
    import anthropic
    r = anthropic.Anthropic(api_key=key).messages.create(
        model=model, max_tokens=200, temperature=0,
        system=_SYSTEM, messages=[{"role": "user", "content": user}],
    )
    return r.content[0].text


def extract_subject(
    title: str,
    description: str = "",
    *,
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Subject:
    """Return {person, keywords, query, subject_type}. Empty on any failure."""
    if not title.strip():
        return dict(_EMPTY)
    provider = (provider or os.environ.get("HEADLINE_CARD_PROVIDER") or "openai").lower()
    if provider not in _DEFAULT_MODELS:
        print(f"extract: unknown provider {provider!r} — skipping.", file=sys.stderr)
        return dict(_EMPTY)
    key = _resolve_key(provider, api_key)
    if not key:
        print(f"extract: no {provider} API key ({_ENV_KEYS[provider]} or config.yaml) "
              "— skipping subject extraction.", file=sys.stderr)
        return dict(_EMPTY)
    model = model or _DEFAULT_MODELS[provider]
    user = f"Headline: {title}" + (f"\nDescription: {description}" if description else "")
    try:
        raw = _call_openai(key, model, user) if provider == "openai" else _call_anthropic(key, model, user)
        out = _parse(raw or "")
        if not out["query"]:  # query must never be empty
            out["query"] = out["person"] or title.strip()
        return out
    except Exception as e:
        print(f"extract: {provider} extraction failed ({str(e)[:160]}) — plain card.",
              file=sys.stderr)
        return dict(_EMPTY)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Extract image-search subject from a headline.")
    ap.add_argument("title")
    ap.add_argument("--description", default="")
    ap.add_argument("--provider", default=None, choices=["openai", "anthropic"])
    args = ap.parse_args()
    print(json.dumps(extract_subject(args.title, args.description, provider=args.provider),
                     ensure_ascii=False, indent=2))
