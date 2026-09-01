from __future__ import annotations

import json

from m365_copilot_openai_proxy.substrate_parse import (
    _combine_text,
    _dedupe_repeated_delta,
    _dedupe_signature,
    _extract_image_urls,
    _image_markdown,
    _is_image_loading_placeholder,
    _message_content,
    _final_fallback_remainder,
    clean_m365_citations,
)


# --- _combine_text ---------------------------------------------------------

def test_combine_text_returns_prompt_when_no_context():
    assert _combine_text("just the prompt", []) == "just the prompt"


def test_combine_text_joins_context_before_prompt():
    result = _combine_text("PROMPT", ["ctx1", "ctx2"])
    assert result == "ctx1\n\nctx2\n\n---\n\nPROMPT"


def test_combine_text_appends_format_hint_when_context_mentions_tool_call():
    result = _combine_text("PROMPT", ["please emit a tool_call block"])
    assert "PROMPT" in result
    assert "[FORMAT]" in result
    assert "tool_call" in result


def test_combine_text_no_format_hint_without_tool_call_context():
    result = _combine_text("PROMPT", ["ordinary context"])
    assert "[FORMAT]" not in result


# --- _is_image_loading_placeholder / _image_markdown -----------------------

def test_is_image_loading_placeholder_case_insensitive():
    assert _is_image_loading_placeholder("Loading image") is True
    assert _is_image_loading_placeholder("  loading image  ") is True


def test_is_image_loading_placeholder_rejects_other_text():
    assert _is_image_loading_placeholder("here is your image") is False


def test_image_markdown_format():
    assert _image_markdown("https://x/a.png") == "![image](https://x/a.png)"


# --- _extract_image_urls ---------------------------------------------------

def test_extract_image_urls_requires_image_context():
    # A bare url with no image context must NOT be extracted.
    assert _extract_image_urls({"url": "https://x/a.png"}) == []


def test_extract_image_urls_from_typed_image_node():
    node = {"type": "image", "url": "https://x/a.png"}
    assert _extract_image_urls(node) == ["https://x/a.png"]


def test_extract_image_urls_from_image_keyed_container():
    # The "image" key itself establishes image context for its children.
    node = {"image": {"url": "https://x/a.png"}}
    assert _extract_image_urls(node) == ["https://x/a.png"]


def test_extract_image_urls_skips_non_http_and_dedupes():
    node = {
        "attachments": [
            {"type": "image", "url": "https://x/a.png"},
            {"type": "image", "url": "https://x/a.png"},
            {"type": "image", "url": "data:image/png;base64,zzz"},
        ]
    }
    assert _extract_image_urls(node) == ["https://x/a.png"]


def test_extract_image_urls_strips_backticks():
    node = {"type": "image", "url": "`https://x/a.png`"}
    assert _extract_image_urls(node) == ["https://x/a.png"]


# --- _final_fallback_remainder ---------------------------------------------

def test_final_fallback_empty_fallback_returns_empty():
    assert _final_fallback_remainder("streamed", "") == ""


def test_final_fallback_empty_streamed_returns_full_fallback():
    assert _final_fallback_remainder("", "full fallback") == "full fallback"


def test_final_fallback_returns_suffix_when_fallback_extends_streamed():
    assert _final_fallback_remainder("Hello", "Hello world") == " world"


def test_final_fallback_empty_when_fallback_already_streamed():
    assert _final_fallback_remainder("Hello world extra", "Hello world") == ""


def test_final_fallback_empty_when_signatures_match_despite_link_noise():
    # streamed carries a backtick URL that dedupe strips; fallback is the same
    # prose without it. Neither startswith/contains the other, so this exercises
    # the signature-match branch (not the prefix branch): nothing extra emitted.
    streamed = "See the docs `https://x/a` now"
    fallback = "See the docs now"
    assert _final_fallback_remainder(streamed, fallback) == ""


