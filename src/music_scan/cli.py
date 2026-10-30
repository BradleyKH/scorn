import argparse
import shutil
import time
from pathlib import Path

from music_scan import compare, fixtures, layout, mei, rhythm
from music_scan.engines import ENGINES


def _reconciled_path(xml: Path) -> Path:
    # Keeps any ".mvtN" so movements don't overwrite each other, and the first-dot stem
    # (which mei.mei_path and the render folder rely on) stays the input's.
    return xml.with_name(xml.name.removesuffix(xml.suffix) + ".reconciled.musicxml")


def _report_path(out: Path) -> Path:
    stem = out.name.removesuffix(".musicxml").removesuffix(".reconciled")
    return out.with_name(stem + ".reconcile.json")


def _reconcile(pdf: Path, xml: Path, out: Path, report_path: Path) -> None:
    from music_scan.reconcile import reconcile

    report = reconcile(pdf, xml, out)
    report_path.write_text(report.to_json(), encoding="utf-8", newline="")
    print(report.summary())
    print(f"  report    {report_path}")


def cmd_reconcile(args: argparse.Namespace) -> None:
    pdf, xml = Path(args.pdf), Path(args.musicxml)
    out = Path(args.out) if args.out else _reconciled_path(xml)
    report_path = Path(args.report) if args.report else _report_path(out)
    _reconcile(pdf, xml, out, report_path)
    print(f"  musicxml  {out}")


def cmd_scan(args: argparse.Namespace) -> None:
    pdf = Path(args.pdf).resolve()
    out_root = Path(args.out).resolve() / pdf.stem
    for engine in args.engine:
        out_dir = out_root / engine
        if out_dir.exists():
            shutil.rmtree(out_dir)
        print(f"[{engine}] scanning {pdf.name}")
        started = time.perf_counter()
        results = ENGINES[engine](pdf, out_dir)
        print(f"[{engine}] done in {time.perf_counter() - started:.1f}s")
        if engine == "audiveris" and not args.no_reconcile:
            for i, raw in enumerate(results):
                fixed = _reconciled_path(raw)
                print(f"[{engine}] reconciling {raw.name} against the PDF")
                print(f"  raw       {raw}")
                print(rhythm.format_report(rhythm.check(raw)))
                try:
                    _reconcile(pdf, raw, fixed, _report_path(fixed))
                except Exception as e:
                    print(f"[{engine}] reconcile failed, keeping the raw file: {type(e).__name__}: {e}")
                    continue
                results[i] = fixed
        for xml in results:
            print(f"  musicxml  {xml}")
            # Before stripping breaks, so the report can still name PDF pages.
            print(rhythm.format_report(rhythm.check(xml)))
            if args.no_page_breaks:
                layout.strip_breaks(xml)
            if args.mei:
                mei_path = mei.mei_path(xml, engine)
                mei.to_mei(xml, mei_path)
                print(f"  mei       {mei_path}")
            if args.render:
                svgs = mei.to_svg(xml, out_dir / "render")
                print(f"  render    {len(svgs)} SVG page(s) in {out_dir / 'render'}")
        if args.truth:
            print(compare.compare(Path(args.truth), results[0]).format())
        print()


def cmd_fixtures(args: argparse.Namespace) -> None:
    for name in args.names or fixtures.PIECES:
        if name not in fixtures.PIECES:
            raise SystemExit(f"unknown fixture {name!r}")
        paths = fixtures.build(name, Path(args.out))
        for kind, path in paths.items():
            print(f"{name:<8} {kind:<8} {path}")


def cmd_compare(args: argparse.Namespace) -> None:
    print(compare.compare(Path(args.truth), Path(args.found)).format())


def cmd_eval(args: argparse.Namespace) -> None:
    from music_scan import evaluate

    fn = None
    if args.reconcile:
        try:
            from music_scan.reconcile import reconcile as fn
        except ImportError as e:
            raise SystemExit(f"--reconcile could not load music_scan.reconcile: {e}")
    pairs = evaluate.discover(Path(args.folder).expanduser())
    if args.cls:
        pairs = [p for p in pairs if p.kind == args.cls]
    if args.limit is not None:
        pairs = pairs[: args.limit]
    if not pairs:
        raise SystemExit("no PDF/MusicXML pairs found")
    out_root = Path(args.out).resolve()
    evaluate.run(pairs, out_root, fn)
    print(f"report  {out_root / 'eval' / 'report.md'}")
    print(f"csv     {out_root / 'eval' / 'report.csv'}")


