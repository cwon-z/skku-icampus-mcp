import pytest

from icampus.browser import _next_link, navigation_allowed, safe_path

C = "https://canvas.skku.edu"


@pytest.mark.parametrize("url", [
    f"{C}/courses/1002/modules/items/7005",
    f"{C}/courses/1001/assignments/5001",
    f"{C}/courses/1001/quizzes/6001",
    f"{C}/courses/1002/discussion_topics/8001",
    f"{C}/courses/1002/files/123/download",
    f"{C}/courses/1002/assignments/syllabus",
    f"{C}/learningx/lti/lecture_attendance/items/view/9001",
    f"{C}/learningx/lti/some_new_viewer",
    f"{C}/conversations",
    "https://kingoinfo.skku.edu/gaia/security/sso.do",
    "http://canvas.skku.edu/",
    "https://www.youtube.com/embed/abc",
])
def test_item_and_foreign_pages_are_blocked(url):
    assert not navigation_allowed(url, "GET", True, "document")


@pytest.mark.parametrize("url,method", [
    (f"{C}/", "GET"),
    (f"{C}/login", "GET"),
    (f"{C}/accounts/1/external_tools/346?launch_type=global_navigation", "GET"),
    (f"{C}/courses/1002/external_tools/297", "GET"),
    (f"{C}/learningx/lti/dashboard_v2", "POST"),
    (f"{C}/learningx/lti/modulebuilder", "POST"),
    (f"{C}/learningx/lti/lecture_attendance/course_menu", "POST"),
    ("https://icampus.skku.edu/xn-sso/login.php?auto_login=true", "GET"),
    ("https://icampus.skku.edu/xn-sso/gw-cb.php?from=web_redirect", "POST"),
])
def test_login_and_panel_pages_are_allowed(url, method):
    assert navigation_allowed(url, method, True, "document")


def test_subresources():
    assert navigation_allowed(f"{C}/api/v1/courses", "GET", False, "fetch")
    assert not navigation_allowed(f"{C}/api/v1/courses/1/discussion_topics/2/read", "PUT", False, "fetch")
    assert not navigation_allowed(f"{C}/api/graphql", "POST", False, "fetch")
    assert not navigation_allowed(f"{C}/learningx/api/v1/dashboard-preference", "POST", False, "xhr")
    assert not navigation_allowed(f"{C}/media/video.mp4", "GET", False, "media")
    assert navigation_allowed("https://icampus.skku.edu/xn-sso/customs/pages/logon-url.php", "POST", False, "xhr")


def test_helpers():
    assert safe_path("https://icampus.skku.edu/xn-sso/gw.php?ssoToken=secret") == "icampus.skku.edu/xn-sso/gw.php"
    link = f'<{C}/api/v1/courses?page=1>; rel="current",<{C}/api/v1/courses?page=2>; rel="next"'
    assert _next_link(link) == f"{C}/api/v1/courses?page=2"
    assert _next_link(f'<{C}/x?page=1>; rel="last"') is None


def test_clean_error_drops_call_log_and_queries():
    exc = Exception("APIRequestContext.get: connect ECONNREFUSED https://canvas.skku.edu/x?token=abc\n"
                    "Call log:\n  - → GET https://canvas.skku.edu/x\n    - cookie: xn_api_token=SECRET\n"
                    "    - Authorization: Bearer SECRET")
    cleaned = __import__("icampus.browser", fromlist=["clean_error"]).clean_error(exc)
    assert "SECRET" not in cleaned and "token=abc" not in cleaned
    assert cleaned == "Exception: APIRequestContext.get: connect ECONNREFUSED https://canvas.skku.edu/x"