def test_final_fallback_drops_near_duplicate_with_inline_cite_markup_diff():
    # Production regression (2026-08-05 /v1/responses): stream and type=2 final
    # were the same essay except for cite presentation \u2014
    #   streamed:  \u7c7b\u4f3c `[[1]](url)` \u7684\u6765\u6e90\u94fe\u63a5   (no [n] yet / different form)
    #   fallback:  \u7c7b\u4f3c `url` \u7684\u6765\u6e90\u94fe\u63a5[2]
    # Old signature only stripped http(s) backticks + markdown http links, so the
    # residual differed and the whole final was re-appended \u2192 visible duplication.
    body = (
        "\u5982\u679c\u4f60\u8bf4\u7684\u662f grok2api \u91cc\u7684 Grok \u6a21\u578b\u5f00\u542f\u8054\u7f51\u641c\u7d22\u540e\uff0c\u56de\u7b54\u91cc\u6ca1\u6709\u663e\u793a\u4fe1\u6e90\uff0c"
        "\u90a3\u8981\u5206\u51e0\u79cd\u60c5\u51b5\u3002xAI \u5b98\u65b9\u7684 Grok \u641c\u7d22\u80fd\u529b\u5b9e\u9645\u4e0a\u662f\u652f\u6301\u8fd4\u56de\u5f15\u7528\u6765\u6e90\u7684\u3002"
        "\u5b98\u65b9\u6587\u6863\u8bf4\u660e Web Search \u5de5\u5177\u6267\u884c\u540e\u4f1a\u8fd4\u56de citations \u5b57\u6bb5\u3002"
        "Responses API \u652f\u6301\u5185\u8054\u5f15\u7528\uff0c\u4f1a\u5728\u56de\u7b54\u4e2d\u63d2\u5165\u7c7b\u4f3c {cite} \u7684\u6765\u6e90\u94fe\u63a5\u3002"
        "\u5373\u4f7f\u4e0d\u5f00\u542f\u5185\u8054\u5f15\u7528\uff0c\u54cd\u5e94\u5bf9\u8c61\u91cc\u901a\u5e38\u4ecd\u6709\u7ed3\u6784\u5316\u7684 citations \u6570\u636e\u3002"
        "\u56e0\u6b64\u5982\u679c\u4f60\u5728 Grok \u5b98\u7f51\u80fd\u770b\u5230\u6765\u6e90\uff0c\u800c\u901a\u8fc7 grok2api \u770b\u4e0d\u5230\uff0c\u95ee\u9898\u5927\u6982\u7387"
        "\u4e0d\u5728\u6a21\u578b\uff0c\u800c\u5728 API \u4e2d\u8f6c\u5c42\u3002\u5f88\u591a OpenAI \u517c\u5bb9\u63a5\u53e3\u53ea\u8fd4\u56de choices.message.content\uff0c"
        "\u4f46\u628a\u539f\u59cb\u54cd\u5e94\u4e2d\u7684 citations \u548c annotations \u8fc7\u6ee4\u6389\u4e86\u3002\u4f60\u53ef\u4ee5\u76f4\u63a5\u67e5\u770b\u5b8c\u6574 JSON\u3002"
        "\u5982\u679c\u63a5\u53e3\u54cd\u5e94\u6839\u672c\u6ca1\u6709\u8fd9\u4e9b\u5b57\u6bb5\uff0c\u90a3\u4e48\u57fa\u672c\u8bf4\u660e\u805a\u5408\u5e73\u53f0\u6ca1\u505a\u652f\u6301\u3002"
    )
    streamed = body.format(cite="`[[1]](url)`")
    fallback = body.format(cite="`url`") + "[2]"
    assert _final_fallback_remainder(streamed, fallback) == ""


# --- _dedupe_repeated_delta ------------------------------------------------

def test_dedupe_repeated_delta_keeps_incremental_repeated_tokens():
    # Math/code stream repeated short tokens across deltas; none may be dropped
    # even though the later token already appeared in the accumulated stream.
    streamed = "2a_1 + 3d = 6\n"
    assert _dedupe_repeated_delta(streamed, "2a_1") == "2a_1"
    assert _dedupe_repeated_delta(streamed, " + 7d = 10\n") == " + 7d = 10\n"


def test_dedupe_repeated_delta_keeps_first_delta_when_nothing_streamed():
    assert _dedupe_repeated_delta("", "hello") == "hello"


def test_dedupe_repeated_delta_drops_full_reemission_with_link_variant():
    # The model restates the ENTIRE answer, swapping a raw backtick URL for a
    # citation link. Signatures match (URL/citation noise stripped), so the
    # whole re-emission is dropped rather than duplicated.
    streamed = "See the report `https://x/a` here"
    reemit = "See the report [link](\ue200cite\ue202turn1file1\ue201) here"
    assert _dedupe_repeated_delta(streamed, reemit) == ""


# --- _dedupe_signature -----------------------------------------------------

