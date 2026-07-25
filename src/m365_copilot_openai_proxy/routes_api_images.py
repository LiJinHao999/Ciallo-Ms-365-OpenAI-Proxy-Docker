"""OpenAI-compatible image generation via M365 Copilot / Designer.

POST /v1/images/generations

This does NOT call a separate Designer REST API. It reuses the existing chat
path that already enables Flux/Designer image-gen feature flags, then harvests
designerapp / asyncgw image URLs from the streamed answer and rewrites them
through the signed media proxy so clients can fetch them with the same host.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .call_log_store import append_call_log
from .config import Settings
from .media_proxy import (
    is_allowed_m365_media_url,
    make_signed_media_proxy_url,
    normalize_m365_media_text,
    verify_signed_media_proxy_params,
)
from .routes_api_common import request_model_alias, resolve_request_tone
from .routes_media_proxy import request_media_rewriter
from .substrate_client import SubstrateCopilotClient, SubstrateCopilotError
from .substrate_parse import _extract_image_urls


_DESIGNER_URL_RE = re.compile(
    r"https://designerapp\.officeapps\.live\.com/designerapp/document\.ashx[^\s`)]+"
)
_ASYNCGW_URL_RE = re.compile(
    r"https://[^\s`)\]]+\.asyncgw\.teams\.microsoft\.com/v1/objects/[^\s`)\]]+/views/original/[^\s`)\]]+",
    re.IGNORECASE,
)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_DATA_URL_RE = re.compile(r"data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)")


class OpenAIImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    model: str | None = None
    n: int = 1
    size: str | None = "1024x1024"
    response_format: Literal["url", "b64_json"] | None = "url"
    user: str | None = None
    quality: str | None = None
    style: str | None = None


def _build_image_prompt(prompt: str, *, size: str, n: int) -> str:
    """Steer Copilot/Designer toward emitting real image resources."""
    prompt = (prompt or "").strip()
    size = (size or "1024x1024").strip() or "1024x1024"
    n = max(1, min(int(n or 1), 4))
    count_hint = "one image" if n == 1 else f"{n} distinct images"
    return (
        "Generate image content with Microsoft Designer / Flux. "
        f"Create {count_hint}. Preferred size: {size}. "
        "Return the generated image resource(s) directly in the answer "
        "(image markdown or designer/asyncgw URLs). "
        "Do not refuse with text-only placeholders when image generation is available.\n\n"
        f"Description:\n{prompt}"
    )


def _harvest_image_urls(text: str) -> list[str]:
    """Collect designer/asyncgw/data-url images from a chat answer, in order."""
    if not text:
        return []
    normalized = normalize_m365_media_text(text)
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        cleaned = (url or "").strip().strip("`").strip()
        if not cleaned or cleaned in seen:
            return
        # Prefer real media hosts / data URLs; ignore unrelated http links.
        if cleaned.startswith("data:image/"):
            seen.add(cleaned)
            found.append(cleaned)
            return
        if is_allowed_m365_media_url(cleaned) or _DESIGNER_URL_RE.match(cleaned) or _ASYNCGW_URL_RE.match(cleaned):
            seen.add(cleaned)
            found.append(cleaned)
            return
        # Already-proxied local media URLs from a rewriter are also acceptable.
        if "/v1/m365-media?" in cleaned:
            seen.add(cleaned)
            found.append(cleaned)

    for match in _MD_IMAGE_RE.finditer(normalized):
        add(match.group(1))
    for match in _DESIGNER_URL_RE.finditer(normalized):
        add(match.group(0))
    for match in _ASYNCGW_URL_RE.finditer(normalized):
        add(match.group(0))
    for match in _DATA_URL_RE.finditer(normalized):
        add(match.group(0))

    # Deep walk as a last resort (handles nested JSON-ish blobs in text).
    try:
        for url in _extract_image_urls({"text": normalized, "type": "image"}):
            add(url)
    except Exception:
        pass

    return found


def _proxy_if_needed(
    url: str,
    *,
    base_url: str,
    account_id: str | None,
    secret: str,
    allowed_suffixes: list[str] | None,
    ttl_seconds: int | None,
) -> str:
    if not url or url.startswith("data:image/") or "/v1/m365-media?" in url:
        return url
    if not account_id or not secret:
        return url
    if not is_allowed_m365_media_url(url, allowed_suffixes):
        return url
    return make_signed_media_proxy_url(
        base_url,
        account_id,
        url,
        secret,
        expires_at=None if ttl_seconds is None else int(time.time()) + int(ttl_seconds),
    )


def _unwrap_proxy_url(url: str, *, secret: str) -> tuple[str | None, str]:
    """If url is a signed /v1/m365-media link, return (account_id, source_url).

    Otherwise return (None, original_url).
    """
    if "/v1/m365-media?" not in url:
        return None, url
    try:
        from urllib.parse import parse_qs, urlsplit

        qs = parse_qs(urlsplit(url).query)
        account_id = (qs.get("account_id") or [""])[0]
        encoded = (qs.get("u") or [""])[0]
        exp = (qs.get("exp") or [""])[0]
        sig = (qs.get("sig") or [""])[0]
        source = verify_signed_media_proxy_params(account_id, encoded, exp, sig, secret)
        if source:
            return account_id or None, source
    except Exception:
        pass
    return None, url


async def _to_b64_json(
    app: FastAPI,
    *,
    account_id: str | None,
    source_url: str,
    secret: str = "",
) -> str:
    """Fetch a designer/asyncgw image and return raw base64 (no data: prefix)."""
    if source_url.startswith("data:image/"):
        m = _DATA_URL_RE.match(source_url)
        if not m:
            raise RuntimeError("invalid data URL")
        return m.group(2)

    proxy_account, fetch_url = _unwrap_proxy_url(source_url, secret=secret)
    use_account = proxy_account or account_id
    if not use_account:
        raise RuntimeError("no bound account for image fetch")
    fetcher = getattr(getattr(app.state, "refresh_scheduler", None), "fetch_image", None)
    if fetcher is None:
        raise RuntimeError("media fetcher unavailable")
    content, content_type = await fetcher(use_account, fetch_url)
    if not content:
        raise RuntimeError("empty image body")
    _ = content_type
    return base64.b64encode(content).decode("ascii")


def register_images_routes(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    get_copilot_client: Callable[[Request], SubstrateCopilotClient],
) -> None:
    @app.post("/v1/images/generations")
    async def images_generations(
        raw_request: Request,
        request: OpenAIImageGenerationRequest,
        settings: Settings = Depends(get_settings),  # noqa: B008 - FastAPI pattern
        client: SubstrateCopilotClient = Depends(get_copilot_client),  # noqa: B008
    ):
        _log = logging.getLogger("copilot_proxy")
        prompt = (request.prompt or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": "prompt is required", "type": "invalid_request_error"}},
            )

        n = int(request.n or 1)
        if n < 1 or n > 4:
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": "n must be between 1 and 4", "type": "invalid_request_error"}},
            )

        response_format = (request.response_format or "url").lower()
        if response_format not in {"url", "b64_json"}:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "response_format must be url or b64_json",
                        "type": "invalid_request_error",
                    }
                },
            )

        size = (request.size or "1024x1024").strip() or "1024x1024"
        model_alias = request_model_alias(app, raw_request, settings)
        resolved_tone, _ = resolve_request_tone(app, request.model)
        # Image gen works best with Magic / auto routing into Designer.
        client._tone = resolved_tone or "Magic"

        account = getattr(raw_request.state, "account", None)
        account_id = getattr(account, "id", None)
        has_designer = bool(getattr(account, "designer_auth_token", "") or "")
        has_media = bool(getattr(account, "media_auth_token", "") or "")

        call_record: dict[str, Any] = {
            "api": "images",
            "endpoint": "/v1/images/generations",
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "stream": False,
            "tools": [],
            "messages": 1,
            "model": request.model or model_alias,
            "tone": client._tone,
            "n": n,
            "size": size,
            "response_format": response_format,
            "has_designer_auth": has_designer,
            "has_media_auth": has_media,
        }
        append_call_log(app.state, call_record)

        image_prompt = _build_image_prompt(prompt, size=size, n=n)
        media_rewriter = request_media_rewriter(app, raw_request)

        try:
            # Fresh session each generation so previous chat context does not pollute.
            answer = await client.chat(image_prompt, [], session=None, images=None)
        except SubstrateCopilotError as exc:
            _log.warning("[/v1/images/generations] substrate error: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={"error": {"message": str(exc), "type": "upstream_error"}},
            ) from exc
        except Exception as exc:
            _log.exception("[/v1/images/generations] unexpected error")
            raise HTTPException(
                status_code=502,
                detail={"error": {"message": f"image generation failed: {exc}", "type": "upstream_error"}},
            ) from exc

        # Rewrite designer/asyncgw URLs to signed local proxy URLs first so
        # clients can load them without Microsoft cookies.
        rewritten = media_rewriter(answer) if callable(media_rewriter) else answer
        urls = _harvest_image_urls(rewritten)
        if not urls:
            # Also try the raw answer before rewrite.
            urls = _harvest_image_urls(answer)

        if not urls:
            preview = (answer or "").strip().replace("\n", " ")
            if len(preview) > 240:
                preview = preview[:240] + "…"
            hint = ""
            if not has_designer and not has_media:
                hint = (
                    " Designer/media auth is missing on this account; "
                    "push cookies/userscript media tokens if image gen should work."
                )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "message": f"upstream returned no image resource.{hint} preview={preview!r}",
                        "type": "upstream_error",
                    }
                },
            )

        if len(urls) > n:
            urls = urls[:n]

        base_url = str(raw_request.base_url).rstrip("/")
        secret = str(getattr(app.state, "media_proxy_secret", "") or "")
        key_obj = getattr(raw_request.state, "api_key_obj", None)
        user_suffixes = list(getattr(key_obj, "media_proxy_suffixes", []) or [])
        runtime = dict(getattr(app.state, "runtime_settings", {}) or {})
        suffixes = user_suffixes if user_suffixes else runtime.get("media_proxy_suffixes")
        ttl_seconds = runtime.get("media_proxy_ttl_seconds")

        data: list[dict[str, str]] = []
        for url in urls:
            # Ensure designer URLs are proxied even if rewriter missed a plain URL.
            proxied = _proxy_if_needed(
                url,
                base_url=base_url,
                account_id=account_id,
                secret=secret,
                allowed_suffixes=suffixes,
                ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
            )
            if response_format == "b64_json":
                try:
                    b64 = await _to_b64_json(
                        app,
                        account_id=account_id,
                        source_url=url,
                        secret=secret,
                    )
                    data.append({"b64_json": b64})
                except Exception as exc:
                    _log.warning("[/v1/images/generations] b64 fetch failed: %s", exc)
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "error": {
                                "message": (
                                    f"failed to materialize b64_json ({exc}). "
                                    "Use response_format=url, or ensure designer/media auth is present."
                                ),
                                "type": "upstream_error",
                            }
                        },
                    ) from exc
            else:
                data.append({"url": proxied})

        call_record["images"] = len(data)
        call_record["tool_calls_result"] = f"images={len(data)}"

        return {
            "created": int(time.time()),
            "data": data,
            "m365": {
                "model": request.model or model_alias,
                "tone": client._tone,
                "size": size,
                "has_designer_auth": has_designer,
                "has_media_auth": has_media,
                "source_count": len(urls),
            },
        }
