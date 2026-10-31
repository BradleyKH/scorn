import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response

import scorn
from scorn import layout, mei, procs, rhythm, simple, update
from scorn.engines import audiveris as audiveris_engine
from scorn.engines import available, get_engine

STATIC = Path(__file__).parent / "static"
SIMPLE_PAGE = STATIC / "simple.html"
DEV_PAGE = STATIC / "dev.html"
PLACEHOLDER = "<!doctype html><title>Scorn</title><p>Scorn is running. The developer page is at <a href='/dev'>/dev</a>.</p>"

SCAN_FAILED = "The music in this PDF could not be read. Try another copy of the PDF, or save the diagnostics and send them in."
PUBLISH_FAILED = "The music was read, but the files could not be saved."
NOT_CORRECTED = "The PDF could not be used to check the scan, so the music is shown as it was read."
NO_BROWSER = "Proof pictures need Google Chrome or Microsoft Edge, and neither was found on this computer."
PROOF_FAILED = "The proof pictures could not be made."
PROOF_BUSY = "Comparison pictures are already being made. Try again when they are done."
STOPPED = "Scorn was closed before this finished."


@dataclass
class Movement:
    name: str
    # Set only when reconcile ran; raw is kept out of `files`, which the page lists as primary downloads.
    summary: str | None = None
    report: str | None = None
    raw: str | None = None
    note: str | None = None


@dataclass
class EngineResult:
    files: list[str] = field(default_factory=list)
    svgs: list[str] = field(default_factory=list)
    error: str | None = None
    seconds: float | None = None
    # One entry per exported movement; Audiveris can write several from one PDF.
    movements: list[Movement] = field(default_factory=list)


@dataclass
class Job:
    id: str
    dir: Path
    pdf: Path
    engines: list[str]
    mei: bool
    render: bool
    page_breaks: bool
    reconcile: bool = False
    status: str = "queued"
    current: str | None = None
    started: float | None = None
    finished: float | None = None
    results: dict[str, EngineResult] = field(default_factory=dict)
    # Simple jobs only: the folder the finished files are published to.
    publish: Path | None = None
    simple: bool = False
    published: list[dict] = field(default_factory=list)
    error: str | None = None
    rhythm_reports: dict[str, str] = field(default_factory=dict)
    proof: dict | None = None

    def progress(self) -> dict | None:
        if self.status != "running" or self.current != "audiveris":
            return None
        found = audiveris_engine.progress(self.dir / "audiveris" / "audiveris.log")
        return {"page": found[0], "pages": found[1]} if found else None

    def summary(self) -> dict:
        end = self.finished or time.monotonic()
        out = {
            "id": self.id,
            "pdf": self.pdf.name,
            "status": self.status,
            "current": self.current,
            "elapsed": end - self.started if self.started else 0,
            "results": {name: asdict(r) for name, r in self.results.items()},
            "progress": self.progress(),
            "simple": None,
            "proof": self.proof,
        }
        if self.simple:
            out["simple"] = {
                "folder": str(self.publish) if self.publish else None,
                "movements": self.published,
                "error": self.error,
            }
        return out


