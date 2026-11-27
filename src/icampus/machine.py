"""What differs between macOS, Windows and everything else, for the fetcher (fetcher.py): where its work directory
goes, whether a directory is inside a folder that some service copies to the cloud, and keeping the machine awake
while a job runs.

Standard library only. Each difference is a few lines and takes the system as an argument, so the logic is tested
on any machine; the two calls that only exist on one system (`caffeinate` on macOS, `SetThreadExecutionState` on
Windows) are the last two functions of this file and are never run by a test.
"""

import json
import logging
import os
import platform
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)
APP = "skku-icampus-fetcher"
# Windows execution states. ES_DISPLAY_REQUIRED is deliberately not here: the screen may always sleep.
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def system(name: str | None = None) -> str:
    """macos, windows or other."""
    name = sys.platform if name is None else name
    return "macos" if name == "darwin" else "windows" if name.startswith("win") else "other"


def describe() -> str:
    """What this machine runs, for the server's list of fetchers: "macOS 15.1 arm64", "Windows 11 AMD64"."""
    kind = system()
    release = platform.mac_ver()[0] if kind == "macos" else platform.release()
    name = {"macos": "macOS", "windows": "Windows"}.get(kind) or platform.system() or "unknown"
    return " ".join(part for part in (name, release, platform.machine()) if part)[:60]


def hostname() -> str:
    """This machine's name, as a fetcher name the server accepts: letters, digits, spaces, dots and hyphens."""
    try:
        raw = socket.gethostname()
    except OSError:
        raw = ""
    name = re.sub(r"[^\w .\-]", "-", raw.removesuffix(".local")).lstrip(" .-")[:40].rstrip()
    return name or "fetcher"


def cache_dir(kind: str | None = None, env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The per-user cache directory for the fetcher's work files: a place the system keeps out of backups and
    cloud folders, and that only this user reads."""
    kind, env, home = kind or system(), os.environ if env is None else env, home or Path.home()
    if kind == "macos":
        return home / "Library" / "Caches" / APP
    if kind == "windows":
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else home / "AppData" / "Local") / APP
    return (Path(env["XDG_CACHE_HOME"]) if env.get("XDG_CACHE_HOME") else home / ".cache") / APP


def _parts(path: Path) -> tuple[str, ...]:
    try:
        path = path.expanduser().resolve()
    except (OSError, RuntimeError):
        path = path.absolute()
    return tuple(part.casefold() for part in path.parts)


def _inside(path: Path, root: Path) -> bool:
    child, parent = _parts(path), _parts(root)
    return len(parent) > 1 and child[:len(parent)] == parent


def synced_by(path: Path, kind: str | None = None, env: Mapping[str, str] | None = None,
              home: Path | None = None) -> str | None:
    """The cloud service whose folder `path` is in (iCloud Drive, OneDrive, Dropbox, Google Drive), or None.
    Lecture videos must never be put where such a service would upload them. A guess from well-known places and
    folder names: it can miss a folder that was set up by hand under another name."""
    kind, env, home = kind or system(), os.environ if env is None else env, home or Path.home()
    roots = [(Path(env[var]), "OneDrive") for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")
             if env.get(var)]
    roots += [(home / "Library" / "Mobile Documents", "iCloud Drive"),
              (home / "Library" / "CloudStorage", "a cloud storage app"),  # OneDrive, Dropbox, Google Drive on macOS
              (home / "iCloudDrive", "iCloud Drive")]  # iCloud for Windows
    for info in (home / ".dropbox" / "info.json", *(Path(env[var]) / "Dropbox" / "info.json"
                                                    for var in ("APPDATA", "LOCALAPPDATA") if env.get(var))):
        try:  # where Dropbox says its folders are
            accounts = json.loads(info.read_text(encoding="utf-8"))
            roots += [(Path(a["path"]), "Dropbox") for a in accounts.values() if isinstance(a, dict) and a.get("path")]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    for root, service in roots:
        if _inside(path, root):
            return service
    for part in _parts(path):  # a folder one of them made, wherever it is
        for prefix, service in (("onedrive", "OneDrive"), ("dropbox", "Dropbox"), ("icloud drive", "iCloud Drive"),
                                ("iclouddrive", "iCloud Drive"), ("google drive", "Google Drive"),
                                ("googledrive", "Google Drive")):
            if part.startswith(prefix):
                return service
    if kind == "macos":  # "Desktop & Documents Folders" in iCloud: then both show up inside iCloud Drive
        for folder in ("Desktop", "Documents"):
            mirrored = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / folder
            if _inside(path, home / folder) and mirrored.exists():
                return "iCloud Drive (Desktop & Documents)"
    return None


class KeepAwake:
    """Keeps the machine from going to sleep by itself while at least one job is held, and lets it again as soon
    as none is. Only idle sleep, never the display, and closing the lid still sleeps the machine (the server then
    takes the job back). On macOS a `caffeinate -i -w <this process>` runs for as long as a job is held; it ends by
    itself if this process dies. On Windows the thread's execution state is set and cleared; both calls must come
    from the same thread, which is the one that runs the fetcher's loop. Elsewhere nothing happens."""

    def __init__(self, enabled: bool = True, *, kind: str | None = None,
                 caffeinate: Callable[[int], Any] | None = None,
                 execution_state: Callable[[int], Any] | None = None):
        self.kind = kind or system()
        self.enabled = enabled and self.kind in ("macos", "windows")
        self.held = 0
        self._caffeinate = caffeinate or _caffeinate
        self._execution_state = execution_state or _execution_state
        self._child: Any = None

    def hold(self) -> None:
        self.held += 1
        if self.held == 1 and self.enabled:
            self._switch(True)

    def release(self) -> None:
        if self.held == 0:
            return
        self.held -= 1
        if self.held == 0 and self.enabled:
            self._switch(False)

    def _switch(self, on: bool) -> None:
        try:
            if self.kind == "windows":
                self._execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
            elif on:
                self._child = self._caffeinate(os.getpid())
            else:
                child, self._child = self._child, None
                if child is not None:
                    child.terminate()
                    child.wait(timeout=5)
        except Exception as exc:  # the job runs either way; it may just be cut short by a sleep
            log.warning("could not %s keeping this machine awake (%s)", "start" if on else "stop", type(exc).__name__)


# --- the two calls that exist on one system only ---------------------------------------------------------------

def _caffeinate(pid: int) -> subprocess.Popen:
    """macOS: no idle sleep (-i) for as long as process `pid` lives (-w)."""
    return subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(pid)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _execution_state(flags: int) -> None:
    """Windows: SetThreadExecutionState for the calling thread."""
    import ctypes
    call = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    call.argtypes, call.restype = [ctypes.c_uint32], ctypes.c_uint32
    call(flags)
