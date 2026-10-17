from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.account_serializers import account_public, user_account_public
from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.app import create_app
from m365_copilot_openai_proxy.config import Settings


SUBJECT = "home:personal-session-owner"


def session_for(subject=SUBJECT):
    return {
        "origin": "https://copilot.com",
        "account_id": subject,
        "cookies": [{"name": "WLSSC", "value": "private-personal-session", "domain": ".live.com", "path": "/", "secure": True}],
        "local_storage": {"msal.active-account-filters": json.dumps({"homeAccountId": subject.removeprefix("home:")})},
        "session_storage": {},
    }


@pytest.fixture
def api(tmp_path, monkeypatch):
    app = create_app(Settings(TOKEN_DIR=str(tmp_path), API_KEY="test-admin"))
    monkeypatch.setattr("m365_copilot_openai_proxy.routes_user._spawn_post_push_refresh", lambda *args, **kwargs: None)
    account = app.state.account_store.add(name="personal")
    key = app.state.key_store.add(account_id=account.id)
    return app, account, TestClient(app), {"Authorization": "Bearer " + key.key}


def test_personal_session_push_survives_restart_without_plaintext_secrets(api, tmp_path):
    app, account, client, headers = api
    expiry = time.time() + 1800
    session = session_for()
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe(), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session, "expires_at": expiry,
    })
    assert response.status_code == 200
    loaded = AccountStore(tmp_path / "accounts.json").get(account.id)
    assert getattr(loaded, "personal_substrate_session", {}) == session
    assert getattr(loaded, "substrate_token_expires_at", 0) == expiry
    public = client.get("/user/me", headers=headers).json()["account"]
    assert public["protocols"]["m365"]["refresh_mode"] == "browser"
    assert "personal_substrate_session" not in public
    persisted = (tmp_path / "accounts.json").read_text(encoding="utf-8")
    assert "private-personal-session" not in persisted
    assert json.loads(persisted)[account.id]["personal_substrate_session"]["__enc__"] == 1


def test_mismatched_session_cannot_replace_a_pushed_personal_token(api):
    app, account, client, headers = api
    original = make_jwe({"kid": "original"})
    assert client.post("/user/account/token", headers=headers, json={
        "token": original, "substrate_account_id": SUBJECT,
    }).status_code == 200
    epoch = account.protocol_epoch
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe({"kid": "untrusted"}), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session_for("home:other-account"),
    })
    assert response.status_code == 409
    assert account.token == original
    assert account.substrate_account_id == SUBJECT
    assert account.protocol_epoch == epoch


def test_untrusted_personal_origin_cannot_enable_browser_navigation(api):
    app, account, client, headers = api
    session = session_for()
    session["origin"] = "https://attacker.invalid"
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe(), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session,
    })
    assert response.status_code == 400
    assert account.token == ""
    assert account.substrate_account_id == ""


@pytest.mark.parametrize("change", [
    {"origin": "https://copilot.com/"},
    {"origin": "https://copilot.com@attacker.invalid"},
    {"cookies": "not-a-cookie-list"},
    {"cookies": [{"name": "WLSSC", "value": "secret", "domain": ".live.com", "secure": "true"}]},
    {"cookies": [{"name": "WLSSC", "value": "secret", "domain": "live.com.attacker.invalid"}]},
    {"cookies": [{"name": "WLSSC", "value": "secret", "domain": ".live.com", "expires": 1}]},
    {"local_storage": {"key": {"not": "a string"}}},
    {"session_storage": ["not a map"]},
    {"local_storage": {"key": "x" * (128 * 1024 + 1)}},
])
def test_malformed_session_rejects_without_account_key_or_disk_mutation(api, tmp_path, change):
    app, account, client, headers = api
    store = app.state.account_store
    store.push_token(account.id, make_jwe({"kid": "stored"}), substrate_account_id=SUBJECT,
                     personal_substrate_session=session_for())
    before_account = asdict(account)
    before_keys = [asdict(key) for key in app.state.key_store.list()]
    before_disk = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    session = session_for()
    session.update(change)
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe({"kid": "replacement"}), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session,
    })
    assert response.status_code == 400
    assert asdict(account) == before_account
    assert [asdict(key) for key in app.state.key_store.list()] == before_keys
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == before_disk


def test_session_on_enterprise_jwt_cannot_displace_keys_or_change_provider(api):
    from test_m365_refresh_token_flow import _jwt

    app, account, client, headers = api
    other = app.state.account_store.add(token=_jwt())
    other_key = app.state.key_store.add(account_id=other.id)
    before = asdict(account), asdict(other), asdict(other_key)
    response = client.post("/user/account/token", headers=headers, json={
        "token": other.token, "substrate_account_id": SUBJECT,
        "personal_substrate_session": session_for(),
    })
    assert response.status_code == 400
    assert (asdict(account), asdict(other), asdict(other_key)) == before


