"""Session.download against a local server: the redirect to a file store, a login page instead of the file,
errors and the size cap. Uses Playwright's HTTP client only, so no browser is needed (nothing leaves the machine)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from icampus.browser import CanvasError, FetchError, NotLoggedIn, Session
from icampus.config import Settings
from icampus.store import Store

ROUTES = {  # path -> (status, body or redirect target)
    "/files/1/download": (302, "/store/1"),
    "/store/1": (200, "hello"),
    "/files/2/download": (302, "/login/canvas"),
    "/login/canvas": (200, "<html>sign in</html>"),
    "/files/3/download": (404, "gone"),
    "/files/4/download": (200, "x" * 2048),
    "/files/5/download": (401, "denied"),
}


def serve() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            status, body = ROUTES.get(self.path.split("?")[0], (404, ""))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", body)
                body = ""
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def test_download(tmp_path):
    server = serve()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        async with async_playwright() as pw:
            request = await pw.request.new_context()
            page = type("Page", (), {"context": type("Context", (), {"request": request})()})()
            s = Session(Settings(_env_file=None), Store(tmp_path / "t.db"), page)
            assert await s.download(f"{base}/files/1/download?verifier=SECRET", 1024) == b"hello"
            with pytest.raises(NotLoggedIn):
                await s.download(f"{base}/files/2/download?verifier=SECRET", 1024)
            with pytest.raises(CanvasError) as error:
                await s.download(f"{base}/files/3/download?verifier=SECRET", 1024)
            assert "SECRET" not in str(error.value) and "404" in str(error.value)
            with pytest.raises(FetchError, match="larger than"):
                await s.download(f"{base}/files/4/download", 1024)
            with pytest.raises(CanvasError, match="HTTP 401"):  # not allowed, which is not "signed out"
                await s.download(f"{base}/files/5/download", 1024)
            await request.dispose()
    finally:
        server.shutdown()
