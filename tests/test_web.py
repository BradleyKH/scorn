import asyncio
import io
import json
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from scorn import web

# One part, "Trumpet II", in 4/4: bar 1 is full, bar 2 (on a new page) has a stray extra crotchet.
SCORE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1"><part-name>Trumpet II</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note>
    </measure>
    <measure number="2">
      <print new-page="yes"/>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>4</duration><voice>1</voice><type>whole</type></note>
      <note><pitch><step>D</step><octave>5</octave></pitch><duration>1</duration><voice>1</voice><type>quarter</type></note>
    </measure>
  </part>
</score-partwise>
"""

CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>
"""


class Client:
    """A minimal synchronous ASGI client; FastAPI's TestClient needs httpx, which isn't a dependency."""

    def __init__(self, app):
        self.app = app

    def request(self, method: str, path: str, body: bytes = b"", content_type: str | None = None):
        url, _, query = path.partition("?")
        headers = [(b"host", b"test")]
        if content_type:
            headers += [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": unquote(url), "raw_path": url.encode(), "query_string": query.encode(),
            "root_path": "", "headers": headers, "client": ("test", 1), "server": ("test", 80),
        }
        status, chunks, response_headers = [], [], {}

        async def run():
            done = asyncio.Event()
            sent = False

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if message["type"] == "http.response.start":
                    status.append(message["status"])
                    response_headers.update({k.decode(): v.decode() for k, v in message["headers"]})
                elif message["type"] == "http.response.body":
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        done.set()

            await self.app(scope, receive, send)

        asyncio.run(run())
        return Response(status[0], b"".join(chunks), response_headers)

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str):
        return self.request("POST", path)

    def upload(self, path: str, filename: str, data: bytes = b"%PDF-1.4 not a real pdf"):
        boundary = "scorntestboundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", path, body, f"multipart/form-data; boundary={boundary}")


class Response:
    def __init__(self, status: int, content: bytes, headers: dict):
        self.status_code, self.content, self.headers = status, content, headers

    def json(self):
        return json.loads(self.content)


def fake_scan(gate: threading.Event | None = None, movements: int = 1):
    def scan(pdf: Path, out_dir: Path) -> list[Path]:
        if gate is not None:
            assert gate.wait(10)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "audiveris.log").write_text("fake scan\n", encoding="utf-8")
        names = [pdf.stem] if movements == 1 else [f"{pdf.stem}.mvt{n}" for n in range(1, movements + 1)]
        exported = []
        for name in names:
            mxl = out_dir / f"{name}.mxl"
            with zipfile.ZipFile(mxl, "w") as z:
                z.writestr("META-INF/container.xml", CONTAINER)
                z.writestr("score.xml", SCORE)
            exported.append(mxl)
        return exported

    return scan


@pytest.fixture
def setup(tmp_path, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(web, "get_engine", lambda name: fake_scan(gate))
    app = web.create_app(tmp_path / "jobs", tmp_path / "published")
    return Client(app), app, gate, tmp_path / "published"


def wait(client: Client, job_id: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_pages_and_version(setup):
    client, *_ = setup
    assert client.get("/").status_code == 200
    dev = client.get("/dev")
    assert dev.status_code == 200 and b"/api/jobs" in dev.content
    assert client.get("/api/version").json() == {"version": web.scorn.__version__}


def test_heartbeat_updates_last_seen(setup):
    client, app, *_ = setup
    app.state.activity.last_seen = 0.0
    assert client.post("/api/heartbeat").status_code == 204
    assert app.state.activity.last_seen > 0.0


def test_simple_job_publishes(setup):
    client, app, gate, published = setup
    assert not app.state.activity.busy()
    job = client.upload("/api/simple/jobs", "Op. 5 Tune.pdf").json()
    assert app.state.activity.busy()
    gate.set()
    job = wait(client, job["id"])
    assert job["status"] == "done", job
    assert not app.state.activity.busy()

    folder = published / "Op. 5 Tune"
    assert job["simple"]["folder"] == str(folder)
    assert job["simple"]["error"] is None
    [mvt] = job["simple"]["movements"]
    assert mvt["name"] == "Op. 5 Tune"
    assert mvt["musicxml"] == "Op. 5 Tune.musicxml"
    assert mvt["no_breaks"] == "Op. 5 Tune (no page breaks).musicxml"
    assert mvt["report"] is None  # the fake PDF makes reconcile raise, so there is no report
    assert mvt["summary"] and not mvt["scanned"]
    assert mvt["rhythm"] == ["Bar 2 on page 2 doesn't add up in Trumpet II"]
    assert 'new-page="yes"' in (folder / mvt["musicxml"]).read_text(encoding="utf-8")
    assert 'new-page="yes"' not in (folder / mvt["no_breaks"]).read_text(encoding="utf-8")

    download = client.get(f"/api/jobs/{job['id']}/published/{quote(mvt['musicxml'])}")
    assert download.status_code == 200 and b"Trumpet II" in download.content


def test_same_name_gets_numbered_folder(setup):
    client, _, gate, published = setup
    gate.set()
    first = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])
    second = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])
    assert first["simple"]["folder"] == str(published / "Tune")
    assert second["simple"]["folder"] == str(published / "Tune (2)")
    assert (published / "Tune (2)" / "Tune.musicxml").is_file()
    assert (published / "Tune" / "Tune (no page breaks).musicxml").is_file()


