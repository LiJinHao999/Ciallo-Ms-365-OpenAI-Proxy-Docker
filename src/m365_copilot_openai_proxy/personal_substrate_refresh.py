"""Reissue personal Sydney v2 credentials through the signed-in Copilot page.

The page owns its pairwise broker flow. Observe its actual token response rather
than guessing a refresh-token exchange or sending a chat to open a WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .consumer_camoufox import (
    _UNPARTITIONED_PREFS,
    _assert_proxy_reachable,
    _default_headless,
    _drop_cloudflare_cookies,
    _proxy_option,
)
from .consumer_refresh_via_rt import account_id_from_client_info
from .personal_substrate_session import (
    PERSONAL_SUBSTRATE_ORIGIN,
    normalize_personal_substrate_error,
    normalize_personal_substrate_session,
)
from .refresh_via_rt import M365_REFRESH_CLIENT_IDS
from .token_store import is_valid_substrate_jwe


_MSA_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
_PERSONAL_SCOPES = frozenset({
    "https://substrate.office.com/sydney/v2/.default",
    "https://substrate.office.com/sydney/v2/sydney.readwrite",
})
_OIDC_SCOPES = frozenset({"openid", "profile", "offline_access"})


class PersonalSubstrateRefreshError(RuntimeError):
    def __init__(self, code: str):
        self.code = normalize_personal_substrate_error(code) or "refresh_failed"
        super().__init__(self.code)


def _token_receipt(url: str, post_data: str, payload: object, expected_subject: str) -> dict | None:
    """Accept only the observed personal resource and its issuing MSA subject."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "login.microsoftonline.com"
        or parsed.username or parsed.password or parsed.port not in (None, 443)
        or parsed.path.lower() not in (
            f"/{_MSA_TENANT}/oauth2/v2.0/token", "/consumers/oauth2/v2.0/token",
        )
    ):
        return None
    try:
        form = parse_qs(post_data, max_num_fields=128)
    except ValueError:
        return None
    client_ids = form.get("client_id", [])
    requested_scopes = form.get("scope", [])
    if (
        len(client_ids) != 1 or client_ids[0].lower() not in M365_REFRESH_CLIENT_IDS
        or len(requested_scopes) != 1
        or not _PERSONAL_SCOPES.intersection(requested_scopes[0].split())
        or not isinstance(payload, dict)
    ):
        return None
    returned_scopes = payload.get("scope")
    if not isinstance(returned_scopes, str):
        return None
    returned = set(returned_scopes.split())
    if not returned.intersection(_PERSONAL_SCOPES) or returned - _PERSONAL_SCOPES - _OIDC_SCOPES:
        return None
    token = payload.get("access_token")
    if not isinstance(token, str) or not is_valid_substrate_jwe(token):
        return None
    subject = account_id_from_client_info(payload.get("client_info"))
    if not subject.endswith("." + _MSA_TENANT):
        raise PersonalSubstrateRefreshError("identity_mismatch")
    if expected_subject.startswith("local:"):
        subject = "local:" + subject.removeprefix("home:").rsplit(".", 1)[0]
    if subject != expected_subject:
        raise PersonalSubstrateRefreshError("identity_mismatch")
    lifetime = payload.get("expires_in")
    if lifetime is None:
        expires_at = 0.0
    elif (
        isinstance(lifetime, bool) or not isinstance(lifetime, (int, float))
        or not math.isfinite(lifetime) or not 0 < lifetime < 253402300799 - time.time()
    ):
        raise PersonalSubstrateRefreshError("issuance_unavailable")
    else:
        expires_at = time.time() + lifetime
    return {"access_token": token, "account_id": subject, "expires_at": expires_at}


def _browser_cookies(cookies: list[dict]) -> list[dict]:
    """Translate captured extension records without changing cookie path scope."""
    records = []
    now = time.time()
    for cookie in _drop_cloudflare_cookies(cookies):
        if not cookie["value"]:
            continue
        deadlines = [cookie[key] for key in ("expires", "expirationDate") if cookie.get(key, 0) > 0]
        if deadlines and min(deadlines) <= now:
            continue
        record = {key: cookie[key] for key in ("name", "value", "domain")}
        record["path"] = cookie.get("path", "/")
        for key in ("secure", "httpOnly"):
            if key in cookie:
                record[key] = cookie[key]
        same_site = {
            "strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None",
        }.get(str(cookie.get("sameSite", "")).lower())
        if same_site:
            record["sameSite"] = same_site
        if deadlines:
            record["expires"] = min(deadlines)
        partition = cookie.get("partitionKey")
        if partition:
            record["partitionKey"] = partition if isinstance(partition, str) else partition["topLevelSite"]
        records.append(record)
    return records


