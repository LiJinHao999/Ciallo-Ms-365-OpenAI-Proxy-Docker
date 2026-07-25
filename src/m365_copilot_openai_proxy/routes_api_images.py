"""OpenAI-compatible image generation / edits via M365 Copilot Designer.

Endpoints:
  POST /v1/images/generations
  POST /v1/images/edits
  GET  /v1/images/{id}          cached image bytes (no auth; id is unguessable)

Default response_format is b64_json so WebUIs get pixels inline and do not
depend on a later Designer media-proxy fetch (which often 504s).

When response_format=url, we still materialize+cache bytes first, then return a
local cache URL under this proxy host (not designerapp), so clients always have
something loadable immediately.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from .call_log_store import append_call_log
from .config import Settings
from .media_proxy import (
    is_allowed_m365_media_url,
    make_signed_media_proxy_url,
    normalize_m365_media_text,
    verify_signed_media_proxy_params,
)
from .models import ImageData
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
    # Default b64_json: most WebUIs render this reliably; url mode still works
    # but is served from our local image cache after materialization.
    response_format: Literal["url", "b64_json"] | None = "b64_json"
    user: str | None = None
    quality: str | None = None
    style: str | None = None


def _public_base_url(request: Request) -> str:
    """Host that clients on the LAN can actually open.

    Prefer reverse-proxy headers, then Host, and only fall back to
    request.base_url (which may be http://127.0.0.1:8000 inside Docker and is
    useless to a browser on another machine).
    """
    xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    xf_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = xf_host or (request.headers.get("host") or "").strip()
    scheme = xf_proto or request.url.scheme or "http"
    if host:
        # If Host is an internal container address, keep request.base_url only when
        # it is already non-loopback; otherwise still use Host (LAN IP:8810).
        return f"{scheme}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _build_image_prompt(prompt: str, *, size: str, n: int, edit: bool = False) -> str:
    prompt = (prompt or "").strip()
    size = (size or "1024x1024").strip() or "1024x1024"
    n = max(1, min(int(n or 1), 4))
    count_hint = "one image" if n == 1 else f"{n} distinct images"
    if edit:
        return (
            "The source image is already attached to this message. "
            "Edit that attached image with Microsoft Designer / Flux. "
            "Do not ask the user to re-upload. "
            f"Produce {count_hint}. Preferred size: {size}. "
            "Apply the edit instructions and return the resulting image "
            "resource(s) directly (image markdown or designer/asyncgw URLs).\n\n"
            f"Edit instructions:\n{prompt}"
        )
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
        if "/v1/m365-media?" in cleaned or "/v1/images/" in cleaned:
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


def _unwrap_proxy_url(url: str, *, secret: str) -> tuple[str | None, str]:
    if "/v1/m365-media?" not in url:
        return None, url
    try:
        from urllib.parse import parse_qs

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


async def _fast_designer_fetch(account, source_url: str) -> tuple[bytes, str] | None:
    """Best-effort direct designer fetch without launching Chromium.

    The scheduler normally forces a browser fetch for designerapp (it rejects
    some plain GETs). Right after generation the URL still carries a fresh
    fileToken and the account often has designer_auth_token — try that first so
    /v1/images/generations can return b64 immediately instead of 504'ing while
    CDP re-captures auth.
    """
    from .media_proxy import designer_file_token, designer_object_fetch_url
    from .refresh_media import _is_designer_media_url

    if not _is_designer_media_url(source_url):
        return None
    designer_token = str(getattr(account, "designer_auth_token", "") or "").strip()
    if not designer_token:
        return None
    import httpx

    fetch_url = designer_object_fetch_url(source_url)
    headers = {
        "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://designerapp.officeapps.live.com/",
        "Authorization": designer_token,
    }
    file_token = designer_file_token(source_url)
    if file_token:
        headers["FileToken"] = file_token
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as http:
            resp = await http.get(fetch_url, headers=headers)
        if resp.status_code >= 400 or not resp.content:
            return None
        ct = resp.headers.get("content-type") or "image/png"
        if "image" not in ct and "octet-stream" not in ct:
            return None
        return resp.content, ct
    except Exception:
        return None


async def _materialize_image_bytes(
    app: FastAPI,
    *,
    account_id: str,
    source_url: str,
    secret: str = "",
) -> tuple[bytes, str]:
    """Return (bytes, content_type) for a designer/asyncgw/data URL."""
    if source_url.startswith("data:image/"):
        m = _DATA_URL_RE.match(source_url)
        if not m:
            raise RuntimeError("invalid data URL")
        raw = base64.b64decode(m.group(2))
        subtype = m.group(1) or "png"
        return raw, f"image/{subtype}"

    # Local cache URL already under this proxy.
    if "/v1/images/" in source_url:
        img_id = source_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        cache = getattr(app.state, "image_cache", None)
        if cache is not None:
            got = cache.get_bytes(img_id)
            if got:
                return got

    proxy_account, fetch_url = _unwrap_proxy_url(source_url, secret=secret)
    use_account = proxy_account or account_id
    if not use_account:
        raise RuntimeError("no bound account for image fetch")
    account = app.state.account_store.get(use_account)
    if account is not None:
        fast = await _fast_designer_fetch(account, fetch_url)
        if fast is not None:
            return fast
    fetcher = getattr(getattr(app.state, "refresh_scheduler", None), "fetch_image", None)
    if fetcher is None:
        raise RuntimeError("media fetcher unavailable")
    content, content_type = await fetcher(use_account, fetch_url)
    if not content:
        raise RuntimeError("empty image body")
    return content, content_type or "image/png"


def _candidate_accounts(app: FastAPI, preferred_id: str | None) -> list[Any]:
    store = app.state.account_store
    accounts = list(store.list())
    now = time.time()

    def usable(acc) -> bool:
        if not acc or not getattr(acc, "token", ""):
            return False
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
    rest = [a for a in accounts if a.id not in seen and usable(a)]
    rest.sort(
        key=lambda a: (
            0 if getattr(a, "designer_auth_token", "") else 1,
            0 if getattr(a, "refresh_token", "") else 1,
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


async def _run_image_job(
    app: FastAPI,
    *,
    raw_request: Request,
    client: SubstrateCopilotClient,
    settings: Settings,
    prompt: str,
    model: str | None,
    n: int,
    size: str,
    response_format: str,
    images: list[ImageData] | None = None,
    kind: str = "generation",
    endpoint: str = "/v1/images/generations",
):
    _log = logging.getLogger("copilot_proxy")
    model_alias = request_model_alias(app, raw_request, settings)
    resolved_tone, _ = resolve_request_tone(app, model)
    tone = resolved_tone or "Magic"
    client._tone = tone

    preferred = getattr(raw_request.state, "account", None)
    preferred_id = getattr(preferred, "id", None)
    candidates = _candidate_accounts(app, preferred_id)
    if not candidates:
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
        "endpoint": endpoint,
        "time": time.strftime("%H:%M:%S"),
        "ts": time.time(),
        "stream": False,
        "tools": [],
        "messages": 1,
        "model": model or model_alias,
        "tone": tone,
        "n": n,
        "size": size,
        "response_format": response_format,
        "kind": kind,
        "route_attempts": [],
    }
    append_call_log(app.state, call_record)

    image_prompt = _build_image_prompt(prompt, size=size, n=n, edit=(kind == "edit"))
    public_base = _public_base_url(raw_request)
    secret = str(getattr(app.state, "media_proxy_secret", "") or "")
    cache = getattr(app.state, "image_cache", None)

    last_error = ""
    tried: list[str] = []
    quota_hits = 0

    for acc in candidates:
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
            answer = await use_client.chat(image_prompt, [], session=None, images=images)
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
                continue
            last_error = f"upstream returned no image resource. preview={preview!r}"
            tried.append(f"{acc.email or acc.id}:no_image")
            continue

        if len(urls) > n:
            urls = urls[:n]

        data: list[dict[str, str]] = []
        cached_ids: list[str] = []
        try:
            for url in urls:
                content, content_type = await _materialize_image_bytes(
                    app,
                    account_id=acc.id,
                    source_url=url,
                    secret=secret,
                )
                meta = None
                if cache is not None:
                    meta = cache.put(
                        content,
                        content_type=content_type or "image/png",
                        account_id=acc.id,
                        account_email=acc.email or "",
                        prompt=prompt,
                        model=model or model_alias,
                        source_url=url,
                        kind=kind,
                    )
                    cached_ids.append(meta.id)
                b64 = base64.b64encode(content).decode("ascii")
                if response_format == "url":
                    if meta is not None:
                        item_url = f"{public_base}/v1/images/{meta.id}"
                    else:
                        # Fallback signed media proxy if cache unavailable.
                        item_url = make_signed_media_proxy_url(public_base, acc.id, url, secret) if secret and is_allowed_m365_media_url(url) else url
                    data.append({"url": item_url, "b64_json": b64})
                else:
                    # Standard OpenAI field is b64_json; also include a cache url
                    # when available so WebUIs can optionally hotlink.
                    item: dict[str, str] = {"b64_json": b64}
                    if meta is not None:
                        item["url"] = f"{public_base}/v1/images/{meta.id}"
                    data.append(item)
        except Exception as exc:
            msg = f"failed to materialize image result ({exc})"
            attempt["error"] = msg
            call_record["route_attempts"].append(attempt)
            last_error = msg
            app.state.account_store.record_image_gen_failure(acc.id, msg, quota_exhausted=False)
            tried.append(f"{acc.email or acc.id}:materialize")
            continue

        # OpenAI-compatible: for b64_json only b64_json is required; for url only
        # url is required. Keep both for convenience but strip the other when
        # clients are strict? Keep both — most clients ignore unknown sibling.
        if response_format == "url":
            data = [{"url": d["url"]} for d in data]
        else:
            data = [{"b64_json": d["b64_json"]} if "url" not in d else {"b64_json": d["b64_json"], "url": d["url"]} for d in data]

        app.state.account_store.record_image_gen_success(acc.id, n=len(data))
        attempt["ok"] = True
        attempt["images"] = len(data)
        attempt["cached_ids"] = cached_ids
        call_record["route_attempts"].append(attempt)
        call_record["images"] = len(data)
        call_record["account_id"] = acc.id
        call_record["account_email"] = acc.email
        call_record["tool_calls_result"] = f"images={len(data)};account={acc.email or acc.id};kind={kind}"
        call_record["routed"] = acc.id != preferred_id

        return {
            "created": int(time.time()),
            "data": data,
            "m365": {
                "model": model or model_alias,
                "tone": tone,
                "size": size,
                "kind": kind,
                "response_format": response_format,
                "account_id": acc.id,
                "account_email": acc.email,
                "routed": acc.id != preferred_id,
                "cached_ids": cached_ids,
                "has_designer_auth": bool(getattr(acc, "designer_auth_token", "")),
                "has_media_auth": bool(getattr(acc, "media_auth_token", "")),
                "source_count": len(urls),
                "tried": tried + [f"{acc.email or acc.id}:ok"],
            },
        }

    call_record["tool_calls_result"] = f"failed;tried={','.join(tried)}"
    if quota_hits and quota_hits >= max(1, len(tried)):
        return _json_err(
            429,
            "今日生图额度已用完（所有可用 Microsoft 账户均已达上限）。请换账号、清空额度标记，或明天再试。",
            "image_quota_error",
        )
    if last_error and _looks_like_quota_exhausted(last_error):
        return _json_err(429, f"今日生图额度已用完。{last_error}", "image_quota_error")
    return _json_err(502, last_error or "生图失败：上游未返回图片资源。", "upstream_error")


def register_images_routes(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    get_copilot_client: Callable[[Request], SubstrateCopilotClient],
) -> None:
    @app.get("/v1/images/{img_id}")
    async def get_cached_image(img_id: str):
        """Serve a previously generated image from local cache (no auth).

        IDs are unguessable (img_ + 32 hex). This avoids WebUI blank images when
        Designer media proxy is slow/unavailable after generation already succeeded.
        """
        cache = getattr(app.state, "image_cache", None)
        if cache is None:
            return _json_err(503, "image cache unavailable", "error")
        got = cache.get_bytes(img_id)
        if not got:
            return _json_err(404, "image not found", "not_found")
        content, content_type = got
        return Response(
            content=content,
            media_type=content_type or "image/png",
            headers={
                "Cache-Control": "private, max-age=86400",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.post("/v1/images/generations")
    async def images_generations(
        raw_request: Request,
        request: OpenAIImageGenerationRequest,
        settings: Settings = Depends(get_settings),  # noqa: B008
        client: SubstrateCopilotClient = Depends(get_copilot_client),  # noqa: B008
    ):
        prompt = (request.prompt or "").strip()
        if not prompt:
            return _json_err(400, "prompt is required", "invalid_request_error")
        n = int(request.n or 1)
        if n < 1 or n > 4:
            return _json_err(400, "n must be between 1 and 4", "invalid_request_error")
        response_format = (request.response_format or "b64_json").lower()
        if response_format not in {"url", "b64_json"}:
            return _json_err(400, "response_format must be url or b64_json", "invalid_request_error")
        size = (request.size or "1024x1024").strip() or "1024x1024"
        return await _run_image_job(
            app,
            raw_request=raw_request,
            client=client,
            settings=settings,
            prompt=prompt,
            model=request.model,
            n=n,
            size=size,
            response_format=response_format,
            images=None,
            kind="generation",
            endpoint="/v1/images/generations",
        )

    @app.post("/v1/images/edits")
    async def images_edits(
        raw_request: Request,
        settings: Settings = Depends(get_settings),  # noqa: B008
        client: SubstrateCopilotClient = Depends(get_copilot_client),  # noqa: B008
        image: UploadFile = File(...),  # noqa: B008
        prompt: str = Form(...),
        mask: UploadFile | None = File(None),  # noqa: B008
        model: str | None = Form(None),
        n: int = Form(1),
        size: str | None = Form("1024x1024"),
        response_format: str | None = Form("b64_json"),
        user: str | None = Form(None),
    ):
        """OpenAI Images Edit-compatible multipart endpoint.

        Accepts image (+ optional mask) and a prompt. Implementation is best-effort
        through Copilot/Designer multimodal chat with the uploaded image attached.
        """
        _ = user
        prompt = (prompt or "").strip()
        if not prompt:
            return _json_err(400, "prompt is required", "invalid_request_error")
        n = int(n or 1)
        if n < 1 or n > 4:
            return _json_err(400, "n must be between 1 and 4", "invalid_request_error")
        response_format = (response_format or "b64_json").lower()
        if response_format not in {"url", "b64_json"}:
            return _json_err(400, "response_format must be url or b64_json", "invalid_request_error")
        size = (size or "1024x1024").strip() or "1024x1024"

        raw = await image.read()
        if not raw:
            return _json_err(400, "image is empty", "invalid_request_error")
        if len(raw) > 20 * 1024 * 1024:
            return _json_err(400, "image too large (max 20MB)", "invalid_request_error")
        media_type = (image.content_type or "image/png").split(";")[0].strip() or "image/png"
        if not media_type.startswith("image/"):
            media_type = "image/png"
        images = [
            ImageData(
                base64=base64.b64encode(raw).decode("ascii"),
                media_type=media_type,
                file_name=image.filename or "edit-source.png",
            )
        ]
        if mask is not None:
            mraw = await mask.read()
            if mraw:
                mtype = (mask.content_type or "image/png").split(";")[0].strip() or "image/png"
                images.append(
                    ImageData(
                        base64=base64.b64encode(mraw).decode("ascii"),
                        media_type=mtype if mtype.startswith("image/") else "image/png",
                        file_name=mask.filename or "edit-mask.png",
                    )
                )

        return await _run_image_job(
            app,
            raw_request=raw_request,
            client=client,
            settings=settings,
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            response_format=response_format,
            images=images,
            kind="edit",
            endpoint="/v1/images/edits",
        )

    