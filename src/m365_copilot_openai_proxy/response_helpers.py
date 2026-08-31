from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable

from fastapi.responses import JSONResponse

from .session_store import PersistentSession
from .substrate_client import SubstrateCopilotClient, SubstrateCopilotError, _dedupe_repeated_delta
from .substrate_parse import (
    anthropic_web_citations_from_sources,
    openai_url_citations_from_sources,
)
from .token_usage import (
    estimate_prompt_tokens,
    estimate_tokens,
    usage_from_turn,
)


def _transform_complete_text(full_text: str, text_transform: Callable[[str], str] | None) -> str:
    return text_transform(full_text) if text_transform is not None else full_text


def _json_err(status: int, message: str, error_type: str = "error") -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": error_type}},
        headers={"Access-Control-Allow-Origin": "*"},
    )


# Media URL rewriting needs the *complete* URL / markdown construct. Streamed
# deltas often split a backtick-wrapped asyncgw URL or a designer image across
# several chunks; if we rewrote each delta independently the pattern would never
# match, and if we rewrote the growing full text on every delta the client would
# already have received the raw prefix (causing duplicated / broken rendering).
# Hold the incomplete tail and emit transformed plain text immediately.
_PARTIAL_URL_SCHEMES = ("https://", "http://")
_BARE_URL_TAIL_RE = re.compile(r"https?://[^\s`)]*$", re.IGNORECASE)
_MEDIA_HOST_TOKENS = (
    "designerapp",
    "officeapps",
    "asyncgw",
    "teams.microsoft",
)


def _looks_like_url_citation_tail(after: str) -> bool:
    """True when text after an opening backtick may be (the start of) a media URL."""
    s = after.lstrip()
    if not s:
        return True
    lower = s.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return True
    for scheme in _PARTIAL_URL_SCHEMES:
        for i in range(1, len(scheme)):
            if lower == scheme[:i]:
                return True
    return False


def _could_be_m365_media_url(url: str) -> bool:
    """True when a bare URL at EOS is (or might still grow into) an M365 media URL."""
    lower = url.lower()
    if any(token in lower for token in _MEDIA_HOST_TOKENS):
        return True
    rest = lower.split("://", 1)[-1]
    host = rest.split("/", 1)[0]
    if not host:
        return True
    # Host still being typed: hold only while it remains a prefix of a known
    # media host token (avoids stalling ordinary links like github.com).
    for token in ("designerapp", "asyncgw", "officeapps", "teams", "microsoft"):
        if token.startswith(host) or host.startswith(token):
            return True
    # Region-prefixed asyncgw hosts: "jp-prod", "kr-prod", ...
    if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]+)*", host) and "." not in host:
        return True
    return False


def _safe_media_emit_end(text: str) -> int:
    """Return the end index of the prefix that is safe to transform and emit.

    The suffix ``text[end:]`` is held until more data arrives or the stream is
    flushed, so ``rewrite_m365_media_urls`` / ``normalize_m365_media_text`` can
    see complete media patterns.
    """
    if not text:
        return 0
    n = len(text)
    hold_from = n

    for scheme in _PARTIAL_URL_SCHEMES:
        for i in range(1, len(scheme)):
            if text.endswith(scheme[:i]):
                hold_from = min(hold_from, n - i)

    last_bt = text.rfind("`")
    if last_bt >= 0 and "`" not in text[last_bt + 1:]:
        if _looks_like_url_citation_tail(text[last_bt + 1:]):
            start = last_bt
            if start >= 2 and text[start - 2:start] == "! ":
                start -= 2
            elif start >= 1 and text[start - 1] == "!":
                start -= 1
            hold_from = min(hold_from, start)

    md_open = text.rfind("](")
    if md_open >= 0 and ")" not in text[md_open + 2:]:
        start = md_open
        label_open = text.rfind("[", 0, md_open)
        if label_open >= 0:
            start = label_open - 1 if label_open >= 1 and text[label_open - 1] == "!" else label_open
        hold_from = min(hold_from, start)

    bare = _BARE_URL_TAIL_RE.search(text)
    if bare is not None and _could_be_m365_media_url(bare.group(0)):
        hold_from = min(hold_from, bare.start())

    return max(0, hold_from)


