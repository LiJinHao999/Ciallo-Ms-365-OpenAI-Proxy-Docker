"""Merely DECLARING a tool must not cost the answer its sources.

A request with a non-empty ``tools`` list takes the tool-capable stream
(`_responses_stream_with_tools` / `_anthropic_stream_with_tools`), whether or
not the model ends up calling anything. Those two paths used to collect no
sources at all, so adding an optional tool to an otherwise identical request
silently dropped every citation -- the answer still cited ``[1]``, and the
client had nowhere to point it.

Event ORDER matters as much as presence: a client builds its content blocks
from the stream, so an annotation has to arrive before the block it belongs to
is closed, and on the same index as the text it annotates.
"""

from __future__ import annotations

import asyncio
import json

from m365_copilot_openai_proxy.response_helpers import _responses_stream_with_tools
from m365_copilot_openai_proxy.routes_api_messages import _anthropic_stream_with_tools

ANSWER = "有雷阵雨[1]"
SOURCES = [{
    "title": "Weather",
    "url": "https://news.weather.com.cn/a",
    "snippet": "雷阵雨",
    "ref_key": "1-aaaaaa",
    "ref_id": "turn1search1",
    "label": "1",
}]


class _CitingClient:
    """Streams an answer that cites ``[1]`` and declares the matching source.

    It never returns a tool call: the point is that the tool was only DECLARED.
    """

    def __init__(self, reply: str = ANSWER):
        self.reply = reply
        self.calls: list[object] = []

    async def chat_stream(self, prompt, additional_context, session=None, images=None, *, sources_out=None):
        self.calls.append(sources_out)
        assert sources_out is not None, "the tool path collected no sources at all"
        sources_out.clear()
        sources_out.extend(dict(source) for source in SOURCES)
        yield self.reply


def _events(gen) -> list[dict]:
    async def run():
        return [chunk async for chunk in gen]

    events = []
    for chunk in asyncio.run(run()):
        for line in chunk.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: "):]))
    return events


# --- OpenAI Responses ------------------------------------------------------

def test_responses_stream_with_declared_tool_annotates_the_text():
    events = _events(_responses_stream_with_tools(
        "m365-copilot",
        _CitingClient(),
        "今天天气",
        [],
        response_tools=[{"type": "function", "name": "get_weather",
                         "parameters": {"type": "object", "properties": {}}}],
        tool_names={"get_weather"},
    ))

    completed = [e for e in events if e["type"] == "response.completed"]
    assert completed, [e["type"] for e in events]
    output = completed[-1]["response"]["output"]
    message = next(item for item in output if item["type"] == "message")
    annotations = message["content"][0]["annotations"]

    assert [a["type"] for a in annotations] == ["url_citation"]
    assert annotations[0]["url"] == "https://news.weather.com.cn/a"
    assert ANSWER[annotations[0]["start_index"]:annotations[0]["end_index"]] == "[1]"


def test_responses_annotation_events_precede_output_text_done():
    """A client assembling the item from the stream needs the annotation before
    the text block it belongs to is declared finished."""
    events = _events(_responses_stream_with_tools(
        "m365-copilot",
        _CitingClient(),
        "今天天气",
        [],
        response_tools=[{"type": "function", "name": "get_weather",
                         "parameters": {"type": "object", "properties": {}}}],
        tool_names={"get_weather"},
    ))

    added = [e for e in events if e["type"] == "response.output_text.annotation.added"]
    done = [e for e in events if e["type"] == "response.output_text.done"]
    assert len(added) == 1 and len(done) == 1
    assert added[0]["annotation"]["url"] == "https://news.weather.com.cn/a"
    assert added[0]["sequence_number"] < done[0]["sequence_number"]

    # Same content block as the text it annotates.
    assert added[0]["output_index"] == done[0]["output_index"]
    assert added[0]["content_index"] == done[0]["content_index"] == 0
    assert added[0]["item_id"] == done[0]["item_id"]


