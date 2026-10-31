import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from scorn import app


class VersionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"version": "x"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port() -> int:
    sock = app.bind_free_port()
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_stale_lock_is_not_running(tmp_path):
    (tmp_path / app.LOCK_NAME).write_text(json.dumps({"pid": 1, "port": free_port()}))
    assert app.running_port(tmp_path) is None


def test_missing_or_garbled_lock_is_not_running(tmp_path):
    assert app.running_port(tmp_path) is None
    (tmp_path / app.LOCK_NAME).write_text("not json")
    assert app.running_port(tmp_path) is None


def test_live_lock_is_reused(tmp_path):
    server = HTTPServer(("127.0.0.1", 0), VersionHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        (tmp_path / app.LOCK_NAME).write_text(json.dumps({"pid": 1, "port": port}))
        assert app.running_port(tmp_path) == port
        assert app.run(tmp_path, tmp_path / "pub", open_browser=False) == 0
        assert not (tmp_path / "jobs").exists()
    finally:
        server.shutdown()


def test_lock_removed_only_by_its_owner(tmp_path):
    lock = app.write_lock(tmp_path, 1234)
    assert json.loads(lock.read_text()) == {"pid": os.getpid(), "port": 1234}
    lock.write_text(json.dumps({"pid": os.getpid() + 1, "port": 1}))
    app.remove_lock(lock)
    assert lock.exists()
    app.write_lock(tmp_path, 1234)
    app.remove_lock(lock)
    assert not lock.exists()


def test_idle_shutdown_takes_over_stale_lock_and_cleans_up(tmp_path):
    (tmp_path / app.LOCK_NAME).write_text(json.dumps({"pid": 1, "port": free_port()}))
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            app.run(tmp_path, tmp_path / "pub", idle_seconds=1.5, open_browser=False, poll=0.2)
        )
    )
    start = time.monotonic()
    thread.start()
    thread.join(30)
    assert not thread.is_alive()
    assert result == [0]
    assert time.monotonic() - start >= 1.5
    assert not (tmp_path / app.LOCK_NAME).exists()
    assert (tmp_path / app.LOG_NAME).is_file()
