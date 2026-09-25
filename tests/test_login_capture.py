"""The SSO login answer must be read even though the page navigates away right after it arrives.
Runs a real headless Chromium against a local imitation of the login page (nothing leaves the machine)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.async_api import async_playwright

from icampus.browser import LoginRejected, Session
from icampus.config import Settings
from icampus.store import Store

PAGE = """<html><body><input id="userid"><input id="password" type="password">
<button id="btnLoginBtn">LOGIN</button><script>
document.getElementById('btnLoginBtn').onclick = async () => {
  const r = await fetch('/xn-sso/customs/pages/logon-url.php', {method: 'POST', body: 'x'});
  const data = JSON.parse(await r.text());
  // like the real page (which posts a form); the delay reproduces the gap before navigation starts
  if (!data.error && data.accounts.length === 1) setTimeout(() => { location.href = '/done'; }, 800);
};</script></body></html>"""


def serve(answer: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body: str, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            self._send("<html>done</html>" if self.path == "/done" else PAGE, "text/html")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self._send(json.dumps(answer), "text/plain")  # the real endpoint says text/plain too

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def run_login(tmp_path, answer: dict):
    server = serve(answer)
    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception:
                pytest.skip("Chromium not installed (uv run playwright install chromium)")
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{server.server_port}/xn-sso/login.php")
            session = Session(Settings(username="u", password="p", _env_file=None), Store(tmp_path / "t.db"), page)
            try:
                await session._submit_credentials()
                return page.url  # must already have left the login form, without any extra waiting
            finally:
                await browser.close()
    finally:
        server.shutdown()


async def test_success_answer_is_read_and_page_moves_on(tmp_path):
    assert (await run_login(tmp_path, {"accounts": [{"user_no": "1"}]})).endswith("/done")


async def test_rejection_is_reported(tmp_path):
    with pytest.raises(LoginRejected):
        await run_login(tmp_path, {"error": "wrong password"})
