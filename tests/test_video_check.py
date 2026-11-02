from icampus.video_check import _named, asset, mask, urls_in

AID = "0123456789abcdef0123456789abcdef"  # made up


def test_asset_from_commons_thumbnail():
    icd = {"thumbnail_url": f"https://lcms.skku.edu/CommonsCore2/v2/contents/{AID}.jpg"}
    assert asset(icd) == ("lcms.skku.edu", AID)


def test_asset_refuses_other_hosts_and_shapes():
    assert asset({"thumbnail_url": f"https://evil.example/CommonsCore2/v2/contents/{AID}.jpg"}) is None
    assert asset({"thumbnail_url": f"http://lcms.skku.edu/CommonsCore2/v2/contents/{AID}.jpg"}) is None
    assert asset({"thumbnail_url": "https://lcms.skku.edu/other.jpg"}) is None
    assert asset({}) is None


def test_mask_hides_ids_and_queries():
    out = mask(f"https://lcms.skku.edu/em/{AID}?token=secret&user=2026123456")
    assert AID not in out and "secret" not in out and "2026123456" not in out
    assert out == "https://lcms.skku.edu/em/<id> (+query)"


def test_urls_found_at_any_depth_and_download_url_read():
    meta = {"result": {"download_url": "https://lcms.skku.edu/x.mp4", "files": [{"u": "http://a.test/b"}], "n": 3}}
    assert sorted(urls_in(meta)) == ["http://a.test/b", "https://lcms.skku.edu/x.mp4"]
    assert _named(meta, "download_url") == "https://lcms.skku.edu/x.mp4"
    assert _named({"result": {"download_url": "javascript:x"}}, "download_url") is None
    assert _named(None, "download_url") is None


async def test_first_byte_asks_for_one_byte_and_reads_the_total():
    import httpx

    from icampus.video_check import _first_byte

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://files.test/v/0123456789abcdef.mp4?sig=x"})
        assert request.headers["range"] == "bytes=0-0" and "cookie" not in request.headers
        return httpx.Response(206, headers={"content-type": "video/mp4", "content-range": "bytes 0-0/104857600"},
                              content=b"\0")

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        out = await _first_byte(client, "https://lcms.test/start")
    assert out == "206 video/mp4 100 MB via 1 redirect(s) to https://files.test/v/<id>.mp4 (+query)"
