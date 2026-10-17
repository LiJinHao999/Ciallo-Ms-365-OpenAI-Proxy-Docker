from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.personal_substrate_refresh import PersonalSubstrateBrowserGate
from test_personal_substrate_refresh import FORM, SUBJECT, URL, receipt
from test_personal_substrate_session import session_for


class TokenResponse:
    status = 200
    url = URL
    request = SimpleNamespace(post_data=FORM)

    def __init__(self, *, stalled=False):
        self.stalled = stalled
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def json(self):
        if self.stalled:
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
        return receipt(access_token=make_jwe({"kid": "new"}))


class IssuingBrowser:
    def __init__(self, *, deliver_token):
        self.deliver_token = deliver_token
        self.stalled = TokenResponse(stalled=True)
        self.listeners = {}
        self.closed = False
        self.profile = None
        self.url = "https://copilot.com/chat"

    def factory(self, **options):
        self.profile = Path(options["user_data_dir"])
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def add_cookies(self, cookies):
        self.cookie_records = cookies

    async def add_init_script(self, script):
        self.init_script = script

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners.pop(event) is callback

    async def new_page(self):
        return self

    async def goto(self, *args, **kwargs):
        self.listeners["response"](self.stalled)
        await self.stalled.started.wait()
        if self.deliver_token:
            self.listeners["response"](TokenResponse())

    async def cookies(self):
        return self.cookie_records


def test_successful_issuance_does_not_wait_for_another_stalled_response_body(tmp_path):
    async def scenario():
        browser = IssuingBrowser(deliver_token=True)
        gate = PersonalSubstrateBrowserGate(
            tmp_path, session=session_for(SUBJECT), previous_token=make_jwe({"kid": "old"}),
        )
        result = await asyncio.wait_for(gate._run(browser.factory), timeout=1)
        assert result["access_token"] == make_jwe({"kid": "new"})
        assert result["account_id"] == SUBJECT
        assert browser.stalled.cancelled.is_set()
        assert browser.closed
        assert not browser.profile.exists()

    asyncio.run(scenario())


def test_cancellation_releases_the_browser_and_profile_without_waiting_for_a_body(tmp_path):
    async def scenario():
        browser = IssuingBrowser(deliver_token=False)
        gate = PersonalSubstrateBrowserGate(
            tmp_path, session=session_for(SUBJECT), previous_token=make_jwe({"kid": "old"}),
        )
        task = asyncio.create_task(gate._run(browser.factory))
        await browser.stalled.started.wait()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=1)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise AssertionError("A stalled response body blocked browser cancellation")
        except asyncio.CancelledError:
            pass
        assert task.cancelled()
        assert browser.stalled.cancelled.is_set()
        assert browser.closed
        assert not browser.profile.exists()

    asyncio.run(scenario())
