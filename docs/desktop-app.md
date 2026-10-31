# Scorn desktop app

Scorn runs as a local web page on Windows for people who aren't technical: they start it, drop in a PDF, and get MusicXML they can open in Dorico, Finale or MuseScore. Everything runs on their own PC, offline. When the PDF is born-digital (exported from notation software rather than scanned), `reconcile`, which corrects the recognized notes against the text and graphics inside the PDF, has already fixed the result; when it is a scan, they get Audiveris's reading and a plain note saying so.

This page describes how the desktop app is built and how it behaves, for contributors. The [README](../README.md) covers setup and the command-line tools.

## Core build

The package's own dependencies are only what Audiveris plus reconcile need: pymupdf, verovio, fastapi, uvicorn and python-multipart. homr, onnxruntime, the NVIDIA libraries and music21 (about 2.2 GB together) live in the `homr` and `eval` dependency groups, so a wheel built with `uv build` pulls in only the core. `uv export --no-default-groups` lists it.

- **Lazy engines.** `engines/__init__.py` imports an engine module only when it's used, and offers `homr` only when it's installed, so the core build never loads homr, onnxruntime or music21.
- **Locating Audiveris.** `engines/audiveris.py` checks `AUDIVERIS_EXE`, then an `audiveris\` folder beside the Python install (with or without the MSI's nested `Audiveris\` folder), then `tools/`, then `PATH`. The second location is for a bundle that ships Audiveris next to an embedded Python.
- **Audiveris license.** Audiveris is licensed under the AGPL-3.0, so a bundle that includes it ships Audiveris's license and points to its source.

## How it runs

1. **Launcher.**
   - **Start:** `python -m scorn.app` (or `pythonw.exe -m scorn.app`, with no console) binds a free localhost port, starts the FastAPI app from `web.py` in-process with uvicorn, and opens the default browser once the server answers.
   - **Logging:** all logging, and any stray output, goes to a log file, since under `pythonw` there is no console.
   - **Single instance:** a lock file, `%LOCALAPPDATA%\Scorn\app.json`, records the pid and port. A second launch that finds a live server there just opens the browser at it; a stale lock is taken over.
   - **State:** `%LOCALAPPDATA%\Scorn\` also holds the job working folders (`jobs\`) and the log (`scorn.log`).
   - **Testing flags:** `--no-browser`, `--idle-seconds`, `--state-dir` and `--publish-dir`.
2. **Shutdown.** The page sends a heartbeat every 10 seconds. When nothing has checked in for two minutes and no job or comparison is queued or running, the server exits and removes the lock, so nothing lingers after the tab closes.
3. **A job.**
   - **Run:** upload, Audiveris, `reconcile`, rhythm check, then MusicXML output.
   - **Progress:** a bar showing page N of M, counted from the pages Audiveris logs as finished, plus elapsed time.
   - **Concurrency:** one job at a time, on the same single-worker queue the developer page uses.
4. **Output.** Results go to `Documents\Scorn\<pdf name>\`, using the real Documents folder (Windows reports it, since it's often redirected to OneDrive). A second scan of a same-named PDF goes to `<pdf name> (2)`, and so on; nothing is overwritten. The folder holds, per movement:
   - `<name>.musicxml`, keeping the PDF's page and system breaks, so the import matches the original layout
   - `<name> (no page breaks).musicxml`, for letting the notation program lay the score out itself
   - `<name>.report.json`, the reconcile report

   `<name>` is the PDF's name, plus `.mvt2`, `.mvt3` and so on when Audiveris exports several movements.

## The page

`/` serves the simple page. The developer page at `/dev` exposes every engine and output option, for testing and comparing engines.

- **Start screen:** one big drop zone, "Drop a PDF of sheet music here", with a Browse button. No engine choices, no MEI checkbox, no page-break checkbox; those defaults are decided for the user.
- **While working:** the file name, the progress bar, elapsed time, and "This usually takes a few minutes for a long score."
- **When done:** for each movement, a primary **Download MusicXML** button and a smaller "Download without page breaks" link, then the reconcile summary in plain words; an **Open folder** button opens the results in Explorer. Two examples of the summary:
  - "This PDF was made by notation software, so every note, dynamic and lyric was checked against it. 9 notes fixed, lyrics added to 212 notes."
  - "Please check the result. This PDF is a scanned image, so it was read by sight only."
- **Rhythm warnings:** listed under "These bars need checking", in plain words such as "Bar 15 on page 4 doesn't add up in Trumpet II".
- **Compare with the PDF:** a secondary button that builds proof images (each page of the PDF above the same page engraved from the result) and shows them in a simple viewer, one at a time, with Previous and Next buttons and arrow keys. Rasterizing needs Chrome or Edge, and Edge ships with Windows 10 and 11.
- **Something went wrong:** a short message, plus a "Save diagnostics" button. It saves a zip of the Audiveris log, reconcile reports, rhythm results and version information, without the PDF unless the user ticks a box, to attach to a [GitHub issue](https://github.com/BradleyKH/scorn/issues). The Audiveris log records full file paths, which include the Windows account name.
- **Footer:** the version, so bug reports name the build. `scorn --version` reports it on the command line.
