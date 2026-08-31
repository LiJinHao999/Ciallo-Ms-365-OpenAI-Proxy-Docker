from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .call_log_store import append_call_log, record_response_text
from .config import Settings
from .models import AnthropicMessagesRequest
from .response_helpers import _anthropic_stream
from .routes_api_common import request_model_alias, resolve_request_tone
from .routes_media_proxy import request_media_rewriter
from .session_helpers import _messages_session_key, _persistent_session
from .substrate_client import SubstrateCopilotClient, SubstrateCopilotError
from .substrate_parse import anthropic_web_citations_from_sources
from .token_usage import usage_from_turn
from .tone_resolver import normalized_session_model
from .translator import translate_anthropic_request


def register_messages_routes(
    app: FastAPI,
    get_settings: Callable[[], Settings],
    get_copilot_client: Callable[[Request], SubstrateCopilotClient],
) -> None:
    @app.post("/v1/messages")
    async def anthropic_messages(
        raw_request: Request,
        request: AnthropicMessagesRequest,
        settings: Settings = Depends(get_settings),
        client: SubstrateCopilotClient = Depends(get_copilot_client),
    ):
        model_alias = request_model_alias(app, raw_request, settings)
        try:
            # The requested model name selects the conversation tone (and its
            # persistent variant); override the client tone and normalize the
            # persist marker for _persistent_session's suffix check.
            resolved_tone, _is_persist = resolve_request_tone(app, request.model)
            client._tone = resolved_tone
            translated = translate_anthropic_request(request)
            session = _persistent_session(app, raw_request, normalized_session_model(request.model), _messages_session_key(request), request)
            media_rewriter = request_media_rewriter(app, raw_request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        call_record = {
            "api": "anthropic",
            "endpoint": "/v1/messages",
            "time": time.strftime("%H:%M:%S"),
            "ts": time.time(),
            "stream": request.stream,
            "tools": [],
            "messages": len(request.messages),
            "model": request.model,
            "tone": resolved_tone,
            "tool_calls_result": None if request.stream else [],
        }

        if request.stream:
            call_record["streaming"] = True
            append_call_log(app.state, call_record)
            return StreamingResponse(
                _anthropic_stream(
                    model_alias,
                    client,
                    translated.prompt,
                    translated.additional_context,
                    session,
                    on_text_done=lambda text: record_response_text(app.state, call_record, text),
                    text_transform=media_rewriter,
                    images=translated.images,
                ),
                media_type="text/event-stream",
            )

        collected_sources: list[dict] = []
        reasoning_out: list[str] = []
        try:
            text = media_rewriter(
                await client.chat(
                    translated.prompt,
                    translated.additional_context,
                    session,
                    translated.images,
                    sources_out=collected_sources,
                    include_sources_markdown=False,
                    reasoning_out=reasoning_out,
                )
            )
        except SubstrateCopilotError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        record_response_text(app.state, call_record, text)
        append_call_log(app.state, call_record)
        citations = anthropic_web_citations_from_sources(text, collected_sources)
        content_block: dict = {"type": "text", "text": text}
        if citations:
            content_block["citations"] = citations
        # Deep-think chain-of-thought becomes a native Anthropic thinking block,
        # which must precede the text block in the content array.
        content: list[dict] = []
        if reasoning_out:
            content.append({"type": "thinking", "thinking": "\n\n".join(reasoning_out)})
        content.append(content_block)

        return JSONResponse({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model_alias,
            "content": content,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage_from_turn(
                translated.prompt,
                translated.additional_context,
                text,
                translated.images,
                style="anthropic",
            ),
        })
