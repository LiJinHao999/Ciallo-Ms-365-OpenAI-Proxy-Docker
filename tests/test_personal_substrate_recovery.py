from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi import FastAPI
from websockets.asyncio.server import serve

from jwe_helpers import make_jwe
from m365_copilot_openai_proxy.account_store import AccountStore
from m365_copilot_openai_proxy.dependencies import create_api_dependencies
from m365_copilot_openai_proxy.refresh_scheduler import RefreshScheduler
from m365_copilot_openai_proxy.session_store import PersistentSession
from m365_copilot_openai_proxy.substrate_client import (
    SIGNALR_SEP,
    SubstrateCopilotClient,
    SubstrateCopilotError,
)
from test_personal_substrate_session import SUBJECT, session_for


@pytest.fixture
def system(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    account = store.add(
        token=make_jwe({"kid": "old"}), substrate_account_id=SUBJECT,
        personal_substrate_session=session_for(), expires_at=time.time() + 4000,
    )
    scheduler = RefreshScheduler(store, tmp_path / "profiles")
    app = FastAPI()
    app.state.account_store = store
    app.state.refresh_scheduler = scheduler
    app.state.copilot_client_factory = lambda token, **kwargs: SubstrateCopilotClient(token)
    _, get_client = create_api_dependencies(app)
    client = get_client(SimpleNamespace(state=SimpleNamespace(account=account, api_key_obj=None)))
    fresh = {
        "access_token": make_jwe({"kid": "fresh"}), "account_id": SUBJECT,
        "session": session_for(), "expires_at": time.time() + 28000,
    }
    return store, account, scheduler, client, fresh


@asynccontextmanager
async def upstream(client, old_token, *, status=HTTPStatus.UNAUTHORIZED, reject_all=False, close_after_invoke=False):
    upgrades = []
    invocations = []

    async def process_request(connection, request):
        query = parse_qs(urlsplit(request.path).query)
        upgrades.append(query)
        if reject_all or query["access_token"] == [old_token]:
            return connection.respond(status, "credential rejected\n")
        return None

    async def handler(socket):
        await socket.recv()
        await socket.send("{}" + SIGNALR_SEP)
        invocations.append(json.loads((await socket.recv()).split(SIGNALR_SEP)[0]))
        if close_after_invoke:
            await socket.close(code=1008, reason="401 Unauthorized after invocation")
            return
        await socket.send(json.dumps({
            "type": 1, "target": "update", "arguments": [{"writeAtCursor": "RECOVERED"}],
        }) + SIGNALR_SEP)
        await socket.send(json.dumps({"type": 3}) + SIGNALR_SEP)

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        client._ws_url = lambda conv, session, request: (
            f"ws://127.0.0.1:{port}/?access_token={quote(client._token)}"
            f"&conversation={conv}&session={session}&request={request}"
        )
        yield upgrades, invocations


def test_upgrade_401_renews_without_replaying_or_resetting_continuation(system):
    store, account, scheduler, client, fresh = system
    old_token = account.token

    async def mint():
        return fresh

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        session = PersistentSession()
        session.turn_count = 2
        original = session.conversation_id, session.client_session_id
        async with upstream(client, old_token) as (upgrades, invocations):
            assert await client.chat("continue the existing conversation", [], session) == "RECOVERED"
        assert len(invocations) == 1
        assert len(upgrades) == 2
        assert upgrades[0]["access_token"] == [old_token]
        assert upgrades[1]["access_token"] == [fresh["access_token"]]
        for key in ("conversation", "session", "request"):
            assert upgrades[0][key] == upgrades[1][key]
        assert (session.conversation_id, session.client_session_id) == original
        assert session.turn_count == 3
        assert account.token == fresh["access_token"]
        assert account.substrate_token_expires_at == fresh["expires_at"]

    asyncio.run(scenario())


def test_second_upgrade_401_stops_after_one_renewal(system):
    store, account, scheduler, client, fresh = system
    old_token = account.token
    attempts = []

    async def mint():
        attempts.append(True)
        return fresh

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        async with upstream(client, old_token, reject_all=True) as (upgrades, invocations):
            with pytest.raises(SubstrateCopilotError, match="401"):
                await client.chat("do not loop", [])
        assert len(upgrades) == 2
        assert invocations == []
        assert attempts == [True]

    asyncio.run(scenario())


def test_upgrade_403_is_not_a_refresh_signal(system):
    store, account, scheduler, client, fresh = system
    old_token = account.token
    attempts = []

    async def mint():
        attempts.append(True)
        return fresh

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        async with upstream(client, old_token, status=HTTPStatus.FORBIDDEN) as (upgrades, invocations):
            with pytest.raises(SubstrateCopilotError, match="403"):
                await client.chat("permission denied", [])
        assert len(upgrades) == 1
        assert invocations == []
        assert attempts == []
        assert account.token == old_token

    asyncio.run(scenario())


def test_authentication_failure_after_invocation_never_replays_the_message(system):
    store, account, scheduler, client, fresh = system
    attempts = []

    async def mint():
        attempts.append(True)
        return fresh

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        async with upstream(client, "not-the-current-token", close_after_invoke=True) as (upgrades, invocations):
            with pytest.raises(SubstrateCopilotError, match="401 Unauthorized after invocation"):
                await client.chat("must execute at most once", [])
        assert len(upgrades) == 1
        assert len(invocations) == 1
        assert attempts == []

    asyncio.run(scenario())


def test_identity_cleared_during_renewal_cannot_reconnect(system):
    store, account, scheduler, client, fresh = system
    old_token = account.token

    async def mint():
        store.clear_token(account.id)
        return fresh

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        async with upstream(client, old_token) as (upgrades, invocations):
            with pytest.raises(SubstrateCopilotError):
                await client.chat("never continue as a different identity", [])
        assert len(upgrades) == 1
        assert invocations == []
        assert account.token == ""
        assert account.personal_substrate_session == {}

    asyncio.run(scenario())


def test_newer_user_push_is_used_without_launching_another_browser(system):
    store, account, scheduler, client, fresh = system
    old_token = account.token
    store.push_token(
        account.id, fresh["access_token"], substrate_account_id=SUBJECT,
        expires_at=fresh["expires_at"],
    )

    async def mint():
        raise AssertionError("The already-pushed credential must win")

    scheduler._personal_substrate_gate_factory = lambda account_id: mint

    async def scenario():
        async with upstream(client, old_token) as (upgrades, invocations):
            assert await client.chat("use the newest pinned credential", []) == "RECOVERED"
        assert len(upgrades) == 2
        assert len(invocations) == 1
        assert account.token == fresh["access_token"]
        assert account.personal_substrate_refresh_error == ""

    asyncio.run(scenario())
