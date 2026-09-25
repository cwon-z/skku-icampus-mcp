"""Parsers against synthetic fixtures shaped like LearningX/Canvas JSON."""

from icampus.parse import html_to_text, kind_of, lectures, lesson_attendance, source_key, split_course_name, todo


def test_source_key_and_kind():
    assert source_key("/courses/1001/modules/items/7001") == ("canvas.skku.edu:1001:modules/items:7001", "1001")
    assert source_key("https://canvas.skku.edu/courses/1001/assignments/5001")[0] == \
        "canvas.skku.edu:1001:assignments:5001"
    assert source_key("https://evil.example/courses/1/assignments/2") is None
    assert source_key("/learningx/lti/lecture_attendance/items/view/9001") is None
    assert kind_of("youtube", "dashboard_movie") == "video"
    assert kind_of(None, "dashboard_quiz") == "quiz"
    assert kind_of("hologram") == "unknown"


def test_course_name():
    assert split_course_name("Data Structures_CSE1001_42(홍길동)") == {
        "name": "Data Structures", "code": "CSE1001", "section": "42", "teacher": "홍길동"}
    assert split_course_name("Linear Algebra_MTH2002_41(김철수)")["name"] == \
        "Linear Algebra"
    assert split_course_name("이용안내")["code"] is None


def test_todo():
    row = todo({"todo_id": 1, "todo_title": "Sample video", "unlock_at": None, "due_at": "2025-03-01T14:59:00+00:00",
                "lock_at": None, "dashboard_content_type": "dashboard_movie", "component_type": "youtube",
                "completed": False, "is_ungraded": False, "url": "/courses/1001/modules/items/7001"}, "1001", 2026)
    assert row["key"] == "canvas.skku.edu:1001:modules/items:7001"
    assert row["kind"] == "video" and row["due_at"] == "2025-03-01T23:59:00+09:00"
    assert row["review_flags"] == ["date_precedes_selected_term_year"]
    quiz = todo({"todo_title": "[Quiz] Binary Heaps", "due_at": None, "dashboard_content_type": "dashboard_quiz",
                 "component_type": None, "completed": False, "url": "/courses/1001/assignments/5001"}, "1001", 2026)
    assert quiz["key"].endswith(":assignments:5001") and quiz["review_flags"] == ["no_due_date_displayed"]
    assert todo({"url": "/somewhere/else"}, "1", 2026) is None


MODULES = [{"module_id": 1, "title": "1주차", "position": 1, "module_items": [
    {"module_item_id": 7002, "title": "Week 1 lecture", "content_type": "attendance_item",
     "content_data": {"item_id": 9001, "use_attendance": True, "lecture_period_status": "late_after",
                      "unlock_at": "2026-03-01T15:00:00Z", "late_at": None, "due_at": "2026-03-15T14:59:59Z",
                      "lock_at": None, "item_content_data": {"content_type": "movie", "duration": 1200}},
     "completion_determinable": True, "completed": False},
    {"module_item_id": 7003, "title": "[Quiz] Week 1", "content_type": "quiz",
     "content_data": {"due_at": "2026-03-15T14:59:00Z"}, "completion_determinable": True, "completed": True},
    {"module_item_id": 7004, "title": "Offline exam", "content_type": "offline_exam", "content_data": {},
     "completion_determinable": False, "completed": None},
]}]


def test_lectures():
    rows = lectures(MODULES, {"attendance_summaries": {"9001": {"attendance_status": "absent"}}}, "1001", 2026)
    video, quiz, exam = rows
    assert video["key"] == "canvas.skku.edu:1001:modules/items:7002" and video["week"] == "1주차"
    assert video["week_no"] == 1
    assert video["kind"] == "video" and video["duration_min"] == 20.0 and video["attendance"] == "absent"
    assert video["due_at"] == "2026-03-15T23:59:59+09:00" and video["completed"] is False
    assert quiz["kind"] == "quiz" and quiz["attendance"] is None and quiz["completed"] is True
    assert exam["completed"] is None and exam["review_flags"] == []  # completion not determinable != incomplete


def test_lesson_attendance_and_text():
    summary = lesson_attendance([{"week_position": 1, "lesson_position": 1, "attendance_status": "attendance"},
                                 {"week_position": 1, "lesson_position": 2, "attendance_status": None}])
    assert summary["counts"] == {"attendance": 1, "none": 1}
    assert [x["status"] for x in summary["lessons"]] == ["attendance", "none"]
    assert html_to_text("<p>Submit by <b>Oct 2</b>,<br>23:59</p><p>&nbsp;</p><p>Thanks</p>") == \
        "Submit by Oct 2,\n23:59\n\nThanks"
