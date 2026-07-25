"""Unit tests for PKCE helpers (no network)."""
from __future__ import annotations

import base64
import hashlib

from m365_copilot_openai_proxy.oauth_pkce import (
    PKCE_CLIENT_ID,
    PKCE_REDIRECT_URI,
    PKCESessionStore,
    SPA_CLIENT_ID,
    authorize_url,
    client_recipe,
    make_challenge,
    make_state,
    make_verifier,
    parse_callback,
)


def test_make_challenge_is_s256_base64url():
    verifier = "test-verifier-abcdefghijklmnopqrstuvwxyz0123456789"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert make_challenge(verifier) == expected


def test_authorize_url_contains_required_params():
    state = make_state()
    challenge = make_challenge(make_verifier())
    url = authorize_url(state=state, challenge=challenge)
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?")
    assert f"client_id={PKCE_CLIENT_ID}" in url
    assert "code_challenge_method=S256" in url
    assert "offline_access" in url
    assert state in url
    assert challenge in url


def test_parse_callback_full_url():
    code, state = parse_callback(
        url="https://login.microsoftonline.com/common/oauth2/nativeclient?code=ABC123&state=STATE99&session_state=x"
    )
    assert code == "ABC123"
    assert state == "STATE99"


def test_parse_callback_bare_code_with_state():
    code, state = parse_callback(code="RAWCODE", state="S1")
    assert code == "RAWCODE"
    assert state == "S1"


def test_parse_callback_query_only():
    code, state = parse_callback(url="?code=QQ&state=SS")
    assert code == "QQ"
    assert state == "SS"


def test_pkce_session_store_roundtrip():
    store = PKCESessionStore(ttl_seconds=60)
    state, url, redirect = store.create(owner_kind="user", owner_id="key1", account_id="acct1")
    assert redirect == PKCE_REDIRECT_URI
    assert state in url
    pending = store.get(state)
    assert pending is not None
    assert pending.owner_kind == "user"
    assert pending.owner_id == "key1"
    assert pending.account_id == "acct1"
    assert pending.verifier
    store.mark_authenticated(state, {"id": "acct1", "email": "a@b.c"})
    pub = store.status_public(state)
    assert pub["status"] == "authenticated"
    assert pub["account"]["email"] == "a@b.c"


def test_client_recipe_pkce_vs_spa():
    cid, scope, origin = client_recipe(PKCE_CLIENT_ID)
    assert cid == PKCE_CLIENT_ID
    assert origin is None
    assert "M365Chat.Read" in scope

    cid, scope, origin = client_recipe("")
    assert cid == SPA_CLIENT_ID
    assert origin == "https://m365.cloud.microsoft"
    assert scope.endswith("/sydney/.default")