def _identity_storage(storage: dict[str, str], subject: str) -> dict[str, str]:
    """Restore account selection, not cached tokens or in-flight OAuth requests.

    MSAL v3 encrypted entries need a browser-local key. Replaying them in a cold
    profile can strand silent SSO; account metadata plus the cookie session lets
    the real page issue fresh credentials instead.
    """
    result = {}
    indexes = {}
    for key, raw in storage.items():
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(value, list) and "account.keys" in key.lower():
            indexes[key] = value
            continue
        if not isinstance(value, dict) or any(
            name in value for name in (
                "credentialType", "credential_type", "secret", "access_token", "refresh_token", "id_token",
            )
        ):
            continue
        home = str(value.get("homeAccountId") or value.get("home_account_id") or "").lower()
        local = str(value.get("localAccountId") or value.get("local_account_id") or "").lower()
        actual = "home:" + home if home else "local:" + local if local else ""
        if actual == subject:
            result[key] = raw
    for key, values in indexes.items():
        selected = [value for value in values if isinstance(value, str) and value in result]
        if selected:
            result[key] = json.dumps(selected)
    return result


class PersonalSubstrateBrowserGate:
    def __init__(
        self, profile_root: str | Path, *, session: dict, previous_token: str,
        proxy_url: str = "", timeout: float = 180.0,
    ):
        self._profile_root = Path(profile_root)
        self._session = normalize_personal_substrate_session(session, session.get("account_id", ""))
        self._previous_token = previous_token
        self._proxy_url = proxy_url
        self._timeout = timeout

    async def __call__(self) -> dict:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError as exc:
            raise PersonalSubstrateRefreshError("browser_unavailable") from exc
        try:
            await _assert_proxy_reachable(_proxy_option(self._proxy_url))
            return await asyncio.wait_for(self._run(AsyncCamoufox), timeout=self._timeout)
        except PersonalSubstrateRefreshError:
            raise
        except asyncio.TimeoutError as exc:
            raise PersonalSubstrateRefreshError("issuance_unavailable") from exc
        except Exception as exc:
            code = "browser_unavailable" if type(exc).__name__ in ("CamoufoxNotInstalled", "FileNotFoundError") else "refresh_failed"
            raise PersonalSubstrateRefreshError(code) from exc

    async def _run(self, browser_factory) -> dict:
        self._profile_root.mkdir(parents=True, exist_ok=True)
        subject = self._session["account_id"]
        storage = {name: _identity_storage(self._session[name], subject) for name in ("local_storage", "session_storage")}
        captured = {}
        ready = asyncio.Event()
        pending: set[asyncio.Task] = set()

        async def observe(response) -> None:
            if ready.is_set() or response.status != 200:
                return
            parsed = urlsplit(response.url)
            if parsed.hostname != "login.microsoftonline.com" or not parsed.path.endswith("/oauth2/v2.0/token"):
                return
            try:
                receipt = _token_receipt(response.url, response.request.post_data or "", await response.json(), subject)
            except PersonalSubstrateRefreshError as exc:
                captured["error"] = exc.code
                ready.set()
                return
            except (ValueError, TypeError):
                return
            if receipt is not None and not ready.is_set():
                if receipt["access_token"] == self._previous_token:
                    captured["error"] = "issuance_unavailable"
                else:
                    captured.update(receipt)
                ready.set()

        def received(response) -> None:
            task = asyncio.create_task(observe(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        with tempfile.TemporaryDirectory(prefix="personal-substrate-", dir=self._profile_root) as profile:
            async with browser_factory(
                headless=_default_headless(), persistent_context=True, user_data_dir=profile,
                os="windows", humanize=True, geoip=True,
                firefox_user_prefs=dict(_UNPARTITIONED_PREFS), proxy=_proxy_option(self._proxy_url),
            ) as browser:
                await browser.add_cookies(_browser_cookies(self._session["cookies"]))
                seed = json.dumps({"origin": PERSONAL_SUBSTRATE_ORIGIN, **storage})
                await browser.add_init_script(
                    "(() => { const s=" + seed + "; if(location.origin!==s.origin)return;"
                    "for(const [name,store] of [['local_storage',localStorage],['session_storage',sessionStorage]])"
                    "for(const [key,value] of Object.entries(s[name]))store.setItem(key,value); })()"
                )
                browser.on("response", received)
                page = await browser.new_page()
                try:
                    await page.goto(PERSONAL_SUBSTRATE_ORIGIN + "/chat", wait_until="domcontentloaded", timeout=60_000)
                    await asyncio.wait_for(ready.wait(), timeout=max(1.0, self._timeout - 65))
                except asyncio.TimeoutError as exc:
                    host = urlsplit(page.url).hostname or ""
                    code = "session_expired" if host in ("login.live.com", "login.microsoftonline.com") else "issuance_unavailable"
                    raise PersonalSubstrateRefreshError(code) from exc
                finally:
                    browser.remove_listener("response", received)
                    if pending:
                        unfinished = tuple(pending)
                        for task in unfinished:
                            task.cancel()
                        await asyncio.gather(*unfinished, return_exceptions=True)
                if "error" in captured:
                    raise PersonalSubstrateRefreshError(captured["error"])
                refreshed = {
                    "origin": PERSONAL_SUBSTRATE_ORIGIN, "account_id": subject,
                    "cookies": _drop_cloudflare_cookies(await browser.cookies()),
                    **storage,
                }
                captured["session"] = normalize_personal_substrate_session(refreshed, subject)
        return captured
