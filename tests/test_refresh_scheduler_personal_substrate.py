from __future__ import annotations

import asyncio
import time

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.refresh_scheduler import RefreshScheduler
from test_personal_substrate_session import SUBJECT, session_for


def account_and_scheduler(tmp_path, *, expires_at=None, with_session=True):
    store = AccountStore(tmp_path / "accounts.json")
    account = store.add(
        token=make_jwe({"kid": "old"}), substrate_account_id=SUBJECT,
        personal_substrate_session=session_for() if with_session else None,
        expires_at=time.time() - 1 if expires_at is None else expires_at,
    )
    scheduler = RefreshScheduler(store, tmp_path / "profiles")
    return store, account, scheduler


def refreshed_auth():
    return {
        "access_token": make_jwe({"kid": "renewed"}),
        "account_id": SUBJECT,
        "session": session_for(),
        "expires_at": time.time() + 28000,
    }


def test_request_before_expired_personal_jwe_renews_without_chromium(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    fresh = refreshed_auth()
    calls = 0

    async def gate():
        nonlocal calls
        calls += 1
        return fresh

    async def chromium(*args, **kwargs):
        raise AssertionError("Personal renewal must not open the enterprise Chromium path")

    scheduler._personal_substrate_gate_factory = lambda account_id: gate
    scheduler._run_cdp_refresh = chromium
    epoch = account.protocol_epoch
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is True
    assert account.token == fresh["access_token"]
    assert account.substrate_token_expires_at == fresh["expires_at"]
    assert account.protocol_epoch == epoch
    assert account.provider == "m365"
    assert calls == 1
    reloaded = AccountStore(tmp_path / "accounts.json").get(account.id)
    assert reloaded.token == fresh["access_token"]
    assert reloaded.personal_substrate_session == fresh["session"]


def test_force_personal_jwe_without_session_cannot_borrow_enterprise_profile(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path, with_session=False)

    async def chromium(*args, **kwargs):
        raise AssertionError("A personal token has no authority to use an enterprise browser profile")

    scheduler._run_cdp_refresh = chromium
    assert asyncio.run(scheduler.ensure_fresh(account.id, force=True)) is False
    assert account.token == make_jwe({"kid": "old"})


def test_still_fresh_personal_jwe_does_not_open_a_browser(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path, expires_at=time.time() + 3600)

    async def gate():
        raise AssertionError("Fresh credentials must not launch a browser")

    scheduler._personal_substrate_gate_factory = lambda account_id: gate
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is True
    assert account.token == make_jwe({"kid": "old"})


def test_concurrent_requests_share_one_personal_renewal_and_new_credential(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    fresh = refreshed_auth()
    calls = 0

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gate():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return fresh

        scheduler._personal_substrate_gate_factory = lambda account_id: gate
        first = asyncio.create_task(scheduler.ensure_fresh(account.id))
        await asyncio.sleep(0)
        waiters = [asyncio.create_task(scheduler.ensure_fresh(account.id)) for _ in range(6)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, *waiters)
        assert all(results)
        assert calls == 1
        assert account.token == fresh["access_token"]
        assert account.substrate_token_expires_at == fresh["expires_at"]

    asyncio.run(scenario())


def test_user_push_while_browser_waits_wins_over_late_renewal(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    fresh = refreshed_auth()
    user_token = make_jwe({"kid": "new-user-push"})

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gate():
            entered.set()
            await release.wait()
            return fresh

        scheduler._personal_substrate_gate_factory = lambda account_id: gate
        task = asyncio.create_task(scheduler.ensure_fresh(account.id))
        await asyncio.sleep(0)
        store.push_token(account.id, user_token, substrate_account_id=SUBJECT, expires_at=time.time() + 4000)
        release.set()
        await task
        assert account.token == user_token
        assert account.personal_substrate_refresh_error == ""

    asyncio.run(scenario())


def test_failed_personal_renewal_backs_off_but_new_session_can_retry(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    calls = 0

    async def failing_gate():
        nonlocal calls
        calls += 1
        raise RuntimeError("private browser failure text")

    scheduler._personal_substrate_gate_factory = lambda account_id: failing_gate
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is False
    assert account.personal_substrate_refresh_error == "refresh_failed"
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is False
    assert calls == 1
    updated = session_for()
    updated["cookies"][0]["value"] = "new-session-secret"
    store.push_token(account.id, account.token, substrate_account_id=SUBJECT, personal_substrate_session=updated)
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is False
    assert calls == 2
    assert "private" not in account.personal_substrate_refresh_error


def test_personal_jwe_failure_before_deadline_preserves_remaining_validity(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path, expires_at=time.time() + 60)

    async def failing_gate():
        raise RuntimeError("temporary unavailability")

    scheduler._personal_substrate_gate_factory = lambda account_id: failing_gate
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is True
    assert account.token == make_jwe({"kid": "old"})
    assert account.personal_substrate_refresh_error == "refresh_failed"


def test_personal_background_eligibility_tracks_issuer_not_cookie_expiry(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path, expires_at=time.time() + 1800)
    assert scheduler._personal_substrate_keepalive_due(account) is False
    account.substrate_token_expires_at = time.time() + 60
    assert scheduler._personal_substrate_keepalive_due(account) is True
    account.provider = "consumer"
    assert scheduler._personal_substrate_keepalive_due(account) is False


def test_concurrent_forced_failures_share_one_browser_attempt(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    attempts = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gate():
            attempts.append(True)
            entered.set()
            await release.wait()
            raise RuntimeError("private OAuth error")

        scheduler._personal_substrate_gate_factory = lambda account_id: gate
        first = asyncio.create_task(scheduler.ensure_fresh(account.id, force=True))
        await entered.wait()
        waiters = [asyncio.create_task(scheduler.ensure_fresh(account.id, force=True)) for _ in range(6)]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, *waiters) == [False] * 7
        assert attempts == [True]
        assert account.personal_substrate_refresh_error == "refresh_failed"

    asyncio.run(scenario())


def test_new_push_while_waiting_for_browser_slot_skips_the_stale_launch(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    fresh = refreshed_auth()
    attempts = []

    async def gate():
        attempts.append(True)
        return refreshed_auth()

    scheduler._personal_substrate_gate_factory = lambda account_id: gate

    async def scenario():
        await scheduler._lock.acquire()
        pending = asyncio.create_task(scheduler.ensure_fresh(account.id))
        await asyncio.sleep(0)
        store.push_token(account.id, fresh["access_token"], substrate_account_id=SUBJECT, expires_at=fresh["expires_at"])
        scheduler._lock.release()
        assert await pending is True
        assert attempts == []
        assert account.token == fresh["access_token"]

    asyncio.run(scenario())


def test_protocol_switch_during_personal_renewal_is_not_undone(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    old_token = account.token
    old_epoch = account.protocol_epoch

    async def gate():
        store.set_consumer_auth(
            account.id, [{"name": "session", "value": "consumer", "domain": ".copilot.com"}],
            "consumer-token", consumer_account_id=SUBJECT,
        )
        return refreshed_auth()

    scheduler._personal_substrate_gate_factory = lambda account_id: gate
    assert asyncio.run(scheduler.ensure_fresh(account.id)) is False
    assert account.provider == "consumer"
    assert account.consumer_token == "consumer-token"
    assert account.token == old_token
    assert account.protocol_epoch != old_epoch
    assert account.personal_substrate_refresh_error == ""


def test_restart_uses_the_encrypted_personal_session_to_renew_an_expired_jwe(tmp_path):
    store, account, scheduler = account_and_scheduler(tmp_path)
    reloaded = AccountStore(tmp_path / "accounts.json")
    restarted = RefreshScheduler(reloaded, tmp_path / "new-profiles")
    fresh = refreshed_auth()

    async def gate():
        return fresh

    restarted._personal_substrate_gate_factory = lambda account_id: gate
    assert asyncio.run(restarted.ensure_fresh(account.id)) is True
    assert reloaded.get(account.id).token == fresh["access_token"]
    assert reloaded.get(account.id).substrate_token_expires_at == fresh["expires_at"]
    assert reloaded.get(account.id).personal_substrate_session == fresh["session"]
