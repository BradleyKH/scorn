# Scorn

Proof of concept: turn a PDF of sheet music into MusicXML with optical music recognition (OMR) that runs entirely on this machine. The output is the MusicXML that Cadence's score dialog already accepts, and `--mei` also converts it to MEI the same way the dialog does (Verovio `loadData` then `getMEI`).

Two engines:

- **Audiveris** (Java, rule based plus a small neural classifier). Reads PDFs directly and joins pages into one score. Ships with its own Java runtime.
- **homr** (Python, transformer models on ONNX Runtime with CUDA). Reads one image at a time, so the PDF is rasterized at 300 dpi, each page is recognized, and the pages are joined.

## Working offline

Once setup has run, everything works without internet: both engines, their models, Verovio, and the music21 corpus behind the fixtures are all on disk. Three rules keep it that way.

1. **Pass `--offline` to uv**, or set `UV_OFFLINE=1` for the shell, so `uv run` never tries to reach PyPI:

   ```bash
   uv run --offline scorn scan path/to/score.pdf --engine audiveris homr
   ```

2. **Don't delete or recreate `.venv`.** It is about 2.6 GB, most of it the NVIDIA CUDA and cuDNN libraries. homr's models (about 320 MB, CPU and GPU weights plus OCR) are downloaded into `.venv/Lib/site-packages/homr/` and `.venv/Lib/site-packages/rapidocr/models/`. A fresh venv needs the internet for both the packages and the models.
3. **Don't delete `tools/`.** It holds the unpacked Audiveris, including its Java runtime and Tesseract OCR. `tools/dl/` keeps the original MSI, so if `tools/audiveris/` goes missing, rerunning `scripts/setup-audiveris.sh` re-extracts it from that MSI without downloading again.

To check that a machine is ready to go offline, run one scan per engine with `--offline` while connected; if both finish, nothing else will be fetched later.

## Setup (needs internet, once)