def test_published_path_escape(setup):
    client, _, gate, published = setup
    gate.set()
    job = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])
    (published / "secret.txt").write_text("secret", encoding="utf-8")
    for rel in ("../secret.txt", "..%2Fsecret.txt", "%2E%2E/secret.txt", "../../jobs/x", "nope.musicxml"):
        assert client.get(f"/api/jobs/{job['id']}/published/{rel}").status_code == 404, rel
    assert client.get("/api/jobs/nope/published/Tune.musicxml").status_code == 404


def test_diagnostics(setup):
    client, _, gate, _ = setup
    gate.set()
    job = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])

    without = zipfile.ZipFile(io.BytesIO(client.get(f"/api/jobs/{job['id']}/diagnostics").content))
    names = set(without.namelist())
    assert {"about.json", "job.json", "audiveris/audiveris.log", "rhythm/Tune.txt"} <= names
    assert "Tune.pdf" not in names
    assert json.loads(without.read("about.json"))["scorn"] == web.scorn.__version__
    assert "Trumpet II" in without.read("rhythm/Tune.txt").decode()

    url = f"/api/jobs/{job['id']}/diagnostics?include_pdf=true"
    with_pdf = zipfile.ZipFile(io.BytesIO(client.get(url).content))
    assert "Tune.pdf" in with_pdf.namelist()


def test_sentences_cover_missing_and_empty():
    from scorn.rhythm import BarError, VoiceError
    from scorn.simple import sentences

    errors = [
        BarError("3", 1, voices=[VoiceError("P1 Trumpet II", "1", 1), VoiceError("P2", "1", -1)]),
        BarError("4", 2, missing=["P3 Horn"], empty=["P1 Trumpet II"]),
        BarError("5", 2, padded=["P1 Trumpet II"]),
    ]
    assert sentences(errors) == [
        "Bar 3 on page 1 doesn't add up in Trumpet II and P2",
        "Bar 4 on page 2 is missing in Horn and is empty in Trumpet II",
    ]


def test_proof_without_browser_fails_plainly(setup, monkeypatch):
    from scorn import proof

    def no_browser():
        raise RuntimeError("No Chrome or Edge found")

    monkeypatch.setattr(proof, "find_browser", no_browser)
    client, app, gate, _ = setup
    gate.set()
    job = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])
    assert job["proof"] is None
    assert client.post(f"/api/jobs/{job['id']}/proof?movement=2").status_code == 404
    queued = client.post(f"/api/jobs/{job['id']}/proof").json()
    assert queued["proof"]["status"] in ("queued", "running", "failed")
    deadline = time.monotonic() + 10
    while app.state.activity.busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    result = client.get(f"/api/jobs/{job['id']}").json()["proof"]
    assert result["status"] == "failed" and "Chrome" in result["error"]
    assert client.post("/api/jobs/nope/open-folder").status_code == 404


def test_mei_path_keeps_full_stem():
    from scorn.mei import mei_path

    folder = Path("out") / "Op. 5" / "audiveris"
    assert mei_path(folder / "Op. 5 No. 2.mxl", "audiveris") == folder / "Op. 5 No. 2.audiveris.mei"
    assert mei_path(folder / "Op. 5.mvt2.reconciled.musicxml", "audiveris") == folder / "Op. 5.mvt2.audiveris.mei"
    assert mei_path(folder / "Op. 5.mvt3.mxl", "audiveris") == folder / "Op. 5.mvt3.audiveris.mei"
    assert mei_path(folder / "Op. 5.homr.musicxml", "homr") == folder / "Op. 5.homr.mei"


