"""HTTP refresh_token -> substrate access token exchange (no browser).

Two public-client recipes are supported (selected by account.oauth_client_id):

1) SPA / userscript (default, client 4765445b-...):
    POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
    Origin: https://m365.cloud.microsoft
    client_id     = 4765445b-32c6-49b0-83e6-1d93765276ca
    grant_type    = refresh_token
    refresh_token = <RT>
    scope         = https://substrate.office.com/sydney/.default

2) Office-web PKCE (client c0ab8ce9-...):
    Same token endpoint, but NO Origin header, and the full Office-web scope
    string used at login. Mixing client_ids across login/refresh breaks the chain.

The response carries an access_token (aud=https://substrate.office.com/) and a
rotated refresh_token, which we persist so the chain keeps renewing. media and
designer tokens are a different client/flow and are NOT produced here.
"""
from __future__ import annotations

import time

from .account_store import AccountStore, extract_identity
from .oauth_pkce import client_recipe, refresh_with_client
from .token_store import decode_jwt_payload, is_substrate_token_claims
from .runtime_flags import elog, ulog


def _tenant_for_account(account) -> str:
    """Best-effort tenant id for the token endpoint.

    The refresh_token itself encodes the tenant, so the multi-tenant
    `organizations` authority works as a fallback, but using the account's own
    tenant id (from the current substrate token's `tid` claim) is more precise
    and avoids surprises with guest/multi-tenant identities.
    """
    token = getattr(account, "token", "") or ""
    if token:
        try:
            claims = decode_jwt_payload(token)
            tid = claims.get("tid")
            if isinstance(tid, str) and tid:
                return tid
        except Exception:
            pass
    return "organizations"


async def refresh_via_rt(accounts: AccountStore, account_id: str) -> bool:
    """Exchange the account's stored refresh_token for a fresh substrate token.

    Returns True and persists the new access token (+ rotated refresh_token) on
    success. Returns False (leaving existing state intact) when the account has
    no refresh_token, the exchange fails, the response is not a substrate token,
    or the captured identity conflicts with the account's known email.
    """
    account = accounts.get(account_id)
    if account is None:
        return False
    rt = (getattr(account, "refresh_token", "") or "").strip()
    if not rt:
        return False

    tenant = _tenant_for_account(account)
    client_id, scope, origin = client_recipe(getattr(account, "oauth_client_id", "") or "")

    try:
        bundle = await refresh_with_client(
            rt,
            client_id=client_id,
            scope=scope,
            tenant=tenant,
            origin=origin,
        )
    except Exception as exc:
        # Keep logs free of token material; RuntimeError from refresh_with_client
        # already strips secrets down to AADSTS summaries.
        elog(f"RT refresh failed for {account_id}: {exc}")
        return False

    access_token = bundle.access_token
    claims = bundle.claims or {}
    # Belt-and-suspenders: refresh_with_client already validates substrate aud,
    # but re-check so a future change can't silently write a bad token.
    if not is_substrate_token_claims(claims):
        try:
            claims = decode_jwt_payload(access_token)
        except Exception as exc:
            elog(f"RT refresh failed for {account_id}: access_token not a JWT: {exc}")
            return False
        if not is_substrate_token_claims(claims):
            elog(f"RT refresh failed for {account_id}: token aud={claims.get('aud')!r} is not substrate")
            return False

    # Identity guard: never overwrite an established account with a token that
    # decodes to a different identity (mirrors the CDP path's guard).
    if account.email:
        _, captured_email = extract_identity(access_token)
        if captured_email and captured_email.lower() != account.email.lower():
            elog(
                f"RT refresh rejected for {account_id}: identity mismatch "
                f"(account={account.email!r}, captured={captured_email!r})"
            )
            return False

    # Persist the rotated refresh_token FIRST so a crash right after can't lose
    # the new RT while the old one is already invalidated by AAD.
    rotated = bundle.refresh_token
    if isinstance(rotated, str) and rotated and rotated != rt:
        accounts.set_refresh_token(account_id, rotated)

    # Preserve the existing token_source (None = leave unchanged): an RT refresh
    # neither creates nor removes a signed-in Chromium profile, so a "manual"
    # account stays "manual" and a "cdp" account stays "cdp".
    accounts.update_token(account_id, access_token)
    seconds = max(0, int(claims.get("exp", 0)) - int(time.time()))
    recipe = "pkce" if origin is None else "spa"
    ulog(
        f"RT refresh succeeded for {account_id}: substrate token via HTTP "
        f"(recipe={recipe}, expires in {seconds}s, rotated_rt={'yes' if rotated and rotated != rt else 'no'})"
    )
    return True