Requires Windows, [uv](https://docs.astral.sh/uv/), and Git Bash.

```bash
uv sync                          # Python 3.12 venv with homr, verovio, music21, pymupdf, onnxruntime-gpu
bash scripts/setup-audiveris.sh  # unpacks Audiveris 5.11.0 into tools/audiveris, no system install
uv run python -c "import sys; sys.argv=['homr','--init']; from homr.main import main; main()"
                                 # downloads homr's recognition and OCR models
```

`uv sync` installs everything for development. The package's own dependencies are the core: Audiveris plus reconcile (pymupdf, verovio, and the web UI). homr with the GPU packages and music21 come from the `homr` and `eval` dependency groups, which uv installs by default. `uv export --no-default-groups` lists the core alone. Without homr installed, the `homr` engine isn't offered; without music21, `scorn compare`, `scorn fixtures`, `scorn eval` and `--truth` fail.

### GPU

homr runs on an NVIDIA GPU through `onnxruntime-gpu` 1.31 (CUDA 13), with the CUDA and cuDNN DLLs installed from pip (about 1 GB), so no CUDA toolkit install is needed, only a recent driver. `pyproject.toml` overrides `onnxruntime` out of the whole resolution: homr's CPU build clashes with `onnxruntime-gpu`, since the two install into the same folder, and pymupdf 2.0 requires it too, so the override is also what keeps it out of the core. homr picks fp16 GPU weights when CUDA is available, which is why `--init` must run after the GPU package is in place.

On Windows the pip DLLs are only found after `onnxruntime.preload_dlls()`, which the homr launcher in `src/scorn/engines/homr.py` calls. Without it homr still sees CUDA in the provider list, and its sessions fall back to the CPU with only a "Failed to create CUDAExecutionProvider" line in `homr.log`.

To check the GPU is in use:

```bash
uv run --offline python -c "import onnxruntime as o; o.preload_dlls(); print(o.get_available_providers())"
```

To run homr on a machine without an NVIDIA GPU, edit `pyproject.toml`: in the `homr` group, replace `onnxruntime-gpu[cuda,cudnn]>=1.31` with `onnxruntime>=1.24.1`, and delete the `override-dependencies` line, keeping the rest of `[tool.uv]`. Then `uv sync` and rerun `--init` while online; homr uses the CPU `onnxruntime` and its fp32 weights. uv can't limit an override to one dependency group, so with that line gone the core carries `onnxruntime` too (pymupdf pulls it in, and `uv export --no-default-groups` lists it); build the core from the unedited `pyproject.toml`.

To use an Audiveris that's installed elsewhere, set `AUDIVERIS_EXE` to its `Audiveris.exe`.

## Usage

```bash
# Recognize a PDF with one or both engines
uv run --offline scorn scan score.pdf --engine audiveris homr --mei --render

# Skip the correction from the PDF that Audiveris output otherwise gets
uv run --offline scorn scan score.pdf --no-reconcile

# Correct Audiveris MusicXML from the PDF it came from (born-digital PDFs only)
uv run --offline scorn reconcile score.pdf score.mxl

# Drop the PDF's page and system breaks so the notation program lays the score out itself
uv run --offline scorn scan score.pdf --no-page-breaks

# Run the test suite (corpus tests skip unless SCORN_SCORES points at a score folder;
# full-score tests skip unless the Stopford score and its scans are in notes/stopford/)
uv run --offline pytest

# Build the ground-truth fixtures (clean and simulated-scan PDFs from known scores)
uv run --offline scorn fixtures

# Scan a fixture and score it against the truth
uv run --offline scorn scan fixtures/melody.scanned.pdf --engine homr --truth fixtures/melody.truth.musicxml

# Score any MusicXML against any other
uv run --offline scorn compare truth.musicxml found.mxl

# List bars whose voices don't add up to the time signature
uv run --offline scorn check found.mxl

# Stack each PDF page over the engraved MusicXML, half a page per image, for proofreading
uv run --offline scorn proof score.pdf found.musicxml
```

### Rhythm check

`scan` runs the rhythm check on every MusicXML it writes, and `check` runs it on any file. It lists each bar where a voice is longer or shorter than the time signature, with the PDF page, the part and voice, and how far off it is in whole notes (`+1/16` is a stray 16th rest). These bars are almost always misreads: a missed or extra augmentation dot, an eighth read as a quarter, a dropped note, or a clef or symbol read as a note.

When one part overflows, Audiveris stretches the whole-bar rests in every other part to match. The check reports those as "padded" rather than listing them, so the parts it names are the ones to fix. Dorico and other notation programs lay every part on one shared bar grid, so a single overfull bar can shift the rhythm of other parts in the bars that follow. Fix every reported bar before importing.

It also lists bars that a part is missing or that hold no notes or rests at all. Audiveris leaves a bar out when its exporter fails on it (look for `Error visiting Measure` in `audiveris.log`), and leaves it empty when it finds nothing on that staff. Notation programs read a part's bars in order, so either one moves the rest of that part a bar early.

The check can't catch a misread that still adds up, such as a wrong pitch, or a quarter read as an eighth next to a missing eighth.

### Reconcile

```bash
uv run --offline scorn reconcile score.pdf score.mxl [--out fixed.musicxml] [--report changes.json]
```

`reconcile` corrects Audiveris MusicXML using the PDF as the authority. It reads the PDF once, with no OCR, rebuilds a score model from the notation drawn in it (glyphs from `page.get_text("rawdict")`, lines and curves from `page.get_drawings()`), aligns that model to the MusicXML bar by bar and staff by staff, and rewrites what the PDF proves. Audiveris's bar structure is kept as the skeleton. Passes run in a fixed order and each fixes one kind of misread:

- **parts**: puts music back in the right instrument on condensed scores and names the parts from the staff labels.
- **pitch**: clefs, key signatures, pitches and accidentals.
- **rhythm**: note values, rests, chords, voices and ties.
- **marks**: dynamics, hairpins, articulations, ornaments, tremolos, fermatas, slurs and pedal marks.
- **text**: lyrics, tempo and other text, title and credits.

The PDF wins wherever it gives unambiguous evidence. Time signatures are corrected first, ahead of that check: a bar measured against the right time signature can fail until the rhythm pass rebuilds it. After that, a pass that would leave more bars failing the rhythm check than before it ran is undone as a whole. Where the PDF and the scan disagree and neither can be proven, the bar is left alone and listed for a person to check. The same inputs always give byte-identical output.

Reconcile applies to born-digital PDFs, exported from Finale, Dorico, Sibelius or MuseScore, whose notation is text in a music font plus vector lines. Fonts are identified by the characters they draw, so anonymized font names still work. Scans and PDFs with the notation outlined into paths have nothing to read; reconcile detects that, copies the input through unchanged and says why.

Outputs:

| File | What it is |
| --- | --- |
| `<name>.reconciled.musicxml` | the corrected score, always plain MusicXML |
| `<name>.reconcile.json` | every change (category, part, bar, before, after, evidence) and every unresolved disagreement; the printed summary is the same list in plain English |

`scan` runs reconcile on Audiveris output by default (`--no-reconcile` skips it) and keeps the raw file beside the result. The reconciled file is what the page-break stripping, `--mei`, `--render` and `--truth` use, so `--no-page-breaks` runs after reconcile, which needs the breaks to align bars to PDF pages. `scan` runs the rhythm check on both the raw and the reconciled file. If reconcile raises, `scan` prints the error and continues with the raw file.

### Reconcile results

`scorn eval ~/Dropbox/Scores --class vector-music --reconcile` scans every born-digital PDF in the corpus that has a MusicXML exported from the same notation file, then scores raw Audiveris and reconciled output against that MusicXML. Of 163 such pairs, Audiveris fails on 3 ("No regularly spaced lines found"), 3 more cannot be scored (see the limits below), and 5 have stale truth: the MusicXML was exported from a different version of the score than the PDF ("Bread of Heaven", "rhythm exercises", "imagine", "Handel - Eternal Source of Light Divine", "happy birthday - brass"). The report lists those 5 separately and leaves them out of the totals and the regression list. The totals cover the remaining 152 files.

| Measure | Raw Audiveris | Reconciled |
| --- | ---: | ---: |
| note accuracy | 70.9% | 76.4% |
| pitch accuracy | 73.1% | 77.9% |
| files at 98% note accuracy or better | 40 | 60 |

| Marking | Raw precision / recall | Reconciled precision / recall |
| --- | ---: | ---: |
| dynamics | 28.4% / 49.0% | 45.5% / 54.6% |
| hairpins | 46.2% / 31.5% | 49.8% / 46.0% |
| articulations | 34.7% / 47.7% | 48.4% / 48.3% |
| ornaments (tremolos included) | 6.2% / 12.9% | 23.5% / 16.1% |
| fermatas | 17.1% / 7.4% | 60.4% / 55.4% |
| ties | 66.8% / 55.1% | 68.2% / 57.9% |
| slurs | 64.8% / 68.0% | 64.6% / 69.4% |
| lyrics | 0% / 0% | 80.0% / 78.5% |
| part names | 2.7% / 2.4% | 12.0% / 10.6% |

Per file, note accuracy rises on 106 files, holds on 41 and falls on 5, by 0.2 to 2.9 points.

Known limits, with what they cost on the corpus:

- **Bars Audiveris drops.** When the scan has far fewer bars than the PDF, alignment can pair a scan bar with the wrong PDF bar, and the passes then rewrite it from the wrong music. "fairest lord jesus - cello" (15 of 60 bars scanned) falls from 24.7% to 22.3%, and "Panis Angelicus - tpt" (52 of 61) from 84.0% to 81.1%.
- **Voices that share noteheads.** Where two voices on one staff share a notehead or cross, the PDF reader can hand a note to the other voice, and the pitch and rhythm passes move it there. This accounts for the other three files that fall: "jupiter" (98.7% to 96.2%), "Victoria - O Magnum Mysterium - piano reduction" (82.7% to 82.5%) and "wide meadow - piano version" (72.2% to 72.0%). Three or more voices per staff, and beams that cross between staves, raise the same risk.
- **Accidentals across octaves.** Engraving rules confine an accidental to its octave, but some engravers let it cover the same letter in another octave. Where the scan follows that reading and the PDF does not repeat the accidental, the bar is left as scanned and listed for checking.
- **Slurs.** Slur recall rises, but precision holds level (64.8% to 64.6%): the reader still misses or mis-ends some curves, so the pass only adds a slur where both ends land on aligned notes.
- **Ties.** The reader misses some tie curves, so the scan's own ties stay in any bar where the PDF draws a curve, and are dropped only from bars with none. Tie recall stays under 60%, mostly from ties the scan and the reader both miss.
- **Audiveris output music21 cannot read.** On "died in your arms transcription", "The Battle from Gladiator - trio" and "Trumpet warmups", Audiveris writes `<divisions>0</divisions>` in movements of whole notes only, which music21 cannot parse, so these 3 pairs are not scored.
- **Unusual fonts and layouts.** PDFs whose notation is outlined into paths or scanned are passed through unchanged. Fonts are recognized by the characters they draw, so a music font whose encoding is outside the known families reads as no notation at all.

### Proofreading against the PDF

The rhythm check proves every bar adds up; only a side-by-side read proves the bars say what the PDF says. `reconcile` does the deterministic part of that read for born-digital PDFs, so proofreading is the final check on its output and the full job for everything else. On the Stopford full score (a 19-page condensed church score, copyrighted and so not included in `examples/`), a MusicXML that passed the rhythm check still had instruments changing parts mid-piece, wrong notes, dropped ties, and about 150 spurious markings. A measure-by-measure read against the PDF found all of them.

**Keep the PDF's page breaks while proofreading.** Engraved with its encoded breaks, the MusicXML puts the same bars on each page as the PDF, so page N of one lines up with page N of the other. Edit and proofread the file with breaks, and strip them (`scan --no-page-breaks`) only as the last step before importing into a notation program.

```bash
uv run --offline scorn proof score.pdf score.musicxml   # images in proof/ next to the MusicXML
```

`proof` engraves the MusicXML with Verovio (encoded breaks, a number over every bar), rasterizes each SVG with headless Chrome or Edge (set `SCORN_BROWSER` to use another Chromium browser), and writes `pNN-top.png` and `pNN-bot.png` for every page. Each image shows half a page of the PDF above a red bar and the same half of the engraving below it, both at 2000 px wide, which is enough to read every note on a 15-staff page. The halves overlap by about 10% so no staff is cut in two. `proof/svg/` keeps the full engraved pages.

How to read the images:

- **Match staves by instrument, not position.** Full scores are often condensed: the PDF hides staves that rest, while the engraving shows every part on every page.
- **Walk every bar of every PDF staff.** Check pitches and accidentals, rhythms and rests, ties, chords and voices, clef and key changes, then dynamics (value, staff and bar), hairpins, tremolos, fermatas and tempo text.
- **Confirm in the XML before fixing.** A misread engraving can look like a misread file; dump the notes of that part and bar to be sure.
- **Split long scores.** Pages are independent, so several reviewers (or parallel Claude Code agents, each given a page range and the list of already-known issues) can work at once.

Misreads seen on the Stopford score, none of which the rhythm check reports:

| Misread | Looks like |
| --- | --- |
| Instruments change parts | Where a condensed score changes which staves it shows, Audiveris can put an instrument in a different part from that page on (Trumpet I in P5, then P4, then P1), or split one grand staff into two parts. Notation software then plays the organ as the choir. |
| Timpani rolls read as chords | The three tremolo strokes become two or three extra noteheads stacked on the real note. |
| Lyrics read as markings | Syllables under the choir staves come out as staccatos, accents, trills, `p`, `sf`, `fz` and `Ped.` marks. |
| Rhythm that still adds up | Two eighths read as quarters with the following note dropped. |
| Lost chord notes and ties | The top note of a final chord, the lower octave of a pedal note, ties across barlines. |
| Markings on the wrong staff | A dynamic between two staves attached to the neighbor, which then has two. |
| Hairpins and slurs left open | A wedge or slur with a start and no stop, which Verovio warns about on import and draws across many bars. |

**Born-digital PDFs carry their own answer key.** A PDF exported from Finale, Sibelius or Dorico usually draws its notation as text in a music font (Maestro, EngraverFontSet, Bravura), with staff lines, barlines and hairpins as vector lines. PyMuPDF reads both: `page.get_text("rawdict")` gives every glyph with its position, and `page.get_drawings()` gives every line. Grouping horizontal lines in fives finds the staves, vertical lines that cross most staves are barlines, and each dynamic, tremolo or fermata glyph then maps to a staff and a bar. On the Stopford score this gave an exact list of the 105 dynamics, 18 hairpins, 19 timpani tremolos and 6 fermatas, and proved there were no staccatos at all: the PDF's 88 dot glyphs matched the MusicXML's 88 augmentation dots one for one. Glyph codes differ by font, so crop and view one example of each before trusting a mapping. In Maestro, `f`, `F`, `P` and `ƒ` are *f*, *mf*, *mp* and *ff*.

After fixing, prove the fix touched only what it meant to: compare each instrument's notes bar by bar between the old and new files, expecting differences only in the bars you changed. Then run `check` and `proof` again and reread the pages.

### Web page

```bash
uv run --offline python -m scorn.app   # opens the simple page in your browser
```

The simple page at `/` takes a PDF and returns its MusicXML; the installed app's Start menu shortcut runs this same command with `pythonw.exe`. The developer page at `/dev` exposes the full set of options: pick or drop a PDF, choose the engines, and optionally MEI and a rendered preview. On `/dev`, clear "Keep the PDF's page breaks" to get MusicXML without the source's page and system breaks. Scans run one at a time in the background while the page shows which engine is working and the elapsed time. As each engine finishes, `/dev` shows download links for its MusicXML (and MEI) and the Verovio render. Jobs are kept only in memory, so a restarted server forgets them, but their files stay on disk.

`python -m scorn.app` runs the server in the same process:

- **Port:** a free port on 127.0.0.1 chosen at each start, so nothing needs configuring.
- **Single instance:** `app.json` in the state folder records the running server's pid and port. A second launch that finds that server answering opens the browser at it and exits; a lock left by a crashed run is taken over.
- **Idle exit:** the page sends a heartbeat while it is open. The server exits after 120 seconds with no page open and no scan or proof running, and removes `app.json`.
- **Files:** state lives in `%LOCALAPPDATA%\Scorn\`: working folders in `jobs\`, the log in `scorn.log` (the only log output, since `pythonw.exe` has no console), and `app.json`. Finished results are published to `Scorn\` inside the real Documents folder, even when OneDrive redirects it.
- **Flags:** `--no-browser`, `--idle-seconds N`, `--state-dir PATH`, `--publish-dir PATH`.

For development, `uv run --offline scorn ui` serves the same pages on a fixed port (`--port`, default 8000) with job folders under `out/ui/<job id>/` and results in `out/ui/published/`.

Output lands in `out/<pdf name>/<engine>/`:

| File | What it is |
| --- | --- |
| `<name>.mxl` | Audiveris's compressed MusicXML (one `.mvtN.mxl` per movement when it finds several) |
| `<name>.homr.musicxml` | homr's pages joined into one score |
| `*.mei` | with `--mei`, the MEI Cadence would store |
| `render/page-NNN.svg` | with `--render`, the recognized score engraved by Verovio, to compare by eye with the PDF |
| `proof/pNN-top.png`, `proof/pNN-bot.png` | from `scorn proof`, each half page of the PDF stacked over the same half of the engraving |
| `audiveris.log`, `homr.log` | engine output, the first stop when a scan fails |
| `homr/page-NNN.png` | the rasterized pages homr read |

## Fixtures and scoring

`scorn fixtures` takes three pieces from the music21 corpus, writes each as ground-truth MusicXML, engraves it to an A4 PDF with Verovio, and makes a "scanned" copy: 200 dpi JPEG pages with a slight skew, blur, gray cast, and noise.

| Fixture | Source | Shape |
| --- | --- | --- |
| `melody` | Handel, *Lascia ch'io pianga*, vocal line only | 1 staff, 54 measures, lyrics |
| `chorale` | Bach, BWV 66.6 | 4 parts, 10 measures |
| `piano` | Joplin, *Maple Leaf Rag* | grand staff, 85 measures, 3 pages |

`compare` lines parts up by order and flattens each into a list of events (note, chord, or rest with its duration; grace notes and hidden rests skipped, the latter being padding music21 writes into short final measures). Pitches compare by MIDI number, so an enharmonic respelling isn't an error. **Pitch acc** is 1 minus the edit distance over pitches alone divided by the truth's event count; **note acc** counts pitch and duration together. Missing and extra events both cost.

## Results so far

| Fixture | Audiveris pitch / note | homr pitch / note |
| --- | --- | --- |
| melody, clean | 100% / 99.5% | 96.2% / 94.7% |
| melody, scanned | 68.3% / 67.3% | 96.2% / 94.7% |
| chorale, clean | 100% / 100% | 100% / 100% |
| chorale, scanned | 100% / 100% | 100% / 100% |
| piano, clean (RH / LH) | 98.7% / 98.5%, 95.5% / 95.2% | 99.0% / 98.3%, 89.2% / 79.3% |
| piano, scanned (RH / LH) | 91.3% / 91.1%, 88.1% / 88.1% | 99.2% / 98.5%, 88.4% / 78.2% |

Audiveris is the more accurate engine on clean engraving but degrades sharply on noisy scans, while homr barely notices the noise. Both get the 4-part chorale exactly right, even from the simulated scan. On the piano left hand homr adds about 10% extra events (386 found for 353), which costs it most of its note accuracy there. Audiveris finds 87 measures for the piano's 85.

Each fixture takes 15 to 50 seconds per engine, with homr on the GPU at about 10 seconds a page.

## Real scores

Born-digital PDFs with no ground truth, checked by eye against the `--render` output. The two horn parts are in `examples/`; the Stopford full score is copyrighted and not included.

| Score | Audiveris | homr |
| --- | --- | --- |
| *A Tune By Mozart*, Horn (F), 1 page | 18 s | 6 s |
| *Warm-ups A-G*, Horn (F), 1 page | 17 s | 8 s |
| *Christ the Lord is risen again* (Stopford), full score, 19 pages | about 4 min | 3 min |

- On both single-line horn parts the engines agree note for note, and the Mozart matches the PDF. Neither engine reads the "Horn (F)" label: both write a part named "Voice" with no `<transpose>`, so the MusicXML holds written pitch with nothing telling Cadence it sounds a fifth lower.
- On the Stopford full score Audiveris keeps 15 parts at 75 to 76 measures each across the whole piece. homr's result is unusable: the score is condensed, so pages carry 10 to 15 staves as instruments drop in and out, and joining pages by staff order mixes parts up. It also reads three of the pages as a single 40-measure staff. Use Audiveris for multi-part scores.

## License

MIT, see [LICENSE](LICENSE).
