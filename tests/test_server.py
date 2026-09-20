"""The MCP surface: exactly one tool, read-only resources, and one prompt."""

import json

import pytest
from mcp import MCPError

from helpers import PDF_BYTES, PDF_URL, FakeGitHub, FakeWeb

pytestmark = pytest.mark.anyio


async def test_exposes_exactly_one_tool(client):
    tools = (await client.list_tools()).tools
    assert [tool.name for tool in tools] == ["archive_source"]
    tool = tools[0]
    assert set(tool.input_schema["properties"]) == {"url", "note"}
    assert tool.input_schema["required"] == ["url"]
    assert tool.annotations.read_only_hint is False  # it has side effects
    assert tool.annotations.destructive_hint is False  # but never deletes evidence


async def test_exposes_read_only_resources(client):
    resources = (await client.list_resources()).resources
    assert [str(resource.uri) for resource in resources] == ["evidence://recent"]
    templates = (await client.list_resource_templates()).resource_templates
    assert [t.uri_template for t in templates] == ["evidence://artifact/{evidence_id}"]


async def test_prompt_guides_the_archival_workflow(client):
    prompts = (await client.list_prompts()).prompts
    assert [prompt.name for prompt in prompts] == ["archive_research_evidence"]

    result = await client.get_prompt(
        "archive_research_evidence", {"source_url": "https://example.org/a.pdf"}
    )
    text = result.messages[0].content.text
    assert "https://example.org/a.pdf" in text
    assert "archive_source" in text
    assert "evidence_id" in text
    assert "Never claim" in text


async def test_archive_then_read_it_back_through_resources(
    client, serve, github_settings, fake_dns
):
    web = FakeWeb({PDF_URL: (200, {"content-type": "application/pdf"}, PDF_BYTES)})
    serve(github_settings, web=web, github=FakeGitHub(status=201))

    result = await client.call_tool("archive_source", {"url": PDF_URL, "note": "Key figure"})
    assert not result.is_error
    archived = result.structured_content
    assert archived["status"] == "archived"
    assert archived["remote_metadata_synced"] is True

    item = await client.read_resource(f"evidence://artifact/{archived['evidence_id']}")
    record = json.loads(item.contents[0].text)
    assert record["sha256"] == archived["sha256"]
    assert record["original_url"] == PDF_URL
    assert record["note"] == "Key figure"
    assert str(github_settings.evidence_root) not in item.contents[0].text  # no absolute paths

    recent = json.loads((await client.read_resource("evidence://recent")).contents[0].text)
    assert recent["count"] == 1
    assert recent["items"][0]["evidence_id"] == archived["evidence_id"]


async def test_reading_resources_changes_nothing(client, serve, settings):
    serve(settings)
    await client.read_resource("evidence://recent")
    with pytest.raises(MCPError):
        await client.read_resource("evidence://artifact/ev_0000000000000000")
    assert not settings.evidence_root.exists()  # a read never creates files or folders
