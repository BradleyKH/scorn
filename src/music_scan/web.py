import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from music_scan import layout, mei
from music_scan.engines import ENGINES

INDEX = Path(__file__).parent / "static" / "index.html"


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

    def summary(self) -> dict:
        end = self.finished or time.monotonic()
        return {
            "id": self.id,
            "pdf": self.pdf.name,
            "status": self.status,
            "current": self.current,
            "elapsed": end - self.started if self.started else 0,
            "results": {name: asdict(r) for name, r in self.results.items()},
        }


def plain_reason(reason: str) -> str:
    if reason.startswith("no font draws"):
        return "This PDF is a scan or has outlined notation, so it was read by image only."
    if reason.startswith("no staff systems") or "have no staff systems" in reason:
        return "The layout of this PDF could not be read, so it was read by image only."
    return f"This PDF could not be used to correct the scan ({reason})."


def reconcile_xml(job: Job, xml: Path, result: Movement) -> Path:
    """Return the file to publish, recording the summary and links on result. Never raises."""
    from music_scan.reconcile import reconcile

    fixed = xml.with_name(xml.name.split(".")[0] + ".reconciled.musicxml")
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
            for raw in ENGINES[engine](job.pdf, out_dir):
                if job.reconcile and engine == "audiveris":
                    movement = Movement(raw.name.split(".")[0])
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
                    svgs = mei.to_svg(xml, out_dir / "render" / xml.name.split(".")[0])
                    result.svgs += [p.relative_to(job.dir).as_posix() for p in svgs]
        except Exception as e:
            result.error = str(e)
        result.seconds = time.perf_counter() - started
    job.current = None
    job.finished = time.monotonic()
    job.status = "failed" if all(r.error for r in job.results.values()) else "done"


def create_app(root: Path) -> FastAPI:
    app = FastAPI()
    jobs: dict[str, Job] = {}
    # One scan at a time: both engines are heavy, and homr shares the GPU.
    worker = ThreadPoolExecutor(max_workers=1)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(INDEX)

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
        name = Path(pdf.filename or "").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(400, "Upload a .pdf file")
        if not engines or any(e not in ENGINES for e in engines):
            raise HTTPException(400, f"Engines must be from {', '.join(ENGINES)}")
        job_id = uuid.uuid4().hex[:12]
        job_dir = root / job_id
        job_dir.mkdir(parents=True)
        # The engines name their output after the PDF, so keep the original filename.
        path = job_dir / name
        path.write_bytes(await pdf.read())
        job = jobs[job_id] = Job(job_id, job_dir, path, engines, mei, render, page_breaks, reconcile)
        worker.submit(run_job, job)
        return job.summary()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        return find(job_id).summary()

    @app.get("/api/jobs/{job_id}/files/{rel:path}")
    def get_file(job_id: str, rel: str) -> FileResponse:
        job_dir = find(job_id).dir.resolve()
        path = (job_dir / rel).resolve()
        if not path.is_relative_to(job_dir) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    def find(job_id: str) -> Job:
        if job_id not in jobs:
            raise HTTPException(404, "No such job")
        return jobs[job_id]

    return app
