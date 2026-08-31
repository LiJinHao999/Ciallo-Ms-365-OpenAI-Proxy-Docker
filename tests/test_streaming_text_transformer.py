from __future__ import annotations

import asyncio
import json

from m365_copilot_openai_proxy.media_proxy import rewrite_m365_media_urls
from m365_copilot_openai_proxy.response_helpers import (
    StreamingTextTransformer,
    _openai_stream,
    _safe_media_emit_end,
)


SOURCE_IMAGE = (
    "https://designerapp.officeapps.live.com/designerapp/document.ashx"
    "?path=%2Fgenerated.png&fileToken=abc"
)
SOURCE_AUDIO = (
    "https://jp-prod.asyncgw.teams.microsoft.com/v1/objects/"
    "0-ea-d4-101412848fe8be7ad7f1c4c110d1fa4f/views/original/cat_meow.wav"
)


def _rewriter(text: str) -> str:
    return rewrite_m365_media_urls(
        text,
        base_url="http://proxy.example",
        account_id="acct_1",
        secret="secret",
        now=1000,
    )


def test_safe_emit_holds_incomplete_backtick_url():
    text = "已生成：\n\n `https://jp-prod.asyncgw.teams.microsoft.com/v1/objects/"
    assert _safe_media_emit_end(text) == text.index("`")


def test_safe_emit_releases_completed_backtick_url():
    text = f"已生成：\n\n `{SOURCE_AUDIO}` \n\n收工。"
    # After the closing backtick the remainder (space/prose) is safe; the
    # complete citation itself is also safe because the pair is closed.
    assert _safe_media_emit_end(text) == len(text)


def test_safe_emit_holds_partial_https_prefix():
    assert _safe_media_emit_end("see htt") < len("see htt")
    assert _safe_media_emit_end("see https") < len("see https")
    assert _safe_media_emit_end("see https://") < len("see https://")


def test_safe_emit_does_not_hold_ordinary_host_forever():
    # github.com is not an M365 media host; bare-URL holdback must not stall it.
    text = "see https://github.com/foo/bar"
    assert _safe_media_emit_end(text) == len(text)


def test_transformer_streams_plain_text_immediately():
    t = StreamingTextTransformer(_rewriter)
    assert t.push("你好，这是一段普通回答。") == "你好，这是一段普通回答。"
    assert t.flush() == ""
    assert t.emitted == "你好，这是一段普通回答。"


def test_transformer_holds_split_asyncgw_url_until_complete():
    t = StreamingTextTransformer(_rewriter)
    parts = [
        "已生成文件：\n\n `https://jp-prod.asyncgw.teams.microsoft.com/v1/objects/",
        "0-ea-d4-101412848fe8be7ad7f1c4c110d1fa4f/views/original/",
        "cat_meow.wav` \n\n（这是一个合成的“喵”声音频，可直接下载播放。）",
    ]
    outs = [t.push(p) for p in parts]
    # First two pieces leave an open backtick citation → held, no emit.
    assert outs[0] == "已生成文件：\n\n "
    assert outs[1] == ""
    # Third piece closes the citation; media is rewritten and trailing prose emitted.
    combined_tail = outs[2] + t.flush()
    assert "/v1/m365-media?" in combined_tail
    assert "asyncgw.teams.microsoft.com" not in combined_tail
    assert "下载 cat_meow.wav" in combined_tail or "cat_meow.wav" in combined_tail
    assert "可直接下载播放" in t.emitted
    assert "asyncgw.teams.microsoft.com" not in t.emitted


def test_transformer_rewrites_designer_image_markdown():
    t = StreamingTextTransformer(_rewriter)
    out = t.push(f"![image]({SOURCE_IMAGE})") + t.flush()
    assert out.startswith("![image](http://proxy.example/v1/m365-media?")
    assert "designerapp.officeapps.live.com" not in out


def test_transformer_without_transform_is_passthrough():
    t = StreamingTextTransformer(None)
    assert t.push("abc") == "abc"
    assert t.push("def") == "def"
    assert t.flush() == ""
    assert t.emitted == "abcdef"


def _collect(gen_factory):
    async def run():
        return [chunk async for chunk in gen_factory()]

    return asyncio.run(run())


def _openai_content_deltas(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            continue
        obj = json.loads(payload)
        for choice in obj.get("choices", []):
            piece = choice.get("delta", {}).get("content")
            if piece:
                out.append(piece)
    return out


def test_openai_stream_emits_incremental_deltas_with_text_transform():
    """Regression: text_transform used to buffer the entire answer and emit
    once at the end, so clients saw no streaming. With the incremental
    transformer, plain-text prefixes must appear as separate content deltas
    before the stream finishes.
    """

    class Client:
        async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
            for d in ["第一句。", "第二句。", "第三句。"]:
                yield d

    body = "".join(
        _collect(
            lambda: _openai_stream(
                "m365-copilot",
                Client(),
                "hi",
                [],
                text_transform=_rewriter,
            )
        )
    )
    deltas = _openai_content_deltas(body)
    assert len(deltas) >= 3
    assert "".join(deltas) == "第一句。第二句。第三句。"


def test_openai_stream_rewrites_split_media_url_incrementally():
    class Client:
        async def chat_stream(self, prompt, additional_context, session=None, images=None, **kwargs):
            yield "前缀文字。\n\n `"
            yield SOURCE_AUDIO
            yield "` \n\n后缀。"

    body = "".join(
        _collect(
            lambda: _openai_stream(
                "m365-copilot",
                Client(),
                "hi",
                [],
                text_transform=_rewriter,
            )
        )
    )
    deltas = _openai_content_deltas(body)
    # Prefix must stream before the media citation closes.
    assert any("前缀文字" in d for d in deltas[:-1]) or (
        len(deltas) >= 2 and "前缀文字" in deltas[0]
    )
    full = "".join(deltas)
    assert "/v1/m365-media?" in full
    assert "asyncgw.teams.microsoft.com" not in full
    assert "后缀。" in full