def test_responses_declared_tool_that_is_not_called_still_annotates():
    """The declared tool produced no call, so the output holds only the message
    -- and that message must still carry the citation."""
    events = _events(_responses_stream_with_tools(
        "m365-copilot",
        _CitingClient("没有工具可用的普通回答[1]"),
        "今天天气",
        [],
        response_tools=[{"type": "function", "name": "get_weather",
                         "parameters": {"type": "object", "properties": {}}}],
        tool_names={"get_weather"},
    ))

    output = [e for e in events if e["type"] == "response.completed"][-1]["response"]["output"]
    assert [item["type"] for item in output] == ["message"]
    assert output[0]["content"][0]["annotations"][0]["url"] == "https://news.weather.com.cn/a"


def test_responses_tool_call_output_carries_no_annotations_of_its_own():
    """Annotations belong to the assistant message, not to the function_call
    items that sit beside it."""
    events = _events(_responses_stream_with_tools(
        "m365-copilot",
        _CitingClient('好的。\n\n```tool_call\n{"name":"get_weather",'
                      '"arguments":{"city":"Paris"}}\n```'),
        "今天天气",
        [],
        response_tools=[{"type": "function", "name": "get_weather",
                         "parameters": {"type": "object",
                                        "properties": {"city": {"type": "string"}},
                                        "required": ["city"]}}],
        tool_names={"get_weather"},
    ))

    output = [e for e in events if e["type"] == "response.completed"][-1]["response"]["output"]
    function_calls = [item for item in output if item["type"] == "function_call"]
    assert [call["name"] for call in function_calls] == ["get_weather"]
    assert "annotations" not in json.dumps(function_calls)


# --- Anthropic Messages ----------------------------------------------------

def test_messages_stream_with_declared_tool_emits_citations_delta():
    events = _events(_anthropic_stream_with_tools(
        "m365-copilot",
        _CitingClient(),
        "今天天气",
        [],
        tool_names={"get_weather"},
    ))

    citations = [
        event["delta"]["citation"]
        for event in events
        if event.get("type") == "content_block_delta"
        and event["delta"].get("type") == "citations_delta"
    ]
    assert [c["type"] for c in citations] == ["web_search_result_location"]
    assert citations[0]["url"] == "https://news.weather.com.cn/a"
    assert citations[0]["cited_text"] == "[1]"


def test_messages_citations_delta_precedes_content_block_stop():
    """The text block is still open when the citations arrive, and they have to
    name THAT block -- otherwise the SDK drops them onto nothing."""
    events = _events(_anthropic_stream_with_tools(
        "m365-copilot",
        _CitingClient(),
        "今天天气",
        [],
        tool_names={"get_weather"},
    ))

    types = [event["type"] for event in events]
    citation_index = next(
        index for index, event in enumerate(events)
        if event["type"] == "content_block_delta"
        and event["delta"].get("type") == "citations_delta"
    )
    stop_index = types.index("content_block_stop")
    assert citation_index < stop_index

    text_start = next(
        event for event in events
        if event["type"] == "content_block_start"
        and event["content_block"]["type"] == "text"
    )
    assert events[citation_index]["index"] == text_start["index"]


def test_messages_declared_tool_with_a_tool_use_block_keeps_text_citations():
    """A turn that DOES call a tool still cites: the text block and the tool_use
    block are separate content blocks of one message."""
    events = _events(_anthropic_stream_with_tools(
        "m365-copilot",
        _CitingClient('好的[1]。\n\n```tool_call\n{"name":"get_weather",'
                      '"arguments":{"city":"Paris"}}\n```'),
        "今天天气",
        [],
        tool_names={"get_weather"},
    ))

    starts = [e for e in events if e["type"] == "content_block_start"]
    assert [s["content_block"]["type"] for s in starts] == ["text", "tool_use"]

    text_index = starts[0]["index"]
    citations = [
        event for event in events
        if event["type"] == "content_block_delta"
        and event["delta"].get("type") == "citations_delta"
    ]
    assert [e["index"] for e in citations] == [text_index]
    assert citations[0]["delta"]["citation"]["url"] == "https://news.weather.com.cn/a"
