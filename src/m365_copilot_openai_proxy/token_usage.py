"""Approximate token usage for OpenAI / Anthropic / Responses envelopes.

M365 Copilot's substrate stream does not report prompt/completion token
counts. Clients still expect a non-zero ``usage`` field for billing UIs,
rate-limit displays, and cost estimates. We therefore estimate from the
text we actually send and receive:

- CJK ideographs / kana / hangul / CJK punctuation ≈ 1 token per char
- everything else ≈ 1 token per 4 chars (rough cl100k English rate)

These numbers are intentionally simple and dependency-free — accurate
enough for dashboards, not for precise billing.
"""

from __future__ import annotations

from .substrate_parse import _combine_text

# Rough per-image cost for vision-style multimodal turns (one low-detail tile).
_IMAGE_INPUT_TOKENS = 85


def estimate_tokens(text: str | None) -> int:
    """Return a rough token count for ``text`` (0 for empty/None)."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF  # CJK Unified Ideographs
            or 0x3400 <= o <= 0x4DBF  # CJK Ext A
            or 0xF900 <= o <= 0xFAFF  # CJK Compatibility
            or 0x3040 <= o <= 0x30FF  # Hiragana / Katakana
            or 0xAC00 <= o <= 0xD7AF  # Hangul Syllables
            or 0x3000 <= o <= 0x303F  # CJK punctuation / symbols
        ):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def estimate_prompt_tokens(
    prompt: str,
    additional_context: list[str] | None = None,
    images: list | None = None,
) -> int:
    """Estimate input tokens for the text (and images) sent upstream."""
    combined = _combine_text(prompt or "", list(additional_context or []))
    n = estimate_tokens(combined)
    if images:
        n += _IMAGE_INPUT_TOKENS * len(images)
    return n


def openai_usage(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
    }


def anthropic_usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
    }


def responses_usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": int(input_tokens) + int(output_tokens),
    }


def usage_from_turn(
    prompt: str,
    additional_context: list[str] | None,
    completion_text: str,
    images: list | None = None,
    *,
    style: str = "openai",
) -> dict[str, int]:
    """Build a usage dict for a completed turn in the requested API style."""
    prompt_tokens = estimate_prompt_tokens(prompt, additional_context, images)
    completion_tokens = estimate_tokens(completion_text)
    if style == "anthropic":
        return anthropic_usage(prompt_tokens, completion_tokens)
    if style == "responses":
        return responses_usage(prompt_tokens, completion_tokens)
    return openai_usage(prompt_tokens, completion_tokens)
