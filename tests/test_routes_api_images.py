"""Unit tests for OpenAI-compatible image generation helpers."""
from __future__ import annotations

from m365_copilot_openai_proxy.routes_api_images import (
    _build_image_prompt,
    _harvest_image_urls,
)


def test_build_image_prompt_includes_size_and_description():
    prompt = _build_image_prompt("一只橘猫", size="1024x1024", n=1)
    assert "1024x1024" in prompt
    assert "一只橘猫" in prompt
    assert "Designer" in prompt or "Flux" in prompt


def test_harvest_designer_markdown_image():
    text = "这里是结果\n![image](https://designerapp.officeapps.live.com/designerapp/document.ashx?id=abc&fileToken=xyz)\n完成"
    urls = _harvest_image_urls(text)
    assert len(urls) == 1
    assert urls[0].startswith("https://designerapp.officeapps.live.com/designerapp/document.ashx")


def test_harvest_asyncgw_and_data_url():
    text = (
        "![a](https://apc.asyncgw.teams.microsoft.com/v1/objects/0-abc/views/original/img.png) "
        "and data:image/png;base64,AAAA"
    )
    urls = _harvest_image_urls(text)
    assert any("asyncgw.teams.microsoft.com" in u for u in urls)
    assert any(u.startswith("data:image/png;base64,") for u in urls)


def test_harvest_dedupes_and_ignores_unrelated_links():
    text = (
        "see https://example.com/cat.png and "
        "![image](https://designerapp.officeapps.live.com/designerapp/document.ashx?id=1) "
        "again ![image](https://designerapp.officeapps.live.com/designerapp/document.ashx?id=1)"
    )
    urls = _harvest_image_urls(text)
    assert len(urls) == 1
    assert "designerapp.officeapps.live.com" in urls[0]


def test_register_images_routes_callable():
    from m365_copilot_openai_proxy.routes_api_images import register_images_routes
    from m365_copilot_openai_proxy.routes_api import register_api_routes

    assert callable(register_images_routes)
    assert callable(register_api_routes)