class StreamingTextTransformer:
    """Apply a full-text transform incrementally across streamed deltas.

    Plain text is emitted immediately (run through ``transform`` per safe
    slice). Incomplete media URL / markdown constructs are held in a buffer
    until they are complete (or ``flush`` at end-of-stream), then transformed
    as a unit so signed proxy rewriting still works for split URLs.
    """

    def __init__(self, transform: Callable[[str], str] | None = None) -> None:
        self._transform = transform
        self._hold = ""
        self.emitted = ""

    def push(self, delta: str) -> str:
        if not delta:
            return ""
        if self._transform is None:
            self.emitted += delta
            return delta
        self._hold += delta
        end = _safe_media_emit_end(self._hold)
        if end <= 0:
            return ""
        piece = self._hold[:end]
        self._hold = self._hold[end:]
        out = self._transform(piece)
        self.emitted += out
        return out

    def flush(self) -> str:
        if self._transform is None or not self._hold:
            return ""
        piece = self._hold
        self._hold = ""
        out = self._transform(piece)
        self.emitted += out
        return out


async def _openai_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    on_text_done: Callable[[str], None] | None = None,
    text_transform: Callable[[str], str] | None = None,
    images: list | None = None,
) -> AsyncIterator[str]:
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first_chunk)}\n\n"
    raw_text = ""
    transformer = StreamingTextTransformer(text_transform)
    reasoning_out: list[str] = []
    reasoning_emitted = False
    try:
        async for delta in client.chat_stream(prompt, additional_context, session, images, reasoning_out=reasoning_out):
            delta = _dedupe_repeated_delta(raw_text, delta)
            if not delta:
                continue
            # Deep-thinking tones collect chain-of-thought summaries before the
            # body starts; emit them as a reasoning_content delta (DeepSeek
            # convention, rendered as "thinking" by OpenWebUI / Cherry Studio /
            # NextChat) BEFORE the first content delta. Late summaries (arriving
            # after the body began) are dropped: reasoning must precede content.
            if not reasoning_emitted:
                reasoning_emitted = True
                if reasoning_out:
                    rchunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_alias,
                        "choices": [{"index": 0, "delta": {"reasoning_content": "\n\n".join(reasoning_out)}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(rchunk)}\n\n"
            raw_text += delta
            out = transformer.push(delta)
            if not out:
                continue
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_alias,
                "choices": [{"index": 0, "delta": {"content": out}, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
    except SubstrateCopilotError as exc:
        yield f"data: {json.dumps({'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield "data: [DONE]\n\n"
        return
    tail = transformer.flush()
    if tail:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [{"index": 0, "delta": {"content": tail}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
    full_text = transformer.emitted
    if on_text_done is not None:
        on_text_done(full_text)
    usage = usage_from_turn(prompt, additional_context, full_text, images, style="openai")
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    # OpenAI stream_options.include_usage shape: empty choices + usage, just
    # before [DONE]. Always emit so clients that only look here still see counts
    # (upstream M365 never reports real token usage).
    usage_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [],
        "usage": usage,
    }
    yield f"data: {json.dumps(usage_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    on_text_done: Callable[[str], None] | None = None,
    text_transform: Callable[[str], str] | None = None,
    response_id: str | None = None,
    images: list | None = None,
) -> AsyncIterator[str]:
    resp_id = response_id or f"resp_{uuid.uuid4().hex}"
    item_id = f"msg_{uuid.uuid4().hex}"
    created = int(time.time())

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'in_progress', 'output': []}})}\n\n"

    raw_text = ""
    transformer = StreamingTextTransformer(text_transform)
    collected_sources: list[dict] = []
    reasoning_out: list[str] = []
    reasoning_item: dict | None = None
    # output_index of the message item: 1 when a reasoning item precedes it.
    msg_index = 0
    started = False
    try:
        async for delta in client.chat_stream(
            prompt,
            additional_context,
            session,
            images,
            sources_out=collected_sources,
            include_sources_markdown=False,
            reasoning_out=reasoning_out,
        ):
            delta = _dedupe_repeated_delta(raw_text, delta)
            if not delta:
                continue
            if not started:
                started = True
                if reasoning_out:
                    rs_id = f"rs_{uuid.uuid4().hex}"
                    text = "\n\n".join(reasoning_out)
                    reasoning_item = {"id": rs_id, "type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}
                    yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': rs_id, 'type': 'reasoning', 'summary': []}})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.reasoning_summary_text.delta', 'item_id': rs_id, 'output_index': 0, 'content_index': 0, 'delta': text})}\n\n"
                    yield f"data: {json.dumps({'type': 'response.reasoning_summary_text.done', 'item_id': rs_id, 'output_index': 0, 'content_index': 0, 'summary': [{'type': 'summary_text', 'text': text}]})}\n\n"
                    msg_index = 1
                yield f"data: {json.dumps({'type': 'response.output_item.added', 'output_index': msg_index, 'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'content': []}})}\n\n"
                yield f"data: {json.dumps({'type': 'response.content_part.added', 'item_id': item_id, 'output_index': msg_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"
            raw_text += delta
            out = transformer.push(delta)
            if not out:
                continue
            yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': msg_index, 'content_index': 0, 'delta': out})}\n\n"
    except SubstrateCopilotError as exc:
        # Emit both the out-of-band `error` event (kept for existing clients) and
        # the semantic `response.failed` envelope. Responses API does NOT use a
        # `[DONE]` sentinel; `response.failed` is the terminal event that lets
        # strict clients stop cleanly instead of hanging for more deltas.
        yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc), 'type': 'upstream_error'}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.failed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'failed', 'error': {'message': str(exc), 'code': 'upstream_error'}}})}\n\n"
        return

    tail = transformer.flush()
    if tail:
        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': item_id, 'output_index': msg_index, 'content_index': 0, 'delta': tail})}\n\n"
    full_text = transformer.emitted
    if on_text_done is not None:
        on_text_done(full_text)
    annotations = openai_url_citations_from_sources(full_text, collected_sources)
    for annotation_index, annotation in enumerate(annotations):
        yield f"data: {json.dumps({'type': 'response.output_text.annotation.added', 'item_id': item_id, 'output_index': msg_index, 'content_index': 0, 'annotation_index': annotation_index, 'annotation': annotation})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_text.done', 'item_id': item_id, 'output_index': msg_index, 'content_index': 0, 'text': full_text})}\n\n"
    usage = usage_from_turn(prompt, additional_context, full_text, images, style="responses")
    output_items = ([reasoning_item] if reasoning_item else []) + [{'id': item_id, 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': full_text, 'annotations': annotations}]}]
    yield f"data: {json.dumps({'type': 'response.completed', 'response': {'id': resp_id, 'object': 'response', 'created_at': created, 'model': model_alias, 'status': 'completed', 'output': output_items, 'usage': usage}})}\n\n"


async def _anthropic_stream(
    model_alias: str,
    client: SubstrateCopilotClient,
    prompt: str,
    additional_context: list[str],
    session: PersistentSession | None = None,
    on_text_done: Callable[[str], None] | None = None,
    text_transform: Callable[[str], str] | None = None,
    images: list | None = None,
) -> AsyncIterator[str]:
    msg_id = f"msg_{uuid.uuid4().hex}"

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    input_tokens = estimate_prompt_tokens(prompt, additional_context, images)
    yield sse("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "content": [], "model": model_alias, "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})
    yield sse("ping", {"type": "ping"})

    raw_text = ""
    transformer = StreamingTextTransformer(text_transform)
    collected_sources: list[dict] = []
    reasoning_out: list[str] = []
    # Anthropic protocol: a thinking block must fully precede the text block, so
    # the text block is opened only when the first body delta arrives (chain-of-
    # thought summaries have all been collected by then).
    text_index = 0
    started = False
    try:
        async for delta in client.chat_stream(
            prompt,
            additional_context,
            session,
            images,
            sources_out=collected_sources,
            include_sources_markdown=False,
            reasoning_out=reasoning_out,
        ):
            delta = _dedupe_repeated_delta(raw_text, delta)
            if not delta:
                continue
            if not started:
                started = True
                if reasoning_out:
                    yield sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
                    yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "\n\n".join(reasoning_out)}})
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                    text_index = 1
                yield sse("content_block_start", {"type": "content_block_start", "index": text_index, "content_block": {"type": "text", "text": ""}})
            raw_text += delta
            out = transformer.push(delta)
            if not out:
                continue
            yield sse("content_block_delta", {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": out}})
    except SubstrateCopilotError as exc:
        yield sse("error", {"type": "error", "error": {"type": "upstream_error", "message": str(exc)}})
        return

    tail = transformer.flush()
    if tail:
        yield sse("content_block_delta", {"type": "content_block_delta", "index": text_index, "delta": {"type": "text_delta", "text": tail}})
    full_text = transformer.emitted
    if on_text_done is not None:
        on_text_done(full_text)
    for citation in anthropic_web_citations_from_sources(full_text, collected_sources):
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": text_index,
                "delta": {"type": "citations_delta", "citation": citation},
            },
        )
    yield sse("content_block_stop", {"type": "content_block_stop", "index": text_index})
    output_tokens = estimate_tokens(full_text)
    yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": output_tokens}})
    yield sse("message_stop", {"type": "message_stop"})
