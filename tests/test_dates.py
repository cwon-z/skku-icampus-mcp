from icampus.dates import from_canvas, iso, review_flags, term_year


def test_review_flags():
    # some courses reuse items whose deadlines lie in an earlier year: keep the date, flag it
    assert review_flags(from_canvas("2025-03-01T14:59:00Z"), 2026) == ["date_precedes_selected_term_year"]
    assert review_flags(from_canvas("2026-10-02T14:59:00Z"), 2026) == []
    assert review_flags(None, 2026) == ["no_due_date_displayed"]
    assert review_flags(from_canvas("2025-03-01T14:59:00Z"), None) == []


def test_utc_to_kst():
    assert iso(from_canvas("2026-10-02T14:59:00Z")) == "2026-10-02T23:59:00+09:00"
    assert iso(from_canvas("2026-09-27T09:00:00+00:00")) == "2026-09-27T18:00:00+09:00"
    assert from_canvas(None) is None and from_canvas("") is None


def test_term_year():
    assert term_year("2026년 2학기") == 2026
    assert term_year("2026 비정규") is None
