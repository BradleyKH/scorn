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
- **Footer:** the version, so bug reports name the build, and an **About** link. `scorn --version` reports it on the command line. The About box shows the version, a line on what Scorn does, the offer of the source code (a link to the GitHub repository, as the AGPL requires), and the bundled components with their licenses: Audiveris, PyMuPDF, Verovio and Python.
- **Update notice:** each time Scorn starts, the page checks GitHub once for a newer release. When there is one, a dismissible notice says "A newer version of Scorn is available" with a Download link. The page shows nothing when offline or when the release is not newer. Updating is manual: the user downloads and runs the new installer.

## Windows installer

Scorn ships as a per-user Inno Setup installer, `Scorn-Setup-<version>.exe` (about 150 MB). It needs no administrator rights and installs for Windows 10 and 11 on x64.

### Installed layout

The default install folder is `%LOCALAPPDATA%\Programs\Scorn`:

- `python\` holds an embedded CPython 3.12 (python-build-standalone) with the `scorn` wheel and its core dependencies. homr, onnxruntime and music21 are not included.
- `audiveris\` holds Audiveris 5.11.0 unpacked from its MSI, with its own Java runtime and Tesseract. `find_executable()` finds it beside `python\` without configuration.
- `licenses\` holds the license texts for Scorn, Python, Audiveris and every bundled package, plus `SOURCE.txt`, the offer of the corresponding source.
- `scorn.ico` is the app icon.

The installed size is about 460 MB. Setup refuses an install folder long enough to push any bundled file past Windows' 259-character path limit; the build measures the longest path inside the bundle and passes it to the installer, which allows folders of about 130 characters. The Start menu shortcut "Scorn" runs `python\pythonw.exe -m scorn.app` from the install folder; the installer can also add a desktop shortcut (unchecked by default) and offers to launch Scorn when it finishes.

### Upgrade and uninstall

- **Upgrade:** running a newer installer over an existing install replaces `python\` and `audiveris\` wholesale, so no files from the previous version linger. Setup first ends any Scorn process whose executable lives under the install folder (`pythonw.exe` and Audiveris's `java.exe`, which Windows' Restart Manager cannot close); processes elsewhere are left alone.
- **Uninstall:** from Settings, Apps, or the Start menu. It ends running Scorn processes the same way, then removes the install folder and `%LOCALAPPDATA%\Scorn\` (lock file, job working folders and log).
- **Kept:** `Documents\Scorn\`, which holds the user's results, is never touched.

### Building locally

Building needs Git Bash, [uv](https://docs.astral.sh/uv/), a network connection for the first run, and [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer step.

```bash
bash scripts/build-bundle.sh      # build/bundle/, then a smoke test of it
bash scripts/build-installer.sh   # the bundle, then dist/Scorn-Setup-<version>.exe
```

- **`scripts/build-bundle.sh [--no-smoke]`** builds the wheel, installs the core requirements exported from `uv.lock` into the embedded Python, unpacks Audiveris, and collects the licenses. The downloads are pinned at the top of the script, each with its SHA-256, which is checked on every run: python-build-standalone CPython 3.12.16 and Audiveris 5.11.0. To change a version, update that block. Downloads are cached in `tools/dl/`. Set `SCORN_UV_OFFLINE=1` to run every uv step offline; that needs each locked package already in uv's cache, which one online run provides. Test suites inside installed packages are removed to keep the bundle small.
- **`scripts/smoke-bundle.sh <bundle dir>`** runs the bundle the way an installed copy runs, with nothing from the development environment: it checks the version and that Audiveris resolves inside the bundle, scans the Mozart example PDF and runs the rhythm check on the result, then starts `scorn.app` and requests `/api/version`. The bundle script runs it last unless given `--no-smoke`.
- **`scripts/build-installer.sh [--skip-bundle] [--no-smoke]`** reads the version from `pyproject.toml`, runs the bundle script first unless given `--skip-bundle` (forwarding `--no-smoke`), and compiles `packaging/scorn.iss`. It looks for `ISCC.exe` on `PATH`, then in `%LOCALAPPDATA%\Programs\Inno Setup 6` and `C:\Program Files (x86)\Inno Setup 6`.

The bundle lands in `build/bundle/` and the installer in `dist/`; both folders are gitignored, as is `tools/`.

### CI and releases

- **CI** (`.github/workflows/ci.yml`) runs on every push to `main`, on pull requests and on manual dispatch. On `windows-latest` it runs the tests with only the core and `dev` dependency groups, so homr is never downloaded, then runs `scripts/build-installer.sh` (bundle, smoke test, installer) and uploads the installer as the `scorn-installer` artifact. The `tools/dl/` downloads are cached, keyed on the pins in `scripts/build-bundle.sh`.
- **Release** (`.github/workflows/release.yml`) runs when a tag matching `v*` is pushed. It fails unless the tag equals `v` plus the version in `pyproject.toml`. It uses the annotated tag's message as the release notes, followed by a line saying the installer is unsigned and SmartScreen may warn. After the tests, the bundle and the installer build, it creates the GitHub Release as a draft with the installer attached, then publishes it as the latest release. A broken build therefore never becomes `releases/latest`, which the in-app update check reads.

### Cutting a release

1. Set the new version in `pyproject.toml` and commit it.
2. Create an annotated tag whose message is the release notes, and push it:

   ```bash
   git tag -a v0.1.0 -F release-notes.txt
   git push origin v0.1.0
   ```

   (`git tag -a v0.1.0` without `-F` opens an editor for the message instead.)
3. Watch the Release workflow. When it finishes, the installer is on the release page.

The installer is unsigned, so Windows SmartScreen warns on first run: the user chooses **More info**, then **Run anyway**.
