from __future__ import annotations

from .account_store import Account
from .key_store import ApiKey


def account_binding_state(acc: Account | None) -> str:
    if acc is None:
        return "none"
    if getattr(acc, "cookie_valid", False):
        return "cookie"
    if acc.token:
        return "token_only"
    return "none"


def user_account_public(acc: Account | None) -> dict | None:
    if acc is None:
        return None
    binding_state = account_binding_state(acc)
    return {
        "id": acc.id,
        "name": acc.name,
        "email": acc.email,
        "token_source": acc.token_source,
        "binding_state": binding_state,
        "updated_at": acc.updated_at,
        "has_token": bool(acc.token),
        "has_media_auth": bool(getattr(acc, "media_auth_token", "")),
        "media_auth_updated_at": getattr(acc, "media_auth_updated_at", 0.0),
        "has_designer_auth": bool(getattr(acc, "designer_auth_token", "")),
        "designer_auth_updated_at": getattr(acc, "designer_auth_updated_at", 0.0),
        "has_media_seed": bool(getattr(acc, "media_seed_url", "")),
        "has_refresh_token": bool(getattr(acc, "refresh_token", "")),
        "refresh_token_updated_at": getattr(acc, "refresh_token_updated_at", 0.0),
        "oauth_client_id": getattr(acc, "oauth_client_id", "") or "",
        "cookie_valid": bool(getattr(acc, "cookie_valid", False)),
        "cookie_updated_at": getattr(acc, "cookie_updated_at", 0.0),
        "cookie_expires_at": getattr(acc, "cookie_expires_at", 0.0),
        "image_gen": image_gen_public(acc),
        "token_status": acc.token_status(),
    }


def image_gen_public(acc: Account) -> dict:
    """Public image-generation quota snapshot for admin/user UIs."""
    import time

    now = time.time()
    day = getattr(acc, "image_gen_day", "") or ""
    exhausted_until = float(getattr(acc, "image_gen_quota_exhausted_until", 0.0) or 0.0)
    # Roll display counters if the stored day is stale (without mutating store here).
    try:
        from .account_store import AccountStore

        today = AccountStore._image_day_key(now)
    except Exception:
        today = day
    success = int(getattr(acc, "image_gen_success_count", 0) or 0)
    fail = int(getattr(acc, "image_gen_fail_count", 0) or 0)
    if day and today and day != today:
        success = 0
        fail = 0
        day = today
        if exhausted_until and now >= exhausted_until:
            exhausted_until = 0.0
    blocked = bool(exhausted_until and now < exhausted_until)
    return {
        "day": day or today,
        "success_count": success,
        "fail_count": fail,
        "quota_exhausted": blocked,
        "quota_exhausted_until": exhausted_until if blocked else 0.0,
        "last_error": getattr(acc, "image_gen_last_error", "") or "",
        "last_success_at": float(getattr(acc, "image_gen_last_success_at", 0.0) or 0.0),
        "last_attempt_at": float(getattr(acc, "image_gen_last_attempt_at", 0.0) or 0.0),
        "available": (not blocked) and bool(getattr(acc, "token", "")),
    }


def account_public(acc: Account, bound_keys: list[ApiKey] | None = None) -> dict:
    keys = bound_keys or []
    binding_state = account_binding_state(acc)
    return {
        "id": acc.id,
        "name": acc.name,
        "email": acc.email,
        "cdp_port": acc.cdp_port,
        "token_source": acc.token_source,
        "binding_state": binding_state,
        "cookie_valid": bool(getattr(acc, "cookie_valid", False)),
        "cookie_updated_at": getattr(acc, "cookie_updated_at", 0.0),
        "cookie_expires_at": getattr(acc, "cookie_expires_at", 0.0),
        "has_token": bool(acc.token),
        "has_media_auth": bool(getattr(acc, "media_auth_token", "")),
        "media_auth_updated_at": getattr(acc, "media_auth_updated_at", 0.0),
        "has_designer_auth": bool(getattr(acc, "designer_auth_token", "")),
        "designer_auth_updated_at": getattr(acc, "designer_auth_updated_at", 0.0),
        "has_media_seed": bool(getattr(acc, "media_seed_url", "")),
        "has_refresh_token": bool(getattr(acc, "refresh_token", "")),
        "refresh_token_updated_at": getattr(acc, "refresh_token_updated_at", 0.0),
        "oauth_client_id": getattr(acc, "oauth_client_id", "") or "",
        "image_gen": image_gen_public(acc),
        "token_status": acc.token_status(),
        "key_count": len(keys),
        "bound_names": [k.name or k.username or k.id for k in keys],
        "created_at": acc.created_at,
        "updated_at": acc.updated_at,
    }
