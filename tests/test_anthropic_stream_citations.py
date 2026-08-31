from __future__ import annotations

import asyncio
import json

from m365_copilot_openai_proxy.response_helpers import _anthropic_stream


def _collect(gen_factory):
    async def run():
        return [chunk async for chunk in gen_factory()]

    return asyncio.run(run())


def test_anthropic_stream_emits_citations_delta_events():
    class CitingStreamClient:
        async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
            sources_out = kwargs.get("sources_out")
            if sources_out is not None:
                sources_out.clear()
                sources_out.append({
                    "title": "Weather",
                    "url": "https://news.weather.com.cn/a",
                    "snippet": "雷阵雨",
                })
            assert kwargs.get("include_sources_markdown") is False
            yield "有雷阵雨[1]"

    chunks = _collect(
        lambda: _anthropic_stream("m365-copilot", CitingStreamClient(), "hi", [])
    )
    body = "".join(chunks)
    assert "event: content_block_delta" in body
    assert '"type": "citations_delta"' in body
    assert '"type": "web_search_result_location"' in body
    assert "https://news.weather.com.cn/a" in body
    assert "### 参考来源" not in body

    # citations_delta arrives before content_block_stop
    assert body.index('"type": "citations_delta"') < body.index('"type": "content_block_stop"')
