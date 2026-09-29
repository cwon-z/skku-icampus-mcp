"""LearningX token recovery and error hints, against a fake browser context (no Chromium, no network)."""

import pytest

from icampus.browser import CanvasError, NotLoggedIn, Session, error_hint
from icampus.config import Settings
from icampus.store import Store


class FakeContext:
    def __init__(self, token: str):
        self.token = token

    async def cookies(self, url):
        return [{"name": "xn_api_token", "value": self.token}] if self.token else []


def session(tmp_path, token, answer):
    """A Session whose LearningX GETs return answer(token used); an exception instance is raised.
    Re-issuing the token (the My Page launch) sets it to 'fresh'."""
    page = type("Page", (), {"context": FakeContext(token)})()
    s = Session(Settings(_env_file=None), Store(tmp_path / "t.db"), page)
    s.calls, s.launches = [], 0

    async def get_json(url, params=None, headers=None):
        used = headers["Authorization"].removeprefix("Bearer ")
        s.calls.append(used)
        result = answer(used)
        if isinstance(result, Exception):
            raise result
        return result, {}

    async def launch(path, marker):
        s.launches += 1
        page.context.token = "fresh"

    s.get_json, s.launch = get_json, launch
    return s


def fresh_only(error):
    return lambda token: {"ok": 1} if token == "fresh" else error


async def test_refused_token_is_reissued_once(tmp_path):
    s = session(tmp_path, "old", fresh_only(CanvasError(400, "x")))
    assert await s.learningx("/learner/todos") == {"ok": 1}
    assert s.calls == ["old", "fresh"] and s.launches == 1


async def test_401_and_403_reissue_too(tmp_path):
    for error in (NotLoggedIn("x"), CanvasError(403, "x")):
        s = session(tmp_path, "old", fresh_only(error))
        assert await s.learningx("/x") == {"ok": 1} and s.launches == 1


async def test_missing_token_is_reissued_before_calling(tmp_path):
    s = session(tmp_path, "", fresh_only(CanvasError(400, "x")))
    assert await s.learningx("/x") == {"ok": 1}
    assert s.calls == ["fresh"] and s.launches == 1


async def test_gives_up_after_one_reissue_per_session(tmp_path):
    s = session(tmp_path, "old", lambda token: CanvasError(400, "x"))
    for path in ("/a", "/b"):
        with pytest.raises(CanvasError):
            await s.learningx(path)
    assert s.launches == 1 and s.calls == ["old", "fresh", "fresh"]


@pytest.mark.parametrize("status", [404, 500])
async def test_other_errors_are_not_retried(tmp_path, status):
    s = session(tmp_path, "old", lambda token: CanvasError(status, "x"))
    with pytest.raises(CanvasError):
        await s.learningx("/a")
    assert s.launches == 0


def test_error_hint():
    assert error_hint('{"message": "Invalid   token"}') == "Invalid token"
    assert error_hint('while(1);{"errors": [{"message": "not found"}]}') == "not found"
    assert error_hint('{"error": "bad token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc"}') == "bad token …"
    assert error_hint("<html>Bad Request</html>") == "" and error_hint('{"error": {"code": 7}}') == ""
    assert len(error_hint('{"message": "' + "ab " * 100 + '"}')) == 120
    assert str(CanvasError(400, "canvas.skku.edu/x", "bad")) == "HTTP 400 for canvas.skku.edu/x (bad)"