def test_proof_per_movement(tmp_path, monkeypatch):
    from scorn import proof

    runs = []

    def fake_proof(pdf: Path, musicxml: Path, out: Path) -> list[Path]:
        runs.append(musicxml.name)
        out.mkdir(parents=True, exist_ok=True)
        image = out / "p01-top.png"
        image.write_bytes(musicxml.name.encode())
        return [image]

    monkeypatch.setattr(proof, "find_browser", lambda: "browser")
    monkeypatch.setattr(proof, "proof", fake_proof)
    monkeypatch.setattr(web, "get_engine", lambda name: fake_scan(movements=2))
    client = Client(web.create_app(tmp_path / "jobs", tmp_path / "published"))
    job_id = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])["id"]

    def proof_for(movement: int | None) -> dict:
        query = "" if movement is None else f"?movement={movement}"
        assert client.post(f"/api/jobs/{job_id}/proof{query}").status_code == 200
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/api/jobs/{job_id}").json()["proof"]
            if result["status"] not in ("queued", "running"):
                return result
            time.sleep(0.05)
        raise AssertionError("proof never finished")

    first = proof_for(None)
    assert first == {"movement": 1, "status": "done", "images": ["proof/mvt1/p01-top.png"], "error": None}
    assert proof_for(1) == first and len(runs) == 1  # a finished movement isn't made again

    second = proof_for(2)
    assert second["movement"] == 2 and second["images"] == ["proof/mvt2/p01-top.png"]
    assert runs == ["Tune.mvt1.musicxml", "Tune.mvt2.musicxml"]
    for movement in (1, 2):
        image = client.get(f"/api/jobs/{job_id}/files/proof/mvt{movement}/p01-top.png")
        assert image.status_code == 200 and image.content == f"Tune.mvt{movement}.musicxml".encode()

    assert proof_for(1)["movement"] == 1 and len(runs) == 3  # switching back makes it again


def test_proof_while_another_runs_is_refused(setup, monkeypatch):
    from scorn import proof

    release = threading.Event()

    def slow_proof(pdf: Path, musicxml: Path, out: Path) -> list[Path]:
        assert release.wait(10)
        return []

    monkeypatch.setattr(proof, "find_browser", lambda: "browser")
    monkeypatch.setattr(proof, "proof", slow_proof)
    client, app, gate, _ = setup
    gate.set()
    job = wait(client, client.upload("/api/simple/jobs", "Tune.pdf").json()["id"])
    assert client.post(f"/api/jobs/{job['id']}/proof").status_code == 200
    for movement in (1, 2):
        busy = client.post(f"/api/jobs/{job['id']}/proof?movement={movement}")
        assert busy.status_code == 409 and busy.json()["detail"] == web.PROOF_BUSY
    release.set()


def test_stop_ends_running_scan_and_fails_jobs(tmp_path, monkeypatch):
    import subprocess
    import sys

    from scorn import procs

    started = threading.Event()

    def stuck_scan(pdf: Path, out_dir: Path) -> list[Path]:
        started.set()
        procs.run([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL)
        raise AssertionError("the program should have been stopped")

    monkeypatch.setattr(procs, "_stopping", False)
    monkeypatch.setattr(web, "get_engine", lambda name: stuck_scan)
    app = web.create_app(tmp_path / "jobs", tmp_path / "published")
    client = Client(app)
    running = client.upload("/api/simple/jobs", "One.pdf").json()["id"]
    queued = client.upload("/api/simple/jobs", "Two.pdf").json()["id"]
    assert started.wait(10)

    began = time.monotonic()
    app.state.stop()
    assert time.monotonic() - began < 5
    for job_id in (running, queued):
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "failed" and job["simple"]["error"] == web.STOPPED, job
    with pytest.raises(procs.Stopped):
        procs.run([sys.executable, "-c", "pass"])


def test_plain_reason_hides_internal_text():
    assert "image only" in web.plain_reason("no font draws noteheads")
    text = web.plain_reason("KeyError: 'P3' in align_systems")
    assert "KeyError" not in text and "align_systems" not in text