def test_dedupe_signature_strips_urls_links_and_whitespace():
    assert _dedupe_signature("a `https://x/a` b") == "ab"
    assert _dedupe_signature("a [t](https://x/a) b") == "ab"
    # Footnote-shaped markdown inside backticks is not a URL; signatures may keep it.


# --- clean_m365_citations --------------------------------------------------

def test_clean_m365_citations_resolves_markdown_cite_links_to_footnotes():
    from m365_copilot_openai_proxy.substrate_parse import CitationTracker

    raw = '已生成音频：\n\n🎧 [流水声](\ue200cite\ue202turn1file1\ue201)\n\n可下载。'
    cleaned = clean_m365_citations(raw, CitationTracker())
    assert "\ue200" not in cleaned
    assert "流水声" in cleaned
    assert "[1]" in cleaned
    assert "已生成音频" in cleaned
    assert "可下载" in cleaned


def test_clean_m365_citations_resolves_bare_pua_cite_runs():
    from m365_copilot_openai_proxy.substrate_parse import CitationTracker

    raw = '见参考 \ue200cite\ue202turn2file0\ue201 结束'
    cleaned = clean_m365_citations(raw, CitationTracker())
    assert "\ue200" not in cleaned
    assert "见参考" in cleaned
    assert "结束" in cleaned
    assert "[1]" in cleaned


def test_clean_m365_citations_leaves_normal_text_and_urls():
    raw = "See [docs](https://example.com) and `https://x/a`"
    assert clean_m365_citations(raw) == raw


def test_clean_m365_citations_tracker_keeps_stable_numbering():
    from m365_copilot_openai_proxy.substrate_parse import CitationTracker

    tracker = CitationTracker()
    first = clean_m365_citations('A [one](\ue200cite\ue202turn1search0\ue201) B', tracker)
    second = clean_m365_citations('C \ue200cite\ue202turn1search1\ue201 D', tracker)
    third = clean_m365_citations('again [one](\ue200cite\ue202turn1search0\ue201)', tracker)
    assert first == "A one[1] B"
    assert second == "C [2] D"
    assert third == "again one[1]"
    assert tracker.count == 2


def test_clean_m365_citations_holds_split_pua_opener():
    from m365_copilot_openai_proxy.substrate_parse import CitationTracker

    tracker = CitationTracker()
    assert clean_m365_citations('基本是对的。\ue200cite\ue202', tracker) == "基本是对的。"
    assert tracker._hold  # opener held
    assert clean_m365_citations('turn1search3\ue201 后面', tracker) == "[1] 后面"
    assert "turn1search" not in clean_m365_citations("x", tracker) + tracker.flush()
    assert "\ue201" not in clean_m365_citations("", tracker) + tracker.flush()


def test_clean_m365_citations_collapses_footnote_plus_turn_residual():
    cleaned = clean_m365_citations('基本是对的。[1]turn1search3\ue201\n单独处理。[2]turn1search6\ue201\n【1-1fd57a】结尾')
    assert "turn1search" not in cleaned
    assert "\ue201" not in cleaned
    assert "【" not in cleaned
    assert "[1]" in cleaned
    assert "[2]" in cleaned


def test_clean_m365_citations_resolves_cn_bracket_ids():
    from m365_copilot_openai_proxy.substrate_parse import CitationTracker

    cleaned = clean_m365_citations('处理。【2-ba0dbb】【1-1fd57a】', CitationTracker())
    assert "【" not in cleaned
    assert "[1]" in cleaned or "[2]" in cleaned
    assert "ba0dbb" not in cleaned
    assert "1fd57a" not in cleaned


def test_extract_and_format_source_attributions():
    from m365_copilot_openai_proxy.substrate_parse import (
        extract_source_attributions,
        sources_markdown_for_stream,
    )

    payload = {
        "item": {
            "messages": [
                {
                    "author": "bot",
                    "sourceAttributions": [
                        {
                            "providerDisplayName": "Re: Team event",
                            "seeMoreUrl": "https://outlook.office365.com/owa/?ItemID=abc",
                            "referenceMetadata": '{"type":"Outlook","snippet":"Regarding the company guidelines"}',
                        },
                        {
                            "providerDisplayName": "Workable policies",
                            "seeMoreUrl": "https://resources.workable.com/tutorial",
                            "referenceMetadata": '{"refType":"Web"}',
                            "searchQuery": "company guidelines",
                        },
                    ],
                }
            ]
        }
    }
    sources = extract_source_attributions(payload)
    assert len(sources) == 2
    assert sources[0]["type"] == "Outlook"
    assert sources[1]["url"].startswith("https://resources.workable.com")
    body = "见公司政策 one[1] 与 web[2]"
    block = sources_markdown_for_stream(body, sources)
    assert "### 参考来源" in block
    assert "[Re: Team event](https://outlook.office365.com/owa/?ItemID=abc)" in block
    assert "（Outlook）" in block
    assert "Workable policies" in block


