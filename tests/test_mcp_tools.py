"""MCP tool output: rows trimmed of bookkeeping, attachments returned as text or as an image."""

import httpx
import pytest
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError

import icampus.mcp_server as server


def test_rows_keep_status_and_drop_raw_completion_fields():
    row = {"key": "k", "title": "Proposal", "status": "todo", "status_reason": "not submitted (Canvas)",
           "done": False, "completed": False, "in_remaining_list": True, "status_checked_at": "t",
           "submission": {"state": "unsubmitted", "late": False, "missing": False, "score": 0, "grade": None},
           "attachments": None, "points_possible": 0.0}
    assert server._row(row, server.STATUS_NOISE) == {
        "title": "Proposal", "status": "todo", "status_reason": "not submitted (Canvas)",
        "submission": {"state": "unsubmitted", "score": 0}, "points_possible": 0.0}


def fake_api(monkeypatch, info: dict, content: bytes = b""):
    async def call(method, path, *, timeout=30, **params):
        assert path == f"/api/v1/files/{info['id']}" and params["max_chars"] == server.TEXT_PAGE
        return dict(info)
    monkeypatch.setattr(server, "_call", call)
    monkeypatch.setattr(server, "_client", lambda timeout: httpx.AsyncClient(
        base_url="http://api", transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))))


async def test_read_attachment_text(monkeypatch):
    fake_api(monkeypatch, {"id": "701", "name": "notice.pdf", "extract": "pdf", "text": "--- page 1 ---\n시험 안내",
                           "next_offset": None})
    info, text = await server.read_attachment("701")
    assert text == "--- page 1 ---\n시험 안내" and "text" not in info


async def test_read_attachment_image(monkeypatch):
    fake_api(monkeypatch, {"id": "702", "name": "diagram.png", "content_type": "image/png", "size": 8,
                           "extract": "image", "text": ""}, b"\x89PNG\r\n\x1a\n")
    info, image = await server.read_attachment("702")
    assert isinstance(image, Image) and image.to_image_content().mime_type == "image/png"
    fake_api(monkeypatch, {"id": "703", "name": "poster.jpg", "size": 9_000_000, "extract": "image", "text": ""})
    [info] = await server.read_attachment("703")
    assert "open it in iCampus" in info["note"]


async def test_read_attachment_checks_its_input():
    for bad in ("../x", "70 1", ""):
        with pytest.raises(ToolError):
            await server.read_attachment(bad)
    with pytest.raises(ToolError):
        await server.read_attachment("701", offset=-1)
