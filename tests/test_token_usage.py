from __future__ import annotations

import asyncio
import json

from m365_copilot_openai_proxy.response_helpers import (
    _anthropic_stream,
    _openai_stream,
    _responses_stream,
)
from m365_copilot_openai_proxy.token_usage import (
    estimate_prompt_tokens,
    estimate_tokens,
    usage_from_turn,
)


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_tokens_english_rough_cl100k():
    # ~1 token per 4 latin chars
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("hello world") == 3  # 11 chars -> ceil(11/4)=3


def test_estimate_tokens_cjk_one_per_char():
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("世界") == 2
    # mixed: 2 CJK + 4 latin (=1) = 3
    assert estimate_tokens("你好abcd") == 3


def test_estimate_prompt_tokens_includes_context_and_images():
    n = estimate_prompt_tokens("hi", ["ctx1", "ctx2"])
    assert n > estimate_tokens("hi")
    with_img = estimate_prompt_tokens("hi", [], images=[{}, {}])
    assert with_img == estimate_tokens("hi") + 85 * 2


def test_usage_from_turn_styles():
    oai = usage_from_turn("hello", [], "world reply", style="openai")
    assert oai["prompt_tokens"] > 0
    assert oai["completion_tokens"] > 0
    assert oai["total_tokens"] == oai["prompt_tokens"] + oai["completion_tokens"]

    ant = usage_from_turn("hello", [], "world reply", style="anthropic")
    assert ant["input_tokens"] > 0
    assert ant["output_tokens"] > 0
    assert "total_tokens" not in ant

    resp = usage_from_turn("hello", [], "world reply", style="responses")
    assert resp["input_tokens"] > 0
    assert resp["output_tokens"] > 0
    assert resp["total_tokens"] == resp["input_tokens"] + resp["output_tokens"]


def _collect(gen_factory):
    async def run():
        return [chunk async for chunk in gen_factory()]

    return asyncio.run(run())


class _OkClient:
    async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
        yield "你好世界"


def test_openai_stream_emits_usage_chunk_before_done():
    body = "".join(_collect(lambda: _openai_stream("m365-copilot", _OkClient(), "hi there", [])))
    lines = [ln for ln in body.splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    usage_payload = json.loads(lines[-2][len("data: "):])
    assert usage_payload["choices"] == []
    usage = usage_payload["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0  # 你好世界 = 4 CJK
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_responses_stream_completed_has_nonzero_usage():
    body = "".join(_collect(lambda: _responses_stream("m365-copilot", _OkClient(), "hi there", [])))
    completed = None
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        obj = json.loads(line[len("data: "):])
        if obj.get("type") == "response.completed":
            completed = obj
    assert completed is not None
    usage = completed["response"]["usage"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] == 4
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


def test_anthropic_stream_emits_input_and_output_tokens():
    body = "".join(_collect(lambda: _anthropic_stream("m365-copilot", _OkClient(), "hi there", [])))
    start_usage = None
    delta_usage = None
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        obj = json.loads(line[len("data: "):])
        if obj.get("type") == "message_start":
            start_usage = obj["message"]["usage"]
        if obj.get("type") == "message_delta":
            delta_usage = obj["usage"]
    assert start_usage is not None and start_usage["input_tokens"] > 0
    assert start_usage["output_tokens"] == 0
    assert delta_usage is not None and delta_usage["output_tokens"] == 4