def cmd_check(args: argparse.Namespace) -> None:
    errors = rhythm.check(Path(args.musicxml))
    print(rhythm.format_report(errors))
    if errors:
        raise SystemExit(1)


def cmd_proof(args: argparse.Namespace) -> None:
    from music_scan import proof

    xml = Path(args.musicxml)
    out = Path(args.out) if args.out else xml.parent / "proof"
    images = proof.proof(Path(args.pdf), xml, out)
    print(f"{len(images)} proof image(s) in {out}")


def cmd_ui(args: argparse.Namespace) -> None:
    import uvicorn

    from music_scan import web

    app = web.create_app(Path(args.out).resolve() / "ui")
    print(f"music-scan UI at http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(prog="music-scan", description="PDF to MusicXML POC")
    sub = parser.add_subparsers(required=True)

    scan = sub.add_parser("scan", help="Recognize a PDF into MusicXML")
    scan.add_argument("pdf")
    scan.add_argument("--engine", nargs="+", choices=list(ENGINES), default=["audiveris"])
    scan.add_argument("--out", default="out")
    scan.add_argument("--truth", help="Ground-truth MusicXML to score the result against")
    scan.add_argument("--mei", action="store_true", help="Also convert to MEI with Verovio")
    scan.add_argument("--render", action="store_true", help="Engrave the result to SVG")
    scan.add_argument(
        "--no-page-breaks", action="store_true", help="Drop the PDF's page and system breaks"
    )
    scan.add_argument(
        "--no-reconcile", action="store_true", help="Skip correcting Audiveris output from the PDF"
    )
    scan.set_defaults(func=cmd_scan)

    rec = sub.add_parser("reconcile", help="Correct Audiveris MusicXML from its born-digital PDF")
    rec.add_argument("pdf")
    rec.add_argument("musicxml", help=".musicxml, .xml or .mxl file")
    rec.add_argument("--out", help="Output MusicXML; default <stem>.reconciled.musicxml next to the input")
    rec.add_argument("--report", help="JSON change report; default <stem>.reconcile.json next to the output")
    rec.set_defaults(func=cmd_reconcile)

    fx = sub.add_parser("fixtures", help="Build ground-truth test PDFs")
    fx.add_argument("names", nargs="*", help=f"Any of {', '.join(fixtures.PIECES)}; all by default")
    fx.add_argument("--out", default="fixtures")
    fx.set_defaults(func=cmd_fixtures)

    cmp = sub.add_parser("compare", help="Score a MusicXML file against ground truth")
    cmp.add_argument("truth")
    cmp.add_argument("found")
    cmp.set_defaults(func=cmd_compare)

    ev = sub.add_parser("eval", help="Scan PDFs that have a MusicXML sibling and score them")
    ev.add_argument("folder")
    ev.add_argument("--limit", type=int, help="Only the first N pairs in sorted path order")
    ev.add_argument("--class", dest="cls", choices=["vector-music", "outlined", "scan", "other"])
    ev.add_argument("--reconcile", action="store_true", help="Also score music_scan.reconcile output")
    ev.add_argument("--out", default="out")
    ev.set_defaults(func=cmd_eval)

    chk = sub.add_parser("check", help="List bars whose voices don't fill the time signature")
    chk.add_argument("musicxml", help=".musicxml, .xml or .mxl file")
    chk.set_defaults(func=cmd_check)

    prf = sub.add_parser("proof", help="Stack each PDF page over its engraving for proofreading")
    prf.add_argument("pdf")
    prf.add_argument("musicxml", help="MusicXML that keeps the PDF's page breaks")
    prf.add_argument("--out", help="Output folder; default is proof/ next to the MusicXML")
    prf.set_defaults(func=cmd_proof)

    ui = sub.add_parser("ui", help="Serve a local web page for uploading PDFs")
    ui.add_argument("--port", type=int, default=8000)
    ui.add_argument("--out", default="out")
    ui.set_defaults(func=cmd_ui)

    args = parser.parse_args()
    args.func(args)
