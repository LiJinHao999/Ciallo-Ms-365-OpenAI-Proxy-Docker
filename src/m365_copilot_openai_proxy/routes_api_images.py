"""OpenAI-compatible image generation via M365 Copilot / Designer.

POST /v1/images/generations

This does NOT call a separate Designer REST API. It reuses the existing chat
path that already enables Flux/Designer image-gen feature flags, then harvests
designerapp / asyncgw image URLs from the streamed answer and rewrites them
through the signed media proxy so clients can fetch them with the same host.

Multi-account routing:
  - Prefer the API key's bound account first.
  - On daily image-quota exhaustion (or empty image answer that looks like a
    quota refusal), mark that account blocked until next Asia/Shanghai midnight
    and automatically try other accounts that still have a usable token.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, ConfigDict

from .call_log_store import append_call_log
from .config import Settings
from .media_proxy import (
    is_allowed_m365_media_url,
    make_signed_media_proxy_url,
    normalize_m365_media_text,
    verify_signed_media_proxy_params,
)
from .response_helpers import _json_err
from .routes_api_common import request_model_alias, resolve_request_tone
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

# Upstream quota refusal phrases observed from M365 Copilot / Designer.
_QUOTA_HINTS = (
    "can't generate any more images today",
    "cannot generate any more images today",
    "no more images today",
    "try again tomorrow",
    "image limit",
    "daily limit",
    "quota",
    "今天不能再生成",
    "今日无法再生成",
    "明天再试",
    "次数已用完",
    "生成次数",
    "已达上限",
)


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


def _looks_like_quota_exhausted(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False
    return any(h in low for h in _QUOTA_HINTS)


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
        if cleaned.startswith("data:image/"):
            seen.add(cleaned)
            found.append(cleaned)
            return
        if is_allowed_m365_media_url(cleaned) or _DESIGNER_URL_RE.match(cleaned) or _ASYNCGW_URL_RE.match(cleaned):
            seen.add(cleaned)
            found.append(cleaned)
            return
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


def _candidate_accounts(app: FastAPI, preferred_id: str | None) -> list[Any]:
    """Ordered account candidates for image generation routing."""
    store = app.state.account_store
    accounts = list(store.list())
    now = time.time()

    def usable(acc) -> bool:
        if not acc or not getattr(acc, "token", ""):
            return False
        # Prefer still-valid JWT; expired tokens may still work if RT can refresh
        # on the next /v1 path, but image path builds its own client so require a
        # non-empty token and not quota-blocked.
        if store.image_quota_blocked(acc.id, now):
            return False
        st = acc.token_status()
        return bool(st.get("valid") or getattr(acc, "refresh_token", ""))

    preferred = store.get(preferred_id) if preferred_id else None
    out: list[Any] = []
    seen: set[str] = set()
    if preferred is not None and usable(preferred):
        out.append(preferred)
        seen.add(preferred.id)

    # Prefer accounts that already look media-capable, then others with tokens.
    rest = [a for a in accounts if a.id not in seen and usable(a)]
    rest.sort(
        key=lambda a: (
            0 if getattr(a, "designer_auth_token", "") else 1,
            0 if getattr(a, "has_refresh_token", False) or getattr(a, "refresh_token", "") else 1,
            -float(getattr(a, "image_gen_last_success_at", 0.0) or 0.0),
        )
    )
    out.extend(rest)
    return out


def _client_for_account(app: FastAPI, account, tone: str, raw_request: Request) -> SubstrateCopilotClient:
    factory = getattr(app.state, "copilot_client_factory", None)
    key_obj = getattr(raw_request.state, "api_key_obj", None)
    global_tp = (getattr(app.state, "tool_prompt", "") or "").strip()
    key_tp = ((key_obj.tool_prompt if key_obj is not None else "") or "").strip()
    tool_prompt = "\n\n".join(p for p in (global_tp, key_tp) if p) or None
    time_zone = getattr(key_obj, "time_zone", "") or getattr(app.state, "time_zone", "Asia/Shanghai")
    key_idle_min = int(getattr(key_obj, "ws_idle_timeout_minutes", 0) or 0) if key_obj is not None else 0
    global_idle_min = int(getattr(app.state, "ws_idle_timeout_minutes", 0) or 0)
    idle_min = key_idle_min or global_idle_min
    idle_timeout = idle_min * 60 if idle_min > 0 else None
    if factory is None:
        return SubstrateCopilotClient(
            access_token=account.token,
            time_zone=time_zone,
            tone=tone,
            extra_tool_prompt=tool_prompt or "",
            idle_timeout=idle_timeout,
        )
    return factory(
        token=account.token,
        tone=tone,
        tool_prompt=tool_prompt,
        time_zone=time_zone,
        idle_timeout=idle_timeout,
    )


def register_images_routes(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    get_copilot_client: Callable[[Request], SubstrateCopilotClient],
) -> None:
    @app.post("/v1/images/generations")
    async def images_generations(
        raw_request: Request,
        request: OpenAIImageGenerationRequest,
        settings: Settings = Depends(get_settings),  # noqa: B008
        client: SubstrateCopilotClient = Depends(get_copilot_client),  # noqa: B008
    ):
        _log = logging.getLogger("copilot_proxy")
        prompt = (request.prompt or "").strip()
        if not prompt:
            return _json_err(400, "prompt is required", "invalid_request_error")

        n = int(request.n or 1)
        if n < 1 or n > 4:
            return _json_err(400, "n must be between 1 and 4", "invalid_request_error")

        response_format = (request.response_format or "url").lower()
        if response_format not in {"url", "b64_json"}:
            return _json_err(400, "response_format must be url or b64_json", "invalid_request_error")

        size = (request.size or "1024x1024").strip() or "1024x1024"
        model_alias = request_model_alias(app, raw_request, settings)
        resolved_tone, _ = resolve_request_tone(app, request.model)
        tone = resolved_tone or "Magic"
        client._tone = tone

        preferred = getattr(raw_request.state, "account", None)
        preferred_id = getattr(preferred, "id", None)
        candidates = _candidate_accounts(app, preferred_id)
        if not candidates:
            # Fall back to the dependency-injected client/account if pool empty.
            if preferred is not None and preferred.token:
                candidates = [preferred]
            else:
                return _json_err(
                    503,
                    "没有可用的 Microsoft 账户用于生图（无 token 或全部达到今日额度）。",
                    "image_quota_error",
                )

        call_record: dict[str, Any] = {
            "api": "images",
            "endpoint": "/v1/images/generations",
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "stream": False,
            "tools": [],
            "messages": 1,
            "model": request.model or model_alias,
            "tone": tone,
            "n": n,
            "size": size,
            "response_format": response_format,
            "route_attempts": [],
        }
        append_call_log(app.state, call_record)

        image_prompt = _build_image_prompt(prompt, size=size, n=n)
        # NOTE: do not rewrite media URLs with the request-bound account before we
        # know which account actually generated the image. Auto-routing may pick a
        # different account; proxy signatures must use that account's id.
        base_url = str(raw_request.base_url).rstrip("/")
        secret = str(getattr(app.state, "media_proxy_secret", "") or "")
        key_obj = getattr(raw_request.state, "api_key_obj", None)
        user_suffixes = list(getattr(key_obj, "media_proxy_suffixes", []) or [])
        runtime = dict(getattr(app.state, "runtime_settings", {}) or {})
        suffixes = user_suffixes if user_suffixes else runtime.get("media_proxy_suffixes")
        ttl_seconds = runtime.get("media_proxy_ttl_seconds")

        last_error = ""
        tried: list[str] = []
        quota_hits = 0

        for acc in candidates:
            # Ensure token is reasonably fresh before chatting.
            try:
                await app.state.refresh_scheduler.ensure_fresh(acc.id, force=False)
                acc = app.state.account_store.get(acc.id) or acc
            except Exception as exc:
                _log.warning("[images] ensure_fresh failed for %s: %s", acc.id, exc)

            if app.state.account_store.image_quota_blocked(acc.id):
                tried.append(f"{acc.email or acc.id}:blocked")
                quota_hits += 1
                continue

            use_client = client if (preferred_id and acc.id == preferred_id) else _client_for_account(app, acc, tone, raw_request)
            use_client._tone = tone

            attempt = {
                "account_id": acc.id,
                "email": acc.email,
                "has_designer_auth": bool(getattr(acc, "designer_auth_token", "")),
                "has_media_auth": bool(getattr(acc, "media_auth_token", "")),
            }
            try:
                answer = await use_client.chat(image_prompt, [], session=None, images=None)
            except SubstrateCopilotError as exc:
                msg = str(exc)
                attempt["error"] = msg
                call_record["route_attempts"].append(attempt)
                last_error = msg
                app.state.account_store.record_image_gen_failure(acc.id, msg, quota_exhausted=_looks_like_quota_exhausted(msg))
                tried.append(f"{acc.email or acc.id}:error")
                continue
            except Exception as exc:
                msg = f"image generation failed: {exc}"
                attempt["error"] = msg
                call_record["route_attempts"].append(attempt)
                last_error = msg
                app.state.account_store.record_image_gen_failure(acc.id, msg, quota_exhausted=False)
                tried.append(f"{acc.email or acc.id}:error")
                continue

            # Harvest from the raw upstream answer, then sign with the winning account.
            urls = _harvest_image_urls(answer)
            if not urls:
                preview = (answer or "").strip().replace("\n", " ")
                if len(preview) > 240:
                    preview = preview[:240] + "…"
                quota = _looks_like_quota_exhausted(answer)
                attempt["preview"] = preview
                attempt["quota"] = quota
                call_record["route_attempts"].append(attempt)
                app.state.account_store.record_image_gen_failure(
                    acc.id,
                    preview or "upstream returned no image resource",
                    quota_exhausted=quota,
                )
                if quota:
                    quota_hits += 1
                    tried.append(f"{acc.email or acc.id}:quota")
                    last_error = preview or "今日生图额度已用完"
                    # Try next account automatically.
                    continue
                last_error = f"upstream returned no image resource. preview={preview!r}"
                tried.append(f"{acc.email or acc.id}:no_image")
                # Non-quota empty answer: still try next account once pool has more.
                continue

            if len(urls) > n:
                urls = urls[:n]

            data: list[dict[str, str]] = []
            try:
                for url in urls:
                    proxied = _proxy_if_needed(
                        url,
                        base_url=base_url,
                        account_id=acc.id,
                        secret=secret,
                        allowed_suffixes=suffixes,
                        ttl_seconds=ttl_seconds if isinstance(ttl_seconds, int) else None,
                    )
                    if response_format == "b64_json":
                        b64 = await _to_b64_json(app, account_id=acc.id, source_url=url, secret=secret)
                        data.append({"b64_json": b64})
                    else:
                        data.append({"url": proxied})
            except Exception as exc:
                msg = f"failed to materialize image result ({exc})"
                attempt["error"] = msg
                call_record["route_attempts"].append(attempt)
                last_error = msg
                app.state.account_store.record_image_gen_failure(acc.id, msg, quota_exhausted=False)
                tried.append(f"{acc.email or acc.id}:materialize")
                continue

            app.state.account_store.record_image_gen_success(acc.id, n=len(data))
            attempt["ok"] = True
            attempt["images"] = len(data)
            call_record["route_attempts"].append(attempt)
            call_record["images"] = len(data)
            call_record["account_id"] = acc.id
            call_record["account_email"] = acc.email
            call_record["tool_calls_result"] = f"images={len(data)};account={acc.email or acc.id}"
            call_record["routed"] = acc.id != preferred_id

            return {
                "created": int(time.time()),
                "data": data,
                "m365": {
                    "model": request.model or model_alias,
                    "tone": tone,
                    "size": size,
                    "account_id": acc.id,
                    "account_email": acc.email,
                    "routed": acc.id != preferred_id,
                    "has_designer_auth": bool(getattr(acc, "designer_auth_token", "")),
                    "has_media_auth": bool(getattr(acc, "media_auth_token", "")),
                    "source_count": len(urls),
                    "tried": tried + [f"{acc.email or acc.id}:ok"],
                },
            }

        # All candidates failed.
        call_record["tool_calls_result"] = f"failed;tried={','.join(tried)}"
        if quota_hits and quota_hits >= max(1, len(tried)):
            return _json_err(
                429,
                "今日生图额度已用完（所有可用 Microsoft 账户均已达上限）。请换账号、清空额度标记，或明天再试。",
                "image_quota_error",
            )
        if last_error and _looks_like_quota_exhausted(last_error):
            return _json_err(
                429,
                f"今日生图额度已用完。{last_error}",
                "image_quota_error",
            )
        return _json_err(
            502,
            last_error or "生图失败：上游未返回图片资源。",
            "upstream_error",
        )
