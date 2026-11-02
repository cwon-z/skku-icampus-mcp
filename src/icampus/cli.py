import argparse
import asyncio
import logging

from .config import Settings


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
                       help="also send HEAD requests for the media files of N videos (no cookies, no download)")
    video.add_argument("--login", choices=["saved", "manual"], default="saved",
                       help="manual: sign in yourself in a browser window if the saved login has expired")
    args = parser.parse_args()

    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would print the Kuma push token

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app
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
        print(asyncio.run(check(settings, args.course, args.head, args.login == "manual")))


if __name__ == "__main__":
    main()