def test_extract_modern_references_map_to_real_urls():
    from m365_copilot_openai_proxy.substrate_parse import (
        CitationTracker,
        clean_m365_citations,
        extract_source_attributions,
        sources_markdown_for_stream,
    )

    payload = {
        "type": 1,
        "target": "update",
        "arguments": [{
            "messages": [{
                "author": "bot",
                "text": "有雷阵雨。【1-1e0595】气温高。【2-0f56d3】",
                "sourceAttributions": [],
                "references": {
                    "1-1e0595": {
                        "targetLink": "https://news.weather.com.cn/2026/08/4747610.shtml",
                        "displayData": {
                            "type": "text/json",
                            "renderType": "CITATION",
                            "content": json.dumps({
                                "metadata": {
                                    "type": "Web",
                                    "referenceId": "turn1search10",
                                    "citationRefId": "turn1search10",
                                },
                                "label": "1",
                                "providerDisplayName": "未来三天北京闷热",
                                "Title": "未来三天北京闷热",
                                "snippet": "午后有分散性雷阵雨",
                            }, ensure_ascii=False),
                        },
                        "isCitedInResponse": True,
                    },
                    "2-0f56d3": {
                        "targetLink": "https://news.weather.com.cn/2026/08/4747797.shtml",
                        "displayData": {
                            "content": json.dumps({
                                "metadata": {"type": "Web", "referenceId": "turn1search3"},
                                "label": "2",
                                "providerDisplayName": "高温黄色预警",
                                "Title": "高温黄色预警",
                            }, ensure_ascii=False),
                        },
                    },
                },
            }]
        }],
    }
    sources = extract_source_attributions(payload)
    assert len(sources) == 2
    assert sources[0]["url"].startswith("https://news.weather.com.cn/")
    assert sources[0]["ref_key"] == "1-1e0595"
    assert sources[0]["ref_id"] == "turn1search10"

    tracker = CitationTracker()
    body = clean_m365_citations("有雷阵雨。【1-1e0595】气温高。【2-0f56d3】", tracker)
    assert "【" not in body
    assert "[1]" in body and "[2]" in body
    block = sources_markdown_for_stream(body, sources, tracker)
    assert "### 参考来源" in block
    assert "https://news.weather.com.cn/2026/08/4747610.shtml" in block
    assert "未来三天北京闷热" in block
    assert "bracket" not in block


def test_sources_markdown_silent_without_real_urls():
    from m365_copilot_openai_proxy.substrate_parse import (
        CitationTracker,
        sources_markdown_for_stream,
        clean_m365_citations,
    )

    tracker = CitationTracker()
    body = clean_m365_citations("见参考 citeturn2file0 结束", tracker)
    assert sources_markdown_for_stream(body, [], tracker) == ""


def test_openai_and_anthropic_native_citations_from_sources():
    from m365_copilot_openai_proxy.substrate_parse import (
        anthropic_web_citations_from_sources,
        openai_url_citations_from_sources,
    )

    text = "有雷阵雨[1] 气温高[2]"
    sources = [
        {"title": "未来三天北京闷热", "url": "https://news.weather.com.cn/a"},
        {"title": "高温黄色预警", "url": "https://news.weather.com.cn/b", "snippet": "午后有雷阵雨"},
    ]
    openai_anns = openai_url_citations_from_sources(text, sources)
    assert len(openai_anns) == 2
    assert openai_anns[0] == {
        "type": "url_citation",
        "start_index": text.index("[1]"),
        "end_index": text.index("[1]") + len("[1]"),
        "url": "https://news.weather.com.cn/a",
        "title": "未来三天北京闷热",
    }
    assert openai_anns[1]["start_index"] == text.index("[2]")
    assert openai_anns[1]["url"] == "https://news.weather.com.cn/b"

    anthropic_cites = anthropic_web_citations_from_sources(text, sources)
    assert len(anthropic_cites) == 2
    assert anthropic_cites[0]["type"] == "web_search_result_location"
    assert anthropic_cites[0]["url"] == "https://news.weather.com.cn/a"
    assert anthropic_cites[0]["cited_text"] == "[1]"
    assert anthropic_cites[0]["encrypted_index"] == "m365_1"
    assert anthropic_cites[1]["cited_text"] == "[2]"


