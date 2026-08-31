from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from m365_copilot_openai_proxy.response_helpers import _anthropic_stream, _openai_stream, _responses_stream
from m365_copilot_openai_proxy.substrate_client import SIGNALR_SEP, SubstrateCopilotClient


def _substrate_jwt() -> str:
    """Build a decodable (unsigned) substrate JWT accepted by the client ctor."""
    claims = {
        "aud": "https://substrate.office.com/",
        "exp": int(time.time()) + 3600,
        "oid": "oid-0000",
        "tid": "tid-0000",
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


_COT_SUMMARY = (
    "**Considering citation strategy**\nI'm thinking about how to structure the "
    "response with citations. I'll aim to use two sources per paragraph."
)
_BODY_TEXT = "## Conclusion\n\nThis is the complete body text with no thinking content."


def _update(**fields):
    msg = {"type": 1, "target": "update", "arguments": [fields]}
    return json.dumps(msg, ensure_ascii=False) + SIGNALR_SEP


def _final(messages):
    msg = {"type": 2, "item": {"messages": messages}}
    return json.dumps(msg, ensure_ascii=False) + SIGNALR_SEP


def _end():
    return json.dumps({"type": 3}) + SIGNALR_SEP


def _cot_entry(text=_COT_SUMMARY):
    return {
        "text": text,
        "messageType": "Progress",
        "contentOrigin": "ChainOfThoughtSummary",
        "author": "bot",
    }


def _body_entry(text=_BODY_TEXT):
    # Real body messages carry NO messageType and come from DeepLeo.
    return {"text": text, "author": "bot", "contentOrigin": "DeepLeo", "responseIdentifier": "Default"}


def _refs_complete_entry():
    return {"messageType": "ReferencesListComplete", "author": "bot"}


@pytest.fixture
def stream_client(monkeypatch):
    """Build a SubstrateCopilotClient whose websocket yields a preset frame list."""
    frames: list[str] = []
    recorded: list[dict] = []

    class FakeWs:
        def __init__(self):
            self._sent: list[str] = []
            self._frames = list(frames)

        async def send(self, data):
            self._sent.append(data)

        async def recv(self):
            # First recv is the SignalR handshake acknowledgement.
            return json.dumps({"type": 6})

        async def _aiter(self):
            for frame in self._frames:
                yield frame

        def __aiter__(self):
            return self._aiter()

    class FakeConnect:
        def __init__(self, url, **kwargs):
            self.url = url

        async def __aenter__(self):
            return FakeWs()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("m365_copilot_openai_proxy.substrate_client.websockets.connect", FakeConnect)

    def build(*args, **kwargs):
        return SubstrateCopilotClient(_substrate_jwt())

    return {"frames": frames, "build": build}


def _stream_body(client, *, reasoning_out: list[str] | None):
    async def run():
        return [c async for c in client.chat_stream("hello", [], None, None, reasoning_out=reasoning_out)]

    return asyncio.run(run())


def test_chat_stream_collects_cot_and_never_leaks_it_into_body(stream_client):
    """Regression: a chain-of-thought summary arriving AFTER the body started
    must be collected into reasoning_out, and must NOT be appended to the
    streamed body via the t==3 fallback reconciliation (the 16:29:57 leak)."""
    client = stream_client["build"]()
    frames = stream_client["frames"]
    frames.extend([
        _update(messages=[_cot_entry()]),
        _update(writeAtCursor=_BODY_TEXT, messages=[_body_entry()]),
        _update(messages=[_cot_entry()]),  # late summary, body already streaming
        _end(),
    ])

    reasoning_out: list[str] = []
    body = "".join(_stream_body(client, reasoning_out=reasoning_out))

    assert reasoning_out == [_COT_SUMMARY, _COT_SUMMARY]
    assert body == _BODY_TEXT
    assert "Considering" not in body
    assert "citation strategy" not in body


def test_chat_stream_collects_cot_without_final_item(stream_client):
    """Even when the final type=2 payload never arrives (fallback stays on the
    last body message), no chain-of-thought text leaks into the body."""
    client = stream_client["build"]()
    frames = stream_client["frames"]
    frames.extend([
        _update(messages=[_cot_entry()]),
        _update(writeAtCursor="## Conclusion", messages=[_body_entry("## Conclusion")]),
        _update(messages=[_cot_entry()]),
        _end(),
    ])

    reasoning_out: list[str] = []
    body = "".join(_stream_body(client, reasoning_out=reasoning_out))

    assert reasoning_out == [_COT_SUMMARY, _COT_SUMMARY]
    assert "Considering" not in body
    assert body == "## Conclusion"


def test_chat_stream_fallback_ignores_references_list_complete(stream_client):
    """ReferencesListComplete (text-less) must not clear a valid body fallback;
    the body still completes when the stream ends."""
    client = stream_client["build"]()
    frames = stream_client["frames"]
    frames.extend([
        _update(writeAtCursor="## Conclusion", messages=[_body_entry("## Conclusion")]),
        _update(messages=[_refs_complete_entry()]),
        _end(),
    ])

    assert "".join(_stream_body(client, reasoning_out=None)) == "## Conclusion"


# ---------------------------------------------------------------------------
# Response-layer formatting helpers
# ---------------------------------------------------------------------------


class ReasoningStreamClient:
    """Mock chat_stream that behaves like the substrate layer: collects
    chain-of-thought into reasoning_out, then streams the body."""

    reasoning = _COT_SUMMARY
    body = _BODY_TEXT

    async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
        reasoning_out = kwargs.get("reasoning_out")
        if reasoning_out is not None:
            reasoning_out.append(self.reasoning)
        yield self.body


def _collect(gen_factory):
    async def run():
        return [chunk async for chunk in gen_factory()]

    return asyncio.run(run())


def test_openai_stream_emits_reasoning_content_before_content():
    chunks = _collect(lambda: _openai_stream("m365-copilot", ReasoningStreamClient(), "hi", []))
    body = "".join(chunks)
    assert '"reasoning_content"' in body
    assert "Considering citation strategy" in body
    # reasoning_content delta must precede the first content delta
    assert body.index('"reasoning_content"') < body.index('"content": "## Conclusion')


def test_responses_stream_emits_reasoning_item():
    chunks = _collect(lambda: _responses_stream("m365-copilot", ReasoningStreamClient(), "hi", []))
    body = "".join(chunks)
    assert '"type": "reasoning"' in body
    assert '"type": "response.reasoning_summary_text.delta"' in body
    assert '"type": "response.reasoning_summary_text.done"' in body
    assert '"type": "summary_text"' in body
    # reasoning summary finishes before the MESSAGE item is added (which sits
    # at output_index 1, after the reasoning item at output_index 0)
    assert body.index("response.reasoning_summary_text.done") < body.index('"type": "message"')
    assert '"output_index": 1' in body
    # completed carries the reasoning item before the message
    completed = body[body.index('"type": "response.completed"'):]
    assert completed.index('"type": "reasoning"') < completed.index('"type": "message"')


def test_anthropic_stream_emits_thinking_block_before_text():
    chunks = _collect(lambda: _anthropic_stream("m365-copilot", ReasoningStreamClient(), "hi", []))
    body = "".join(chunks)
    assert '"type": "thinking"' in body
    assert '"type": "thinking_delta"' in body
    # thinking block index 0 fully precedes text block index 1
    assert body.index('"type": "thinking"') < body.index('"type": "text"')
    assert '"index": 0' in body[:body.index('"index": 1')]


def test_openai_stream_plain_tone_has_no_reasoning_chunk():
    class PlainClient:
        async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
            yield "plain reply"

    chunks = _collect(lambda: _openai_stream("m365-copilot", PlainClient(), "hi", []))
    body = "".join(chunks)
    assert '"reasoning_content"' not in body
    assert "plain reply" in body
