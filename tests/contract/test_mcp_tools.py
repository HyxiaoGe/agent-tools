"""Contract tests for MCP tools."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client


@pytest.mark.asyncio
async def test_generate_image_tool(client: httpx.AsyncClient, app):
    """T023: generate_image tool returns image URL."""
    mcp_client = Client(app.state.mcp)

    async with mcp_client:
        result = await mcp_client.call_tool(
            "generate_image",
            {"prompt": "a sunset over mountains"},
        )

    text_parts = [p.text for p in result.content if hasattr(p, "text")]
    assert len(text_parts) > 0
    combined = "\n".join(text_parts)
    assert "/static/images/" in combined
    assert "google/gemini-2.5-flash-image" in combined


@pytest.mark.asyncio
async def test_list_models_tool(app):
    """T024: list_models tool returns all configured models."""
    mcp_client = Client(app.state.mcp)

    async with mcp_client:
        result = await mcp_client.call_tool("list_models", {})

    text_parts = [p.text for p in result.content if hasattr(p, "text")]
    combined = "\n".join(text_parts)
    assert "Gemini 2.5 Flash Image" in combined
    assert "GPT-5 Image Mini" in combined
    assert "GPT-5 Image" in combined