def test_normalization_filters_unrelated_domains_and_preserves_cookie_replay_metadata(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    session = session_for()
    cookie = session["cookies"][0]
    cookie.update(httpOnly=True, sameSite="None", expirationDate=time.time() + 7200,
                  hostOnly=False, session=False, storeId="0")
    session["cookies"].append({"name": "unrelated", "value": "private", "domain": ".example.com"})
    account = store.add(token=make_jwe(), substrate_account_id=SUBJECT,
                        personal_substrate_session=session)
    expected = deepcopy(session)
    expected["cookies"] = [deepcopy(cookie)]
    assert account.personal_substrate_session == expected
    session["cookies"][0]["value"] = "caller-mutated"
    assert account.personal_substrate_session == expected
    assert AccountStore(tmp_path / "accounts.json").get(account.id).personal_substrate_session == expected


def test_token_only_same_subject_preserves_session_but_not_old_issuer_deadline(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    token = make_jwe({"kid": "original"})
    expiry = time.time() + 1000
    account = store.add(token=token, substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for(), expires_at=expiry)
    captured_at = account.token_updated_at
    store.push_token(account.id, token, substrate_account_id=SUBJECT, expires_at=expiry + 9000)
    assert account.token_updated_at == captured_at
    assert account.substrate_token_expires_at == expiry
    store.push_token(account.id, make_jwe({"kid": "fresh"}), substrate_account_id=SUBJECT)
    assert account.personal_substrate_session == session_for()
    assert account.substrate_token_expires_at == 0


@pytest.mark.parametrize("operation", ["replace", "clear_token", "clear_credentials", "clear_subject"])
def test_subject_replacement_and_clear_remove_session_expiry_and_error(operation):
    store = AccountStore()
    account = store.add(token=make_jwe(), substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for(), expires_at=time.time() + 3600)
    account.personal_substrate_refresh_error = "refresh_failed"
    if operation == "replace":
        store.push_token(account.id, make_jwe({"kid": "other"}), substrate_account_id="home:other")
    elif operation == "clear_subject":
        store.push_token(account.id, make_jwe({"kid": "unbound"}), substrate_account_id="")
    else:
        getattr(store, operation)(account.id)
    assert account.personal_substrate_session == {}
    assert account.substrate_token_expires_at == 0
    assert account.personal_substrate_refresh_error == ""


def test_inactive_personal_save_does_not_touch_consumer_credentials(api):
    app, account, client, headers = api
    store = app.state.account_store
    store.set_consumer_auth(account.id, [{"name": "consumer", "value": "secret", "domain": ".copilot.com"}],
                            "consumer-token", consumer_account_id=SUBJECT,
                            consumer_refresh_token="consumer-grant", consumer_refresh_token_client_id="client",
                            consumer_refresh_token_scope="scope")
    consumer = {key: value for key, value in asdict(account).items() if key.startswith("consumer_")}
    epoch = account.protocol_epoch
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe(), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session_for(), "activate": False,
    })
    assert response.status_code == 200
    assert account.provider == "consumer"
    assert account.protocol_epoch == epoch
    assert {key: value for key, value in asdict(account).items() if key.startswith("consumer_")} == consumer


