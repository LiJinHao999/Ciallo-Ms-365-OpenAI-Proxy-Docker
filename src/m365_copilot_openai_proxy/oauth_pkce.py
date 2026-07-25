"""Browser PKCE OAuth for Microsoft 365 Copilot substrate tokens.

This is the stable login path ported from M365-Copilot2API's flow:

  1) POST /user|/admin/oauth/start  -> authorize URL + state
  2) User signs in with a real browser
  3) Browser lands on nativeclient?code=...&state=...
  4) User pastes the full callback URL back
  5) Server exchanges code for access_token + refresh_token

Important: PKCE uses the Office web Copilot public client
(``c0ab8ce9-...``). The userscript SPA RT path still uses
``4765445b-...``. Do NOT mix client_ids on the same refresh chain —
each account stores ``oauth_client_id`` and refresh_via_rt picks the
matching recipe.

Native-client code exchange must NOT send an Origin header, or AAD
returns AADSTS9002326 (SPA-only cross-origin redemption).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .token_store import decode_jwt_payload, is_substrate_token_claims

# Office web Copilot first-party client (verified by Copilot2API PKCE).
PKCE_CLIENT_ID = "c0ab8ce9-e9a0-42e7-b064-33d422df41f1"
# Legacy SPA client used by the Tampermonkey-captured RT path.
SPA_CLIENT_ID = "4765445b-32c6-49b0-83e6-1d93765276ca"

PKCE_REDIRECT_URI = "https://login.microsoftonline.com/common/oauth2/nativeclient"
PKCE_AUTHORITY = "https://login.microsoftonline.com/common"
PKCE_AUTHORIZE_URL = f"{PKCE_AUTHORITY}/oauth2/v2.0/authorize"
PKCE_TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

PKCE_SCOPE = " ".join(
    [
        "openid",
        "profile",
        "offline_access",
        "https://substrate.office.com/sydney/M365Chat.Read",
        "https://substrate.office.com/sydney/sydney.readwrite",
    ]
)
SPA_SCOPE = "https://substrate.office.com/sydney/.default"
SPA_ORIGIN = "https://m365.cloud.microsoft"

_HTTP_TIMEOUT_SECONDS = 20
_PENDING_TTL_SECONDS = 10 * 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_verifier() -> str:
    return _b64url(secrets.token_bytes(64))


def make_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def make_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(*, state: str, challenge: str, prompt: str = "select_account") -> str:
    q = {
        "client_id": PKCE_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": PKCE_REDIRECT_URI,
        "response_mode": "query",
        "scope": PKCE_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if prompt:
        q["prompt"] = prompt
    return f"{PKCE_AUTHORIZE_URL}?{urlencode(q)}"


def parse_callback(
    *,
    url: str = "",
    code: str = "",
    state: str = "",
) -> tuple[str, str]:
    """Extract (code, state) from a pasted callback URL or raw fields.

    Accepts:
      - full nativeclient URL with ?code=&state=
      - bare query string
      - raw authorization code (state must be supplied separately)
    """
    raw_url = (url or "").strip()
    out_code = (code or "").strip()
    out_state = (state or "").strip()

    if raw_url:
        candidate = raw_url
        if "://" not in candidate and candidate.startswith("?"):
            candidate = "https://callback.local/" + candidate
        elif "://" not in candidate and "code=" in candidate:
            candidate = "https://callback.local/?" + candidate.lstrip("?")
        try:
            parsed = urlparse(candidate)
            qs = parse_qs(parsed.query)
            if not out_code:
                vals = qs.get("code") or []
                if vals:
                    out_code = vals[0].strip()
            if not out_state:
                vals = qs.get("state") or []
                if vals:
                    out_state = vals[0].strip()
            # Fragment form (rare)
            if (not out_code or not out_state) and parsed.fragment:
                fqs = parse_qs(parsed.fragment)
                if not out_code and fqs.get("code"):
                    out_code = fqs["code"][0].strip()
                if not out_state and fqs.get("state"):
                    out_state = fqs["state"][0].strip()
        except Exception:
            # If it doesn't parse as a URL, treat the whole string as a bare code
            # only when it does not look like a query string.
            if not out_code and "code=" not in raw_url and "://" not in raw_url:
                out_code = raw_url

    return out_code, out_state


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    expires_in: int = 0
    scope: str = ""
    claims: dict[str, Any] = field(default_factory=dict)


async def exchange_code(code: str, verifier: str, *, tenant: str = "common") -> TokenBundle:
    """Exchange an authorization code for tokens (PKCE public client, no Origin)."""
    data = {
        "client_id": PKCE_CLIENT_ID,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": PKCE_REDIRECT_URI,
        "code_verifier": verifier,
        "scope": PKCE_SCOPE,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    url = PKCE_TOKEN_URL_TMPL.format(tenant=tenant or "common")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, data=data, headers=headers)
    return _parse_token_response(resp)


async def refresh_with_client(
    refresh_token: str,
    *,
    client_id: str,
    scope: str,
    tenant: str = "organizations",
    origin: str | None = None,
) -> TokenBundle:
    """Refresh an RT under a specific public client.

    SPA recipe (4765445b): send Origin. Office-web PKCE recipe (c0ab8ce9): omit Origin.
    """
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": scope,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    url = PKCE_TOKEN_URL_TMPL.format(tenant=tenant or "organizations")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, data=data, headers=headers)
    return _parse_token_response(resp)


def client_recipe(oauth_client_id: str | None) -> tuple[str, str, str | None]:
    """Return (client_id, scope, origin_or_None) for RT refresh."""
    cid = (oauth_client_id or "").strip()
    if cid == PKCE_CLIENT_ID:
        return PKCE_CLIENT_ID, PKCE_SCOPE, None
    # Default / empty / SPA: keep the historical userscript recipe.
    return SPA_CLIENT_ID, SPA_SCOPE, SPA_ORIGIN


def _parse_token_response(resp: httpx.Response) -> TokenBundle:
    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"token endpoint returned non-JSON (HTTP {resp.status_code})") from exc

    if resp.status_code != 200:
        err = str(payload.get("error") or "")
        desc = str(payload.get("error_description") or "")
        first = desc.splitlines()[0] if desc else ""
        detail = f"{err}: {first}".strip(": ")
        raise RuntimeError(f"token endpoint HTTP {resp.status_code}: {detail or 'unknown error'}")

    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("token endpoint returned empty access_token")

    try:
        claims = decode_jwt_payload(access)
    except Exception as exc:
        raise RuntimeError("access_token is not a JWT") from exc
    if not is_substrate_token_claims(claims):
        raise RuntimeError(f"access_token aud={claims.get('aud')!r} is not a substrate token")

    refresh = payload.get("refresh_token") if isinstance(payload.get("refresh_token"), str) else ""
    id_token = payload.get("id_token") if isinstance(payload.get("id_token"), str) else ""
    expires_in = int(payload.get("expires_in") or 0)
    scope = str(payload.get("scope") or "")
    return TokenBundle(
        access_token=access,
        refresh_token=refresh or "",
        id_token=id_token or "",
        expires_in=expires_in,
        scope=scope,
        claims=claims,
    )


@dataclass
class PendingPKCE:
    verifier: str
    owner_kind: str  # "user" | "admin"
    owner_id: str  # key id for user; "admin" for admin
    account_id: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending | authenticated | error
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)


class PKCESessionStore:
    """In-memory PKCE state store (single-process Docker is fine).

    Multi-worker deployments would need a shared store; current compose runs
    one serve process.
    """

    def __init__(self, ttl_seconds: int = _PENDING_TTL_SECONDS):
        self._ttl = max(60, int(ttl_seconds))
        self._lock = threading.RLock()
        self._items: dict[str, PendingPKCE] = {}

    def create(
        self,
        *,
        owner_kind: str,
        owner_id: str,
        account_id: str = "",
    ) -> tuple[str, str, str]:
        """Create a pending session. Returns (state, auth_url, redirect_uri)."""
        self._purge_locked()
        verifier = make_verifier()
        challenge = make_challenge(verifier)
        state = make_state()
        with self._lock:
            self._items[state] = PendingPKCE(
                verifier=verifier,
                owner_kind=owner_kind,
                owner_id=owner_id,
                account_id=account_id or "",
            )
        return state, authorize_url(state=state, challenge=challenge), PKCE_REDIRECT_URI

    def get(self, state: str) -> PendingPKCE | None:
        with self._lock:
            self._purge_locked()
            item = self._items.get(state)
            if item is None:
                return None
            if time.time() - item.created_at > self._ttl:
                self._items.pop(state, None)
                return None
            return item

    def pop_verifier(self, state: str) -> PendingPKCE | None:
        """Fetch and keep the pending entry (status still pending) for exchange."""
        return self.get(state)

    def mark_authenticated(self, state: str, result: dict[str, Any]) -> None:
        with self._lock:
            item = self._items.get(state)
            if item is None:
                return
            item.status = "authenticated"
            item.result = dict(result)
            item.error = ""

    def mark_error(self, state: str, error: str) -> None:
        with self._lock:
            item = self._items.get(state)
            if item is None:
                return
            item.status = "error"
            item.error = error

    def status_public(self, state: str) -> dict[str, Any]:
        item = self.get(state)
        if item is None:
            return {"status": "expired"}
        out: dict[str, Any] = {"status": item.status}
        if item.error:
            out["error"] = item.error
        if item.result:
            out["account"] = item.result
        return out

    def _purge_locked(self) -> None:
        now = time.time()
        dead = [k for k, v in self._items.items() if now - v.created_at > self._ttl]
        for k in dead:
            self._items.pop(k, None)
