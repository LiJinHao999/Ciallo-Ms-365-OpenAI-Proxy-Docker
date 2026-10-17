from __future__ import annotations

from dataclasses import asdict
import time

from fastapi.testclient import TestClient

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings
from test_personal_substrate_session import SUBJECT, session_for


def test_legacy_cookie_push_cannot_mutate_a_personal_substrate_account(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="admin"))
    account = app.state.account_store.add(
        token=make_jwe(), substrate_account_id=SUBJECT,
        personal_substrate_session=session_for(), expires_at=time.time() + 4000,
    )
    key = app.state.key_store.add(account_id=account.id)
    before = asdict(account)

    async def unsafe_injection(*args, **kwargs):
        raise AssertionError("A personal JWE must not reach the enterprise cookie browser")

    app.state.refresh_scheduler.inject_cookies = unsafe_injection
    response = TestClient(app).post("/user/account/cookies", headers={"Authorization": "Bearer " + key.key}, json={
        "cookies": [{"name": "unrelated", "value": "new-cookie", "domain": ".microsoft.com"}],
        "local_storage": {"msal.account": "unrelated"},
    })
    assert response.status_code == 400
    assert asdict(account) == before


def test_admin_cookie_refresh_renews_the_personal_slot_not_the_enterprise_profile(tmp_path):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="admin", ADMIN_PASSWORD="admin-pass"))
    store = app.state.account_store
    account = store.add(
        token=make_jwe({"kid": "old"}), substrate_account_id=SUBJECT,
        personal_substrate_session=session_for(), expires_at=time.time() - 1,
    )
    store.set_cookies(account.id, [{"name": "old", "value": "enterprise", "domain": ".microsoft.com"}])
    fresh = make_jwe({"kid": "renewed"})

    async def mint():
        return {"access_token": fresh, "account_id": SUBJECT, "session": session_for(), "expires_at": time.time() + 28000}

    async def unsafe_injection(*args, **kwargs):
        raise AssertionError("Do not relaunch the enterprise profile for a personal refresh")

    app.state.refresh_scheduler._personal_substrate_gate_factory = lambda account_id: mint
    app.state.refresh_scheduler.inject_cookies = unsafe_injection
    client = TestClient(app)
    assert client.post("/admin/login", json={"password": "admin-pass"}).status_code == 200
    response = client.post("/admin/accounts/" + account.id + "/cookie-refresh")
    assert response.status_code == 200
    assert response.json()["refreshed"] is True
    assert account.token == fresh
    assert account.personal_substrate_session == session_for()
    assert account.provider == "m365"
