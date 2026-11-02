from icampus.video_check import content_base, mask

CID = "0123456789abcdef0123456789abcdef"  # made up


def test_content_base_from_thumbnail():
    icd = {"content_id": CID, "thumbnail_url": f"https://lcms.skku.edu/contents/site1/{CID}/contents/thumb.jpg"}
    assert content_base(icd) == f"https://lcms.skku.edu/contents/site1/{CID}/contents/"


def test_content_base_refuses_other_hosts_and_shapes():
    assert content_base({"content_id": CID, "thumbnail_url": f"https://evil.example/{CID}/x.jpg"}) is None
    assert content_base({"content_id": CID, "thumbnail_url": "https://lcms.skku.edu/other.jpg"}) is None
    assert content_base({"thumbnail_url": f"https://lcms.skku.edu/{CID}/x.jpg"}) is None


def test_mask_hides_ids_and_queries():
    out = mask(f"https://lcms.skku.edu/em/{CID}?token=secret&user=2026123456")
    assert CID not in out and "secret" not in out and "2026123456" not in out
    assert out == "https://lcms.skku.edu/em/<id> (+query)"
