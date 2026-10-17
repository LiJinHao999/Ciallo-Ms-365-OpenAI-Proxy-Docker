from __future__ import annotations

from .account_store import Account
from .key_store import ApiKey
from .personal_substrate_session import normalize_personal_substrate_error


def account_binding_state(acc: Account | None) -> str:
    if acc is None:
        return "none"
    if acc.provider == "consumer":
        return "cookie" if acc.consumer_cookies else "token_only" if acc.consumer_token else "none"
    if getattr(acc, "cookie_valid", False):
        return "cookie"
    if acc.token:
        return "token_only"
    return "none"


def _provider_fields(acc: Account) -> dict:
    """Provider tag plus presence-only flags for the consumer credential pair."""
    return {
        "provider": getattr(acc, "provider", "m365"),
        "is_personal": acc.is_personal,
        "protocols": acc.protocol_states(),
        "cookie_valid": bool(acc.consumer_cookies) if acc.provider == "consumer" else acc.cookie_valid,
        "cookie_updated_at": acc.consumer_updated_at if acc.provider == "consumer" else acc.cookie_updated_at,
        "cookie_expires_at": 0.0 if acc.provider == "consumer" else acc.cookie_expires_at,
        "throttled_until": acc.throttled_until if acc.provider == "consumer" else 0.0,
        "throttled_mode": acc.throttled_mode if acc.provider == "consumer" else "",
        "throttled_at": acc.throttled_at if acc.provider == "consumer" else 0.0,
        "has_token": bool(acc.token) if acc.provider == "m365" else False,
        "has_consumer_token": bool(getattr(acc, "consumer_token", "")),
        "has_personal_substrate_session": acc.has_personal_substrate_session,
        "personal_substrate_refresh_error": normalize_personal_substrate_error(acc.personal_substrate_refresh_error),
        "substrate_token_expires_at": acc.substrate_token_expires_at,
        "consumer_updated_at": getattr(acc, "consumer_updated_at", 0.0),
        # Presence only, never the value: this is a long-lived bearer credential.
        # Surfaced because it is what decides whether renewal costs ~1.4s of HTTP
        # or a ~7s browser launch, so an operator needs to see it went missing.
        "has_consumer_refresh_token": bool(
            getattr(acc, "consumer_refresh_token", "")
        ),
        "consumer_refresh_token_updated_at": getattr(
            acc, "consumer_refresh_token_updated_at", 0.0
        ),
        # Why the fast path is gone, as a stable code the UI maps to text. Without
        # it a discarded RT is indistinguishable from one that never existed, and
        # the two need different actions from the user.
        "consumer_refresh_token_disabled_reason": str(
            getattr(acc, "consumer_refresh_token_disabled_reason", "") or ""
        ),
        "consumer_refresh_token_disabled_at": getattr(
            acc, "consumer_refresh_token_disabled_at", 0.0
        ),
        # Exposed in full, not as a presence flag: the user has to see and edit
        # the value. Credentials in a proxy URL are the user's own and were
        # supplied through this same endpoint.
        "proxy_url": getattr(acc, "proxy_url", ""),
        "studio_agent_ready": bool(getattr(acc, "studio_agent_ready", False)),
    }


def user_account_public(acc: Account | None) -> dict | None:
    if acc is None:
        return None
    binding_state = account_binding_state(acc)
    return {
        "id": acc.id,
        "name": acc.name,
        "email": acc.display_email,
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
        # Why the RT is gone, so the page can say "SPA RT hit its 24h ceiling,
        # sign in again" instead of only flipping has_refresh_token to false.
        # A stable code, mapped to text in template_*_i18n.py.
        "refresh_token_disabled_reason": str(
            getattr(acc, "refresh_token_disabled_reason", "") or ""
        ),
        "refresh_token_disabled_at": getattr(acc, "refresh_token_disabled_at", 0.0),
        "cookie_valid": bool(getattr(acc, "cookie_valid", False)),
        "cookie_updated_at": getattr(acc, "cookie_updated_at", 0.0),
        "cookie_expires_at": getattr(acc, "cookie_expires_at", 0.0),
        "throttled_until": getattr(acc, "throttled_until", 0.0),
        "throttled_mode": getattr(acc, "throttled_mode", ""),
        "throttled_at": getattr(acc, "throttled_at", 0.0),
        "token_status": acc.token_status(),
        **_provider_fields(acc),
    }


def account_public(acc: Account, bound_keys: list[ApiKey] | None = None) -> dict:
    keys = bound_keys or []
    binding_state = account_binding_state(acc)
    return {
        "id": acc.id,
        "name": acc.name,
        "email": acc.display_email,
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
        "refresh_token_disabled_reason": str(
            getattr(acc, "refresh_token_disabled_reason", "") or ""
        ),
        "refresh_token_disabled_at": getattr(acc, "refresh_token_disabled_at", 0.0),
        "throttled_until": getattr(acc, "throttled_until", 0.0),
        "throttled_mode": getattr(acc, "throttled_mode", ""),
        "throttled_at": getattr(acc, "throttled_at", 0.0),
        "token_status": acc.token_status(),
        "key_count": len(keys),
        "bound_names": [k.username or k.name or k.id for k in keys],
        "created_at": acc.created_at,
        "updated_at": acc.updated_at,
        **_provider_fields(acc),
    }
