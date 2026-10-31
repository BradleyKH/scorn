"""Desktop launcher: one local server per user, opened in the browser, gone when unused."""

from __future__ import annotations

import argparse
import json
import logging
import logging.config
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

IDLE_SECONDS = 120.0
LOCK_NAME = "app.json"
LOG_NAME = "scorn.log"
log = logging.getLogger("scorn.app")


def default_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Scorn"


def documents_dir() -> Path:
    """The real Documents folder; it is often redirected to OneDrive, so ask Windows."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", ctypes.c_ulong),
                    ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            # FOLDERID_Documents
            guid = GUID(
                0xFDD39AD0,
                0x238F,
                0x46AF,
                (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
            )
            path = ctypes.c_wchar_p()
            get = ctypes.windll.shell32.SHGetKnownFolderPath
            get.argtypes = [
                ctypes.POINTER(GUID),
                wintypes.DWORD,
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            if get(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0 and path.value:
                result = Path(path.value)
                ctypes.windll.ole32.CoTaskMemFree(path)
                return result
        except Exception:
            log.exception("Could not resolve the Documents folder")
    return Path.home() / "Documents"


def setup_logging(state_dir: Path) -> None:
    """Log only to a file: under pythonw there is no stdout or stderr for logging to use."""
    state_dir.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": str(state_dir / LOG_NAME),
                    "encoding": "utf-8",
                    "formatter": "plain",
                }
            },
            "root": {"level": "INFO", "handlers": ["file"]},
        }
    )
    # Uncaught errors and stray prints would otherwise crash or vanish with no console.
    if sys.stdout is None or sys.stderr is None:
        stream = open(state_dir / LOG_NAME, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = stream
        if sys.stderr is None:
            sys.stderr = stream


def answers(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=timeout) as r:
            return "version" in json.loads(r.read())
    except Exception:
        return False


def running_port(state_dir: Path) -> int | None:
    """The port of a live Scorn server named by the lock file, or None for a missing or stale lock."""
    try:
        port = int(json.loads((state_dir / LOCK_NAME).read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return port if answers(port) else None


def write_lock(state_dir: Path, port: int) -> Path:
    lock = state_dir / LOCK_NAME
    lock.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    return lock


def remove_lock(lock: Path) -> None:
    """Remove the lock only if this process wrote it."""
    try:
        if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


def bind_free_port() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


def watch_idle(server, app, idle_seconds: float, poll: float) -> None:
    while not server.should_exit:
        time.sleep(poll)
        activity = app.state.activity
        if not activity.busy() and time.monotonic() - activity.last_seen >= idle_seconds:
            log.info("Idle for %s seconds, shutting down", idle_seconds)
            server.should_exit = True


def open_when_ready(server, url: str) -> None:
    while not server.started and not server.should_exit:
        time.sleep(0.1)
    if server.started:
        webbrowser.open(url)


def run(
    state_dir: Path,
    publish_dir: Path,
    idle_seconds: float = IDLE_SECONDS,
    open_browser: bool = True,
    poll: float = 1.0,
) -> int:
    setup_logging(state_dir)
    existing = running_port(state_dir)
    if existing is not None:
        log.info("Already running on port %s", existing)
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{existing}/")
        return 0

    import uvicorn

    from scorn.web import create_app

    jobs_dir = state_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    publish_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(jobs_dir, publish_dir=publish_dir)
    # Handing uvicorn the bound socket leaves no gap for another program to take the port.
    sock = bind_free_port()
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/"
    lock = write_lock(state_dir, port)
    log.info("Starting on %s, jobs in %s, results in %s", url, jobs_dir, publish_dir)
    # log_config=None keeps uvicorn from reconfiguring logging onto the console.
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    threading.Thread(target=watch_idle, args=(server, app, idle_seconds, poll), daemon=True).start()
    if open_browser:
        threading.Thread(target=open_when_ready, args=(server, url), daemon=True).start()
    try:
        server.run(sockets=[sock])
    except BaseException:
        log.exception("Server failed")
        raise
    finally:
        remove_lock(lock)
        log.info("Stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scorn.app", description="Run the Scorn web page for one user.")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--publish-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    state_dir = args.state_dir or default_state_dir()
    publish_dir = args.publish_dir or documents_dir() / "Scorn"
    return run(state_dir, publish_dir, args.idle_seconds, not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
