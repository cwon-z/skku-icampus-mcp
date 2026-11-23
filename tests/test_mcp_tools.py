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


async def test_only_read_attachment_touches_icampus():
    tools = {t.name: t.annotations for t in await server.mcp.list_tools()}
    assert tools["read_attachment"].read_only_hint is False  # its first read opens the file in iCampus
    assert tools["list_tasks"].read_only_hint and tools["read_assignment"].read_only_hint


async def test_read_attachment_checks_its_input():
    for bad in ("../x", "70 1", ""):
        with pytest.raises(ToolError):
            await server.read_attachment(bad)
    with pytest.raises(ToolError):
        await server.read_attachment("701", offset=-1)


async def test_through_the_mcp_protocol(monkeypatch):
    fake_api(monkeypatch, {"id": "702", "name": "diagram.png", "content_type": "image/png", "size": 8,
                           "extract": "image", "text": ""}, b"\x89PNG\r\n\x1a\n")
    result = await server.mcp.call_tool("read_attachment", {"file_id": "702"})
    assert [c.type for c in result.content] == ["text", "image"] and result.content[1].mime_type == "image/png"
    fake_api(monkeypatch, {"id": "701", "name": "notice.txt", "extract": "text", "text": "시험 안내"})
    result = await server.mcp.call_tool("read_attachment", {"file_id": "701"})
    assert [c.type for c in result.content] == ["text", "text"] and result.content[1].text == "시험 안내"


async def test_list_lectures_marks_exportable_videos(monkeypatch):
    async def call(method, path, *, timeout=30, **params):
        return {"items": [
            {"key": "k", "item_id": "7002", "title": "Week 1", "status": "todo",
             "video": {"asset_id": "a1b2c3", "host": "lcms.skku.edu", "listed_size": 104857600}},
            {"key": "k2", "item_id": "7003", "title": "Guest talk (YouTube)", "status": "todo"}]}
    monkeypatch.setattr(server, "_call", call)
    body = await server.list_lectures()
    hosted, youtube = body["items"]
    assert hosted == {"item_id": "7002", "title": "Week 1", "status": "todo", "exportable": True, "video_mb": 100}
    assert "exportable" not in youtube


async def test_export_video_links_when_ready(monkeypatch):
    calls = []

    async def call(method, path, *, timeout=30, **params):
        calls.append((method, path))
        return {"state": "ready", "item_id": "7002", "size_mb": 100}
    monkeypatch.setattr(server, "_call", call)
    monkeypatch.setattr(server.settings, "public_url", "https://mcp.example.com/")
    body = await server.export_video("7002")
    assert calls == [("POST", "/api/v1/videos/7002")] and body["link"] == "https://mcp.example.com/videos/7002"
    with pytest.raises(ToolError):
        await server.export_video("../7002")
    tools = {t.name: t.annotations for t in await server.mcp.list_tools()}
    assert tools["export_video"].read_only_hint is False


async def test_video_link_streams_from_the_api_with_range(monkeypatch):
    from starlette.requests import Request

    seen = {}

    def answer(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, range=request.headers.get("range"), auth=request.headers.get("authorization"))
        if request.url.path.endswith("/9/content"):
            return httpx.Response(404, json={"error": "no synced lecture video"})
        async def body():  # streamed like a real response (bytes content would count as already read)
            yield b"ftyp"
        return httpx.Response(206, content=body(), headers={
            "content-type": "video/mp4", "content-range": "bytes 4-7/4016", "content-length": "4",
            "set-cookie": "x=y"})
    monkeypatch.setattr(server, "_client", lambda timeout: httpx.AsyncClient(
        base_url="http://api", headers={"Authorization": "Bearer api-token"}, transport=httpx.MockTransport(answer)))

    def request(item_id: str) -> Request:
        return Request({"type": "http", "method": "GET", "path": f"/videos/{item_id}", "path_params": {"item_id": item_id},
                        "headers": [(b"range", b"bytes=4-7"), (b"cookie", b"CF_Authorization=abc")], "query_string": b""})

    resp = await server.video_file(request("7002"))
    body = b"".join([chunk async for chunk in resp.body_iterator])
    await resp.background()
    assert resp.status_code == 206 and body == b"ftyp" and resp.headers["content-range"] == "bytes 4-7/4016"
    assert "set-cookie" not in resp.headers
    assert seen == {"path": "/api/v1/videos/7002/content", "range": "bytes=4-7", "auth": "Bearer api-token"}
    assert (await server.video_file(request("9"))).status_code == 404
    assert (await server.video_file(request("../x"))).status_code == 404


def test_video_link_needs_the_same_auth_as_mcp(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setattr(server.settings, "tokens", "laptop:" + "k" * 24)
    monkeypatch.setattr(server.settings, "access_team", "")
    app = server.http_app()
    with TestClient(app, base_url="http://127.0.0.1:8724") as c:
        assert c.get("/videos/7002").status_code == 401
        assert c.get("/health").status_code == 200
