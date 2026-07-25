"""Image-generation quota helpers and harvest/quota detection."""
from __future__ import annotations

import time

from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.routes_api_images import (
    _harvest_image_urls,
    _looks_like_quota_exhausted,
)


def test_looks_like_quota_exhausted_english():
    text = "Sorry, I can’t generate any more images today. Try again tomorrow."
    assert _looks_like_quota_exhausted(text) is True


def test_looks_like_quota_exhausted_negative():
    assert _looks_like_quota_exhausted("Here is your image ![image](https://x)") is False


def test_harvest_designer_url_still_works():
    urls = _harvest_image_urls(
        "![image](https://designerapp.officeapps.live.com/designerapp/document.ashx?id=1)"
    )
    assert len(urls) == 1


def test_account_image_quota_block_and_clear(tmp_path):
    store = AccountStore(persist_path=tmp_path / "accounts.json")
    acc = store.add(name="t", token="", token_source="manual")
    assert store.image_quota_blocked(acc.id) is False
    store.record_image_gen_failure(acc.id, "can't generate any more images today", quota_exhausted=True)
    assert store.image_quota_blocked(acc.id) is True
    acc2 = store.get(acc.id)
    assert acc2 is not None
    assert acc2.image_gen_fail_count == 1
    assert acc2.image_gen_quota_exhausted_until > time.time()
    store.clear_image_quota(acc.id)
    assert store.image_quota_blocked(acc.id) is False


def test_account_image_success_clears_exhausted(tmp_path):
    store = AccountStore(persist_path=tmp_path / "accounts.json")
    acc = store.add(name="t", token="", token_source="manual")
    store.record_image_gen_failure(acc.id, "quota", quota_exhausted=True)
    assert store.image_quota_blocked(acc.id) is True
    store.record_image_gen_success(acc.id, n=1)
    assert store.image_quota_blocked(acc.id) is False
    acc2 = store.get(acc.id)
    assert acc2 is not None
    assert acc2.image_gen_success_count == 1