def test_personal_refresh_commits_atomically_and_keeps_protocol_epoch(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    original = make_jwe({"kid": "old"})
    account = store.add(token=original, substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    epoch = account.protocol_epoch
    session = deepcopy(account.personal_substrate_session)
    fresh_session = session_for()
    fresh_session["cookies"][0]["value"] = "rotated-private-session"
    expiry = time.time() + 7200
    account.personal_substrate_refresh_error = "refresh_failed"
    result = store.apply_personal_substrate_refresh_result(
        account.id, expected_token=original, expected_epoch=epoch, expected_session=session,
        token=make_jwe({"kid": "fresh"}), account_id=SUBJECT, session=fresh_session, expires_at=expiry,
    )
    assert result is account
    assert account.protocol_epoch == epoch
    assert account.provider == "m365"
    assert account.personal_substrate_refresh_error == ""
    assert account.personal_substrate_session == fresh_session
    assert account.substrate_token_expires_at == expiry
    assert AccountStore(tmp_path / "accounts.json").get(account.id).token == account.token


@pytest.mark.parametrize("race", ["token", "session", "epoch", "provider", "same_token", "subject", "invalid_token", "invalid_session"])
def test_rejected_refresh_preserves_every_newer_credential_and_error(race):
    store = AccountStore()
    original = make_jwe({"kid": "old"})
    account = store.add(token=original, substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    kwargs = dict(expected_token=original, expected_epoch=account.protocol_epoch,
                  expected_session=deepcopy(account.personal_substrate_session),
                  token=make_jwe({"kid": "fresh"}), account_id=SUBJECT, session=session_for())
    if race == "token":
        store.push_token(account.id, make_jwe({"kid": "user-push"}), substrate_account_id=SUBJECT)
    elif race == "session":
        changed = session_for()
        changed["local_storage"]["new"] = "user-push"
        store.push_token(account.id, original, substrate_account_id=SUBJECT, personal_substrate_session=changed)
    elif race == "epoch":
        account.protocol_epoch += 2
    elif race == "provider":
        account.provider = "consumer"
    elif race == "same_token":
        kwargs["token"] = original
    elif race == "subject":
        kwargs["account_id"] = "home:other"
        kwargs["session"] = session_for("home:other")
    elif race == "invalid_token":
        kwargs["token"] = "invalid-jwe"
    else:
        kwargs["session"]["origin"] = "https://attacker.invalid"
    account.personal_substrate_refresh_error = "identity_mismatch"
    before = asdict(account)
    assert store.apply_personal_substrate_refresh_result(account.id, **kwargs) is None
    assert asdict(account) == before


def test_jwe_status_distinguishes_issuer_expiry_from_capture_age():
    from m365_copilot_openai_proxy.account_store import Account

    now = time.time()
    account = Account(token=make_jwe(), token_updated_at=now - 100)
    status = account.token_status()
    assert status["valid"]
    assert status["expires_at"] is None
    assert status["expiry_source"] == "capture_age"
    assert status["recheck_at"] == account.token_updated_at + 3600
    account.substrate_token_expires_at = now - 1
    status = account.token_status()
    assert not status["valid"]
    assert status["expiry_source"] == "issuer"
    assert status["expires_at"] is not None
    account.substrate_token_expires_at = now + 7200
    account.token_updated_at = now - 7200
    assert account.token_status()["valid"]


def test_invalid_persisted_session_is_dropped_without_losing_tokens(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    account = store.add(token=make_jwe(), substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    raw = json.loads(path.read_text())
    invalid = session_for()
    invalid["origin"] = "https://attacker.invalid"
    raw[account.id]["personal_substrate_session"] = invalid
    raw[account.id]["personal_substrate_refresh_error"] = "private-token-from-browser-error"
    path.write_text(json.dumps(raw))
    loaded = AccountStore(path).get(account.id)
    assert loaded.token == account.token
    assert loaded.personal_substrate_session == {}
    assert loaded.personal_substrate_refresh_error == ""
    assert loaded.protocol_states()["m365"]["refresh_mode"] == "manual"


@pytest.mark.parametrize("expiry", [True, "2030-01-01", -1, 10**400])
def test_invalid_personal_expiry_is_rejected_before_mutation(api, expiry):
    _, account, client, headers = api
    before = asdict(account)
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe(), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session_for(), "expires_at": expiry,
    })
    assert response.status_code == 400
    assert asdict(account) == before


def test_snapshot_detaches_nested_personal_session():
    store = AccountStore()
    account = store.add(token=make_jwe(), substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    snapshot = store.get_request_snapshot(account.id)
    snapshot.personal_substrate_session["cookies"][0]["value"] = "changed"
    snapshot.personal_substrate_session["local_storage"]["new"] = "changed"
    assert account.personal_substrate_session == session_for()


def test_only_one_concurrent_refresh_can_commit_the_same_snapshot():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = AccountStore()
    original = make_jwe({"kid": "old"})
    account = store.add(token=original, substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    epoch = account.protocol_epoch
    snapshot = deepcopy(account.personal_substrate_session)
    barrier = Barrier(2)

    def apply(index):
        barrier.wait(timeout=10)
        return store.apply_personal_substrate_refresh_result(
            account.id, expected_token=original, expected_epoch=epoch, expected_session=snapshot,
            token=make_jwe({"kid": str(index)}), account_id=SUBJECT, session=session_for(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, index) for index in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(result is account for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert account.token != original
    assert account.protocol_epoch == epoch


def test_refresh_error_is_safe_persistent_and_snapshot_guarded(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    original = make_jwe({"kid": "old"})
    account = store.add(token=original, substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    expected = dict(expected_token=original, expected_epoch=account.protocol_epoch,
                    expected_session=deepcopy(account.personal_substrate_session))
    assert store.set_personal_substrate_refresh_error(account.id, "session_expired", **expected) is account
    assert AccountStore(tmp_path / "accounts.json").get(account.id).personal_substrate_refresh_error == "session_expired"
    assert store.set_personal_substrate_refresh_error(account.id, "Bearer private-token", **expected) is None
    assert account.personal_substrate_refresh_error == "session_expired"
    assert store.set_personal_substrate_refresh_error(account.id, "", **expected) is None
    store.push_token(account.id, make_jwe({"kid": "new"}), substrate_account_id=SUBJECT)
    before = asdict(account)
    assert store.set_personal_substrate_refresh_error(account.id, "refresh_failed", **expected) is None
    assert asdict(account) == before


@pytest.mark.parametrize("mutate", [
    lambda cookie: cookie.update(partitionKey="https://[invalid"),
    lambda cookie: cookie.update(partitionKey="https://copilot.com:invalid"),
    lambda cookie: cookie.update(partitionKey={"topLevelSite": "https://attacker.invalid"}),
    lambda cookie: cookie.update(expirationDate=10**400),
    lambda cookie: cookie.update(sameSite={"not": "a string"}),
])
def test_malformed_browser_cookie_metadata_cannot_escape_validation(api, mutate):
    _, account, client, headers = api
    session = session_for()
    mutate(session["cookies"][0])
    response = client.post("/user/account/token", headers=headers, json={
        "token": make_jwe(), "substrate_account_id": SUBJECT,
        "personal_substrate_session": session,
    })
    assert response.status_code == 400
    assert account.token == ""


def test_both_public_serializers_expose_presence_not_session_secrets():

    account = AccountStore().add(token=make_jwe(), substrate_account_id=SUBJECT,
                                 personal_substrate_session=session_for())
    for serialize in (account_public, user_account_public):
        public = serialize(account)
        assert public["has_personal_substrate_session"] is True
        assert public["protocols"]["m365"]["refresh_mode"] == "browser"
        assert "personal_substrate_session" not in public
        assert "private-personal-session" not in json.dumps(public)
        assert SUBJECT not in json.dumps(public)


def test_expired_session_never_advertises_usable_browser_renewal():
    account = AccountStore().add(token=make_jwe(), substrate_account_id=SUBJECT,
                                 personal_substrate_session=session_for())
    account.personal_substrate_session["cookies"][0]["expires"] = 1
    assert not account.has_personal_substrate_session
    assert account.protocol_states()["m365"]["refresh_mode"] == "manual"


@pytest.mark.parametrize("serialize", [account_public, user_account_public], ids=["admin", "user"])
def test_personal_cookie_binding_uses_owned_session_not_legacy_cookie_expiry(serialize):
    account = AccountStore().add(token=make_jwe(), substrate_account_id=SUBJECT,
                                 personal_substrate_session=session_for())
    account.cookie_updated_at = 1
    account.cookie_expires_at = 2

    public = serialize(account)

    assert public["cookie_valid"] is True
    assert public["binding_state"] == "cookie"
    assert public["cookie_updated_at"] == 0
    assert public["cookie_expires_at"] == 0
    assert account.cookie_valid is False
    assert account.cookie_expires_at == 2


@pytest.mark.parametrize("serialize", [account_public, user_account_public], ids=["admin", "user"])
@pytest.mark.parametrize("unusable_session", ["missing", "expired", "other_account"])
def test_personal_cookie_binding_never_borrows_legacy_enterprise_cookie_status(serialize, unusable_session):
    account = AccountStore().add(token=make_jwe(), substrate_account_id=SUBJECT,
                                 personal_substrate_session=session_for())
    account.cookie_valid = True
    if unusable_session == "missing":
        account.personal_substrate_session = {}
    elif unusable_session == "expired":
        account.personal_substrate_session["cookies"][0]["expires"] = 1
    else:
        account.personal_substrate_session = session_for("home:other-account")

    public = serialize(account)

    assert public["cookie_valid"] is False
    assert public["binding_state"] == "token_only"
    assert public["protocols"]["m365"]["refresh_mode"] == "manual"
    assert account.cookie_valid is True


def test_nonpersistable_storage_unicode_is_rejected_before_store_mutation(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    account = store.add(token=make_jwe(), substrate_account_id=SUBJECT,
                        personal_substrate_session=session_for())
    before = asdict(account)
    session = session_for()
    session["local_storage"]["broken"] = "\ud800"
    with pytest.raises(ValueError):
        store.push_token(account.id, make_jwe({"kid": "replacement"}), substrate_account_id=SUBJECT,
                         personal_substrate_session=session)
    assert asdict(account) == before