def test_message_content_resolves_citations_from_text():
    entry = {"text": "音频 [x](citeturn1file1) 完成"}
    content = _message_content(entry)
    assert "" not in content
    assert "完成" in content
    assert "turn1file1" not in content

# One marker can carry SEVERAL ids, separated by the same private-use character
# that opens the id run. Every case above has exactly one id, which is how the
# pattern below shipped for weeks only ever consuming the first: production
# delivered "已有更高版本的 Node.js 也可能直接触发这个错误。turn4search10turn4search12"
# to a client, the ids butted straight against the prose.

def test_clean_m365_citations_strips_every_id_in_a_multi_id_marker():
    raw = (
        "已有更高版本也可能触发这个错误。"
        "citeturn4search10turn4search12"
        "\n\n先检查是否已安装。"
    )
    cleaned = clean_m365_citations(raw)
    assert "turn4search10" not in cleaned
    assert "turn4search12" not in cleaned
    assert not any("" <= c <= "" for c in cleaned)
    assert "已有更高版本也可能触发这个错误。" in cleaned
    assert "先检查是否已安装。" in cleaned


def test_clean_m365_citations_strips_a_three_id_marker():
    """The id run repeats without bound, so two ids cannot be the whole rule."""
    raw = (
        "没有可靠来源确认这项联动。"
        "citeturn2search15turn2search20turn2search21"
        " 初步判断如下。"
    )
    cleaned = clean_m365_citations(raw)
    for marker in ("turn2search15", "turn2search20", "turn2search21"):
        assert marker not in cleaned
    assert not any("" <= c <= "" for c in cleaned)
    assert "没有可靠来源确认这项联动。" in cleaned
    assert "初步判断如下。" in cleaned


# Upstream splits its stream wherever it likes, including inside a marker, and
# the streaming path cleans each delta on its own (substrate_client feeds every
# writeAtCursor straight in), so the two halves are never seen together and each
# has to be handled alone. Production leaked "优先选择 LTS。turn3search5" exactly
# this way: the opening half was stripped by itself, then nothing matched the rest.

def test_clean_m365_citations_strips_both_halves_of_a_marker_split_across_deltas():
    deltas = ["官方建议优先选择 LTS。cite", "turn3search5\n\n### 方法一"]
    joined = "".join(clean_m365_citations(d) for d in deltas)
    assert "turn3search5" not in joined
    assert "cite" not in joined.lower()
    assert not any("" <= c <= "" for c in joined)
    assert "官方建议优先选择 LTS。" in joined
    assert "### 方法一" in joined


# The two shapes where the id run's own repetition is what saves the text: a
# delta that stops mid-run, and one that stops on the word "cite" itself. Neither
# leaves a trailing private-use delimiter behind, so the orphan rule below cannot
# reach them -- taking the whole run in one match is the only thing that does. A
# mutation run is what turned these up: with the id run narrowed back to a single
# id, every other case in this file still passed, because the orphan rule covered
# each of them. These two are the reason that pattern reads the way it does.

def test_clean_m365_citations_strips_an_id_run_cut_off_without_its_delimiter():
    raw = "已有更高版本也可能触发这个错误。citeturn4search10turn4search12"
    cleaned = clean_m365_citations(raw)
    assert "turn4search10" not in cleaned
    assert "turn4search12" not in cleaned
    assert "cite" not in cleaned.lower()
    assert "已有更高版本也可能触发这个错误。" in cleaned


def test_clean_m365_citations_strips_an_opener_that_ends_on_the_word_cite():
    """A delta can stop between "cite" and the delimiter that follows it."""
    cleaned = clean_m365_citations("官方建议优先选择 LTS。cite")
    assert "cite" not in cleaned.lower()
    assert not any("" <= c <= "" for c in cleaned)
    assert "官方建议优先选择 LTS。" in cleaned


def test_clean_m365_citations_strips_an_orphaned_multi_id_tail():
    """A split can land mid-id-run, orphaning several ids at once."""
    tail = clean_m365_citations("turn4search10turn4search12 后续段落")
    assert "turn4search10" not in tail
    assert "turn4search12" not in tail
    assert not any("" <= c <= "" for c in tail)
    assert "后续段落" in tail


