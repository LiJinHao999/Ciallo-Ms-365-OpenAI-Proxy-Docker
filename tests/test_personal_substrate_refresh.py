from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlencode

import pytest

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.personal_substrate_refresh import (
    PersonalSubstrateRefreshError,
    _identity_storage,
    _token_receipt,
)


TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
UID = "00000000-0000-0000-511b-0123456789ab"
SUBJECT = "home:" + UID + "." + TENANT
URL = "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0/token"
SCOPE = "https://substrate.office.com/sydney/v2/.default"
FORM = urlencode({"client_id": "c0ab8ce9-e9a0-42e7-b064-33d422df41f1", "scope": SCOPE})


def receipt(*, uid=UID, tenant=TENANT, **changes):
    info = base64.urlsafe_b64encode(json.dumps({"uid": uid, "utid": tenant}).encode()).decode().rstrip("=")
    return {"access_token": make_jwe(), "scope": SCOPE, "client_info": info, "expires_in": 28799, **changes}


def test_issuer_deadline_is_measured_from_the_matching_response_only():
    before = time.time()
    captured = _token_receipt(URL, FORM, receipt(), SUBJECT)
    assert captured["account_id"] == SUBJECT
    assert before + 28799 <= captured["expires_at"] <= time.time() + 28799
    assert _token_receipt(URL, FORM, receipt(expires_in=None), SUBJECT)["expires_at"] == 0.0


@pytest.mark.parametrize("changes", [
    {"uid": "different-user"},
    {"tenant": "different-tenant"},
    {"client_info": "malformed"},
])
def test_wrong_or_missing_issuer_identity_cannot_authorize_a_fresh_jwe(changes):
    with pytest.raises(PersonalSubstrateRefreshError, match="identity_mismatch"):
        _token_receipt(URL, FORM, receipt(**changes), SUBJECT)


@pytest.mark.parametrize("changes", [
    {"scope": "https://substrate.office.com/sydney/.default"},
    {"scope": SCOPE + " https://graph.microsoft.com/User.Read"},
    {"access_token": "not-a-personal-jwe"},
])
def test_enterprise_or_mixed_resource_receipts_cannot_be_saved(changes):
    assert _token_receipt(URL, FORM, receipt(**changes), SUBJECT) is None


@pytest.mark.parametrize("lifetime", [True, "28799", -1, 0, float("inf"), float("nan")])
def test_malformed_issuer_lifetime_is_not_replaced_with_a_guessed_expiry(lifetime):
    with pytest.raises(PersonalSubstrateRefreshError, match="issuance_unavailable"):
        _token_receipt(URL, FORM, receipt(expires_in=lifetime), SUBJECT)


def test_storage_restores_only_the_pinned_account_without_cached_grants():
    account = json.dumps({"homeAccountId": SUBJECT.removeprefix("home:"), "username": "test@example.com"})
    storage = {
        "account-key": account,
        "msal.3.account.keys": json.dumps(["account-key", "other-key"]),
        "other-key": json.dumps({"homeAccountId": "another-account"}),
        "access-key": json.dumps({"homeAccountId": SUBJECT.removeprefix("home:"), "credentialType": "AccessToken", "secret": "cached-token"}),
        "refresh-key": json.dumps({"homeAccountId": SUBJECT.removeprefix("home:"), "refresh_token": "cached-grant"}),
        "encrypted-key": json.dumps({"id": "opaque", "nonce": "nonce", "data": "ciphertext"}),
    }
    assert _identity_storage(storage, SUBJECT) == {
        "account-key": account, "msal.3.account.keys": json.dumps(["account-key"]),
    }
