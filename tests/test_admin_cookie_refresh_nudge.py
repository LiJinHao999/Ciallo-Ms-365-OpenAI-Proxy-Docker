from __future__ import annotations

from fastapi.testclient import TestClient

from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings


def make_client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="api-key", ADMIN_PASSWORD="admin-pass")))


def _login(client: TestClient) -> None:
    assert client.post("/admin/login", json={"password": "admin-pass"}).status_code == 200


def test_admin_cookie_refresh_without_stored_cookies_returns_400(tmp_path):
    client = make_client(tmp_path)
    _login(client)

    store = client.app.state.account_store
    acc = store.add(name="acct", token="tok")

    resp = client.post(f"/admin/accounts/{acc.id}/cookie-refresh")

    assert resp.status_code == 400