def test_clean_m365_citations_keeps_an_id_shaped_word_that_is_real_prose():
    """The orphan rule keys on the trailing delimiter, not the id shape alone.

    Without the delimiter requirement, prose that merely mentions a marker id --
    a bug report, this project's own docs -- would lose the word it is about.

    The bare sentence proves less than it looks: carrying no marker character at
    all, it comes back off the fast path before any pattern runs, so it passed
    even with the delimiter made optional. The second case is the one that
    reaches the rule, and it is also the realistic one -- a turn that cites a
    source while its prose discusses an id.
    """
    plain = "日志里残留了 turn3search5 这个锚点，是清理漏了。"
    assert clean_m365_citations(plain) == plain

    mixed = plain + "citeturn1search4"
    cleaned = clean_m365_citations(mixed)
    assert "turn3search5" in cleaned
    assert "turn1search4" not in cleaned
    assert not any("" <= c <= "" for c in cleaned)


# The two NON-PUA citation renderings, both seen in a single live turn against a
# real deployment: the streamed deltas carried literal <cite> tags while the
# cumulative snapshot of the same sentences carried bracket marks. Leaving either
# form in place cost twice over -- the markers reached the client as visible
# noise, and because _dedupe_signature is built on clean_m365_citations the same
# sentence produced two different signatures, which drove coverage down and made
# the final reconciliation append text the reader already had.

def test_clean_m365_citations_strips_literal_cite_tags():
    raw = "FastAPI 性能媲美 Node.js。<cite>turn1search7</cite> 它底层依赖 Starlette。"
    cleaned = clean_m365_citations(raw)
    assert "cite" not in cleaned.lower()
    assert "FastAPI 性能媲美 Node.js。" in cleaned
    assert "它底层依赖 Starlette。" in cleaned


def test_clean_m365_citations_strips_bracket_cite_marks():
    raw = "已被 Microsoft、Netflix 采用。【4-6f710b】 FastAPI 已被广泛使用。"
    cleaned = clean_m365_citations(raw)
    assert "【4-6f710b】" not in cleaned
    assert "已被 Microsoft、Netflix 采用。" in cleaned
    assert "FastAPI 已被广泛使用。" in cleaned


def test_clean_m365_citations_keeps_bracket_emphasis_that_is_not_a_citation():
    """【】 is ordinary CJK punctuation; only citation-shaped marks may go.

    Stripping every 【...】 would delete real content from Chinese and Japanese
    answers, where the brackets are used for emphasis and for titles.
    """
    raw = "【重要】请先阅读文档，【注意事项】见下。"
    assert clean_m365_citations(raw) == raw


def test_clean_m365_citations_keeps_html_cite_element_with_prose():
    """HTML's <cite> marks the title of a work; that is real content.

    Only citation-id payloads (``turn1search7``) are markers, so the pattern is
    bounded to id characters and a real title with spaces survives.
    """
    raw = "The novel <cite>Moby Dick</cite> is referenced."
    assert clean_m365_citations(raw) == raw


def test_dedupe_signature_collapses_both_citation_renderings():
    """The same sentence in either rendering must yield ONE signature.

    This is the comparison that decides whether the final frame's restatement is
    appended, so a mismatch here is what surfaced as a duplicated answer.
    """
    delta_form = "FastAPI 性能媲美 Node.js。<cite>turn1search7</cite>"
    snapshot_form = "FastAPI 性能媲美 Node.js。【4-6f710b】"
    assert _dedupe_signature(delta_form) == _dedupe_signature(snapshot_form)


def test_message_content_strips_citations_from_text():
    entry = {"text": "音频 [x](\ue200cite\ue202turn1file1\ue201) 完成"}
    assert "\ue200" not in _message_content(entry)
    assert "完成" in _message_content(entry)


# --- _message_content ------------------------------------------------------

def test_message_content_plain_text():
    assert _message_content({"text": "hello"}) == "hello"


def test_message_content_combines_text_and_image_markdown():
    entry = {"text": "caption", "image": {"url": "https://x/a.png"}}
    assert _message_content(entry) == "caption\n\n![image](https://x/a.png)"


def test_message_content_drops_loading_placeholder_when_image_present():
    entry = {"text": "Loading image", "image": {"url": "https://x/a.png"}}
    # The placeholder text is dropped, leaving only the real image.
    assert _message_content(entry) == "![image](https://x/a.png)"