class Worker:
    """Runs submitted work one item at a time on a daemon thread, so a scan never holds up process exit."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable | None] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while (fn := self._queue.get()) is not None:
            fn()

    def submit(self, fn: Callable) -> None:
        self._queue.put(fn)

    def stop(self, timeout: float) -> None:
        """Drop queued work and wait up to timeout for the current item to end."""
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        self._queue.put(None)
        self._thread.join(timeout)


class Activity:
    """What the launcher polls to decide when nobody is using the app any more."""

    def __init__(self) -> None:
        self.last_seen = time.monotonic()
        self._pending = 0
        self._lock = threading.Lock()

    def touch(self) -> None:
        self.last_seen = time.monotonic()

    def busy(self) -> bool:
        return self._pending > 0

    def submit(self, worker: Worker, fn: Callable, *args) -> None:
        # Counted from submit to finish, so queued work keeps the app busy as well as running work.
        with self._lock:
            self._pending += 1

        def run() -> None:
            try:
                fn(*args)
            finally:
                with self._lock:
                    self._pending -= 1

        worker.submit(run)


def plain_reason(reason: str) -> str:
    if reason.startswith("no font draws"):
        return "This PDF is a scan or has outlined notation, so it was read by image only."
    if reason.startswith("no staff systems") or "have no staff systems" in reason:
        return "The layout of this PDF could not be read, so it was read by image only."
    return "This PDF could not be used to correct the scan, so the music is shown as it was read."


def reconcile_xml(job: Job, xml: Path, result: Movement) -> Path:
    """Return the file to publish, recording the summary and links on result. Never raises."""
    from scorn.reconcile import reconcile

    fixed = xml.with_name(simple.movement_name(xml) + ".reconciled.musicxml")
    try:
        report = reconcile(job.pdf, xml, fixed)
        report_path = fixed.with_name(fixed.stem + ".report.json")
        report_path.write_text(report.to_json(), encoding="utf-8")
    except Exception as e:
        result.note = f"The PDF could not be used to correct the scan ({e}), so the original is shown."
        return xml
    result.report = report_path.relative_to(job.dir).as_posix()
    if not report.applicable:
        result.summary = plain_reason(report.reason)
        fixed.unlink(missing_ok=True)
        return xml
    result.summary = report.summary()
    result.raw = xml.relative_to(job.dir).as_posix()
    return fixed


def run_job(job: Job) -> None:
    job.status = "running"
    job.started = time.monotonic()
    for engine in job.engines:
        job.current = engine
        result = job.results[engine] = EngineResult()
        out_dir = job.dir / engine
        started = time.perf_counter()
        try:
            for raw in get_engine(engine)(job.pdf, out_dir):
                if job.reconcile and engine == "audiveris":
                    movement = Movement(simple.movement_name(raw))
                    result.movements.append(movement)
                    xml = reconcile_xml(job, raw, movement)
                else:
                    xml = raw
                if not job.page_breaks:
                    layout.strip_breaks(xml)
                    if xml != raw:
                        layout.strip_breaks(raw)
                outputs = [xml]
                if job.mei:
                    outputs.append(mei.mei_path(xml, engine))
                    mei.to_mei(xml, outputs[-1])
                result.files += [p.relative_to(job.dir).as_posix() for p in outputs]
                if job.render:
                    # Audiveris can export several movements, so each gets its own render folder.
                    svgs = mei.to_svg(xml, out_dir / "render" / simple.movement_name(xml))
                    result.svgs += [p.relative_to(job.dir).as_posix() for p in svgs]
        except Exception as e:
            result.error = str(e)
        result.seconds = time.perf_counter() - started
    job.current = None
    job.finished = time.monotonic()
    job.status = "failed" if all(r.error for r in job.results.values()) else "done"


def publish_movement(job: Job, xml: Path, movement: Movement) -> dict:
    report = job.dir / movement.report if movement.report else None
    entry = {"name": movement.name, **simple.publish(xml, report, job.publish, movement.name)}
    try:
        errors = rhythm.check(job.publish / entry["musicxml"])
        job.rhythm_reports[movement.name] = rhythm.format_report(errors)
        bars = simple.sentences(errors)
    except Exception as e:
        job.rhythm_reports[movement.name] = f"rhythm check failed: {e}"
        bars = []
    entry["summary"] = movement.summary or (NOT_CORRECTED if movement.note else None)
    # A report without a raw link means reconcile ran but found nothing in the PDF to use.
    entry["scanned"] = movement.report is not None and movement.raw is None
    entry["rhythm"] = bars
    return entry


def run_simple_job(job: Job) -> None:
    job.status = "running"
    job.started = time.monotonic()
    job.current = "audiveris"
    result = job.results["audiveris"] = EngineResult()
    started = time.perf_counter()
    try:
        raws = get_engine("audiveris")(job.pdf, job.dir / "audiveris")
    except Exception as e:
        result.error = str(e)
        job.error = SCAN_FAILED
    else:
        try:
            for raw in raws:
                movement = Movement(simple.movement_name(raw))
                result.movements.append(movement)
                xml = reconcile_xml(job, raw, movement)
                result.files.append(xml.relative_to(job.dir).as_posix())
                job.published.append(publish_movement(job, xml, movement))
        except Exception as e:
            result.error = str(e)
            job.error = PUBLISH_FAILED
    result.seconds = time.perf_counter() - started
    if job.error and job.publish and not any(job.publish.iterdir()):
        job.publish.rmdir()
        job.publish = None
    job.current = None
    job.finished = time.monotonic()
    job.status = "failed" if job.error else "done"


def run_proof(job: Job, musicxml: Path) -> None:
    from scorn import proof

    state = job.proof
    state["status"] = "running"
    try:
        proof.find_browser()
    except RuntimeError:
        state.update(status="failed", error=NO_BROWSER)
        return
    try:
        images = proof.proof(job.pdf, musicxml, job.dir / "proof" / f"mvt{state['movement']}")
    except Exception:
        state.update(status="failed", error=PROOF_FAILED)
        return
    state.update(status="done", images=[p.relative_to(job.dir).as_posix() for p in images])


def inside(base: Path, rel: str) -> Path:
    """The file at rel under base, or 404 for anything missing or outside it."""
    base = base.resolve()
    path = (base / rel).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404)
    return path


def create_app(root: Path, publish_dir: Path | None = None) -> FastAPI:
    publish_dir = publish_dir if publish_dir is not None else root / "published"
    jobs: dict[str, Job] = {}
    # One scan at a time: both engines are heavy, and homr shares the GPU.
    worker = Worker()

    def stop() -> None:
        """End running programs and fail unfinished work, so shutdown doesn't wait out a scan."""
        unfinished = [(job, job.current) for job in jobs.values() if job.status in ("queued", "running")]
        proofs = [job.proof for job in jobs.values() if job.proof and job.proof["status"] in ("queued", "running")]
        procs.stop_all()
        worker.stop(timeout=5)
        # Marked after the worker ends, so its own failure handling can't overwrite the message.
        for job, current in unfinished:
            job.status, job.current, job.finished, job.error = "failed", None, time.monotonic(), STOPPED
            if current in job.results:
                job.results[current].error = STOPPED
        for proof in proofs:
            proof.update(status="failed", error=STOPPED)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        stop()

    app = FastAPI(lifespan=lifespan)
    app.state.stop = stop
    activity = app.state.activity = Activity()

    @app.middleware("http")
    async def track_activity(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            activity.touch()
        return await call_next(request)

    @app.get("/")
    def index() -> Response:
        if SIMPLE_PAGE.is_file():
            return FileResponse(SIMPLE_PAGE)
        return HTMLResponse(PLACEHOLDER)

    @app.get("/dev")
    def dev() -> FileResponse:
        return FileResponse(DEV_PAGE)

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        return FileResponse(STATIC / "scorn.ico", media_type="image/x-icon")

    @app.get("/api/version")
    def version() -> dict:
        return {"version": scorn.__version__}

    release_lock = threading.Lock()
    release: list[dict | None] = []

    @app.get("/api/update")
    def update_check() -> dict:
        # Looked up on first request only; later requests reuse the answer even if it was a failure.
        with release_lock:
            if not release:
                release.append(update.latest_release())
        latest = release[0]
        newer = latest is not None and update.is_newer(latest["version"], scorn.__version__)
        return {"newer": newer, "latest": latest["version"] if latest else None, "url": latest["url"] if latest else None}

    @app.post("/api/heartbeat", status_code=204)
    def heartbeat() -> Response:
        return Response(status_code=204)

    async def save_upload(pdf: UploadFile) -> tuple[str, Path]:
        name = Path(pdf.filename or "").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "Upload a .pdf file")
        job_id = uuid.uuid4().hex[:12]
        job_dir = root / job_id
        job_dir.mkdir(parents=True)
        # The engines name their output after the PDF, so keep the original filename.
        path = job_dir / name
        path.write_bytes(await pdf.read())
        return job_id, path

    @app.post("/api/jobs")
    async def create_job(
        pdf: Annotated[UploadFile, File()],
        engines: Annotated[list[str], Form()],
        mei: Annotated[bool, Form()] = False,
        render: Annotated[bool, Form()] = False,
        # An unchecked checkbox sends nothing, so absent means the box was cleared.
        page_breaks: Annotated[bool, Form()] = False,
        reconcile: Annotated[bool, Form()] = False,
    ) -> dict:
        if not Path(pdf.filename or "").name.lower().endswith(".pdf"):
            raise HTTPException(400, "Upload a .pdf file")
        names = available()
        if not engines or any(e not in names for e in engines):
            raise HTTPException(400, f"Engines must be from {', '.join(names)}")
        job_id, path = await save_upload(pdf)
        job = jobs[job_id] = Job(job_id, path.parent, path, engines, mei, render, page_breaks, reconcile)
        activity.submit(worker, run_job, job)
        return job.summary()

    @app.post("/api/simple/jobs")
    async def create_simple_job(pdf: Annotated[UploadFile, File()]) -> dict:
        job_id, path = await save_upload(pdf)
        job = Job(job_id, path.parent, path, ["audiveris"], mei=False, render=False, page_breaks=True,
                  reconcile=True, simple=True)
        # Claimed up front so two queued scans of same-named PDFs get distinct folders in upload order.
        job.publish = simple.claim_folder(publish_dir, path.stem)
        jobs[job_id] = job
        activity.submit(worker, run_simple_job, job)
        return job.summary()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return find(job_id).summary()

    @app.get("/api/jobs/{job_id}/files/{rel:path}")
    def get_file(job_id: str, rel: str) -> FileResponse:
        return FileResponse(inside(find(job_id).dir, rel))

    @app.get("/api/jobs/{job_id}/published/{rel:path}")
    def get_published(job_id: str, rel: str) -> FileResponse:
        job = find(job_id)
        if job.publish is None:
            raise HTTPException(404)
        path = inside(job.publish, rel)
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs/{job_id}/open-folder", status_code=204)
    def open_folder(job_id: str) -> Response:
        job = find(job_id)
        if job.publish is None or not job.publish.is_dir():
            raise HTTPException(404, "The folder is gone")
        os.startfile(job.publish)
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}/diagnostics")
    def get_diagnostics(job_id: str, include_pdf: bool = False) -> Response:
        job = find(job_id)
        data = simple.diagnostics(job.dir, job.pdf, job.summary(), job.rhythm_reports, include_pdf)
        name = f"scorn diagnostics {job.pdf.stem}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"})

    @app.post("/api/jobs/{job_id}/proof")
    def create_proof(job_id: str, movement: int = 1) -> dict:
        job = find(job_id)
        if job.proof and job.proof["status"] in ("queued", "running"):
            raise HTTPException(409, PROOF_BUSY)
        if job.status != "done" or job.publish is None:
            raise HTTPException(409, "The scan isn't finished yet")
        if not 1 <= movement <= len(job.published):
            raise HTTPException(404, "No such movement")
        if job.proof and job.proof["movement"] == movement and job.proof["status"] == "done":
            return job.summary()
        musicxml = job.publish / job.published[movement - 1]["musicxml"]
        # Each movement has its own folder, so an earlier movement's pictures stay downloadable.
        job.proof = {"movement": movement, "status": "queued", "images": [], "error": None}
        activity.submit(worker, run_proof, job, musicxml)
        return job.summary()

    def find(job_id: str) -> Job:
        if job_id not in jobs:
            raise HTTPException(404, "No such job")
        return jobs[job_id]

    return app
