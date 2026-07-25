"""Browser PKCE OAuth routes for user + admin.

Flow:
  POST /{user|admin}/oauth/start    -> {state, url, redirect_uri}
  POST /{user|admin}/oauth/callback -> paste nativeclient?code=... URL
  GET  /{user|admin}/oauth/status   -> pending / authenticated / error / expired

Never logs code / access_token / refresh_token.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable

from fastapi import FastAPI, Request

from .account_serializers import account_public, user_account_public
from .account_store import extract_identity
from .key_store import ApiKey
from .oauth_pkce import PKCE_CLIENT_ID, exchange_code, parse_callback
from .response_helpers import _json_err
from .runtime_flags import elog, ulog


def register_oauth_routes(app: FastAPI, require_admin: Callable[[Request], object | None]) -> None:
    def _resolve_user_key(request: Request) -> ApiKey | None:
        auth = request.headers.get("Authorization", "")
        m = re.match(r"^Bearer\s+(.+)$", auth, re.IGNORECASE)
        if not m:
            return None
        return app.state.key_store.resolve(m.group(1).strip())

    def _pkce_store():
        store = getattr(app.state, "pkce_store", None)
        if store is None:
            # Lazy fallback so older tests that skip full state_init still work.
            from .oauth_pkce import PKCESessionStore

            store = PKCESessionStore()
            app.state.pkce_store = store
        return store

    def _apply_tokens_to_account(
        *,
        access_token: str,
        refresh_token: str,
        preferred_account_id: str = "",
        preferred_name: str = "",
        bind_key: ApiKey | None = None,
    ) -> tuple[object, int]:
        """Write access+RT onto an account (create/reuse/displace like token push).

        Returns (account, displaced_count).
        """
        name, email = extract_identity(access_token)
        displaced = 0
        reused = app.state.account_store.find_by_email(email) if email else None

        if preferred_account_id:
            target = app.state.account_store.get(preferred_account_id)
            if target is not None:
                # Identity guard when the target already has an email.
                if target.email and email and target.email.lower() != email.lower():
                    raise ValueError(
                        f"identity mismatch: account={target.email!r}, captured={email!r}"
                    )
                acc = app.state.account_store.push_token(target.id, access_token)
                if refresh_token:
                    app.state.account_store.set_refresh_token(target.id, refresh_token)
                app.state.account_store.set_oauth_client_id(target.id, PKCE_CLIENT_ID)
                if bind_key is not None:
                    app.state.key_store.update(bind_key.id, account_id=target.id, displaced_at=0.0)
                return acc, 0

        if reused is not None:
            acc = app.state.account_store.push_token(reused.id, access_token)
            if refresh_token:
                app.state.account_store.set_refresh_token(reused.id, refresh_token)
            app.state.account_store.set_oauth_client_id(reused.id, PKCE_CLIENT_ID)
            if bind_key is not None:
                now = time.time()
                for other in app.state.key_store.list_for_account(reused.id):
                    if other.id == bind_key.id:
                        continue
                    app.state.key_store.update(other.id, account_id="", displaced_at=now)
                    displaced += 1
                old_acc_id = bind_key.account_id
                app.state.key_store.update(bind_key.id, account_id=reused.id, displaced_at=0.0)
                if old_acc_id and old_acc_id != reused.id and not app.state.key_store.list_for_account(old_acc_id):
                    app.state.account_store.remove(old_acc_id)
            return acc, displaced

        # Create or update the key's current account.
        if bind_key is not None:
            acc_id = bind_key.account_id
            if not acc_id or app.state.account_store.get(acc_id) is None:
                acc = app.state.account_store.add(
                    name=preferred_name or name or bind_key.name or bind_key.username or "user",
                    token=access_token,
                    token_source="manual",
                )
                app.state.key_store.update(bind_key.id, account_id=acc.id, displaced_at=0.0)
            else:
                # Identity guard on the already-bound account.
                existing = app.state.account_store.get(acc_id)
                if existing and existing.email and email and existing.email.lower() != email.lower():
                    raise ValueError(
                        f"identity mismatch: account={existing.email!r}, captured={email!r}"
                    )
                acc = app.state.account_store.push_token(acc_id, access_token)
                if bind_key.displaced_at:
                    app.state.key_store.update(bind_key.id, displaced_at=0.0)
            if refresh_token:
                app.state.account_store.set_refresh_token(acc.id, refresh_token)
            app.state.account_store.set_oauth_client_id(acc.id, PKCE_CLIENT_ID)
            return acc, displaced

        # Admin path with no preferred account: create a new pool entry.
        acc = app.state.account_store.add(
            name=preferred_name or name or "oauth",
            token=access_token,
            token_source="manual",
        )
        if refresh_token:
            app.state.account_store.set_refresh_token(acc.id, refresh_token)
        app.state.account_store.set_oauth_client_id(acc.id, PKCE_CLIENT_ID)
        return acc, displaced

    async def _handle_callback(
        *,
        owner_kind: str,
        owner_id: str,
        body: dict,
        bind_key: ApiKey | None = None,
        allow_account_id: str = "",
    ) -> dict:
        store = _pkce_store()
        url = str(body.get("url") or body.get("callback") or "")
        code = str(body.get("code") or "")
        state = str(body.get("state") or "")
        code, state = parse_callback(url=url, code=code, state=state)
        if not state or not code:
            return _json_err(400, "missing state or code (paste the full nativeclient callback URL)")

        pending = store.pop_verifier(state)
        if pending is None:
            return _json_err(400, "invalid or expired state; start OAuth again")
        if pending.owner_kind != owner_kind or pending.owner_id != owner_id:
            return _json_err(403, "OAuth session does not belong to this caller")

        try:
            bundle = await exchange_code(code, pending.verifier)
        except Exception as exc:
            # Never include the code or tokens in the error surface.
            msg = str(exc) or "token exchange failed"
            store.mark_error(state, msg)
            elog(f"OAuth exchange failed for {owner_kind}/{owner_id}: {msg}")
            return _json_err(400, msg)

        preferred_account_id = allow_account_id or pending.account_id or ""
        try:
            acc, displaced = _apply_tokens_to_account(
                access_token=bundle.access_token,
                refresh_token=bundle.refresh_token,
                preferred_account_id=preferred_account_id,
                bind_key=bind_key,
            )
        except ValueError as exc:
            store.mark_error(state, str(exc))
            return _json_err(409, str(exc))

        if acc is None:
            store.mark_error(state, "failed to persist account")
            return _json_err(500, "failed to persist account")

        result = {
            "id": acc.id,
            "email": acc.email,
            "name": acc.name,
            "has_token": bool(acc.token),
            "has_refresh_token": bool(getattr(acc, "refresh_token", "")),
            "oauth_client_id": PKCE_CLIENT_ID,
        }
        store.mark_authenticated(state, result)
        ulog(
            f"OAuth login ok for {owner_kind}/{owner_id}: account={acc.id} "
            f"email={acc.email or '-'} rt={'yes' if bundle.refresh_token else 'no'}"
        )
        out = {
            "status": "ok",
            "account": user_account_public(acc) if bind_key is not None else account_public(acc),
            "displaced": displaced,
            "has_refresh_token": bool(bundle.refresh_token),
        }
        if not bundle.refresh_token:
            out["warning"] = "Login succeeded but no refresh_token was returned; token will expire without RT."
        return out

    # ------------------------------------------------------------------ user
    @app.post("/user/oauth/start")
    async def user_oauth_start(request: Request) -> dict:
        k = _resolve_user_key(request)
        if k is None:
            return _json_err(401, "Invalid API key", "auth_error")
        if not k.enabled:
            return _json_err(403, "This account is disabled", "auth_error")
        store = _pkce_store()
        state, url, redirect_uri = store.create(
            owner_kind="user",
            owner_id=k.id,
            account_id=k.account_id or "",
        )
        return {
            "status": "pkce_ready",
            "state": state,
            "url": url,
            "redirect_uri": redirect_uri,
            "note": "Sign in with Microsoft, then paste the full nativeclient callback URL (with code=) into /user/oauth/callback.",
        }

    @app.post("/user/oauth/callback")
    async def user_oauth_callback(request: Request) -> dict:
        k = _resolve_user_key(request)
        if k is None:
            return _json_err(401, "Invalid API key", "auth_error")
        if not k.enabled:
            return _json_err(403, "This account is disabled", "auth_error")
        body = await request.json()
        if not isinstance(body, dict):
            return _json_err(400, "JSON body required")
        return await _handle_callback(
            owner_kind="user",
            owner_id=k.id,
            body=body,
            bind_key=k,
        )

    @app.get("/user/oauth/status")
    async def user_oauth_status(request: Request) -> dict:
        k = _resolve_user_key(request)
        if k is None:
            return _json_err(401, "Invalid API key", "auth_error")
        state = (request.query_params.get("state") or "").strip()
        if not state:
            return _json_err(400, "missing state")
        store = _pkce_store()
        pending = store.get(state)
        if pending is not None and (pending.owner_kind != "user" or pending.owner_id != k.id):
            return _json_err(403, "OAuth session does not belong to this caller")
        return store.status_public(state)

    # ------------------------------------------------------------------ admin
    @app.post("/admin/oauth/start")
    async def admin_oauth_start(request: Request) -> dict:
        err = require_admin(request)
        if err:
            return err
        body: dict = {}
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            body = {}
        account_id = str(body.get("account_id") or "").strip()
        if account_id and app.state.account_store.get(account_id) is None:
            return _json_err(404, "Account not found")
        store = _pkce_store()
        state, url, redirect_uri = store.create(
            owner_kind="admin",
            owner_id="admin",
            account_id=account_id,
        )
        return {
            "status": "pkce_ready",
            "state": state,
            "url": url,
            "redirect_uri": redirect_uri,
            "account_id": account_id,
            "note": "Sign in with Microsoft, then paste the full nativeclient callback URL (with code=) into /admin/oauth/callback.",
        }

    @app.post("/admin/oauth/callback")
    async def admin_oauth_callback(request: Request) -> dict:
        err = require_admin(request)
        if err:
            return err
        body = await request.json()
        if not isinstance(body, dict):
            return _json_err(400, "JSON body required")
        # Prefer explicit account_id in body; otherwise the pending session's.
        allow_account_id = str(body.get("account_id") or "").strip()
        return await _handle_callback(
            owner_kind="admin",
            owner_id="admin",
            body=body,
            bind_key=None,
            allow_account_id=allow_account_id,
        )

    @app.get("/admin/oauth/status")
    async def admin_oauth_status(request: Request) -> dict:
        err = require_admin(request)
        if err:
            return err
        state = (request.query_params.get("state") or "").strip()
        if not state:
            return _json_err(400, "missing state")
        store = _pkce_store()
        pending = store.get(state)
        if pending is not None and pending.owner_kind != "admin":
            return _json_err(403, "OAuth session does not belong to admin")
        return store.status_public(state)
