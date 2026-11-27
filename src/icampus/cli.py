import argparse
import asyncio
import logging

from .config import Settings


def export_dotenv(path: str = ".env") -> None:
    """A local run keeps provider keys (ELEVENLABS_API_KEY=...) in .env beside the ICAMPUS_* settings, but only
    those are read from the file by the settings. Put the other variables into this process's environment, where
    a provider's config looks its key up. Docker passes them itself; what is already set wins."""
    import os

    from dotenv import dotenv_values
    for name, value in dotenv_values(path).items():
        if value and not name.startswith("ICAMPUS_") and name not in os.environ:
            os.environ[name] = value


def main() -> None:
    parser = argparse.ArgumentParser(prog="icampus", description="SKKU iCampus collector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the REST API and the sync schedule")
    sync = sub.add_parser("sync", help="sync once now")
    sync.add_argument("--headed", action="store_true", help="show the browser")
    sync.add_argument("--retry-login", action="store_true", help="clear a login block and try credentials")
    probe = sub.add_parser("probe", help="one-off live check of what the iCampus pages load (read-only)")
    probe.add_argument("--login", choices=["manual", "auto"], default="manual")
    probe.add_argument("--course", help="course ID to inspect (default: first current course)")
    video = sub.add_parser("video-check", help="one-off check whether lecture videos can be fetched without "
                                               "their viewer (shapes only; no downloads)")
    video.add_argument("--course", help="course ID (default: all current courses)")
    video.add_argument("--head", type=int, default=0, metavar="N",
                       help="also ask the content server about N videos: description and one byte of the file "
                            "(no cookies)")
    video.add_argument("--captions", action="store_true",
                       help="read every hosted video's Commons description (no cookies) for caption/script flags")
    video.add_argument("--login", choices=["saved", "manual"], default="saved",
                       help="manual: sign in yourself in a browser window if the saved login has expired")
    material = sub.add_parser("material-check", help="one-off check whether lecture materials (slide PDFs) sit on "
                                                     "the content server like the videos (shapes only; nothing is "
                                                     "opened or downloaded)")
    material.add_argument("--course", help="course ID (default: all current courses)")
    material.add_argument("--head", type=int, default=0, metavar="N",
                          help="also ask the content server to describe N materials (no cookies, no download)")
    material.add_argument("--login", choices=["saved", "manual"], default="saved",
                          help="manual: sign in yourself in a browser window if the saved login has expired")
    measure = sub.add_parser("bench", help="measure download speed from this machine to the content server "
                                           "(nothing is kept); needs the server running")
    measure.add_argument("--seconds", type=float, default=45, help="how long each step downloads (default 45)")
    measure.add_argument("--streams", default="1,2,3", help="connections per step (default 1,2,3)")
    measure.add_argument("--server", help="the API to ask for videos to try (default: this machine's)")
    args = parser.parse_args()

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):  # their INFO lines would print the Kuma push token, and at DEBUG the
        logging.getLogger(noisy).setLevel(logging.WARNING)  # content server's redirect address and every header

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app
        export_dotenv()
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                    log_level=settings.log_level.lower())
    elif args.cmd == "sync":
        from .store import Store
        from .sync import Syncer
        store = Store(settings.db_path)
        syncer = Syncer(settings, store)
        if args.headed:
            settings.headless = False
        run_id = asyncio.run(syncer.run("cli", retry_login=args.retry_login))
        run = next(r for r in store.recent_runs(20) if r["id"] == run_id)
        print(run["status"], run["detail"])
    elif args.cmd == "probe":
        from .probe import probe as run_probe
        out = asyncio.run(run_probe(settings, args.login, args.course))
        print(f"report: {out / 'report.md'}")
    elif args.cmd == "video-check":
        from .video_check import check
        print(asyncio.run(check(settings, args.course, args.head, args.login == "manual", args.captions)))
    elif args.cmd == "material-check":
        from .video_check import check_materials
        print(asyncio.run(check_materials(settings, args.course, args.head, args.login == "manual")))
    elif args.cmd == "bench":
        from . import bench
        from .config import parse_tokens
        try:
            streams = bench.parse_streams(args.streams)
        except ValueError as exc:
            raise SystemExit(f"icampus bench: {exc}") from None
        token = next(iter(parse_tokens(settings.api_tokens)), "")  # asks its own API, as any client would
        if not token:
            raise SystemExit("icampus bench: no API token configured (ICAMPUS_API_TOKENS)")
        server = args.server or f"http://127.0.0.1:{settings.port}"
        raise SystemExit(asyncio.run(bench.main(server, token, seconds=max(1.0, args.seconds), streams=streams)))


if __name__ == "__main__":
    main()
